"""plan 노드의 LLM 호출 2회(활동 선택 · 추천 이유)와 입력 메시지 · 세션 컨텍스트를 대역으로 본다."""

import asyncio

import pytest

from app import protocols
from app.agent import llm
from app.agent.context import AgentContext, PlannerContext
from app.agent.nodes.planner import (
    ActivityChoice,
    CitedEvidence,
    LlmPlanner,
    PlanRationale,
    make_chooser,
    make_explainer,
    planner_context,
)
from app.agent.prompts import CHOOSE_SYSTEM_PROMPT, EXPLAIN_SYSTEM_PROMPT
from app.agent.state import (
    CarePlan,
    IntakeResult,
    NextStep,
    OutcomeEvaluation,
    PlannedActivity,
    Round,
    SessionReport,
)
from app.agent.validation import judge
from app.schemas.care import CareGoal, CareRequest, ContentType, Environment, UserProfile
from app.schemas.evidence import InterventionOrder, Medium
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
CHOICE = ActivityChoice(
    protocol_id="PR-21", search_query="nature images rumination", reason="이미지 선호"
)


def candidates_for(state: dict) -> list[dict]:
    session = planner_context(AgentContext(), state)
    found = protocols.candidates(
        session.goal, order=session.user_order, minutes=session.minutes_available
    )
    return [protocols.compact(p) for p in found]


def cited(evidence_record_id: int, media: list[Medium]) -> CitedEvidence:
    hit = next(h for h in fakes.load_hits() if h.evidence_record_id == evidence_record_id)
    verdict = judge(
        hit,
        goal=CareGoal.S1,
        media=media,
        orders=[InterventionOrder.POST_STRESS],
        candidates=fakes.load_hits(),
    )
    return CitedEvidence(hit, verdict)


def test_chooser_sends_state_candidates_and_problems():
    model = fakes.FakeStructuredModel(CHOICE)
    choose = make_chooser(model)
    problems = ["PR-99는 검수된 프로토콜이 아니다"]

    choice = asyncio.run(choose(S1_STATE, candidates_for(S1_STATE), problems))

    assert choice is CHOICE
    assert model.schemas == [(ActivityChoice, "json_schema")]
    system, human = model.inputs[0]
    assert system.content == CHOOSE_SYSTEM_PROMPT
    text = human.content
    assert "## 사용자 입력\n같은 생각이 계속 돌아요" in text
    assert "- 목표: S1 — 반복되는 생각에서 벗어나기(반추 완화)" in text
    assert "- 사용자 상태: 스트레스를 겪은 뒤(POST_STRESS)" in text
    assert "AI 생성 이미지" in text
    assert "## 후보 프로토콜 (등급 순)" in text
    assert text.index('"PR-09"') < text.index('"PR-21"')  # 코드가 정렬한 순서 그대로
    assert "## 직전 계획의 거부 사유\n- PR-99는 검수된 프로토콜이 아니다" in text


def test_choice_input_tells_which_activity_was_removed():
    state = {**S1_STATE, "rounds": [changed_round("PR-09")]}
    model = fakes.FakeStructuredModel(CHOICE)
    asyncio.run(make_chooser(model)(state, candidates_for(state), []))
    text = model.inputs[0][1].content
    assert "## 지난 라운드" in text and "PR-09는 후보에서 뺐다" in text
    assert "거부 사유" not in text


def test_explainer_lists_only_verified_evidence():
    model = fakes.FakeStructuredModel(PlanRationale(rationale="자연 이미지를 보며 … 다릅니다."))
    explain = make_explainer(model)
    protocol = protocols.get_protocol("PR-21")
    evidence = [cited(102, [Medium.AI_IMAGE, Medium.MINDFULNESS])]

    rationale = asyncio.run(explain(S1_STATE, CHOICE, protocol, evidence))

    assert rationale == "자연 이미지를 보며 … 다릅니다."
    assert model.schemas == [(PlanRationale, "json_schema")]
    system, human = model.inputs[0]
    assert system.content == EXPLAIN_SYSTEM_PROMPT
    text = human.content
    assert "PR-21" in text and "구성 EXPERIMENTAL" in text and "반추 —" in text
    assert "- 고른 이유: 이미지 선호" in text
    assert "## 검증된 근거 (1건)" in text
    assert "[102]" in text and "P24-E1 RESULTS" in text and "목표 지지: 예" in text
    assert "개선 지표: 반추" in text and "ADAPTED_STIMULUS" in text
    assert "발췌: " in text


def test_explainer_says_when_nothing_was_verified():
    model = fakes.FakeStructuredModel(PlanRationale(rationale="근거를 찾지 못했습니다."))
    asyncio.run(make_explainer(model)(S1_STATE, CHOICE, protocols.get_protocol("PR-21"), []))
    assert "검증을 통과한 근거가 없다" in model.inputs[0][1].content


class ClientError(Exception):
    status_code = 401


def test_llm_planner_falls_back_to_the_next_provider(monkeypatch):
    monkeypatch.setattr(llm, "BACKOFF_SECONDS", 0)
    broken = fakes.FakeStructuredModel(ClientError(), ClientError())
    working = fakes.FakeStructuredModel(CHOICE, PlanRationale(rationale="…"))
    planner = LlmPlanner([("openai", broken), ("anthropic", working)])

    choice = asyncio.run(planner.choose(S1_STATE, [], []))
    rationale = asyncio.run(planner.explain(S1_STATE, CHOICE, protocols.get_protocol("PR-21"), []))

    assert choice is CHOICE and rationale == "…"
    assert len(working.inputs) == 2


def test_planner_context_carries_the_session():
    context = planner_context(AgentContext(), S1_STATE)
    assert context == PlannerContext(
        db_pool=None,
        goal=CareGoal.S1,
        user_order=InterventionOrder.POST_STRESS,
        minutes_available=20,
        environment=Environment(),  # 인테이크가 환경을 모른다 — 거르지 않는다
        content_type=ContentType.AI_IMAGE,
    )


def test_planner_context_excludes_the_activity_the_user_changed_away_from():
    state = {**S1_STATE, "rounds": [changed_round("PR-09")]}
    assert planner_context(AgentContext(), state).excluded_protocol_ids == ("PR-09",)
    # 추가 세션은 같은 활동을 다시 할 수 있다
    kept = changed_round("PR-09").model_copy(update={"next_step": NextStep.ADDITIONAL_SESSION})
    again = planner_context(AgentContext(), {**S1_STATE, "rounds": [kept]})
    assert again.excluded_protocol_ids == ()


def changed_round(protocol_id: str) -> Round:
    """활동 변경(CHANGE_ACTIVITY)을 고른 직전 라운드. 측정값은 비워 둔다."""
    sam = {"instrument_id": InstrumentId.SAM, "answers": {}, "scores": {}}
    return Round(
        plan=CarePlan(activity=PlannedActivity(protocol_id=protocol_id, minutes=3), rationale="-"),
        report=SessionReport(completed=True),
        pre=[Measurement(timepoint=Timepoint.PRE, round=0, **sam)],
        post=[Measurement(timepoint=Timepoint.POST, round=1, **sam)],
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S1, round=1, deltas={}, improved=False, completed=True
        ),
        next_step=NextStep.CHANGE_ACTIVITY,
    )


@pytest.mark.parametrize(
    ("protocol_id", "available", "expected"),
    [
        ("PR-06", 10, 5),  # 5분 고정
        ("PR-06", None, 5),
        ("PR-11", 30, 30),  # 기본 50, 최소 20 → 쓸 수 있는 30분
        ("PR-11", 10, 20),  # 최소 20분 아래로는 줄이지 않는다(check_plan이 거른다)
        ("PR-07", 10, 9),  # 9분 고정
        ("PR-01", 60, 6),  # 기본 6.0 → 6
    ],
)
def test_planned_minutes(protocol_id, available, expected):
    assert protocols.planned_minutes(protocols.get_protocol(protocol_id), available) == expected
