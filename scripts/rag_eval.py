"""근거 검색 평가 — 질의마다 정답 레코드를 두고 hybrid.search의 Hit@K · Recall@K · MRR을 잰다.

    .venv/bin/python scripts/rag_eval.py build                      # data/rag_eval.jsonl을 만든다
    .venv/bin/python scripts/rag_eval.py run                        # .env DATABASE_URL (읽기 전용)
    .venv/bin/python scripts/rag_eval.py run --database-url <URL> --out before.json --misses
    .venv/bin/python scripts/rag_eval.py compare before.json after.json

- 정답 단위는 레코드(paper_id, experiment_id)다. 검색은 chunk(METHODS · RESULTS)를 돌려주므로
  레코드의 순위는 그 chunk들 중 가장 좋은 순위다. 정답은 활동 노드 JSON evidence[] 가운데 그 목표의
  1차 지표를 측정한 항목이다 — 검색의 outcomes 필터와 같은 축이라, 지표가 다른 근거를 못 찾았다고
  세지 않는다.
- 질의 묶음(group):
  planner  gpt-4o-mini가 S1 · S2 · S3 세션에서 만든 질의(plan_trial 2026-10-26). 필터는 plan 노드가
           지금 거는 것과 같다 — 활동의 연구 매체 · 사용자 상태 · 목표 1차 지표
  template 프로토콜 × 목표마다 연구 용어로 만든 질의. 필터는 plan 노드(find_evidence)와 같다 —
           제공 매체를 연구 매체로 바꿔 매체마다 검색해 합치고, 목표 1차 지표로 거른다. 사용자
           상태는 없다
  open     template와 같은 질의를 필터 없이. 모든 chunk에서 순위를 매기므로 임베딩 · 키워드만의
           품질이다
  planner · template는 plan 노드처럼 그 활동의 evidence[] 안에서 먼저 찾고, 0건이면 같은 필터로
  전체에서 찾는다(2026-10-28부터. 그 전 운영은 evidence[] 범위 없이 찾았다)
- 방식(mode): hybrid(운영과 같은 RRF) · dense(벡터 순위만) · keyword(낱말 순위만). SQL · 필터 ·
  후보 수는 app/retrieval/hybrid.py의 것을 그대로 쓴다. 매체가 여럿이면 레코드별 최고 점수로
  합친다(plan 노드와 같다).
- 지표: Hit@K(정답 레코드가 상위 K chunk 안에 하나라도), Recall@K(정답 레코드 중 상위 K chunk 안의
  비율), MRR(첫 정답 chunk 순위의 역수). K = 3 · 5 · 10. DB에 없는 정답(미적재 논문)은 분모에서
  빼고 따로 센다. Foreign@5는 상위 5 chunk에 든 레코드 중 그 활동의 evidence[]에 없는 것(같은
  매체를 쓰는 다른 활동의 근거 등)의 비율이다 — plan이 상위 5건을 검증해 인용하므로 Reference
  카드에 남의 근거가 얼마나 섞이는지다. 낮을수록 좋다.
- DB는 읽기만 한다. 질의 임베딩에 OPENAI_API_KEY가 든다(없으면 dense가 비고 hybrid는 keyword만 —
  운영과 같다). content를 바꿔 다시 적재(make seed-evidence)하기 전후에 돌려 compare로 비교한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
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
from app.config import get_settings  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.retrieval import embedding, hybrid  # noqa: E402
from app.schemas.care import CareGoal  # noqa: E402
from app.schemas.evidence import InterventionOrder, Medium, Outcome, study_medium  # noqa: E402
from app.schemas.protocol import CareProtocol, EvidenceRole  # noqa: E402
from app.schemas.retrieval import GOAL_OUTCOMES  # noqa: E402

CASES = ROOT / "data" / "rag_eval.jsonl"
KS = (3, 5, 10)
LIMIT = max(KS)
GROUPS = ("planner", "template", "open")
MODES = ("hybrid", "dense", "keyword")
METRICS = tuple(f"{name}@{k}" for name in ("hit", "recall") for k in KS) + ("mrr", "foreign@5")

Group = Literal["planner", "template", "open"]
Key = tuple[str, str | None]  # (paper_id, experiment_id)

# 질의에 쓰는 연구 용어. 레코드의 영어 라벨(seed의 embedding_text)과 논문 용어에 맞춘다
MEDIUM_TERMS: dict[Medium, str] = {
    Medium.STATIC_IMAGE: "nature images",
    Medium.VIDEO: "nature video",
    Medium.VR: "virtual reality nature",
    Medium.NATURE_SOUND: "nature sounds",
    Medium.MUSIC: "music listening",
    Medium.OUTDOOR_WALK: "nature walk",
    Medium.BREATHING: "slow breathing",
    Medium.PMR: "progressive muscle relaxation",
    Medium.GUIDED_IMAGERY: "guided imagery",
    Medium.MINDFULNESS: "mindfulness meditation",
    Medium.NOTICING: "gratitude three good things",
    Medium.COLORING: "mandala coloring",
    Medium.WRITING: "expressive writing",
    Medium.GROUNDING: "grounding exercise",
    Medium.ART_VIEWING: "art viewing",
    Medium.CBT: "cognitive behavioral",
}
GOAL_TERMS: dict[CareGoal, str] = {
    CareGoal.S1: "rumination",
    CareGoal.S2: "subjective stress anxiety",
    CareGoal.S3: "attention fatigue restoration",
}


class RecordKey(BaseModel):
    """정답 레코드. role은 참고용이다(PRIMARY · CONFLICT · META 모두 플래너가 봐야 할 근거)."""

    paper_id: str
    experiment_id: str | None = None
    role: EvidenceRole

    @property
    def key(self) -> Key:
        return (self.paper_id, self.experiment_id)


class EvalCase(BaseModel):
    """data/rag_eval.jsonl 한 줄. media가 비면 매체 필터 없이 한 번 검색한다."""

    id: str
    group: Group
    query: str
    protocol_id: str
    goal: CareGoal
    media: list[Medium] = []  # 연구 매체(study_medium 적용 후)
    intervention_order: InterventionOrder | None = None
    outcomes: list[Outcome] = []
    relevant: list[RecordKey]
    source: str | None = None


# ── 케이스 만들기 ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlannerQuery:
    scenario: str
    protocol_id: str
    goal: CareGoal
    query: str
    medium: Medium
    order: InterventionOrder | None


# make plan-trial ARGS="S1 S2 S3 --queries x.jsonl"로 모은 질의(2026-10-26, gpt-4o-mini).
# 같은 질의는 하나만 둔다
PLANNER_QUERIES: tuple[PlannerQuery, ...] = (
    PlannerQuery("S1", "PR-09", CareGoal.S1, "mindfulness rumination", Medium.MINDFULNESS, None),
    PlannerQuery(
        "S1", "PR-09", CareGoal.S1, "mindfulness rumination reduction", Medium.MINDFULNESS, None
    ),
    PlannerQuery(
        "S1", "PR-09", CareGoal.S1, "mindfulness meditation rumination", Medium.MINDFULNESS, None
    ),
    PlannerQuery(
        "S2",
        "PR-06",
        CareGoal.S2,
        "slow breathing subjective stress",
        Medium.BREATHING,
        InterventionOrder.PRE_STRESS,
    ),
    PlannerQuery(
        "S2",
        "PR-06",
        CareGoal.S2,
        "breathing subjective stress",
        Medium.BREATHING,
        InterventionOrder.PRE_STRESS,
    ),
    PlannerQuery(
        "S2",
        "PR-06",
        CareGoal.S2,
        "structured breathing anxiety stress",
        Medium.BREATHING,
        InterventionOrder.PRE_STRESS,
    ),
    PlannerQuery(
        "S3",
        "PR-01",
        CareGoal.S3,
        "nature images attention",
        Medium.STATIC_IMAGE,
        InterventionOrder.POST_FATIGUE,
    ),
    PlannerQuery(
        "S3",
        "PR-01",
        CareGoal.S3,
        "nature images fatigue recovery",
        Medium.STATIC_IMAGE,
        InterventionOrder.POST_FATIGUE,
    ),
    PlannerQuery(
        "S3",
        "PR-01",
        CareGoal.S3,
        "nature images attention recovery",
        Medium.STATIC_IMAGE,
        InterventionOrder.POST_FATIGUE,
    ),
)


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


def research_media(protocol: CareProtocol) -> list[Medium]:
    """제공 매체 → 연구 매체(AI_IMAGE → STATIC_IMAGE), 중복 없이. plan 노드와 같다."""
    return list(dict.fromkeys(study_medium(m) for m in protocol.media))


def template_query(media: Sequence[Medium], goal: CareGoal) -> str:
    return " ".join([*(MEDIUM_TERMS[m] for m in media), GOAL_TERMS[goal]])


def build_cases() -> tuple[list[EvalCase], list[str]]:
    """planner 질의 + 프로토콜 × 목표의 template · open 질의. 정답 없는 조합은 사유와 함께 뺀다."""
    loaded = protocols.load_protocols()
    cases: list[EvalCase] = []
    skipped: list[str] = []
    no_gold = "목표 지표를 측정한 근거가 없다"
    for i, pq in enumerate(PLANNER_QUERIES, 1):
        protocol = loaded[pq.protocol_id]
        relevant = gold(protocol, pq.goal)
        if not relevant:
            skipped.append(f"planner {pq.protocol_id} {pq.goal.value}: {no_gold}")
            continue
        cases.append(
            EvalCase(
                id=f"{pq.protocol_id}-{pq.goal.value}-planner-{i}",
                group="planner",
                query=pq.query,
                protocol_id=pq.protocol_id,
                goal=pq.goal,
                media=[pq.medium],
                intervention_order=pq.order,
                outcomes=list(GOAL_OUTCOMES[pq.goal].primary),
                relevant=relevant,
                source=f"plan_trial 2026-10-26 {pq.scenario}",
            )
        )
    for protocol_id, protocol in sorted(loaded.items()):
        media = research_media(protocol)
        if protocol.record_type != "care_protocol" or not media:
            skipped.append(f"{protocol_id}: 단계 모듈이거나 매체가 없다")
            continue
        for goal in dict.fromkeys([protocol.goal, *protocol.secondary_goals]):
            relevant = gold(protocol, goal)
            if not relevant:
                skipped.append(f"{protocol_id} {goal.value}: {no_gold}")
                continue
            common = {
                "query": template_query(media, goal),
                "protocol_id": protocol_id,
                "goal": goal,
                "relevant": relevant,
                "source": "template",
            }
            cases.append(
                EvalCase(
                    id=f"{protocol_id}-{goal.value}-template",
                    group="template",
                    media=media,
                    outcomes=list(GOAL_OUTCOMES[goal].primary),
                    **common,
                )
            )
            cases.append(EvalCase(id=f"{protocol_id}-{goal.value}-open", group="open", **common))
    return cases, skipped


def write_cases(path: Path, cases: Iterable[EvalCase]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for case in cases:
            f.write(json.dumps(case.model_dump(mode="json"), ensure_ascii=False) + "\n")


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


async def resolve_records(pool: AsyncConnectionPool) -> dict[Key, list[int]]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "select id, paper_id, experiment_id from public.evidence_records order by id"
        )
        rows = await cur.fetchall()
    out: dict[Key, list[int]] = {}
    for row in rows:
        out.setdefault((row["paper_id"], row["experiment_id"]), []).append(row["id"])
    return out


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
    mode: str
    ranked: list[int]  # 상위 LIMIT chunk id
    ranks: dict[Key, int]  # 레코드 → 순위(상위 LIMIT 안에 든 것만)
    gold: list[Key]  # DB에 있는 정답
    unresolved: list[Key]  # DB에 없는 정답
    own: frozenset[Key]  # 그 활동의 evidence[] 전부(역할 · 지표 무관)

    @property
    def metrics(self) -> dict[str, float]:
        return metrics(self.ranks, self.gold) | {"foreign@5": foreign_at(self.ranks, self.own)}

    def to_json(self) -> dict[str, Any]:
        return {
            "case": self.case.id,
            "group": self.case.group,
            "mode": self.mode,
            "ranked": self.ranked,
            "ranks": {label(key): rank for key, rank in self.ranks.items()},
            "gold": [label(key) for key in self.gold],
            "unresolved": [label(key) for key in self.unresolved],
            "foreign": [label(key) for key in foreign(self.ranks, self.own)],
            "metrics": self.metrics,
        }


def own_records(protocol_id: str) -> frozenset[Key]:
    """그 활동의 evidence[] 레코드 — Foreign@5의 '자기 근거'이자 planner · template의 검색 범위."""
    protocol = protocols.load_protocols()[protocol_id]
    return frozenset((e.paper_id, e.experiment_id) for e in protocol.evidence)


async def search_media(
    pool: AsyncConnectionPool,
    case: EvalCase,
    vector: list[float] | None,
    evidence_keys: Sequence[str] = (),
) -> list[list[dict[str, Any]]]:
    """매체마다 후보 행. 매체가 없으면(open) 매체 필터 없이 한 번."""
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
        for medium in list(case.media) or [None]
    ]


async def evaluate(
    pool: AsyncConnectionPool, cases: Sequence[EvalCase], modes: Sequence[str]
) -> list[CaseResult]:
    records = await resolve_records(pool)
    vectors: dict[str, list[float] | None] = {}
    results: list[CaseResult] = []
    for case in cases:
        if case.query not in vectors:
            vectors[case.query] = await embedding.embed_query(case.query)
        own = own_records(case.protocol_id)
        # planner · template는 plan 노드(find_evidence)처럼 그 활동의 evidence[] 안에서 먼저 찾고,
        # 0건이면 같은 필터로 전체에서 찾는다. open은 범위도 필터도 없다
        scope = sorted(e or p for p, e in own) if case.group != "open" else []
        rows_by_medium = await search_media(pool, case, vectors[case.query], scope)
        if scope and not any(rows_by_medium):
            rows_by_medium = await search_media(pool, case, vectors[case.query])
        key_of: dict[int, Key] = {
            row["id"]: (row["paper_id"], row["experiment_id"])
            for rows in rows_by_medium
            for row in rows
        }
        gold_keys = [r.key for r in case.relevant if r.key in records]
        unresolved = [r.key for r in case.relevant if r.key not in records]
        for mode in modes:
            rankings = (rank_rows(rows, mode, LIMIT) for rows in rows_by_medium)
            ranked = merge_rankings(rankings, LIMIT)
            results.append(
                CaseResult(
                    case=case,
                    mode=mode,
                    ranked=ranked,
                    ranks=record_ranks(ranked, key_of),
                    gold=gold_keys,
                    unresolved=unresolved,
                    own=own,
                )
            )
    return results


def summarize(results: Sequence[CaseResult]) -> dict[str, dict[str, Any]]:
    """(group, mode)마다 지표 평균. 정답이 DB에 하나도 없는 케이스는 뺀다."""
    groups: dict[str, list[CaseResult]] = {}
    for r in results:
        if r.gold:
            groups.setdefault(f"{r.case.group}/{r.mode}", []).append(r)
    summary: dict[str, dict[str, Any]] = {}
    for name, rs in groups.items():
        summary[name] = {"n": len(rs)}
        for metric in METRICS:
            summary[name][metric] = sum(r.metrics[metric] for r in rs) / len(rs)
    return summary


def _order(name: str) -> tuple[int, int]:
    group, mode = name.split("/")
    return GROUPS.index(group), MODES.index(mode)


def format_summary(summary: Mapping[str, Mapping[str, Any]]) -> str:
    lines = [f"{'group/mode':18s} {'n':>3s} " + " ".join(f"{m:>9s}" for m in METRICS)]
    for name in sorted(summary, key=_order):
        row = summary[name]
        cells = " ".join(f"{row[m]:>9.3f}" for m in METRICS)
        lines.append(f"{name:18s} {row['n']:>3d} {cells}")
    return "\n".join(lines)


def format_misses(results: Sequence[CaseResult], mode: str = "hybrid", k: int = 5) -> str:
    """상위 k 안에 못 든 정답 레코드와 실제 순위(없으면 -)."""
    lines = []
    for r in results:
        if r.mode != mode or not r.gold or r.metrics[f"recall@{k}"] >= 1:
            continue
        missed = [key for key in r.gold if r.ranks.get(key, math.inf) > k]
        shown = ", ".join(f"{label(key)}@{r.ranks.get(key, '-')}" for key in missed)
        lines.append(f"- {r.case.id} ({r.case.query!r}): {shown}")
    unresolved = {key for r in results for key in r.unresolved}
    if unresolved:
        names = ", ".join(sorted(label(key) for key in unresolved))
        lines.append(f"DB에 없는 정답(분모에서 뺌): {names}")
    return "\n".join(lines) or f"(상위 {k} 안에 정답을 모두 찾았다)"


def format_foreign(results: Sequence[CaseResult], mode: str = "hybrid") -> str:
    """상위 5에 든 그 활동 evidence[] 밖 레코드. open은 필터가 없어 늘 섞이므로 뺀다."""
    lines = []
    for r in results:
        if r.mode != mode or r.case.group == "open" or not r.gold:
            continue
        if found := foreign(r.ranks, r.own):
            names = ", ".join(label(key) for key in found)
            lines.append(f"- {r.case.id} ({r.case.query!r}): {names}")
    return "\n".join(lines) or "(상위 5에 다른 활동의 근거가 없다)"


def db_label(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 5432}{parts.path}"


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
        results = await evaluate(pool, cases, args.modes)
    finally:
        await pool.close()
    summary = summarize(results)
    print(f"DB {db_label(url)} · 케이스 {len(cases)}개 · {time.perf_counter() - started:.1f}s")
    print(format_summary(summary))
    if args.misses:
        print("\n상위 5 안에 못 든 정답(hybrid):")
        print(format_misses(results))
        print("\n상위 5에 든 다른 활동의 근거(hybrid · planner · template):")
        print(format_foreign(results))
    if args.out:
        payload = {
            "meta": {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "db": db_label(url),
                "embedding_model": get_settings().embedding_model,
                "cases": str(args.cases),
                "n_cases": len(cases),
            },
            "summary": summary,
            "results": [r.to_json() for r in results],
        }
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n결과: {args.out}")
    return 0


def compare(before_path: Path, after_path: Path) -> str:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    b_meta, a_meta = before["meta"], after["meta"]
    lines = [
        f"before {b_meta['db']} ({b_meta['at']}) → after {a_meta['db']} ({a_meta['at']})",
        f"{'group/mode':18s} " + " ".join(f"{m:>9s}" for m in METRICS) + "   (after − before)",
    ]
    for name in sorted(before["summary"], key=_order):
        if name not in after["summary"]:
            continue
        b, a = before["summary"][name], after["summary"][name]
        # 지표가 없는 쪽(foreign@5 전에 남긴 결과)은 —
        cells = (f"{a[m] - b[m]:>+9.3f}" if m in a and m in b else f"{'—':>9s}" for m in METRICS)
        lines.append(f"{name:18s} " + " ".join(cells))
    by_case_before = {(r["case"], r["mode"]): r for r in before["results"]}
    changed = []
    for r in after["results"]:
        prev = by_case_before.get((r["case"], r["mode"]))
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
    b = sub.add_parser("build", help="data/protocols에서 케이스 파일을 만든다")
    b.add_argument("--out", type=Path, default=CASES)
    r = sub.add_parser("run", help="케이스를 돌려 지표를 낸다(DB 읽기 전용)")
    r.add_argument("--cases", type=Path, default=CASES)
    r.add_argument("--database-url", help="없으면 .env의 DATABASE_URL")
    r.add_argument("--group", nargs="*", choices=GROUPS)
    r.add_argument("--modes", nargs="*", default=list(MODES), choices=MODES)
    r.add_argument("--out", type=Path, help="결과 JSON(compare용)")
    r.add_argument("--misses", action="store_true", help="상위 5 안에 못 든 정답을 보인다")
    c = sub.add_parser("compare", help="run --out 결과 둘을 비교한다")
    c.add_argument("before", type=Path)
    c.add_argument("after", type=Path)
    args = parser.parse_args()

    if args.command == "build":
        cases, skipped = build_cases()
        write_cases(args.out, cases)
        counts = " · ".join(f"{g} {sum(c.group == g for c in cases)}" for g in GROUPS)
        print(f"케이스 {len(cases)}개 → {args.out} ({counts})")
        for line in skipped:
            print(f"  뺀 조합: {line}")
        return 0
    if args.command == "compare":
        print(compare(args.before, args.after))
        return 0
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
