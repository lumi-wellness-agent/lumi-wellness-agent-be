"""근거 검색 평가 — 운영 plan 노드가 쓴 질의로 hybrid.search의 Hit@K · Recall@K · MRR을 잰다.

    .venv/bin/python scripts/rag_eval.py collect      # 질의를 모은다(LLM 호출, 비용)
    .venv/bin/python scripts/rag_eval.py build        # data/rag_eval.jsonl을 만든다
    .venv/bin/python scripts/rag_eval.py run          # .env DATABASE_URL (읽기 전용)
    .venv/bin/python scripts/rag_eval.py run --database-url <URL> --out before.json --misses
    .venv/bin/python scripts/rag_eval.py compare before.json after.json
    .venv/bin/python scripts/rag_eval.py run --database-url <URL> --out before.json \
        --md docs/rag-eval/YYYY-MM-DD-retrieval-<주제>.md --title "기준선"   # 실행 기록까지
    .venv/bin/python scripts/rag_eval.py record before.json --md <경로>   # 저장한 결과로 기록만

- 질의: data/eval_scenarios.jsonl(목표 3 × 사용자 상태 5 × 문장 2 = 30)마다 그 목표 ·
  상태의 후보(protocols.plan_candidates, 시간 · 환경 칩 없이)를 한 바퀴 돈다. 운영 choose
  프롬프트(make_chooser)에 고른 활동을 빼 가며 다시 물어 활동마다 search_query 하나를
  받는다. 목표 · 상태는 intake를 거치지 않고 직접 넣는다. 모은 질의
  (data/rag_eval_queries.jsonl)는 다시 만들 수 없어 케이스와 따로 둔다. collect는 빠진
  (시나리오, 활동)만 채운다(--fresh는 전부 다시).
- 정답: 활동 노드 JSON evidence[] 가운데 그 목표의 1차 지표를 잰 레코드(paper_id,
  experiment_id). 검색의 outcomes 필터와 같은 축이고, build가 붙이므로 evidence[]가 바뀌면
  build만 다시 돌린다. run은 DB에 없는 정답과, 사용자 상태에 인용할 수 없는 연구 조건
  (ORDER_COMPATIBILITY)의 정답을 분모에서 빼고 따로 센다 — 뒤의 것은 검색의 순서 필터가
  일부러 거르는 레코드다.
- 묶음(group): planner는 정답이 있는 상황, fallback은 그 활동 evidence[]에 목표 지표 근거가
  없어 plan이 같은 필터로 전체에서 찾는 상황이다(PR-15 · PR-21 S1, PR-03 S2). fallback은
  상위 5가 늘 다른 활동의 근거라 점수 대신 상위 5에 든 레코드 수(found@5 — 0이면 Reference
  카드에 근거가 없다)와 목록을 본다.
- 검색은 plan 노드(find_evidence)와 같다 — 그 활동의 evidence[] 안에서 먼저, 0건이면 같은
  필터로 전체에서. 매체는 제공 매체(delivered_media)를 연구 매체로 바꿔 매체마다 찾고
  레코드별 최고 점수로 합친다. run --open은 planner 질의를 필터 · 범위 없이도 돈다(RRF
  가중치 실험용).
- 방식(mode): hybrid(운영과 같은 RRF) · dense(벡터 순위만) · keyword(낱말 순위만). SQL ·
  필터 · 후보 수는 app/retrieval/hybrid.py의 것을 그대로 쓴다.
- 지표: Hit@K(정답 레코드가 상위 K chunk 안에 하나라도), Recall@K(정답 레코드 중 상위 K
  chunk 안의 비율), MRR(첫 정답 chunk 순위의 역수). K = 3 · 5 · 10. Foreign@5는 상위 5
  chunk에 든 레코드 중 그 활동 evidence[] 밖의 비율이다 — plan이 상위 5건을 검증해
  인용하므로 Reference 카드에 남의 근거가 섞이는 정도다. 낮을수록 좋다. 평균은 문항마다 같은
  무게다 — 운영에서 나올 수 있는 검색 상황 전체의 평균이고, S2 활동이 많아 S2 상황이 많다.
- DB는 읽기만 한다. 질의 임베딩에 OPENAI_API_KEY가 든다(없으면 dense가 비고 hybrid는
  keyword만 — 운영과 같다). content를 바꿔 다시 적재하기 전후에 돌려 compare로 비교한다.
- 실행 기록 — run --md · record가 docs/rag-eval/YYYY-MM-DD-retrieval-<주제>.md를 쓴다(구성 ·
  지표 표 · 놓친 정답 · 다른 활동 근거 · fallback 상위 5). 해석은 같은 디렉터리의 판정
  메모(-verdict.md)에 사람이 쓴다. 결과 JSON의 meta에 검색 구성과 코드 커밋을 남긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import math
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import protocols  # noqa: E402
from app.agent.assembly import chat_models  # noqa: E402
from app.agent.llm import with_fallback  # noqa: E402
from app.agent.nodes.planner import ActivityChoice, make_chooser  # noqa: E402
from app.agent.state import IntakeResult  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.retrieval import embedding, hybrid  # noqa: E402
from app.schemas.care import CareGoal, CareRequest, ContentType, Setting, UserProfile  # noqa: E402
from app.schemas.evidence import InterventionOrder, Medium, Outcome, study_medium  # noqa: E402
from app.schemas.intake import GoalSource  # noqa: E402
from app.schemas.protocol import CareProtocol, EvidenceRole  # noqa: E402
from app.schemas.retrieval import GOAL_OUTCOMES, ORDER_COMPATIBILITY  # noqa: E402
from scripts.plan_trial import Usage  # noqa: E402

SCENARIOS = ROOT / "data" / "eval_scenarios.jsonl"
QUERIES = ROOT / "data" / "rag_eval_queries.jsonl"
CASES = ROOT / "data" / "rag_eval.jsonl"
KS = (3, 5, 10)
LIMIT = max(KS)
GROUPS = ("planner", "fallback")
VIEWS = ("planner", "fallback", "open")  # open = planner 질의를 필터 · 범위 없이(run --open)
MODES = ("hybrid", "dense", "keyword")
METRICS = tuple(f"{name}@{k}" for name in ("hit", "recall") for k in KS) + ("mrr", "foreign@5")
FALLBACK_METRICS = ("found@5", "empty@5")

Group = Literal["planner", "fallback"]
Key = tuple[str, str | None]  # (paper_id, experiment_id)


class Scenario(BaseModel):
    """data/eval_scenarios.jsonl 한 줄 — 검색 질의 수집과 계획 평가가 같이 쓴다.

    목표 · 사용자 상태는 intake 결과로 직접 넣는다. 칩(setting · earphones · minutes)은 계획
    평가만 쓴다 — 질의 수집은 칩 없이 그 목표 · 상태의 후보를 한 바퀴 돈다. tier는 칩이
    넉넉한지(roomy) 빠듯한지(tight)다.
    """

    id: str
    goal: CareGoal
    order_condition: InterventionOrder | None = None
    phrasing: Literal["direct", "indirect"]
    text: str
    tier: Literal["roomy", "tight"]
    setting: Setting
    earphones: bool | None = None
    minutes: int
    content_type: ContentType = ContentType.CURATED_PHOTO
    change_activity: bool = False


class CollectedQuery(BaseModel):
    """data/rag_eval_queries.jsonl 한 줄 — 운영 choose 프롬프트가 쓴 search_query."""

    scenario: str
    goal: CareGoal
    order_condition: InterventionOrder | None = None
    protocol_id: str
    query: str
    step: int  # 한 바퀴에서 고른 순서. 0이 칩 없이 처음 고른 활동
    forced: bool = False  # 두 번 다 후보 밖을 골라 그 활동 하나만 보여 주고 받았다
    model: str  # 답한 모델(폴백이면 폴백 모델)
    collected: str  # 모은 날


class RecordKey(BaseModel):
    """정답 레코드. role은 참고용이다(PRIMARY · CONFLICT · META 모두 플래너가 봐야 할 근거)."""

    paper_id: str
    experiment_id: str | None = None
    role: EvidenceRole

    @property
    def key(self) -> Key:
        return (self.paper_id, self.experiment_id)


class EvalCase(BaseModel):
    """data/rag_eval.jsonl 한 줄. fallback은 relevant가 비어 있다."""

    id: str
    group: Group
    query: str
    protocol_id: str
    goal: CareGoal
    media: list[Medium] = []  # 연구 매체(제공 매체에 study_medium 적용 후)
    intervention_order: InterventionOrder | None = None
    outcomes: list[Outcome] = []
    relevant: list[RecordKey] = []
    source: str | None = None


# ── 시나리오 · 질의 ──────────────────────────────────────────────────────────────────────────


def load_scenarios(path: Path = SCENARIOS) -> list[Scenario]:
    with path.open(encoding="utf-8") as f:
        return [Scenario.model_validate_json(line) for line in f if line.strip()]


def load_queries(path: Path = QUERIES) -> list[CollectedQuery]:
    with path.open(encoding="utf-8") as f:
        return [CollectedQuery.model_validate_json(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: Iterable[BaseModel]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row.model_dump(mode="json"), ensure_ascii=False) + "\n")


def situation_candidates(scenario: Scenario) -> list[CareProtocol]:
    """질의 수집이 도는 후보 — 그 목표 · 상태의 plan_candidates, 시간 · 환경 조건 없이."""
    return protocols.plan_candidates(scenario.goal, order=scenario.order_condition).protocols


def is_current(query: CollectedQuery, scenarios: Mapping[str, Scenario]) -> bool:
    """모은 질의가 지금 시나리오 · 후보와 맞는가. 아니면 collect가 다시 모은다."""
    scenario = scenarios.get(query.scenario)
    return (
        scenario is not None
        and (query.goal, query.order_condition) == (scenario.goal, scenario.order_condition)
        and query.protocol_id in {p.protocol_id for p in situation_candidates(scenario)}
    )


Choose = Callable[[dict, Sequence[dict], Sequence[str]], Awaitable[tuple[str, ActivityChoice]]]


async def walk(
    choose: Choose, scenario: Scenario, done: Collection[str], first_step: int, today: str
) -> list[CollectedQuery]:
    """그 목표 · 상태의 후보를 한 바퀴 — 고른 활동을 빼 가며 같은 프롬프트로 다시 묻는다.

    done(이미 모은 활동)은 처음부터 뺀다. 후보 밖을 두 번 고르면 남은 첫 활동 하나만 보여
    주고 받는다.
    """
    state = {
        "request": CareRequest(
            text=scenario.text, profile=UserProfile(content_type=scenario.content_type)
        ),
        "intake": IntakeResult(
            goal=scenario.goal,
            goal_source=GoalSource.CHIP,
            target_outcomes=list(GOAL_OUTCOMES[scenario.goal].primary),
            order_condition=scenario.order_condition,
        ),
    }
    remaining = [p for p in situation_candidates(scenario) if p.protocol_id not in done]
    rows: list[CollectedQuery] = []
    while remaining:
        ids = {p.protocol_id for p in remaining}
        candidates = [protocols.compact(p) for p in remaining]
        model, choice = await choose(state, candidates, [])
        if choice.protocol_id not in ids:
            problem = f"{choice.protocol_id}는 후보 목록에 없다"
            model, choice = await choose(state, candidates, [problem])
        forced = choice.protocol_id not in ids
        if forced:
            model, choice = await choose(state, candidates[:1], [])
        protocol_id = remaining[0].protocol_id if forced else choice.protocol_id
        rows.append(
            CollectedQuery(
                scenario=scenario.id,
                goal=scenario.goal,
                order_condition=scenario.order_condition,
                protocol_id=protocol_id,
                query=choice.search_query,
                step=first_step + len(rows),
                forced=forced,
                model=model,
                collected=today,
            )
        )
        remaining = [p for p in remaining if p.protocol_id != protocol_id]
    return rows


async def collect(args: argparse.Namespace) -> int:
    settings = get_settings()
    models = chat_models(settings)
    if not models:
        print("LLM 키가 없다 — .env의 OPENAI_API_KEY 또는 ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    usage = Usage()
    for _, model in models:
        model.callbacks = [usage]
    names = {"openai": settings.llm_model, "anthropic": settings.fallback_model}

    def tagged(name: str, choose):
        """답한 모델 이름을 같이 돌려준다 — 폴백이 답했는지 남기려고."""

        async def call(*call_args: Any) -> tuple[str, ActivityChoice]:
            return names.get(name, name), await choose(*call_args)

        return call

    choose = with_fallback([(name, tagged(name, make_chooser(m))) for name, m in models])
    scenarios = load_scenarios(args.scenarios)
    by_id = {s.id: s for s in scenarios}
    targets = [s for s in scenarios if not args.only or s.id in args.only]
    redo = {s.id for s in targets} if args.fresh else set()
    existing = load_queries(args.out) if args.out.exists() else []
    stale = [q for q in existing if not is_current(q, by_id)]
    kept = [q for q in existing if is_current(q, by_id) and q.scenario not in redo]
    today = datetime.date.today().isoformat()
    gate = asyncio.Semaphore(args.concurrency)

    async def one(scenario: Scenario) -> list[CollectedQuery]:
        mine = [q for q in kept if q.scenario == scenario.id]
        done = {q.protocol_id for q in mine}
        first_step = max((q.step for q in mine), default=-1) + 1
        async with gate:
            return await walk(choose, scenario, done, first_step, today)

    started = time.perf_counter()
    new = [q for rows in await asyncio.gather(*(one(s) for s in targets)) for q in rows]
    order = {s.id: i for i, s in enumerate(scenarios)}
    rows = sorted(kept + new, key=lambda q: (order[q.scenario], q.step))
    write_jsonl(args.out, rows)
    print(
        f"질의 {len(new)}개를 모았다 → {args.out} (전체 {len(rows)}개 · 후보가 아니게 돼 지운 것 "
        f"{len(stale)}개 · 모델 호출 {usage.calls}회 · 토큰 {usage.input:,}/{usage.output:,} · "
        f"{time.perf_counter() - started:.0f}s)"
    )
    for q in new:
        if q.forced:
            print(f"  후보 밖을 두 번 골라 하나만 보여 줌: {q.scenario} {q.protocol_id}")
    return 0


# ── 케이스 만들기 ──────────────────────────────────────────────────────────────────────────


def gold(protocol: CareProtocol, goal: CareGoal) -> list[RecordKey]:
    """그 목표의 1차 지표를 측정한 evidence[] 항목. 같은 레코드는 하나만."""
    primary = set(GOAL_OUTCOMES[goal].primary)
    seen: dict[Key, RecordKey] = {}
    for entry in protocol.evidence:
        if not primary & set(entry.outcomes):
            continue
        key = (entry.paper_id, entry.experiment_id)
        if key not in seen:
            seen[key] = RecordKey(
                paper_id=entry.paper_id, experiment_id=entry.experiment_id, role=entry.role
            )
    return list(seen.values())


def search_media(protocol: CareProtocol, content_type: ContentType) -> list[Medium]:
    """plan 노드(_search_media)가 찾는 매체 — 제공 매체를 연구 매체로, 중복 없이."""
    delivered = protocols.delivered_media(protocol, content_type)
    return list(dict.fromkeys(study_medium(m) for m in delivered))


def build_cases(
    scenarios: Sequence[Scenario], queries: Sequence[CollectedQuery]
) -> tuple[list[EvalCase], list[str]]:
    """모은 질의마다 케이스 하나. 지금 시나리오 · 후보와 맞지 않는 질의는 사유와 함께 뺀다.

    같은 목표 · 상태의 두 문장이 같은 활동에 똑같은 질의를 내면 검색도 결과도 같으므로 하나로
    합치고, source에 두 시나리오를 다 적는다.
    """
    loaded = protocols.load_protocols()
    by_id = {s.id: s for s in scenarios}
    cases: list[EvalCase] = []
    seen: dict[tuple, EvalCase] = {}
    skipped: list[str] = []
    for q in queries:
        if not is_current(q, by_id):
            skipped.append(f"{q.scenario} {q.protocol_id}: 시나리오 · 후보와 맞지 않는다(collect)")
            continue
        scenario, protocol = by_id[q.scenario], loaded[q.protocol_id]
        media = search_media(protocol, scenario.content_type)
        same = (q.protocol_id, scenario.goal, scenario.order_condition, tuple(media), q.query)
        if (merged := seen.get(same)) is not None:
            merged.source = f"{merged.source} {scenario.id}"
            continue
        relevant = gold(protocol, scenario.goal)
        seen[same] = case = EvalCase(
            id=f"{scenario.id}-{q.protocol_id}",
            group="planner" if relevant else "fallback",
            query=q.query,
            protocol_id=q.protocol_id,
            goal=scenario.goal,
            media=media,
            intervention_order=scenario.order_condition,
            outcomes=list(GOAL_OUTCOMES[scenario.goal].primary),
            relevant=relevant,
            source=f"{q.model} {q.collected} · {scenario.id}",
        )
        cases.append(case)
    return cases, skipped


def load_cases(path: Path = CASES) -> list[EvalCase]:
    with path.open(encoding="utf-8") as f:
        return [EvalCase.model_validate_json(line) for line in f if line.strip()]


# ── 지표 ─────────────────────────────────────────────────────────────────────────────────────


def record_ranks(ranked_ids: Sequence[int], key_of: Mapping[int, Key]) -> dict[Key, int]:
    """chunk 순위 → 레코드 순위(그 레코드의 chunk 중 가장 좋은 순위)."""
    ranks: dict[Key, int] = {}
    for rank, chunk_id in enumerate(ranked_ids, 1):
        ranks.setdefault(key_of[chunk_id], rank)
    return ranks


def hit_at(ranks: Mapping[Key, int], relevant: Sequence[Key], k: int) -> float:
    return float(any(ranks.get(key, math.inf) <= k for key in relevant))


def recall_at(ranks: Mapping[Key, int], relevant: Sequence[Key], k: int) -> float:
    if not relevant:
        return math.nan
    return sum(ranks.get(key, math.inf) <= k for key in relevant) / len(relevant)


def mrr(ranks: Mapping[Key, int], relevant: Sequence[Key]) -> float:
    best = min((ranks[key] for key in relevant if key in ranks), default=None)
    return 0.0 if best is None else 1 / best


def metrics(ranks: Mapping[Key, int], relevant: Sequence[Key]) -> dict[str, float]:
    out = {f"hit@{k}": hit_at(ranks, relevant, k) for k in KS}
    out |= {f"recall@{k}": recall_at(ranks, relevant, k) for k in KS}
    out["mrr"] = mrr(ranks, relevant)
    return out


def foreign(ranks: Mapping[Key, int], own: Collection[Key], k: int = 5) -> list[Key]:
    """상위 k chunk에 든 레코드 중 그 활동의 evidence[]에 없는 것, 순위 순. k = 5는 plan이
    검증에 넘기는 건수(hybrid.TOP_K)다."""
    top = sorted((rank, key) for key, rank in ranks.items() if rank <= k)
    return [key for _, key in top if key not in own]


def foreign_at(ranks: Mapping[Key, int], own: Collection[Key], k: int = 5) -> float:
    """상위 k chunk에 든 레코드 중 그 활동의 evidence[] 밖 비율. 하나도 안 들면 0이다."""
    top = sum(rank <= k for rank in ranks.values())
    return len(foreign(ranks, own, k)) / top if top else 0.0


def found_at(ranks: Mapping[Key, int], k: int = 5) -> int:
    """상위 k chunk에 든 레코드 수 — fallback에서 plan이 검증에 넘길 근거 수."""
    return sum(rank <= k for rank in ranks.values())


def policy_excluded(
    keys: Sequence[Key],
    orders: Mapping[Key, Collection[str]],
    user_order: InterventionOrder | None,
) -> list[Key]:
    """그 사용자 상태에 인용할 수 없는 연구 조건의 레코드 — 검색의 순서 필터가 일부러 거른다.

    hybrid.SEARCH_SQL과 같다: 연구 조건이 미상(빈 목록)이면 거르지 않는다.
    """
    if user_order is None:
        return []
    allowed = {o.value for o in ORDER_COMPATIBILITY[user_order]}
    return [key for key in keys if orders.get(key) and not set(orders[key]) & allowed]


def merge_rankings(rankings: Iterable[Sequence[tuple[int, float]]], limit: int) -> list[int]:
    """매체마다 나온 (chunk id, 점수) 순위를 레코드별 최고 점수로 합친다. 동점이면 id 순."""
    best: dict[int, float] = {}
    for ranking in rankings:
        for chunk_id, score in ranking:
            if chunk_id not in best or score > best[chunk_id]:
                best[chunk_id] = score
    return [cid for cid, _ in sorted(best.items(), key=lambda p: (-p[1], p[0]))[:limit]]


def rank_rows(rows: Sequence[dict[str, Any]], mode: str, limit: int) -> list[tuple[int, float]]:
    """SEARCH_SQL 행 → (chunk id, 점수). hybrid는 운영의 fuse, dense · keyword는 순위의 음수다."""
    if mode == "hybrid":
        return [(hit.evidence_record_id, hit.score) for hit in hybrid.fuse(rows, limit)]
    column = f"{mode}_rank"
    ranked = sorted((r for r in rows if r[column] is not None), key=lambda r: (r[column], r["id"]))
    return [(r["id"], -float(r[column])) for r in ranked[:limit]]


# ── 실행 ─────────────────────────────────────────────────────────────────────────────────────


async def resolve_records(
    pool: AsyncConnectionPool,
) -> tuple[dict[Key, list[int]], dict[Key, frozenset[str]]]:
    """레코드 키 → chunk id들, 키 → 연구 조건(metadata.intervention_orders)."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "select id, paper_id, experiment_id, metadata -> 'intervention_orders' as orders"
            " from public.evidence_records order by id"
        )
        rows = await cur.fetchall()
    ids: dict[Key, list[int]] = {}
    orders: dict[Key, frozenset[str]] = {}
    for row in rows:
        key = (row["paper_id"], row["experiment_id"])
        ids.setdefault(key, []).append(row["id"])
        orders[key] = frozenset(row["orders"] or [])
    return ids, orders


async def search_rows(
    pool: AsyncConnectionPool,
    *,
    query: str,
    vector: list[float] | None,
    medium: Medium | None,
    order: InterventionOrder | None,
    outcomes: Sequence[Outcome],
    evidence_keys: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """hybrid.search와 같은 SQL · 인자로 후보 행(벡터 · 키워드 순위 포함)을 받는다."""
    params = hybrid.search_params(
        query=query,
        vector=vector,
        medium=medium,
        intervention_order=order,
        outcomes=outcomes,
        record_type=None,
        evidence_keys=evidence_keys,
    )
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(hybrid.SEARCH_SQL, params)
        return await cur.fetchall()


def label(key: Key) -> str:
    return "/".join(part for part in key if part)


@dataclass
class CaseResult:
    case: EvalCase
    view: str  # planner · fallback · open
    mode: str
    ranked: list[int]  # 상위 LIMIT chunk id
    ranks: dict[Key, int]  # 레코드 → 순위(상위 LIMIT 안에 든 것만)
    gold: list[Key]  # 셀 정답 — DB에 있고 사용자 상태로 거르지 않는 것
    unresolved: list[Key]  # DB에 없는 정답
    excluded: list[Key]  # 사용자 상태에 인용할 수 없는 연구 조건의 정답
    own: frozenset[Key]  # 그 활동의 evidence[] 전부(역할 · 지표 무관)

    @property
    def metrics(self) -> dict[str, float]:
        if self.view == "fallback":
            found = found_at(self.ranks)
            return {"found@5": float(found), "empty@5": float(found == 0)}
        return metrics(self.ranks, self.gold) | {"foreign@5": foreign_at(self.ranks, self.own)}

    @property
    def scored(self) -> bool:
        """평균에 넣는가. 정답이 모두 빠진 planner 케이스는 뺀다."""
        return self.view == "fallback" or bool(self.gold)

    def to_json(self) -> dict[str, Any]:
        return {
            "case": self.case.id,
            "group": self.case.group,
            "view": self.view,
            "mode": self.mode,
            "ranked": self.ranked,
            "ranks": {label(key): rank for key, rank in self.ranks.items()},
            "gold": [label(key) for key in self.gold],
            "unresolved": [label(key) for key in self.unresolved],
            "excluded": [label(key) for key in self.excluded],
            "foreign": [label(key) for key in foreign(self.ranks, self.own)],
            "metrics": self.metrics,
        }


def own_records(protocol_id: str) -> frozenset[Key]:
    """그 활동의 evidence[] 레코드 — Foreign@5의 '자기 근거'이자 먼저 찾는 범위."""
    protocol = protocols.load_protocols()[protocol_id]
    return frozenset((e.paper_id, e.experiment_id) for e in protocol.evidence)


async def search_by_medium(
    pool: AsyncConnectionPool,
    case: EvalCase,
    vector: list[float] | None,
    evidence_keys: Sequence[str] = (),
) -> list[list[dict[str, Any]]]:
    """매체마다 후보 행."""
    return [
        await search_rows(
            pool,
            query=case.query,
            vector=vector,
            medium=medium,
            order=case.intervention_order,
            outcomes=case.outcomes,
            evidence_keys=evidence_keys,
        )
        for medium in case.media
    ]


async def evaluate(
    pool: AsyncConnectionPool,
    cases: Sequence[EvalCase],
    modes: Sequence[str],
    *,
    open_view: bool = False,
) -> list[CaseResult]:
    records, orders = await resolve_records(pool)
    vectors: dict[str, list[float] | None] = {}
    results: list[CaseResult] = []
    for case in cases:
        if case.query not in vectors:
            vectors[case.query] = await embedding.embed_query(case.query)
        vector = vectors[case.query]
        own = own_records(case.protocol_id)
        # plan 노드(find_evidence)처럼 그 활동의 evidence[] 안에서 먼저 찾고,
        # 0건이면 같은 필터로 전체에서 찾는다
        scope = sorted(e or p for p, e in own)
        rows_by_medium = await search_by_medium(pool, case, vector, scope)
        if scope and not any(rows_by_medium):
            rows_by_medium = await search_by_medium(pool, case, vector)
        resolved = [r.key for r in case.relevant if r.key in records]
        unresolved = [r.key for r in case.relevant if r.key not in records]
        excluded = policy_excluded(resolved, orders, case.intervention_order)
        views = [(case.group, rows_by_medium, [k for k in resolved if k not in excluded], excluded)]
        if open_view and case.group == "planner":
            unfiltered = await search_rows(
                pool, query=case.query, vector=vector, medium=None, order=None, outcomes=()
            )
            views.append(("open", [unfiltered], resolved, []))
        for view, rows_list, view_gold, view_excluded in views:
            key_of: dict[int, Key] = {
                row["id"]: (row["paper_id"], row["experiment_id"])
                for rows in rows_list
                for row in rows
            }
            for mode in modes:
                rankings = (rank_rows(rows, mode, LIMIT) for rows in rows_list)
                ranked = merge_rankings(rankings, LIMIT)
                results.append(
                    CaseResult(
                        case=case,
                        view=view,
                        mode=mode,
                        ranked=ranked,
                        ranks=record_ranks(ranked, key_of),
                        gold=view_gold,
                        unresolved=unresolved,
                        excluded=view_excluded,
                        own=own,
                    )
                )
    return results


def metrics_of(name: str) -> tuple[str, ...]:
    return FALLBACK_METRICS if name.startswith("fallback/") else METRICS


def summarize(results: Sequence[CaseResult]) -> dict[str, dict[str, Any]]:
    """(view, mode)마다 지표 평균. 문항마다 같은 무게다."""
    groups: dict[str, list[CaseResult]] = {}
    for r in results:
        if r.scored:
            groups.setdefault(f"{r.view}/{r.mode}", []).append(r)
    summary: dict[str, dict[str, Any]] = {}
    for name, rs in groups.items():
        summary[name] = {"n": len(rs)}
        for metric in metrics_of(name):
            summary[name][metric] = sum(r.metrics[metric] for r in rs) / len(rs)
    return summary


def _order(name: str) -> tuple[int, int]:
    view, mode = name.split("/")
    known = view in VIEWS and mode in MODES
    return (VIEWS.index(view), MODES.index(mode)) if known else (len(VIEWS), 0)


def format_summary(summary: Mapping[str, Mapping[str, Any]]) -> str:
    lines = []
    for columns in (METRICS, FALLBACK_METRICS):
        names = [n for n in sorted(summary, key=_order) if metrics_of(n) == columns]
        if not names:
            continue
        if lines:
            lines.append("")
        lines.append(f"{'view/mode':18s} {'n':>3s} " + " ".join(f"{m:>9s}" for m in columns))
        for name in names:
            row = summary[name]
            cells = " ".join(f"{row[m]:>9.3f}" for m in columns)
            lines.append(f"{name:18s} {row['n']:>3d} {cells}")
    return "\n".join(lines)


def format_misses(results: Sequence[CaseResult], mode: str = "hybrid", k: int = 5) -> str:
    """상위 k 안에 못 든 정답 레코드와 실제 순위(없으면 -)."""
    lines = []
    for r in results:
        if r.view != "planner" or r.mode != mode or not r.gold or r.metrics[f"recall@{k}"] >= 1:
            continue
        missed = [key for key in r.gold if r.ranks.get(key, math.inf) > k]
        shown = ", ".join(f"{label(key)}@{r.ranks.get(key, '-')}" for key in missed)
        lines.append(f"- {r.case.id} ({r.case.query!r}): {shown}")
    planner = [r for r in results if r.view == "planner" and r.mode == mode]
    if excluded := sum(len(r.excluded) for r in planner):
        lines.append(f"사용자 상태로 일부러 거른 정답 {excluded}건(분모에서 뺌)")
    if empty := sum(not r.gold for r in planner):
        lines.append(f"정답이 모두 빠져 평균에서 뺀 케이스 {empty}개")
    unresolved = {key for r in results for key in r.unresolved}
    if unresolved:
        names = ", ".join(sorted(label(key) for key in unresolved))
        lines.append(f"DB에 없는 정답(분모에서 뺌): {names}")
    return "\n".join(lines) or f"(상위 {k} 안에 정답을 모두 찾았다)"


def format_foreign(results: Sequence[CaseResult], mode: str = "hybrid") -> str:
    """planner 상위 5에 든 그 활동 evidence[] 밖 레코드."""
    lines = []
    for r in results:
        if r.view != "planner" or r.mode != mode or not r.gold:
            continue
        if found := foreign(r.ranks, r.own):
            names = ", ".join(label(key) for key in found)
            lines.append(f"- {r.case.id} ({r.case.query!r}): {names}")
    return "\n".join(lines) or "(상위 5에 다른 활동의 근거가 없다)"


def format_fallback(results: Sequence[CaseResult], mode: str = "hybrid") -> str:
    """fallback 상위 5에 든 레코드 — plan이 검증해 Reference 카드에 빌려 올 근거."""
    lines = []
    for r in results:
        if r.view != "fallback" or r.mode != mode:
            continue
        top = sorted((rank, key) for key, rank in r.ranks.items() if rank <= 5)
        names = ", ".join(label(key) for _, key in top) or "없음 — 카드에 근거가 없다"
        lines.append(f"- {r.case.id} ({r.case.query!r}): {names}")
    return "\n".join(lines) or "(fallback 케이스가 없다)"


def db_label(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 5432}{parts.path}"


def retrieval_setup(embedding_model: str) -> str:
    """검색 구성 한 줄 — 운영 hybrid.search의 상수. 실행 기록끼리 바뀐 것이 보이게 남긴다."""
    return (
        f"hybrid RRF k={hybrid.RRF_K} · 후보 {hybrid.CANDIDATES}×2(벡터 · 키워드) · "
        f"상위 {hybrid.TOP_K} · 키워드 유사도 {hybrid.KEYWORD_MIN_SIMILARITY} · "
        f"임베딩 {embedding_model}"
    )


def git_commit() -> str | None:
    """지금 코드의 커밋(짧은 해시). 작업 트리에 커밋 안 된 변경이 있으면 '+'를 붙인다."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if head.returncode != 0:
        return None
    return head.stdout.strip() + ("+" if dirty.stdout.strip() else "")


def _rate(value: float) -> str:
    return "—" if math.isnan(value) else f"{value:.3f}"


def _summary_table(summary: Mapping[str, Mapping[str, Any]], columns: Sequence[str]) -> list[str]:
    names = [n for n in sorted(summary, key=_order) if metrics_of(n) == columns]
    lines = [
        "| view/mode | n | " + " | ".join(columns) + " |",
        "| --- | ---: | " + " | ".join("---:" for _ in columns) + " |",
    ]
    for name in names:
        row = summary[name]
        cells = " | ".join(_rate(row[m]) if m in row else "—" for m in columns)
        lines.append(f"| {name} | {row['n']} | {cells} |")
    return lines


def record_markdown(title: str, payload: Mapping[str, Any], queries: Mapping[str, str]) -> str:
    """실행 기록(docs/rag-eval/YYYY-MM-DD-retrieval-<주제>.md) — run --out 결과로 쓴다.
    queries는 케이스 ID → 질의(놓친 정답 표에 싣는다)."""
    meta, summary, results = payload["meta"], payload["summary"], payload["results"]
    groups = meta.get("groups") or {}
    counts = " · ".join(f"{g} {n}" for g, n in groups.items())
    lines = [
        f"# 근거 검색 평가 — {title}",
        "",
        f"- 평가셋 `{Path(meta['cases']).name}` · 케이스 {meta['n_cases']}개"
        + (f"({counts})" if counts else ""),
        f"- 근거 DB `{meta['db']}` · 검색 구성 {meta.get('setup') or '—'}",
        f"- 코드 `{meta.get('commit') or '—'}` · {meta['at']}",
        "",
        "## 정답이 있는 상황",
        "",
        *_summary_table(summary, METRICS),
        "",
        "## fallback — 그 활동 evidence[]에 목표 지표 근거가 없는 상황",
        "",
        *_summary_table(summary, FALLBACK_METRICS),
        "",
    ]
    planner = [r for r in results if r.get("view") == "planner" and r["mode"] == "hybrid"]
    missed = []
    for r in planner:
        if not r["gold"] or r["metrics"]["recall@5"] >= 1:
            continue
        ranks = r["ranks"]
        names = [f"{g}@{ranks.get(g, '-')}" for g in r["gold"] if ranks.get(g, math.inf) > 5]
        missed.append(f"| {r['case']} | {queries.get(r['case'], '—')} | {', '.join(names)} |")
    lines += [f"## 상위 5 안에 못 든 정답 (planner · hybrid) — {len(missed)}건", ""]
    if missed:
        lines += ["| 케이스 | 질의 | 놓친 정답@순위 |", "| --- | --- | --- |", *missed]
    else:
        lines.append("없음.")
    excluded = sum(len(r["excluded"]) for r in planner)
    empty = sum(not r["gold"] for r in planner)
    unresolved = sorted({key for r in results for key in r["unresolved"]})
    notes = []
    if excluded:
        notes.append(f"- 사용자 상태로 일부러 거른 정답 {excluded}건(분모에서 뺌)")
    if empty:
        notes.append(f"- 정답이 모두 빠져 평균에서 뺀 케이스 {empty}개")
    if unresolved:
        notes.append(f"- DB에 없는 정답(분모에서 뺌): {', '.join(unresolved)}")
    if notes:
        lines += ["", *notes]
    foreign_rows = [
        f"| {r['case']} | {queries.get(r['case'], '—')} | {', '.join(r['foreign'])} |"
        for r in planner
        if r["gold"] and r.get("foreign")
    ]
    lines += ["", f"## 상위 5에 든 다른 활동의 근거 (planner · hybrid) — {len(foreign_rows)}건", ""]
    if foreign_rows:
        lines += ["| 케이스 | 질의 | 레코드 |", "| --- | --- | --- |", *foreign_rows]
    else:
        lines.append("없음.")
    fallback = [r for r in results if r.get("view") == "fallback" and r["mode"] == "hybrid"]
    lines += ["", f"## fallback 상위 5 (hybrid) — {len(fallback)}건", ""]
    if fallback:
        lines += ["| 케이스 | 질의 | 상위 5 레코드 |", "| --- | --- | --- |"]
        for r in fallback:
            top = sorted((rank, key) for key, rank in r["ranks"].items() if rank <= 5)
            names = ", ".join(key for _, key in top) or "없음 — 카드에 근거가 없다"
            lines.append(f"| {r['case']} | {queries.get(r['case'], '—')} | {names} |")
    else:
        lines.append("없음.")
    return "\n".join(lines) + "\n"


def write_record(
    path: Path, title: str | None, payload: Mapping[str, Any], cases_path: Path
) -> None:
    queries = {c.id: c.query for c in load_cases(cases_path)} if cases_path.exists() else {}
    path.write_text(record_markdown(title or path.stem, payload, queries), encoding="utf-8")
    print(f"실행 기록: {path}")


async def run(args: argparse.Namespace) -> int:
    url = args.database_url or get_settings().database_url
    if not url:
        print("DB가 없다 — --database-url 또는 .env의 DATABASE_URL", file=sys.stderr)
        return 2
    cases = load_cases(args.cases)
    if args.group:
        cases = [c for c in cases if c.group in args.group]
    if not get_settings().openai_api_key:
        print("주의: OPENAI_API_KEY가 없어 dense는 비고 hybrid는 keyword만 본다", file=sys.stderr)
    pool = create_pool(url)
    await pool.open()
    started = time.perf_counter()
    try:
        results = await evaluate(pool, cases, args.modes, open_view=args.open)
    finally:
        await pool.close()
    summary = summarize(results)
    counts = " · ".join(f"{g} {sum(c.group == g for c in cases)}" for g in GROUPS)
    elapsed = time.perf_counter() - started
    print(f"DB {db_label(url)} · 케이스 {len(cases)}개({counts}) · {elapsed:.1f}s")
    print(format_summary(summary))
    if args.misses:
        print("\n상위 5 안에 못 든 정답(planner · hybrid):")
        print(format_misses(results))
        print("\n상위 5에 든 다른 활동의 근거(planner · hybrid):")
        print(format_foreign(results))
        print("\nfallback 상위 5에 든 근거(hybrid):")
        print(format_fallback(results))
    embedding_model = get_settings().embedding_model
    payload = {
        "meta": {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "db": db_label(url),
            "embedding_model": embedding_model,
            "cases": str(args.cases),
            "n_cases": len(cases),
            "groups": {g: sum(c.group == g for c in cases) for g in GROUPS},
            "modes": list(args.modes),
            "open": args.open,
            "setup": retrieval_setup(embedding_model),
            "commit": git_commit(),
        },
        "summary": summary,
        "results": [r.to_json() for r in results],
    }
    if args.out:
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n결과: {args.out}")
    if args.md:
        write_record(args.md, args.title, payload, args.cases)
    return 0


def compare(before_path: Path, after_path: Path) -> str:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    b_meta, a_meta = before["meta"], after["meta"]
    lines = [f"before {b_meta['db']} ({b_meta['at']}) → after {a_meta['db']} ({a_meta['at']})"]
    for columns in (METRICS, FALLBACK_METRICS):
        names = [
            n
            for n in sorted(before["summary"], key=_order)
            if n in after["summary"] and metrics_of(n) == columns
        ]
        if not names:
            continue
        lines.append(
            f"{'view/mode':18s} " + " ".join(f"{m:>9s}" for m in columns) + "   (after − before)"
        )
        for name in names:
            b, a = before["summary"][name], after["summary"][name]
            # 지표가 없는 쪽(foreign@5 전에 남긴 결과)은 —
            cells = (
                f"{a[m] - b[m]:>+9.3f}" if m in a and m in b else f"{'—':>9s}" for m in columns
            )
            lines.append(f"{name:18s} " + " ".join(cells))
    by_case_before = {(r["case"], r["mode"], r.get("view")): r for r in before["results"]}
    changed = []
    for r in after["results"]:
        prev = by_case_before.get((r["case"], r["mode"], r.get("view")))
        if prev is None or r["mode"] != "hybrid" or not r["gold"]:
            continue
        shifts = [
            f"{m} {prev['metrics'][m]:.2f} → {r['metrics'][m]:.2f}"
            for m in ("recall@5", "foreign@5")
            if m in prev["metrics"] and m in r["metrics"] and prev["metrics"][m] != r["metrics"][m]
        ]
        if shifts:
            changed.append(f"- {r['case']}: {' · '.join(shifts)}")
    which = "hybrid recall@5 · foreign@5가 바뀐 케이스"
    title = f"{which}:" if changed else f"{which} 없음"
    lines.append(f"\n{title}")
    lines.extend(changed)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect", help="시나리오마다 후보를 한 바퀴 돌며 질의를 모은다(LLM)")
    c.add_argument("--scenarios", type=Path, default=SCENARIOS)
    c.add_argument("--out", type=Path, default=QUERIES)
    c.add_argument("--only", nargs="*", help="이 시나리오 ID만")
    c.add_argument("--fresh", action="store_true", help="이미 모은 질의도 다시 모은다")
    c.add_argument("--concurrency", type=int, default=4, help="동시에 도는 시나리오 수")
    b = sub.add_parser("build", help="모은 질의 + data/protocols로 케이스 파일을 만든다")
    b.add_argument("--scenarios", type=Path, default=SCENARIOS)
    b.add_argument("--queries", type=Path, default=QUERIES)
    b.add_argument("--out", type=Path, default=CASES)
    r = sub.add_parser("run", help="케이스를 돌려 지표를 낸다(DB 읽기 전용)")
    r.add_argument("--cases", type=Path, default=CASES)
    r.add_argument("--database-url", help="없으면 .env의 DATABASE_URL")
    r.add_argument("--group", nargs="*", choices=GROUPS)
    r.add_argument("--modes", nargs="*", default=list(MODES), choices=MODES)
    r.add_argument("--open", action="store_true", help="planner 질의를 필터 · 범위 없이도 돈다")
    r.add_argument("--out", type=Path, help="결과 JSON(compare용)")
    r.add_argument("--misses", action="store_true", help="못 찾은 정답 · 섞인 근거를 보인다")
    r.add_argument(
        "--md", type=Path, help="실행 기록(docs/rag-eval/YYYY-MM-DD-retrieval-<주제>.md)"
    )
    r.add_argument("--title", help="실행 기록 제목. 없으면 파일 이름")
    p = sub.add_parser("compare", help="run --out 결과 둘을 비교한다")
    p.add_argument("before", type=Path)
    p.add_argument("after", type=Path)
    d = sub.add_parser("record", help="run --out 결과로 실행 기록을 쓴다(DB · LLM 없음)")
    d.add_argument("result", type=Path, help="run --out 결과")
    d.add_argument("--md", type=Path, required=True, help="실행 기록 경로")
    d.add_argument("--title", help="실행 기록 제목. 없으면 파일 이름")
    d.add_argument("--cases", type=Path, default=CASES, help="질의를 찾을 케이스 파일")
    args = parser.parse_args()

    if args.command == "collect":
        return asyncio.run(collect(args))
    if args.command == "build":
        cases, skipped = build_cases(load_scenarios(args.scenarios), load_queries(args.queries))
        write_jsonl(args.out, cases)
        counts = " · ".join(f"{g} {sum(c.group == g for c in cases)}" for g in GROUPS)
        print(f"케이스 {len(cases)}개 → {args.out} ({counts})")
        for line in skipped:
            print(f"  뺀 질의: {line}")
        return 0
    if args.command == "compare":
        print(compare(args.before, args.after))
        return 0
    if args.command == "record":
        payload = json.loads(args.result.read_text(encoding="utf-8"))
        write_record(args.md, args.title, payload, args.cases)
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
