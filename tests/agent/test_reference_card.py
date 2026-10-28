"""Reference 카드 — fixture 근거(tests/fixtures/evidence_hits.json), 실제 검증 규칙(judge)의 판정,
data/protocols로 카드를 만들어 배지 · 한계 문장 · 근거 묶음 · 목표 근거를 본다."""

import logging

from app import intake_cues, protocols
from app.agent import reference_card
from app.agent.checkpoint import serializer
from app.agent.state import GoalEvidence, IntakeResult, PlannedActivity
from app.agent.validation import judge
from app.schemas.card import CardBadge, ReferenceCard
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Medium, Outcome, Section
from app.schemas.intake import GoalSource
from app.schemas.measure import InstrumentId
from app.schemas.retrieval import Flag
from tests.agent import fakes


def cited(ids, *, goal, media, order):
    hits = fakes.load_hits()
    by_id = {h.evidence_record_id: h for h in hits}
    return [
        (by_id[i], judge(by_id[i], goal=goal, media=media, orders=[order], candidates=hits))
        for i in ids
    ]


def card(protocol_id, intake, evidence=(), *, delivered=None, relaxed=(), minutes=10):
    protocol = protocols.get_protocol(protocol_id)
    return reference_card.build_card(
        intake,
        protocol,
        PlannedActivity(protocol_id=protocol_id, minutes=minutes),
        list(evidence),
        delivered=list(protocol.media) if delivered is None else delivered,
        relaxed=relaxed,
    )


S3 = IntakeResult(
    goal=CareGoal.S3,
    goal_source=GoalSource.INFERRED,
    order_condition=InterventionOrder.POST_FATIGUE,
)


def test_s3_card_groups_chunks_and_states_what_lumi_measures():
    evidence = cited(
        [122, 121, 142, 141],
        goal=CareGoal.S3,
        media=[Medium.STATIC_IMAGE],
        order=InterventionOrder.POST_FATIGUE,
    )
    got = card("PR-01", S3, evidence, delivered=[Medium.STATIC_IMAGE], minutes=6)

    assert (got.protocol_id, got.name, got.minutes) == ("PR-01", "자연 사진 감상", 6)
    # 같은 실험의 METHODS · RESULTS chunk는 하나로, 원문 구간은 섹션 순서로
    assert [e.experiment_id for e in got.evidence] == ["P03-E1", "N19-E1"]
    first = got.evidence[0]
    assert [x.section for x in first.excerpts] == [Section.METHODS, Section.RESULTS]
    assert first.evidence_record_ids == [121, 122]
    assert first.supports_goal and first.supported_outcomes == [Outcome.ATTENTION]
    assert first.paper.title and first.paper.url  # 논문 출처(검색 결과의 papers 행)
    assert {r.outcome for r in first.results} == {Outcome.ATTENTION}
    assert len(first.checks) == 6
    # 재현 실패(N19) — 지지하지 않는 참고 근거
    second = got.evidence[1]
    assert not second.supports_goal and Flag.NOT_SUPPORTING in second.flags
    assert reference_card.FLAG_LIMITS[Flag.NOT_SUPPORTING] in second.limits

    # S3는 근거가 주의 과제를 봤지만 LUMI는 정신 피로(임시 도구)를 잰다
    assert got.goal.evidence_outcomes == [Outcome.ATTENTION]
    assert got.goal.measured_by.instrument_id is InstrumentId.MFVAS
    assert got.goal.measured_by.provisional is True
    assert reference_card.MEASURE_GAPS[CareGoal.S3] in got.goal.notes
    assert CardBadge.PROVISIONAL_MEASURE in got.badges
    assert got.grades == {
        Outcome.ATTENTION: protocols.get_protocol("PR-01").grades[Outcome.ATTENTION].grade
    }
    assert got.limits[0] == reference_card.GENERAL_LIMIT


def test_a_review_is_one_entry_and_a_matching_experiment_keeps_the_card_unadapted():
    # P01은 사진 · 영상 · 산책을 모은 메타분석(실험 ID 없음)이라 제공 사진과 자극이 다르다
    evidence = cited(
        [161, 162, 121, 122],
        goal=CareGoal.S3,
        media=[Medium.STATIC_IMAGE],
        order=InterventionOrder.POST_FATIGUE,
    )
    got = card("PR-01", S3, evidence, delivered=[Medium.STATIC_IMAGE])

    assert [(e.paper.paper_id, e.evidence_record_ids) for e in got.evidence] == [
        ("P01", [161, 162]),
        ("P03", [121, 122]),
    ]
    review = got.evidence[0]
    assert Flag.ADAPTED_STIMULUS in review.flags
    assert reference_card.FLAG_LIMITS[Flag.ADAPTED_STIMULUS] in review.limits
    # 사진 실험(P03-E1)이 제공 콘텐츠와 같은 자극이라 카드 배지는 달지 않는다
    assert CardBadge.ADAPTED_STIMULUS not in got.badges


def test_default_goal_without_evidence_uses_the_protocol_note_for_the_adapted_script():
    intake = IntakeResult(goal=CareGoal.S2, goal_source=GoalSource.DEFAULT)
    got = card("PR-06", intake)

    assert got.badges == [
        CardBadge.NO_VERIFIED_EVIDENCE,
        CardBadge.ADAPTED_STIMULUS,  # when_adapted_script — 한국어 각색 안내
        CardBadge.DEFAULT_GOAL,
        CardBadge.PROVISIONAL_MEASURE,
    ]
    assert got.evidence == [] and got.goal.definitions == []
    assert got.goal.notes[0] == "상황 단서가 부족해 기본 목표(긴장·스트레스 완화)로 시작했습니다."
    note = protocols.get_protocol("PR-06").content_spec.model_dump()["reference_card_note"]
    assert got.limits == [
        reference_card.GENERAL_LIMIT,
        reference_card.BADGE_LIMITS[CardBadge.NO_VERIFIED_EVIDENCE],
        note,  # 작성자가 카드용으로 적은 메모가 고정 문구 대신 들어간다
        protocols.get_protocol("PR-06").population_note,
    ]


def test_inferred_goal_definitions_carry_concept_page_and_paper():
    goals = (CareGoal.S1, CareGoal.S2)
    evidence = [
        GoalEvidence(paper_id=d.paper_id, sentence=d.sentence)
        for goal in goals
        for d in intake_cues.definitions_for(goal)
    ]
    intake = IntakeResult(
        goal=CareGoal.S1,
        secondary_goal=CareGoal.S2,
        goal_source=GoalSource.INFERRED,
        goal_reason="발표가 끝났는데 같은 장면이 계속 떠오른다고 했다",
        goal_evidence=evidence,
    )
    got = card("PR-09", intake).goal

    assert (got.goal, got.label, got.secondary_goal) == (CareGoal.S1, "반추 완화", CareGoal.S2)
    assert got.reason == intake.goal_reason
    assert [d.goal for d in got.definitions] == [
        goal for goal in goals for _ in intake_cues.definitions_for(goal)
    ]
    first = intake_cues.definitions_for(CareGoal.S1)[0]
    assert (got.definitions[0].concept, got.definitions[0].page) == (first.concept, first.page)
    assert got.definitions[0].paper.paper_id == first.paper_id
    assert all(d.paper and d.paper.title for d in got.definitions)  # manifest의 출처


def test_badges_follow_protocol_flags_media_and_relaxed_conditions():
    s1 = IntakeResult(goal=CareGoal.S1, goal_source=GoalSource.INFERRED)
    s2 = IntakeResult(goal=CareGoal.S2, goal_source=GoalSource.INFERRED)
    # PR-01은 S1에서만 실험적(by_goal)
    assert CardBadge.EXPERIMENTAL in card("PR-01", s1).badges
    assert CardBadge.EXPERIMENTAL not in card("PR-01", s2).badges
    # AI 생성 이미지면 슬롯 생성 설정의 메모가 그 배지의 문장이다
    ai = card("PR-01", s2, delivered=[Medium.AI_IMAGE])
    assert CardBadge.AI_IMAGE_UNVERIFIED in ai.badges
    assert CardBadge.ADAPTED_STIMULUS not in ai.badges  # 생성 이미지는 AI 배지가 밝힌다
    assert any(limit.startswith("AI 생성 이미지와 연구 사진 자극") for limit in ai.limits)
    # 후보가 모자라 푼 조건
    relaxed = card("PR-01", s2, relaxed=["order", "environment"])
    assert {CardBadge.RELAXED_ORDER, CardBadge.RELAXED_ENVIRONMENT} <= set(relaxed.badges)
    assert reference_card.BADGE_LIMITS[CardBadge.RELAXED_ORDER] in relaxed.limits
    # 조합 근거가 없는 구성
    assert card("PR-22", s2).badges[0] is CardBadge.EXPERIMENTAL


def test_a_full_card_survives_the_checkpoint(caplog):
    evidence = cited(
        [101, 102, 111, 112],
        goal=CareGoal.S1,
        media=[Medium.AI_IMAGE, Medium.MINDFULNESS],
        order=InterventionOrder.POST_STRESS,
    )
    got = card("PR-21", IntakeResult(goal=CareGoal.S1), evidence, delivered=[Medium.AI_IMAGE])
    assert got.evidence and got.evidence[0].checks

    with caplog.at_level(logging.WARNING):
        restored = serializer().loads_typed(serializer().dumps_typed(got))
    assert not [r for r in caplog.records if "Blocked deserialization" in r.getMessage()]
    assert isinstance(restored, ReferenceCard) and restored == got
