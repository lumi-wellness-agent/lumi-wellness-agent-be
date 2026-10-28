"""실제 모델로 intake → plan 노드를 돌려 Planner가 무엇을 고르고 무엇을 찾았는지 출력한다.

    .venv/bin/python scripts/plan_trial.py                       # S1 · S2
    .venv/bin/python scripts/plan_trial.py S2 --repeat 3
    .venv/bin/python scripts/plan_trial.py S1 --minutes 20 --change-from PR-09
    .venv/bin/python scripts/plan_trial.py --text "하루 종일 집중했더니 머리가 멍해요"
    .venv/bin/python scripts/plan_trial.py S1 S2 --queries queries.jsonl

- 발표 리허설과 RAG 평가셋 질의 수집용이다. intake는 질문 없이 한 번에 돈다(nodes/intake.intake_once
  — 상황 읽기 → 목표 추론(단서표) → 끝). 세션이었다면 물었을 질문을 함께 출력한다.
  plan 노드는 그대로 부르므로 후보 → 선택(LLM) → 검색 → 검증 → 설명(LLM) 절차, check_plan 재질의,
  공급자 폴백까지 운영과 같다. 측정·실행 interrupt는 거치지 않는다.
- LLM(.env의 OPENAI_API_KEY · ANTHROPIC_API_KEY)을 실제로 불러 비용이 든다. 근거 DB는 읽기만 한다.
  DB는 --database-url, 없으면 .env의 DATABASE_URL.
- --queries는 검색 호출(질의 · 필터 · 결과 ID)과 최종 계획의 프로토콜을 JSONL로 덧붙인다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from langgraph.runtime import Runtime  # noqa: E402

from app.agent import validation  # noqa: E402
from app.agent.assembly import chat_models  # noqa: E402
from app.agent.context import AgentContext  # noqa: E402
from app.agent.nodes.intake import LlmIntaker, intake_once  # noqa: E402
from app.agent.nodes.planner import LlmPlanner, PlanRejected, make_plan_node  # noqa: E402
from app.agent.state import (  # noqa: E402
    CarePlan,
    NextStep,
    OutcomeEvaluation,
    PlannedActivity,
    Round,
    SessionReport,
)
from app.config import get_settings  # noqa: E402
from app.db import create_pool  # noqa: E402
from app.retrieval import hybrid  # noqa: E402
from app.schemas.care import CareGoal, CareRequest, ContentType, UserProfile  # noqa: E402
from app.schemas.measure import InstrumentId, Measurement, Timepoint  # noqa: E402

# 인계 메모의 확인 시나리오. S2가 발표 시나리오다(TEAM.md 목)
SCENARIOS: dict[str, CareRequest] = {
    "S1": CareRequest(
        text="같은 생각이 계속 돌아요", profile=UserProfile(content_type=ContentType.AI_IMAGE)
    ),
    "S2": CareRequest(text="발표 앞두고 긴장돼요"),
    "S3": CareRequest(text="하루 종일 집중했더니 머리가 멍해요"),
}


class Usage(BaseCallbackHandler):
    """모델 호출 수와 토큰."""

    def __init__(self) -> None:
        self.calls = self.input = self.output = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        self.calls += 1
        for generations in response.generations:
            for generation in generations:
                usage = getattr(generation, "message", None)
                usage = getattr(usage, "usage_metadata", None) or {}
                self.input += usage.get("input_tokens", 0)
                self.output += usage.get("output_tokens", 0)


class Recorder:
    """검색·검증 본체(hybrid.search · validation.validate)를 감싸 인자와 결과를 남긴다."""

    def __init__(self) -> None:
        self.searches: list[dict] = []
        self.validations: list[dict] = []
        self._search, self._validate = hybrid.search, validation.validate
        hybrid.search, validation.validate = self.search, self.validate

    async def search(self, pool, **kwargs):
        hits = await self._search(pool, **kwargs)
        self.searches.append(
            {
                "query": kwargs["query"],
                "medium": _value(kwargs.get("medium")),
                "intervention_order": _value(kwargs.get("intervention_order")),
                "outcomes": [o.value for o in kwargs.get("outcomes") or []],
                "record_type": _value(kwargs.get("record_type")),
                "within": list(kwargs.get("evidence_keys") or []),
                "hits": [h.evidence_record_id for h in hits],
            }
        )
        return hits

    async def validate(self, pool, **kwargs):
        verdicts = await self._validate(pool, **kwargs)
        self.validations.append(
            {
                "protocol_id": kwargs.get("protocol_id"),
                "media": [m.value for m in kwargs["media"]],
                "verdicts": {
                    v.evidence_record_id: ("usable" if v.usable else "blocked")
                    + (f" {[f.value for f in v.flags]}" if v.flags else "")
                    for v in verdicts
                },
            }
        )
        return verdicts


def _value(enum: Any) -> str | None:
    return enum.value if enum is not None else None


def previous_round(protocol_id: str, goal: CareGoal) -> Round:
    """활동 변경(CHANGE_ACTIVITY)을 고른 직전 라운드. 측정값은 비워 둔다."""
    sam = {"instrument_id": InstrumentId.SAM, "answers": {}, "scores": {}}
    return Round(
        plan=CarePlan(
            activity=PlannedActivity(protocol_id=protocol_id, minutes=5), rationale="(직전 라운드)"
        ),
        report=SessionReport(completed=True),
        pre=[Measurement(timepoint=Timepoint.PRE, round=0, **sam)],
        post=[Measurement(timepoint=Timepoint.POST, round=1, **sam)],
        evaluation=OutcomeEvaluation(
            goal=goal, round=1, deltas={}, improved=False, completed=True
        ),
        next_step=NextStep.CHANGE_ACTIVITY,
    )


async def trial(name: str, request: CareRequest, args, pool, recorder: Recorder) -> dict:
    usage = Usage()
    models = chat_models(get_settings())
    for _, model in models:
        model.callbacks = [usage]
    planner = LlmPlanner(models)
    intaker = LlmIntaker(models)
    intake, would_ask = await intake_once(intaker, request)
    state: dict = {"request": request, "intake": intake}
    print(f"\n== {name} {request.text!r} ({request.profile.content_type.value})")
    defaulted = f" · 기본값 {intake.missing_slots}" if intake.missing_slots else ""
    secondary = f"(+{_value(intake.secondary_goal)})" if intake.secondary_goal else ""
    print(
        f"intake  {_value(intake.goal)}{secondary} [{_value(intake.goal_source)}]"
        f" · {_value(intake.order_condition)} · "
        f"{intake.minutes}분 · "
        f"{_value(intake.setting) or _value(intake.place)}{defaulted}"
    )
    print(f"        {intake.goal_reason or '-'}")
    if would_ask:
        print(f"        세션이었다면 물었을 것: {would_ask}")
    if args.change_from:
        state["rounds"] = [previous_round(args.change_from, intake.goal)]
    recorder.searches.clear()
    recorder.validations.clear()
    started = time.perf_counter()
    plan_node = make_plan_node(planner)
    try:
        out = await plan_node(state, Runtime(context=AgentContext(db_pool=pool)))
    except PlanRejected as exc:
        print(f"계획 실패: {exc}")
        return {"scenario": name, "rejected": str(exc)}
    plan: CarePlan = out["plan"]
    elapsed = time.perf_counter() - started
    keys = ("medium", "intervention_order", "outcomes", "record_type", "within")
    for s in recorder.searches:
        filters = " ".join(f"{k}={s[k]}" for k in keys if s[k])
        print(f"search  {s['query']!r} {filters} → {s['hits'] or '0건'}")
    for v in recorder.validations:
        print(f"verify  {v['protocol_id']} {v['media']} → {v['verdicts']}")
    activity = plan.activity
    cited = activity.evidence_record_ids
    print(f"plan    {activity.protocol_id} {activity.minutes}분 · 근거 {cited}")
    print(f"        {plan.rationale}")
    if factors := plan.content_factors:
        picked = factors.model_dump(mode="json", exclude={"reason"})
        print(f"factors {picked} — {factors.reason}")
    tokens = f"{usage.input:,}/{usage.output:,}"
    print(f"        {elapsed:.1f}s · 모델 호출 {usage.calls}회 · 토큰 {tokens}")
    if card := plan.reference_card:
        arms = [
            f"{e.experiment_id or e.paper.paper_id}({'지지' if e.supports_goal else '참고'})"
            for e in card.evidence
        ]
        print(f"card    {[b.value for b in card.badges]} · 근거 {arms or '없음'}")
        for limit in card.limits[1:]:  # 첫 문장은 모든 카드에 같은 고지
            print(f"        - {limit}")
    return {
        "scenario": name,
        "text": request.text,
        "goal": _value(intake.goal),
        "intervention_order": _value(intake.order_condition),
        "protocol_id": activity.protocol_id,
        "evidence_record_ids": cited,
        "searches": list(recorder.searches),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scenarios", nargs="*", help=f"{' · '.join(SCENARIOS)} (기본 S1 S2)")
    parser.add_argument("--text", help="시나리오 대신 이 입력으로 돌린다")
    parser.add_argument("--goal", choices=[g.value for g in CareGoal], help="목표 칩")
    parser.add_argument("--minutes", type=int, help="쓸 수 있는 시간 칩(분)")
    parser.add_argument("--ai-image", action="store_true", help="AI 생성 이미지 선호")
    parser.add_argument("--change-from", help="직전 라운드에서 활동 변경을 고른 프로토콜 ID")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--database-url", help="근거 DB. 없으면 .env의 DATABASE_URL")
    parser.add_argument("--queries", type=Path, help="검색 기록을 덧붙일 JSONL")
    args = parser.parse_args()

    # 선택 이유·질의(plan 노드 INFO 로그)를 같이 보여 준다
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("app.agent.nodes.planner").setLevel(logging.INFO)

    names = args.scenarios or ([] if args.text else ["S1", "S2"])
    requests = [(n, SCENARIOS[n]) for n in names]
    if args.text:
        requests.append(("TEXT", CareRequest(text=args.text)))
    chips = {
        "goal": CareGoal(args.goal) if args.goal else None,
        "minutes_available": args.minutes,
    }
    chips = {k: v for k, v in chips.items() if v is not None}
    if args.ai_image:
        chips["profile"] = UserProfile(content_type=ContentType.AI_IMAGE)

    database_url = args.database_url or get_settings().database_url
    if not database_url:
        raise SystemExit("근거 DB가 없습니다. --database-url이나 .env의 DATABASE_URL을 채우세요.")
    pool = create_pool(database_url)
    await pool.open()
    recorder = Recorder()
    try:
        for _ in range(args.repeat):
            for name, request in requests:
                result = await trial(name, request.model_copy(update=chips), args, pool, recorder)
                if args.queries and "searches" in result:
                    with args.queries.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
