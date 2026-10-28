"""계획 평가 — 시나리오(data/eval_scenarios.jsonl)마다 운영 plan 노드를 돌려 규칙으로 판정한다.

    .venv/bin/python scripts/plan_eval.py --database-url <URL>   # LLM 호출(비용) · DB 읽기 전용
    .venv/bin/python scripts/plan_eval.py --only S2-pre-a S3-pre-b --out plan_eval.json

- 목표 · 사용자 상태 · 칩(setting · earphones · minutes)은 intake 결과로 직접 넣는다(intake는
  따로 평가한다). plan 노드는 그대로 부르므로 후보 → 선택(LLM) → 검색 → 검증 → 설명(LLM),
  check_plan 재질의, 공급자 폴백까지 운영과 같다.
- 정답은 활동 하나가 아니라 규칙이다. 기대 후보 · 풀릴 조건은 protocols.plan_candidates로
  계산한다 — planned(계획이 나온다) · first_try(LLM의 첫 선택이 재질의 없이 통과) ·
  in_candidates(고른 활동이 기대 후보 안) · relaxed(풀린 조건이 기대와 같다) · constraints(최종
  계획이 check_plan을 통과) · changed(활동 변경 뒤 다른 활동). change_activity 시나리오는 첫
  계획의 활동을 바꾸기로 한 다음 라운드를 한 번 더 돈다.
- Reference 카드에 검증을 통과한 근거 수와, 그중 그 활동 evidence[] 밖에서 빌려 온 것을 센다.
  근거가 없는 카드는 규칙 위반이 아니라 근거 부족이라 따로 보인다.
- 근거 DB는 lumi-pg로 돌린다(운영 Supabase의 session pooler는 클라이언트 한도가 15).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langgraph.runtime import Runtime  # noqa: E402

from app import protocols  # noqa: E402
from app.agent.assembly import chat_models  # noqa: E402
from app.agent.context import AgentContext  # noqa: E402
from app.agent.nodes.planner import (  # noqa: E402
    ActivityChoice,
    LlmPlanner,
    Planner,
    PlanRejected,
    check_plan,
    make_plan_node,
)
from app.agent.state import IntakeResult  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.schemas.care import CareRequest, UserProfile, environment_for, place_for  # noqa: E402
from app.schemas.intake import GoalSource  # noqa: E402
from app.schemas.retrieval import GOAL_OUTCOMES  # noqa: E402
from scripts.plan_trial import Usage, previous_round  # noqa: E402
from scripts.rag_eval import SCENARIOS, Scenario, db_label, load_scenarios  # noqa: E402

CHECKS = ("planned", "first_try", "in_candidates", "relaxed", "constraints", "changed")


class CountingPlanner:
    """Planner를 감싸 choose 호출 수와 마지막 선택을 남긴다. 2회면 첫 선택이 거부됐다."""

    def __init__(self, inner: Planner):
        self.inner = inner
        self.calls = 0
        self.last: ActivityChoice | None = None

    async def choose(self, state, candidates, problems):
        self.calls += 1
        self.last = await self.inner.choose(state, candidates, problems)
        return self.last

    async def explain(self, state, choice, protocol, evidence):
        return await self.inner.explain(state, choice, protocol, evidence)


def scenario_state(scenario: Scenario) -> dict:
    """시나리오 → plan 노드 입력. 칩은 intake처럼 환경 조건(environment_for) · 장소로 바꾼다."""
    request = CareRequest(
        text=scenario.text,
        setting=scenario.setting,
        earphones=scenario.earphones,
        minutes_available=scenario.minutes,
        profile=UserProfile(content_type=scenario.content_type),
    )
    intake = IntakeResult(
        goal=scenario.goal,
        goal_source=GoalSource.CHIP,
        target_outcomes=list(GOAL_OUTCOMES[scenario.goal].primary),
        order_condition=scenario.order_condition,
        setting=scenario.setting,
        earphones=scenario.earphones,
        environment=environment_for(scenario.setting, scenario.earphones),
        minutes=scenario.minutes,
        place=place_for(scenario.setting),
    )
    return {"request": request, "intake": intake}


def expected(scenario: Scenario, exclude: Sequence[str] = ()) -> protocols.CandidateSearch:
    """plan 노드(search_candidates)가 같은 칩으로 볼 후보와 풀 조건."""
    return protocols.plan_candidates(
        scenario.goal,
        order=scenario.order_condition,
        minutes=scenario.minutes,
        environment=environment_for(scenario.setting, scenario.earphones),
        exclude=exclude,
    )


@dataclass
class PlanRun:
    """plan 노드 한 번. round 1은 활동 변경 뒤다(previous = 바꾸기로 한 활동)."""

    scenario: str
    goal: str
    round: int
    candidates: list[str]
    expected_relaxed: list[str]
    previous: str | None = None
    protocol_id: str | None = None
    minutes: int | None = None
    relaxed: list[str] = field(default_factory=list)
    query: str | None = None
    calls: int = 0  # choose 호출 수
    rejected: str | None = None  # PlanRejected 사유
    problems: list[str] = field(default_factory=list)  # 최종 계획을 check_plan에 다시 넣은 결과
    cited: list[str] = field(default_factory=list)  # Reference 카드 근거(experiment_id · paper_id)
    borrowed: list[str] = field(default_factory=list)  # 그중 그 활동 evidence[] 밖
    seconds: float = 0.0
    tokens: tuple[int, int] = (0, 0)  # 입력 · 출력

    @property
    def checks(self) -> dict[str, bool]:
        """규칙 판정. changed는 활동 변경 라운드에만 있다."""
        planned = self.rejected is None
        out = {
            "planned": planned,
            "first_try": planned and self.calls == 1,
            "in_candidates": self.protocol_id in self.candidates,
            "relaxed": sorted(self.relaxed) == sorted(self.expected_relaxed),
            "constraints": planned and not self.problems,
        }
        if self.previous is not None:
            out["changed"] = self.protocol_id not in (None, self.previous)
        return out


async def plan_once(
    planner: Planner,
    scenario: Scenario,
    pool: Any = None,
    usage: Usage | None = None,
    previous: str | None = None,
) -> PlanRun:
    """plan 노드를 한 번 돌려 판정에 쓸 것을 모은다. previous가 있으면 그 활동을 바꾸기로 한
    다음 라운드다."""
    state = scenario_state(scenario)
    if previous:
        state["rounds"] = [previous_round(previous, scenario.goal)]
    search = expected(scenario, exclude=[previous] if previous else ())
    run = PlanRun(
        scenario=scenario.id,
        goal=scenario.goal.value,
        round=1 if previous else 0,
        candidates=[p.protocol_id for p in search.protocols],
        expected_relaxed=list(search.relaxed),
        previous=previous,
    )
    counting = CountingPlanner(planner)
    before = (usage.input, usage.output) if usage else (0, 0)
    started = time.perf_counter()
    try:
        out = await make_plan_node(counting)(state, Runtime(context=AgentContext(db_pool=pool)))
    except PlanRejected as exc:
        run.rejected = str(exc)
    else:
        plan = out["plan"]
        protocol = protocols.get_protocol(plan.activity.protocol_id)
        own = {ref.key for ref in protocol.evidence} if protocol else set()
        card = plan.reference_card.evidence if plan.reference_card else []
        run.protocol_id = plan.activity.protocol_id
        run.minutes = plan.activity.minutes
        run.relaxed = list(plan.relaxed)
        run.problems = check_plan(plan.activity, state, plan.relaxed)
        run.cited = [e.experiment_id or e.paper.paper_id for e in card]
        run.borrowed = [key for key in run.cited if key not in own]
    run.calls = counting.calls
    run.query = counting.last.search_query if counting.last else None
    run.seconds = time.perf_counter() - started
    if usage:
        run.tokens = (usage.input - before[0], usage.output - before[1])
    return run


def summarize(runs: Sequence[PlanRun]) -> dict[str, Any]:
    """규칙마다 (통과, 대상 수), 근거 · 비용 집계, 첫 계획에서 고른 활동."""
    planned = [r for r in runs if r.rejected is None]
    checks = {}
    for name in CHECKS:
        judged = [r.checks[name] for r in runs if name in r.checks]
        checks[name] = [sum(judged), len(judged)]
    return {
        "runs": len(runs),
        "scenarios": len({r.scenario for r in runs}),
        "change_rounds": sum(r.round for r in runs),
        "checks": checks,
        "with_evidence": [sum(bool(r.cited) for r in planned), len(planned)],
        "empty_cards": [f"{r.scenario} {r.protocol_id}" for r in planned if not r.cited],
        "borrowed": [f"{r.scenario} {r.protocol_id}" for r in planned if r.borrowed],
        "seconds": sum(r.seconds for r in runs) / len(runs) if runs else 0.0,
        "tokens": [sum(r.tokens[i] for r in runs) / len(runs) if runs else 0.0 for i in (0, 1)],
        "chosen": dict(
            Counter(f"{r.goal} {r.protocol_id}" for r in runs if r.round == 0 and r.protocol_id)
        ),
    }


def format_run(run: PlanRun) -> str:
    head = f"{run.scenario}{' 변경' if run.round else ''}"
    if run.rejected:
        return f"{head:18s} 계획 실패: {run.rejected}"
    failed = [name for name, ok in run.checks.items() if not ok]
    relaxed = f" · 풀림 {','.join(run.relaxed)}" if run.relaxed else ""
    borrowed = f" (빌림 {', '.join(run.borrowed)})" if run.borrowed else ""
    flags = f"  ✗ {' · '.join(failed)}" if failed else ""
    return (
        f"{head:18s} {run.protocol_id} {run.minutes}분 · 후보 {len(run.candidates)}{relaxed}"
        f" · 선택 {run.calls}회 · 근거 {', '.join(run.cited) or '없음'}{borrowed}"
        f" · {run.seconds:.1f}s{flags}"
    )


def format_summary(summary: dict[str, Any]) -> str:
    checks = " · ".join(f"{name} {ok}/{n}" for name, (ok, n) in summary["checks"].items())
    ok, n = summary["with_evidence"]
    chosen = " · ".join(f"{k} ×{v}" for k, v in sorted(summary["chosen"].items()))
    lines = [
        f"계획 {summary['runs']}개(시나리오 {summary['scenarios']} + 활동 변경 "
        f"{summary['change_rounds']})",
        f"규칙  {checks}",
        f"근거  카드에 근거 있음 {ok}/{n} · 빌려 온 근거가 든 카드 {len(summary['borrowed'])}",
        f"      근거 없는 카드: {', '.join(summary['empty_cards']) or '없음'}",
        f"비용  계획 한 번 평균 {summary['seconds']:.1f}s · 토큰 "
        f"{summary['tokens'][0]:,.0f}/{summary['tokens'][1]:,.0f}",
        f"첫 계획에서 고른 활동: {chosen}",
    ]
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    url = args.database_url or settings.database_url
    if not url:
        print("DB가 없다 — --database-url 또는 .env의 DATABASE_URL", file=sys.stderr)
        return 2
    models = chat_models(settings)
    if not models:
        print("LLM 키가 없다 — .env의 OPENAI_API_KEY 또는 ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    usage = Usage()
    for _, model in models:
        model.callbacks = [usage]
    planner = LlmPlanner(models)
    scenarios = [s for s in load_scenarios(args.scenarios) if not args.only or s.id in args.only]
    pool = create_pool(url)
    await pool.open()
    runs: list[PlanRun] = []
    try:
        for scenario in scenarios:
            first = await plan_once(planner, scenario, pool, usage)
            runs.append(first)
            print(format_run(first), flush=True)
            if scenario.change_activity and first.protocol_id:
                second = await plan_once(planner, scenario, pool, usage, first.protocol_id)
                runs.append(second)
                print(format_run(second), flush=True)
    finally:
        await pool.close()
    summary = summarize(runs)
    print(f"\nDB {db_label(url)} · 모델 {settings.llm_model}")
    print(format_summary(summary))
    if args.out:
        payload = {
            "meta": {
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "db": db_label(url),
                "model": settings.llm_model,
                "scenarios": str(args.scenarios),
            },
            "summary": summary,
            "runs": [asdict(r) | {"checks": r.checks} for r in runs],
        }
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n결과: {args.out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--only", nargs="*", help="이 시나리오 ID만")
    parser.add_argument("--database-url", help="근거 DB. 없으면 .env의 DATABASE_URL")
    parser.add_argument("--out", type=Path, help="결과 JSON")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
