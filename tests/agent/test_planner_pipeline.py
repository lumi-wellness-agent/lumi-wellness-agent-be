"""plan 노드의 고정 절차 — 후보 → 선택(대역) → 검색 → 검증 → 설명(대역) → CarePlan.

검색·검증은 fixture 기반 가짜(tests/agent/fakes.py)로 바꿔 끼우고, Planner 대역이 받은 입력을
남긴다.
"""

import asyncio

import pytest
from langgraph.runtime import Runtime

from app import protocols
from app.agent.context import AgentContext
from app.agent.nodes.planner import ActivityChoice, PlanRejected, make_plan_node
from app.agent.state import IntakeResult
from app.schemas.card import CardBadge
from app.schemas.care import CareGoal, CareRequest, ContentType, Environment, UserProfile
from app.schemas.evidence import InterventionOrder, Medium, Outcome
from tests.agent import fakes

# AI 생성 이미지를 고른 사용자의 S1(반추) 세션. 20분이 있어 PR-21(실내 자연 이미지 + 명상,
# EXPERIMENTAL)을 고르는 대본이다. 검색은 STATIC_IMAGE로 찾고, 검증은 제공 매체
# [AI_IMAGE, MINDFULNESS]로 본다
REQUEST = CareRequest(
    text="같은 생각이 계속 돌아요", profile=UserProfile(content_type=ContentType.AI_IMAGE)
)
INTAKE = IntakeResult(goal=CareGoal.S1, order_condition=InterventionOrder.POST_STRESS, minutes=20)
STATE = {"request": REQUEST, "intake": INTAKE}


class RecordingPlanner:
    """정해 둔 선택을 차례로 내고, choose · explain이 받은 것을 남긴다."""

    def __init__(self, *choices: ActivityChoice, rationale: str = "추천 이유"):
        self.choices = list(choices)
        self.rationale = rationale
        self.chosen: list[tuple[list[str], list[str]]] = []  # (후보 ID, 거부 사유)
        self.explained: list[tuple[str, str, list[int]]] = []  # (프로토콜, 이유, 인용 근거)

    async def choose(self, state, candidates, problems):
        self.chosen.append(([c["protocol_id"] for c in candidates], list(problems)))
        return self.choices.pop(0)

    async def explain(self, state, choice, protocol, evidence):
        self.explained.append(
            (protocol.protocol_id, choice.reason, [c.evidence_record_id for c in evidence])
        )
        return self.rationale


def choice(protocol_id: str, query: str = "nature images rumination") -> ActivityChoice:
    return ActivityChoice(protocol_id=protocol_id, search_query=query, reason="-")


@pytest.fixture
def fake_backends(monkeypatch):
    searches: list[dict] = []

    async def recording_search(pool, **kwargs):
        searches.append(kwargs)
        return await fakes.fake_search(pool, **kwargs)

    monkeypatch.setattr("app.retrieval.hybrid.search", recording_search)
    monkeypatch.setattr("app.agent.validation.validate", fakes.fake_validate)
    return searches


def run_plan(planner, state=STATE):
    node = make_plan_node(planner)
    return asyncio.run(node(state, Runtime(context=AgentContext())))["plan"]


def test_pipeline_chooses_searches_verifies_and_explains(fake_backends):
    planner = RecordingPlanner(choice("PR-21"))

    plan = run_plan(planner)

    # 후보는 세션(S1 · POST_STRESS · 20분)으로 걸러 등급 순으로 준다
    assert planner.chosen == [(["PR-09", "PR-15", "PR-21", "PR-14"], [])]
    # 검색은 제공 매체마다(AI_IMAGE → 연구 매체 STATIC_IMAGE, MINDFULNESS) 같은 필터로
    assert [s["medium"] for s in fake_backends] == [Medium.STATIC_IMAGE, Medium.MINDFULNESS]
    assert all(s["query"] == "nature images rumination" for s in fake_backends)
    assert all(s["intervention_order"] is InterventionOrder.POST_STRESS for s in fake_backends)
    assert all(list(s["outcomes"]) == [Outcome.RUMINATION] for s in fake_backends)
    # 검증을 통과한 것만. 목표를 지지하는 arm(P24-E1: 102 · 101)이 비유의 arm(P33-E1: 112 · 111)보다
    # 앞
    assert plan.activity.protocol_id == "PR-21"
    # 시간은 코드가 정한다 — 프로토콜 기본값(≤ 쓸 수 있는 20분)
    assert plan.activity.minutes == round(protocols.get_protocol("PR-21").duration_minutes.default)
    assert plan.activity.evidence_record_ids == [102, 101, 112, 111]
    assert plan.rationale == "추천 이유"
    assert planner.explained == [("PR-21", "-", [102, 101, 112, 111])]


def test_pipeline_builds_the_reference_card_from_the_cited_evidence(fake_backends):
    plan = run_plan(RecordingPlanner(choice("PR-21")))

    card = plan.reference_card
    assert (card.protocol_id, card.minutes) == ("PR-21", plan.activity.minutes)
    # 인용한 chunk 넷이 실험 둘로 — 목표를 지지하는 P24-E1이 앞
    assert [e.experiment_id for e in card.evidence] == ["P24-E1", "P33-E1"]
    assert sorted(i for e in card.evidence for i in e.evidence_record_ids) == [101, 102, 111, 112]
    assert card.evidence[0].supports_goal and not card.evidence[1].supports_goal
    # 조합 근거가 없는 구성 · AI 생성 이미지(사용자 선호)
    assert {CardBadge.EXPERIMENTAL, CardBadge.AI_IMAGE_UNVERIFIED} <= set(card.badges)
    assert card.goal.goal is CareGoal.S1 and card.goal.measured_by.instrument_id.value == "INS-BSRI"


def test_pipeline_retries_a_choice_outside_the_candidates_then_rejects(fake_backends):
    planner = RecordingPlanner(choice("PR-06"), choice("PR-06"))  # S2 호흡을 S1 세션에
    with pytest.raises(PlanRejected, match="S1 목표용이 아니다"):
        run_plan(planner)
    assert planner.chosen[1][1] == ["PR-06는 후보 목록에 없다", "PR-06는 S1 목표용이 아니다"]
    assert planner.explained == []


def test_pipeline_recovers_when_the_second_choice_is_valid(fake_backends):
    planner = RecordingPlanner(choice("PR-99"), choice("PR-09"))
    plan = run_plan(planner)
    assert planner.chosen[1][1] == ["PR-99는 후보 목록에 없다"]
    assert plan.activity.protocol_id == "PR-09" and plan.activity.minutes == 3
    assert plan.activity.evidence_record_ids == []  # fixture에 MINDFULNESS 근거가 없다


def test_pipeline_without_db_plans_without_evidence():
    planner = RecordingPlanner(choice("PR-21"))
    plan = run_plan(planner)  # 풀이 없으면 검색이 0건이라 검증을 부르지 않는다
    assert plan.activity.evidence_record_ids == []
    assert planner.explained == [("PR-21", "-", [])]


def test_pipeline_rejects_when_nothing_fits():
    state = {**STATE, "intake": INTAKE.model_copy(update={"minutes": 2})}
    with pytest.raises(PlanRejected, match="맞는 검수 프로토콜이 없다"):
        run_plan(RecordingPlanner(choice("PR-09")), state)


def test_pipeline_relaxes_the_user_state_when_no_activity_fits_it():
    # S3 + 사건 전 — 상태가 맞는 S3 활동이 없다. 상태를 풀고(check_plan도 상태를 보지 않는다) 남긴다
    intake = IntakeResult(
        goal=CareGoal.S3, order_condition=InterventionOrder.PRE_STRESS, minutes=10
    )
    state = {
        "request": CareRequest(text="시험 앞두고 오래 공부해서 머리가 멍해요"),
        "intake": intake,
    }
    planner = RecordingPlanner(choice("PR-01"))
    plan = run_plan(planner, state)
    assert planner.chosen == [(["PR-01", "PR-04"], [])]
    assert plan.activity.protocol_id == "PR-01"
    assert (plan.relaxed, plan.environment_gaps) == (["order"], [])


def test_pipeline_relaxes_the_environment_last_and_reports_the_gaps():
    nothing = Environment(audio=False, screen=False, quiet=False, outdoor=False)
    state = {**STATE, "intake": INTAKE.model_copy(update={"environment": nothing})}
    plan = run_plan(RecordingPlanner(choice("PR-15")), state)
    assert (plan.relaxed, plan.environment_gaps) == (["environment"], ["SCREEN"])


def test_demo_s2_session_offers_breathing_first():
    """발표 시나리오(TEAM.md): '발표 앞두고 긴장돼요' → S2 · PRE_STRESS · 기본 10분 → PR-06.

    플래너는 목록 맨 앞을 고른다. PR-03(스트레스 전 예방)은 이름이 상황과 맞아 보여도 S2 1차 지표
    등급이 —라 뒤에 온다. JSON 등급이 바뀌어 이 테스트가 깨지면 발표 경로가 바뀐 것이다.
    """
    found = protocols.candidates(CareGoal.S2, order=InterventionOrder.PRE_STRESS, minutes=10)
    ids = [p.protocol_id for p in found]
    assert ids[0] == "PR-06"
    assert ids.index("PR-03") > ids.index("PR-06")
