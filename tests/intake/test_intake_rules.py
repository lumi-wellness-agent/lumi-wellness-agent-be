"""intake 규칙 — 질문 검증 · 답 병합 · 무엇을 물을지 · 목표 정하기 · 끝.

LLM · LangGraph 없이 본다.
"""

import pytest
from pydantic import ValidationError

from app import constructs, protocols
from app.agent import intake_rules as rules
from app.agent.intake_rules import DefinitionHit, GoalInference, StateReading
from app.agent.state import IntakeResult
from app.schemas.care import CareGoal, CareRequest, Environment, Location, Place, environment_for
from app.schemas.evidence import InterventionOrder
from app.schemas.intake import (
    UNKNOWN_LABEL,
    GoalSource,
    IntakeAnswer,
    IntakeTurn,
    QuestionSlot,
    StateOptionDraft,
    StateQuestionDraft,
    parse_set,
)

ASK_TIMING = StateQuestionDraft(
    followup=False,
    text="방금 있었던 일 때문인가요, 곧 있을 일 때문인가요?",
    options=[
        StateOptionDraft(label="방금 있었던 일", order_condition=InterventionOrder.POST_STRESS),
        StateOptionDraft(label="곧 있을 일", order_condition=InterventionOrder.PRE_STRESS),
    ],
)


def reading(**update) -> StateReading:
    base = {
        "order_condition": None,
        "event": None,
        "rumination_cue": False,
        "stress_fatigue_ambiguous": False,
        "cues": [],
        "constraints": [],
        "location": None,
        "earphones": None,
        "minutes": None,
        "search_query": "state anxiety",
        "summary": "-",
        "question": None,
    }
    return StateReading(**(base | update))


def turn(slot: QuestionSlot, n: int, *, answer: dict | None = None, text: str | None = None):
    return IntakeTurn(question_id=f"q{n}", slot=slot, text="-", answer=answer, free_text=text)


# ── 계약 ──────────────────────────────────────────────────────────────────────────────────


def test_parse_set_allows_only_the_slot_keys_and_values():
    assert parse_set(QuestionSlot.STATE, {"order_condition": "POST_STRESS"}) == {
        "order_condition": InterventionOrder.POST_STRESS
    }
    with pytest.raises(ValueError, match="goal_hint"):  # 목표 단서는 재질문에서만
        parse_set(QuestionSlot.STATE, {"goal_hint": ["S1"]})
    assert parse_set(QuestionSlot.STATE_FOLLOWUP, {"goal_hint": ["S2", "S3"]})["goal_hint"] == [
        CareGoal.S2,
        CareGoal.S3,
    ]
    with pytest.raises(ValueError):
        parse_set(QuestionSlot.STATE_FOLLOWUP, {"goal_hint": ["S1", "S2", "S3"]})
    with pytest.raises(ValueError):
        parse_set(QuestionSlot.CONTEXT, {"minutes": 15})  # 5 · 10 · 20만
    with pytest.raises(ValueError):
        parse_set(QuestionSlot.CONTEXT, {"earphones": "yes"})
    with pytest.raises(ValueError):
        parse_set(QuestionSlot.CONTEXT, {"location": "PARK"})
    assert parse_set(QuestionSlot.CONTEXT, {}) == {}  # 잘 모르겠어요 · 건너뜀


def test_answer_carries_either_chips_or_text():
    assert IntakeAnswer(question_id="q1", answer={}).answer == {}
    assert IntakeAnswer(question_id="q1", text="그냥 계속 그래요").text
    with pytest.raises(ValidationError):
        IntakeAnswer(question_id="q1")
    with pytest.raises(ValidationError):
        IntakeAnswer(question_id="q1", answer={}, text="둘 다")


# ── ① 합치기 · State 질문 ────────────────────────────────────────────────────────────────


def test_compose_layers_text_then_chip_answers_then_request_chips():
    request = CareRequest(text="…", minutes_available=20)
    text = reading(location=Location.HOME, minutes=5, order_condition=InterventionOrder.POST_STRESS)
    answers = [turn(QuestionSlot.CONTEXT, 1, answer={"location": "OFFICE_CAFE"})]
    previous = IntakeResult(goal=CareGoal.S1, goal_source=GoalSource.INFERRED)
    out = rules.compose(request, text, answers, previous=previous)
    assert out.location is Location.OFFICE_CAFE  # 칩 답 > 문장
    assert out.minutes == 20  # 요청 칩 > 문장
    assert out.order_condition is InterventionOrder.POST_STRESS
    assert out.goal is CareGoal.S1  # 목표 칸은 그대로
    # 옛 place 칩은 location이 없을 때만 OUTDOOR를 옮긴다
    assert rules.compose(CareRequest(text="…", place=Place.OUTDOOR), reading(), []).location is (
        Location.OUTDOOR
    )
    assert rules.compose(CareRequest(text="…"), reading(minutes=0), []).minutes is None


def test_state_question_only_when_state_is_unknown_and_within_budget():
    q = rules.state_question(reading(question=ASK_TIMING), [])
    assert q.slot is QuestionSlot.STATE and q.question_id == "q1" and q.turn == 1
    assert q.allow_free_text and q.groups[0].options[-1].label == UNKNOWN_LABEL
    assert q.groups[0].options[-1].set == {}
    # 상태를 정했으면 묻지 않는다
    known = reading(order_condition=InterventionOrder.POST_STRESS, question=ASK_TIMING)
    assert rules.state_question(known, []) is None
    # 칩으로 답했으면 같은 질문을 다시 하지 않는다
    chipped = [turn(QuestionSlot.STATE, 1, answer={})]
    assert rules.state_question(reading(question=ASK_TIMING), chipped) is None


def test_followup_only_after_free_text_when_stress_and_fatigue_split():
    asked = [turn(QuestionSlot.STATE, 1, text="그냥 계속 그래요")]
    unclear = reading(question=ASK_TIMING, stress_fatigue_ambiguous=True)
    q = rules.state_question(unclear, asked)
    assert q.slot is QuestionSlot.STATE_FOLLOWUP and q.question_id == "q2"
    assert (
        rules.state_question(reading(question=ASK_TIMING), asked) is None
    )  # 갈리지 않으면 안 묻는다
    # State는 2회까지
    twice = [*asked, turn(QuestionSlot.STATE_FOLLOWUP, 2, text="몰라요")]
    assert rules.state_question(unclear, twice) is None


def test_invalid_draft_falls_back_to_fixed_options_and_strips_goal_hint():
    one = StateQuestionDraft(
        followup=False,
        text="?",
        options=[StateOptionDraft(label="방금", order_condition=InterventionOrder.POST_STRESS)],
    )
    q = rules.state_question(reading(question=one), [])
    assert [o.label for o in q.groups[0].options][:4] == [
        label for label, _ in rules.STATE_FALLBACK
    ]
    hinted = StateQuestionDraft(
        followup=False,
        text="?",
        options=[
            StateOptionDraft(
                label="a", order_condition=InterventionOrder.POST_STRESS, goal_hint=[CareGoal.S2]
            ),
            StateOptionDraft(
                label="b", order_condition=InterventionOrder.POST_FATIGUE, goal_hint=[CareGoal.S3]
            ),
        ],
    )
    q = rules.state_question(reading(question=hinted), [])
    assert all("goal_hint" not in o.set for o in q.groups[0].options)  # 시점 질문은 목표 단서 없음


# ── ④ 환경 질문 ─────────────────────────────────────────────────────────────────────────────


def test_environment_filters_candidates_with_eligibility():
    office = environment_for(Location.OFFICE_CAFE, False)
    s1 = [p.protocol_id for p in protocols.candidates(CareGoal.S1, environment=office)]
    assert "PR-09" not in s1 and s1[0] == "PR-15"  # 안내 음성 마음챙김은 소리·조용한 곳 필요
    with_earphones = environment_for(Location.OFFICE_CAFE, True)
    assert protocols.candidates(CareGoal.S1, environment=with_earphones)[0].protocol_id == "PR-09"
    indoor = Environment(outdoor=False)
    s3 = [p.protocol_id for p in protocols.candidates(CareGoal.S3, minutes=30, environment=indoor)]
    # 야외 보행(PR-11)은 빠지고, 장소 제한이 없는 자연환경 알아차리기(PR-10)가 1순위가 된다
    assert "PR-11" not in s3 and s3[0] == "PR-10"
    # 모르는 조건(None)으로는 거르지 않는다
    assert protocols.candidates(CareGoal.S1, environment=Environment()) == protocols.candidates(
        CareGoal.S1
    )


def test_context_asks_only_slots_that_change_the_top_activity():
    s2_home = IntakeResult(goal=CareGoal.S2, location=Location.HOME)
    assert rules.context_groups(s2_home) == []  # 시간이 달라도 1순위 PR-06
    s1 = IntakeResult(goal=CareGoal.S1)
    assert rules.context_groups(s1) == ["location", "earphones"]  # 시간은 1순위를 안 바꾼다
    s1_office = IntakeResult(goal=CareGoal.S1, location=Location.OFFICE_CAFE)
    assert rules.context_groups(s1_office) == ["earphones"]
    s3 = IntakeResult(goal=CareGoal.S3, order_condition=InterventionOrder.POST_FATIGUE)
    # S3는 장소(야외 보행) · 이어폰(PR-10 안내 음성) · 시간(20분 미만이면 자연 사진)이
    # 모두 1순위를 바꾼다
    assert rules.context_groups(s3) == ["location", "earphones", "minutes"]
    s3_home = IntakeResult(goal=CareGoal.S3, location=Location.HOME)
    assert rules.context_groups(s3_home) == ["minutes"]  # 10분 PR-01 · 20분 PR-10

    q = rules.context_question(["location", "earphones", "minutes"], 2)
    assert q.slot is QuestionSlot.CONTEXT and q.question_id == "q2" and not q.allow_free_text
    assert [(g.key, g.select.value) for g in q.groups] == [
        ("location", "single"),
        ("earphones", "toggle"),
        ("minutes", "single"),
    ]
    assert q.groups[2].options[-1].label == "20분 이상"
    assert rules.context_question([], 2) is None


def test_apply_answer_validates_and_records_json_values():
    q = rules.context_question(["location", "minutes"], 3)
    values, record = rules.apply_answer(
        q, IntakeAnswer(question_id="q3", answer={"location": "TRANSIT", "minutes": 5})
    )
    assert values == {"location": Location.TRANSIT, "minutes": 5}
    assert record.answer == {"location": "TRANSIT", "minutes": 5} and record.free_text is None
    with pytest.raises(ValueError, match="q3의 답이 아니다"):
        rules.apply_answer(q, IntakeAnswer(question_id="q1", answer={}))
    with pytest.raises(ValueError, match="직접 입력"):
        rules.apply_answer(q, IntakeAnswer(question_id="q3", text="카페"))
    with pytest.raises(ValueError):
        rules.apply_answer(
            q, IntakeAnswer(question_id="q3", answer={"order_condition": "PRE_STRESS"})
        )

    state_q = rules.state_question(reading(question=ASK_TIMING), [])
    values, record = rules.apply_answer(state_q, IntakeAnswer(question_id="q1", text="그냥 그래요"))
    assert values == {} and record.free_text == "그냥 그래요" and record.answer is None


# ── ③ 목표 · 끝 ──────────────────────────────────────────────────────────────────────────


HITS = [
    DefinitionHit(11, "P12", CareGoal.S1, "Stenzel", 2025, "…"),
    DefinitionHit(12, "N07", CareGoal.S2, "Marteau", 1992, "…"),
]


def inference(**update) -> GoalInference:
    base = {
        "has_cues": True,
        "goal": CareGoal.S1,
        "secondary_goal": CareGoal.S2,
        "evidence_ids": [11, 99, 11],
        "reason": "계속 생각나요 — 반복적 부정 사고",
    }
    return GoalInference(**(base | update))


def test_goal_comes_from_inference_with_only_retrieved_evidence():
    out = rules.apply_goal(IntakeResult(), inference(), HITS, CareRequest(text="…"))
    assert (out.goal, out.secondary_goal, out.goal_source) == (
        CareGoal.S1,
        CareGoal.S2,
        GoalSource.INFERRED,
    )
    assert out.goal_evidence_ids == [11]  # 검색 결과에 없던 99는 버린다
    same = rules.apply_goal(
        IntakeResult(), inference(secondary_goal=CareGoal.S1), HITS, CareRequest(text="…")
    )
    assert same.secondary_goal is None


def test_goal_falls_back_to_hint_then_default_and_chip_wins():
    hinted = IntakeResult(goal_hint=[CareGoal.S3, CareGoal.S2])
    no_cues = inference(has_cues=False)
    out = rules.apply_goal(hinted, no_cues, [], CareRequest(text="…"))
    assert (out.goal, out.secondary_goal, out.goal_source) == (
        CareGoal.S3,
        CareGoal.S2,
        GoalSource.INFERRED,
    )
    out = rules.apply_goal(IntakeResult(), no_cues, [], CareRequest(text="…"))
    assert out.goal is CareGoal.S2 and out.goal_source is GoalSource.DEFAULT
    out = rules.apply_goal(
        IntakeResult(), inference(), HITS, CareRequest(text="…", goal=CareGoal.S3)
    )
    assert out.goal is CareGoal.S3 and out.goal_source is GoalSource.CHIP


def test_finish_fills_defaults_and_environment():
    out = rules.finish(IntakeResult(goal=CareGoal.S2))
    assert out.minutes == rules.DEFAULT_MINUTES and out.place is Place.INDOOR
    assert out.environment == Environment(audio=True, quiet=True, screen=True, outdoor=False)
    assert out.missing_slots == ["minutes", "location"]
    out = rules.finish(IntakeResult(goal=CareGoal.S3, location=Location.OUTDOOR, minutes=30))
    assert (
        out.place is Place.OUTDOOR and out.environment.outdoor is True and out.missing_slots == []
    )
    out = rules.finish(IntakeResult(location=Location.TRANSIT, earphones=True, minutes=5))
    assert out.environment == Environment(audio=True, quiet=False, screen=True, outdoor=False)
    assert out.goal is CareGoal.S2 and out.goal_source is GoalSource.DEFAULT


# ── 개념 카드 ─────────────────────────────────────────────────────────────────────────────


def test_construct_cards_cover_every_goal_with_sources():
    cards = constructs.load_cards()
    assert set(cards.goals) == set(CareGoal)
    assert all(card.sources and card.cues for card in cards.goals.values())
    assert constructs.goal_for_paper("P12") is CareGoal.S1
    assert constructs.goal_for_paper("N07") is CareGoal.S2
    assert constructs.goal_for_paper("N27") is CareGoal.S3
    assert constructs.goal_for_paper("E01") is None
    assert len(constructs.all_sources()) == 9  # 목표 출처 8 + 경계 출처(P06) 1
    hint = constructs.state_hint(InterventionOrder.POST_FATIGUE)
    assert hint.lean == [CareGoal.S3] and hint.strength == "STRONG"
    assert constructs.state_hint(None) is None
    assert {h.order_condition for h in cards.state_hints} == set(InterventionOrder)
