"""Planner의 LangChain 배선(create_agent + ToolStrategy)과 plan 노드 후처리를 대본 모델로 본다."""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.runtime import Runtime

from app.agent.context import AgentContext, PlannerContext
from app.agent.nodes.planner import (
    build_planner_agent,
    keep_usable_evidence,
    make_agent_planner,
    make_plan_node,
    planner_context,
)
from app.agent.prompts import build_planner_input
from app.agent.state import (
    CarePlan,
    IntakeResult,
    NextStep,
    OutcomeEvaluation,
    PlannedActivity,
    Round,
    SessionReport,
)
from app.schemas.care import CareGoal, CareRequest, ContentType, UserProfile
from app.schemas.evidence import InterventionOrder
from app.schemas.measure import InstrumentId, Measurement, Timepoint
from tests.agent import fakes

S1_STATE = {
    "request": CareRequest(
        text="같은 생각이 계속 돌아요", profile=UserProfile(content_type=ContentType.AI_IMAGE)
    ),
    "intake": IntakeResult(
        goal=CareGoal.S1, order_condition=InterventionOrder.POST_STRESS, minutes=20
    ),
}


TOOLS = ["get_care_protocols", "search_evidence", "validate_evidence", "CarePlan"]
PLAN_ARGS = {"activity": {"protocol_id": "PR-09", "minutes": 3}, "rationale": "-"}


def call(name: str, args: dict, call_id: str = "1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def submit(plan: dict) -> fakes.ScriptedChatModel:
    return fakes.ScriptedChatModel(messages=iter([call("CarePlan", plan)]))


def submit_call() -> AIMessage:
    return call("CarePlan", PLAN_ARGS, "submit")


def test_agent_planner_returns_submitted_plan():
    model = submit(PLAN_ARGS)
    planner = make_agent_planner(model)

    plan = asyncio.run(planner(S1_STATE, AgentContext(), []))

    assert plan.activity.protocol_id == "PR-09"
    assert plan.activity.evidence_record_ids == []
    # 부르는 순서대로 내놓는다. 마지막은 ToolStrategy의 구조화 출력
    assert model.offered[0] == TOOLS


def test_last_budgeted_call_can_only_submit():
    """모델 호출 예산의 마지막 회차에는 CarePlan만 남는다(tool_choice는 ToolStrategy가 강제)."""
    model = fakes.ScriptedChatModel(
        messages=iter([call("get_care_protocols", {}, str(i)) for i in range(2)] + [submit_call()])
    )
    agent = build_planner_agent(model, max_model_calls=3)
    message = HumanMessage(build_planner_input(S1_STATE, []))
    context = planner_context(AgentContext(), S1_STATE)

    result = asyncio.run(agent.ainvoke({"messages": [message]}, context=context))

    assert model.offered == [TOOLS, TOOLS, ["CarePlan"]]
    assert result["structured_response"].activity.protocol_id == "PR-09"


def test_planner_that_never_submits_is_cut_off():
    """제출을 강제해도 계속 Tool만 부르면 재귀 한도에서 끊는다(기본 한도면 사실상 끝나지 않는다)."""
    model = fakes.ScriptedChatModel(
        messages=iter([call("get_care_protocols", {}, str(i)) for i in range(100)])
    )
    with pytest.raises(GraphRecursionError):
        asyncio.run(make_agent_planner(model)(S1_STATE, AgentContext(), []))
    assert len(model.offered) < 20


def test_planner_context_carries_the_session():
    context = planner_context(AgentContext(), S1_STATE)
    assert context == PlannerContext(
        db_pool=None,
        goal=CareGoal.S1,
        user_order=InterventionOrder.POST_STRESS,
        minutes_available=20,
        content_type=ContentType.AI_IMAGE,
    )


def test_planner_context_excludes_the_activity_the_user_changed_away_from():
    state = {**S1_STATE, "rounds": [changed_round("PR-09")]}
    assert planner_context(AgentContext(), state).excluded_protocol_ids == ("PR-09",)
    # 추가 세션은 같은 활동을 다시 할 수 있다
    kept = changed_round("PR-09").model_copy(update={"next_step": NextStep.ADDITIONAL_SESSION})
    again = planner_context(AgentContext(), {**S1_STATE, "rounds": [kept]})
    assert again.excluded_protocol_ids == ()
    text = build_planner_input(state, [])
    assert "PR-09는 목록에서 뺐다" in text


def changed_round(protocol_id: str) -> Round:
    """활동 변경(CHANGE_ACTIVITY)을 고른 직전 라운드. 측정값은 비워 둔다."""
    sam = {"instrument_id": InstrumentId.SAM, "answers": {}, "scores": {}}
    return Round(
        plan=CarePlan(activity=PlannedActivity(protocol_id=protocol_id, minutes=3), rationale="-"),
        report=SessionReport(completed=True),
        pre=Measurement(timepoint=Timepoint.PRE, round=0, **sam),
        post=Measurement(timepoint=Timepoint.POST, round=1, **sam),
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S1, round=1, deltas={}, improved=False, completed=True
        ),
        next_step=NextStep.CHANGE_ACTIVITY,
    )


def test_planner_input_names_the_search_filters():
    text = build_planner_input(S1_STATE, ["PR-99는 검수된 프로토콜이 아니다"])
    assert "같은 생각이 계속 돌아요" in text
    assert "목표 1차 지표: RUMINATION (outcomes)" in text
    assert "사용자 상태: POST_STRESS (intervention_order)" in text
    assert "AI 생성 이미지" in text
    assert "## 직전 계획의 거부 사유\n- PR-99는 검수된 프로토콜이 아니다" in text


def plan_citing(*ids: int) -> CarePlan:
    return CarePlan(
        activity=PlannedActivity(protocol_id="PR-21", minutes=15, evidence_record_ids=list(ids)),
        rationale="-",
    )


@pytest.fixture
def fake_validate(monkeypatch):
    monkeypatch.setattr("app.agent.validation.validate", fakes.fake_validate)


def test_keep_usable_evidence_drops_unusable_and_unknown_ids(fake_validate, caplog):
    session = planner_context(AgentContext(), S1_STATE)
    # 152 N24 메타: S1 1차 지표(반추) 미측정 → 차단. 999 없는 ID. 102 중복
    kept = asyncio.run(keep_usable_evidence(plan_citing(102, 152, 112, 999, 102), session))
    assert kept.activity.evidence_record_ids == [102, 112]
    assert "[152, 999]" in caplog.text

    unchanged = plan_citing(102)
    assert asyncio.run(keep_usable_evidence(unchanged, session)) is unchanged
    # 목표를 지지하는 근거(102 반추 감소)가 비유의 근거(112)보다 앞에 온다
    reordered = asyncio.run(keep_usable_evidence(plan_citing(112, 102), session))
    assert reordered.activity.evidence_record_ids == [102, 112]


def test_keep_usable_evidence_without_db_drops_everything():
    session = planner_context(AgentContext(), S1_STATE)  # 풀 없음 → 출처 확인 불가
    kept = asyncio.run(keep_usable_evidence(plan_citing(102), session))
    assert kept.activity.evidence_record_ids == []


def test_plan_node_returns_plan_with_only_usable_evidence(fake_validate):
    model = submit(plan_citing(102, 999).model_dump(mode="json"))
    node = make_plan_node(make_agent_planner(model))

    out = asyncio.run(node(S1_STATE, Runtime(context=AgentContext())))

    assert out["plan"].activity.protocol_id == "PR-21"
    assert out["plan"].activity.evidence_record_ids == [102]
