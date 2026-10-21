"""세션 그래프의 배선을 LLM 없이 확인한다. Planner 자리에 정해 둔 계획을 내는 대역을 넣는다."""

import asyncio

import pytest
from langgraph.graph import END
from langgraph.types import Command

from app.agent.checkpoint import memory_checkpointer
from app.agent.context import AgentContext
from app.agent.graph import build_graph, route_after_confirm
from app.agent.nodes.planner import PlanRejected, check_plan
from app.agent.state import (
    ActivityContent,
    CarePlan,
    NextStep,
    OutcomeEvaluation,
    PlanMode,
    PlannedActivity,
    ReplanProposal,
    Round,
    SessionReport,
)
from app.schemas.care import CareGoal, CareRequest
from app.schemas.evidence import Medium

CONFIG = {"configurable": {"thread_id": "session-1"}}


def activity(protocol_id: str, goal=CareGoal.S1, medium=Medium.MINDFULNESS) -> PlannedActivity:
    return PlannedActivity(protocol_id=protocol_id, goal=goal, medium=medium, minutes=3)


SINGLE = CarePlan(mode=PlanMode.SINGLE, activities=[activity("s1-mindfulness")], rationale="-")
SEQUENTIAL = CarePlan(
    mode=PlanMode.SEQUENTIAL,
    activities=[
        activity("s2-breathing", CareGoal.S2, Medium.BREATHING),
        activity("s2-nature-photo", CareGoal.S2, Medium.STATIC_IMAGE),
    ],
    rationale="-",
)


class FakePlanner:
    """계획을 차례로 돌려주고, 호출마다 (지난 라운드 수, 거부 사유)를 남긴다."""

    def __init__(self, *plans: CarePlan):
        self.plans = list(plans)
        self.calls: list[tuple[int, list[str]]] = []

    async def __call__(self, state, context, problems):
        self.calls.append((len(state.get("rounds", [])), list(problems)))
        return self.plans.pop(0)


def run(graph, value):
    return asyncio.run(graph.ainvoke(value, CONFIG, context=AgentContext()))


def test_session_loops_until_user_ends(caplog):
    planner = FakePlanner(SINGLE, SEQUENTIAL)
    graph = build_graph(planner, memory_checkpointer())

    # ① 계획 → ② 콘텐츠 준비 → FE 실행을 기다린다
    out = run(graph, {"request": CareRequest(stress_level=7, goal=CareGoal.S1)})
    session = out["__interrupt__"][0].value
    assert session["plan"]["mode"] == "SINGLE"
    assert [c["protocol_id"] for c in session["contents"]] == ["s1-mindfulness"]

    # 사후 측정 → ③ 평가 → ④ 제안 → 사용자 선택을 기다린다
    out = run(graph, Command(resume={"completed": True, "post_stress_level": 4}))
    assert out["evaluation"].stress_change == -3
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "END"

    # 추가 세션에 동의 → Planner로 돌아간다. 다음 라운드의 사전 값은 이번 사후 값이다
    out = run(graph, Command(resume={"next_step": "ADDITIONAL_SESSION"}))
    assert out["__interrupt__"][0].value["plan"]["mode"] == "SEQUENTIAL"
    assert out["request"].stress_level == 4

    stopped = {"completed": False, "stopped_reason": "어지러움", "post_stress_level": 6}
    out = run(graph, Command(resume=stopped))
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "CHANGE_ACTIVITY"

    out = run(graph, Command(resume={"next_step": "END"}))
    assert "__interrupt__" not in out
    assert [r.next_step for r in out["rounds"]] == [NextStep.ADDITIONAL_SESSION, NextStep.END]
    assert [r.evaluation.stress_change for r in out["rounds"]] == [-3, 2]
    assert planner.calls == [(0, []), (1, [])]

    # 체크포인트에서 복원한 값이 원래 타입이다. checkpoint.STATE_TYPES에 빠진 타입이 있으면
    # 역직렬화가 막히고(로그) dict나 원시 값으로 돌아온다
    assert not [r for r in caplog.records if "Blocked deserialization" in r.getMessage()]
    values = asyncio.run(graph.aget_state(CONFIG)).values
    assert isinstance(values["request"], CareRequest)
    assert isinstance(values["plan"], CarePlan)
    assert isinstance(values["contents"][0], ActivityContent)
    assert isinstance(values["report"], SessionReport)
    assert isinstance(values["evaluation"], OutcomeEvaluation)
    assert isinstance(values["proposal"], ReplanProposal)
    assert values["next_step"] is NextStep.END
    assert all(isinstance(r, Round) for r in values["rounds"])


def test_plan_is_retried_once_with_problems_then_rejected():
    invalid = SINGLE.model_copy(update={"activities": [activity("a"), activity("b")]})
    planner = FakePlanner(invalid, invalid)
    graph = build_graph(planner, memory_checkpointer())

    with pytest.raises(PlanRejected):
        run(graph, {"request": CareRequest(stress_level=7, goal=CareGoal.S1)})
    assert planner.calls == [(0, []), (0, check_plan(invalid))]


def test_check_plan_rules():
    assert check_plan(SINGLE) == []
    assert check_plan(SEQUENTIAL) == []
    assert len(check_plan(SINGLE.model_copy(update={"activities": SEQUENTIAL.activities}))) == 1
    assert len(check_plan(SEQUENTIAL.model_copy(update={"activities": SINGLE.activities}))) == 1
    duplicated = [activity("s2-breathing"), activity("s2-breathing")]
    assert len(check_plan(SEQUENTIAL.model_copy(update={"activities": duplicated}))) == 1


def test_route_after_confirm():
    assert route_after_confirm({"next_step": NextStep.END}) == END
    assert route_after_confirm({"next_step": NextStep.CHANGE_ACTIVITY}) == "plan"
    assert route_after_confirm({"next_step": NextStep.ADDITIONAL_SESSION}) == "plan"
