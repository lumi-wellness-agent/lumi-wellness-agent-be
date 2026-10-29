"""plan 노드의 고정 절차 — 후보 → 선택(대역) → 검색 → 검증 → 설명(대역) → CarePlan.

검색·검증은 fixture 기반 가짜(tests/agent/fakes.py)로 바꿔 끼우고, Planner 대역이 받은 입력을
남긴다.
"""

import asyncio

import pytest
from langgraph.runtime import Runtime

from app import protocols
from app.agent.context import AgentContext
from app.agent.nodes.planner import (
    REPEAT_REASON,
    ActivityChoice,
    PlanRationale,
    PlanRejected,
    make_plan_node,
    planner_context,
    search_candidates,
)
from app.agent.state import (
    CarePlan,
    IntakeResult,
    NextStep,
    OutcomeEvaluation,
    Round,
    Selection,
    SelectionMethod,
    SessionReport,
)
from app.schemas.card import CardBadge
from app.schemas.care import CareGoal, CareRequest, ContentType, Environment, UserProfile
from app.schemas.content import ArtSubject, ContentFactors, MusicTrack, Scenery, Soundscape
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

    def __init__(
        self,
        *choices: ActivityChoice,
        rationale: str = "추천 이유",
        factors: ContentFactors | None = None,
    ):
        self.choices = list(choices)
        self.rationale = rationale
        self.factors = factors or ContentFactors.none()
        self.chosen: list[tuple[list[str], list[str]]] = []  # (후보 ID, 거부 사유)
        self.explained: list[tuple[str, str, list[int]]] = []  # (프로토콜, 이유, 인용 근거)

    async def choose(self, state, candidates, problems):
        self.chosen.append(([c["protocol_id"] for c in candidates], list(problems)))
        return self.choices.pop(0)

    async def explain(self, state, choice, protocol, evidence):
        self.explained.append(
            (protocol.protocol_id, choice.reason, [c.evidence_record_id for c in evidence])
        )
        return PlanRationale(rationale=self.rationale, content_factors=self.factors)


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


def run_plan(planner, state=STATE, session_id: str | None = None):
    node = make_plan_node(planner)
    context = AgentContext(session_id=session_id)
    return asyncio.run(node(state, Runtime(context=context)))["plan"]


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


def test_pipeline_keeps_only_the_content_factors_the_activity_can_use():
    # PR-21은 이미지 1장 — 풍경은 하나만, 자연음 · 작품 칸은 없다
    factors = ContentFactors(
        scenery=[Scenery.SEA, Scenery.FOREST, Scenery.SEA],
        soundscape=Soundscape.RAIN,
        art_subject=ArtSubject.LANDSCAPE,
        music=MusicTrack.MOZART_K448,
        reason="바다를 보고 싶다고 했다",
    )
    plan = run_plan(RecordingPlanner(choice("PR-21"), factors=factors))
    assert plan.content_factors == ContentFactors(
        scenery=[Scenery.SEA],
        soundscape=None,
        art_subject=None,
        music=None,
        reason="바다를 보고 싶다고 했다",
    )


def test_pipeline_leaves_content_factors_empty_when_the_llm_picks_none():
    plan = run_plan(RecordingPlanner(choice("PR-21")))
    assert plan.content_factors is None


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


S3_STATE = {
    "request": CareRequest(text="하루 종일 집중했더니 머리가 멍해요"),
    "intake": IntakeResult(
        goal=CareGoal.S3, order_condition=InterventionOrder.POST_FATIGUE, minutes=10
    ),
}


def add_other_activitys_hit(monkeypatch) -> int:
    """S3 필터(STATIC_IMAGE · ATTENTION · POST_FATIGUE)에 맞는 PR-03의 근거(P28-E1) chunk를 점수
    최고로 더한다. 매체 필터만으로 찾으면 PR-01 세션에 이것이 맨 앞에 나온다."""
    hits = fakes.load_hits()
    base = next(h for h in hits if h.evidence_record_id == 122)  # P03-E1 RESULTS
    other = base.model_copy(
        update={
            "evidence_record_id": 901,
            "paper": base.paper.model_copy(update={"paper_id": "P28"}),
            "experiment_id": "P28-E1",
            "score": 0.05,
        }
    )
    monkeypatch.setattr(fakes, "load_hits", lambda: (*hits, other))
    return other.evidence_record_id


def test_pipeline_searches_the_activitys_own_evidence_first(fake_backends, monkeypatch):
    other = add_other_activitys_hit(monkeypatch)
    wide = asyncio.run(
        fakes.fake_search(
            None,
            query="-",
            medium=Medium.STATIC_IMAGE,
            intervention_order=InterventionOrder.POST_FATIGUE,
            outcomes=[Outcome.ATTENTION],
        )
    )
    assert wide[0].evidence_record_id == other

    plan = run_plan(RecordingPlanner(choice("PR-01", "nature images attention")), S3_STATE)

    # 한 번에 끝난다 — PR-01의 evidence[] 안에서 찾았다
    pr01 = protocols.get_protocol("PR-01")
    assert [list(s["evidence_keys"]) for s in fake_backends] == [[r.key for r in pr01.evidence]]
    assert plan.activity.evidence_record_ids and other not in plan.activity.evidence_record_ids
    own_papers = {r.paper_id for r in pr01.evidence}
    assert {e.paper.paper_id for e in plan.reference_card.evidence} <= own_papers


def test_pipeline_widens_to_the_same_medium_when_its_evidence_does_not_fit(fake_backends):
    # PR-03(스트레스 전 자연 장면)의 근거 P28-E1은 생리 지표만 쟀다. S2 1차 지표로는 0건이라 같은
    # 필터로 전체에서 찾고, 같은 매체 메타분석 N24를 인용한다
    state = {
        "request": CareRequest(text="발표 앞두고 긴장돼요"),
        "intake": IntakeResult(
            goal=CareGoal.S2, order_condition=InterventionOrder.PRE_STRESS, minutes=10
        ),
    }
    plan = run_plan(RecordingPlanner(choice("PR-03", "nature images stress")), state)
    assert [list(s["evidence_keys"]) for s in fake_backends] == [["P28-E1"], []]
    assert plan.activity.evidence_record_ids == [152, 151]


def test_pipeline_relaxes_time_last_when_nothing_fits_the_minutes():
    # S1 · 2분 — 2분 안의 S1 활동이 없다. 전에는 실패했고, 이제 시간을 마지막에 풀어 가장 짧은
    # 활동(PR-09 3분 마음챙김)을 근거 용량 그대로 준다
    state = {**STATE, "intake": INTAKE.model_copy(update={"minutes": 2})}
    planner = RecordingPlanner(choice("PR-09"))
    plan = run_plan(planner, state)
    assert planner.chosen[0][0][0] == "PR-09"  # 가장 짧은 활동이 맨 앞
    assert plan.relaxed == ["minutes"]
    assert plan.activity.minutes == round(protocols.get_protocol("PR-09").duration_minutes.default)


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


def finished(plan: CarePlan, next_step: NextStep) -> Round:
    """끝난 라운드 — 사용자가 next_step을 골랐다. 측정값은 비워 둔다."""
    return Round(
        plan=plan,
        report=SessionReport(completed=True),
        pre=[],
        post=[],
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S1, round=1, deltas={}, improved=True, completed=True
        ),
        next_step=next_step,
    )


def test_one_more_round_repeats_the_activity_without_choosing_again(fake_backends):
    first = run_plan(RecordingPlanner(choice("PR-21")))
    assert first.search_query == "nature images rumination"
    fake_backends.clear()

    planner = RecordingPlanner()  # choose를 부르면 꺼낼 선택이 없어 IndexError
    rounds = [finished(first, NextStep.ADDITIONAL_SESSION)]
    again = run_plan(planner, {**STATE, "rounds": rounds})

    assert planner.chosen == []
    assert again.selection == Selection(method=SelectionMethod.REPEAT, order=["PR-21"])
    assert again.activity.protocol_id == "PR-21"
    assert again.activity.minutes == first.activity.minutes
    # 같은 질의로 근거를 다시 찾고, 설명(콘텐츠 Factor)만 새로 쓴다
    assert fake_backends and all(s["query"] == first.search_query for s in fake_backends)
    assert again.activity.evidence_record_ids == first.activity.evidence_record_ids
    assert planner.explained == [("PR-21", REPEAT_REASON, [102, 101, 112, 111])]


def test_one_more_round_without_a_saved_query_searches_by_the_activity_name(fake_backends):
    # 질의를 남기기 전의 체크포인트 — 그 활동의 evidence[]에서 이름으로 찾는다
    first = run_plan(RecordingPlanner(choice("PR-21"))).model_copy(update={"search_query": None})
    fake_backends.clear()
    rounds = [finished(first, NextStep.ADDITIONAL_SESSION)]
    again = run_plan(RecordingPlanner(), {**STATE, "rounds": rounds})
    name = protocols.get_protocol("PR-21").name
    assert again.search_query == name
    assert fake_backends and all(s["query"] == name for s in fake_backends)


def test_a_session_shows_the_candidates_in_its_weighted_order(fake_backends):
    session = planner_context(AgentContext(session_id="session-1"), STATE)
    expected = protocols.recommendation_order(
        search_candidates(session), CareGoal.S1, seed="session-1"
    )
    order = [p.protocol_id for p in expected.protocols]
    assert order != ["PR-09", "PR-15", "PR-21", "PR-14"]  # 등급 순과 다르다(이 시드에서)
    planner = RecordingPlanner(choice(order[1]))  # 맨 앞을 건너뛰었다

    plan = run_plan(planner, session_id="session-1")

    assert planner.chosen == [(order, [])]
    assert plan.selection == Selection(
        method=SelectionMethod.WEIGHTED,
        order=order,
        weights=expected.weights,
        probability=expected.probability(order[1]),
        position=1,
    )


def test_without_a_session_the_candidates_stay_ranked(fake_backends):
    plan = run_plan(RecordingPlanner(choice("PR-21")))
    assert plan.selection == Selection(
        method=SelectionMethod.RANKED, order=["PR-09", "PR-15", "PR-21", "PR-14"], position=2
    )


def test_demo_s2_session_offers_breathing_first():
    """발표 시나리오(TEAM.md): '발표 앞두고 긴장돼요' → S2 · PRE_STRESS · 기본 10분 → PR-06.

    플래너는 목록 맨 앞을 고른다. PR-03(스트레스 전 예방)은 이름이 상황과 맞아 보여도 S2 1차 지표
    등급이 —라 뒤에 온다. JSON 등급이 바뀌어 이 테스트가 깨지면 발표 경로가 바뀐 것이다.
    """
    found = protocols.candidates(CareGoal.S2, order=InterventionOrder.PRE_STRESS, minutes=10)
    ids = [p.protocol_id for p in found]
    assert ids[0] == "PR-06"
    assert ids.index("PR-03") > ids.index("PR-06")


def test_short_time_without_a_fitting_activity_keeps_the_evidence_dose():
    # S3에서 3분 칩 — 3분 안의 S3 활동이 없어 시간을 풀고, 활동은 줄이지 않고 근거 용량(6분)으로
    state = {
        "request": CareRequest(text="하루 종일 집중했더니 머리가 멍해요"),
        "intake": IntakeResult(
            goal=CareGoal.S3, order_condition=InterventionOrder.POST_FATIGUE, minutes=3
        ),
    }
    plan = run_plan(RecordingPlanner(choice("PR-01")), state)
    assert plan.relaxed == ["minutes"]
    assert plan.activity.protocol_id == "PR-01"
    assert plan.activity.minutes == round(protocols.get_protocol("PR-01").duration_minutes.default)
