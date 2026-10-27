"""intake 규칙 — 질문 검증 · 답 병합 · 입구 되묻기 · 무엇을 물을지 · 목표 정하기 · 끝.

LLM · LangGraph 없이 본다. 계약은 app/schemas/intake.py 모듈 docstring.
"""

import pytest
from pydantic import ValidationError

from app import intake_cues
from app.agent import intake_rules as rules
from app.agent.intake_rules import GoalInference, StateReading
from app.agent.state import IntakeResult
from app.schemas.care import (
    CareGoal,
    CareRequest,
    Environment,
    Place,
    Setting,
    environment_for,
    place_for,
)
from app.schemas.evidence import InterventionOrder, Outcome
from app.schemas.intake import (
    MAX_OPTIONS,
    MIN_OPTIONS,
    SKIP_LABEL,
    UNKNOWN_LABEL,
    GoalSource,
    IntakeAnswer,
    IntakeEndReason,
    IntakeQuestion,
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
        "safety": False,
        "relevance": "RELEVANT",
        "restate_text": None,
        "order_condition": None,
        "event": None,
        "rumination_cue": False,
        "stress_fatigue_ambiguous": False,
        "cues": [],
        "constraints": [],
        "setting": None,
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
        parse_set(QuestionSlot.CONTEXT, {"location": "HOME"})  # 키는 setting
    assert parse_set(QuestionSlot.CONTEXT, {"setting": "TRANSIT", "earphones": False}) == {
        "setting": Setting.TRANSIT,
        "earphones": False,  # 없어요(false)와 무응답(키 없음)이 구분된다
    }
    assert parse_set(QuestionSlot.CONTEXT, {}) == {}


def test_answer_carries_either_chips_or_text():
    assert IntakeAnswer(question_id="q1", answer={}).answer == {}
    assert IntakeAnswer(question_id="q1", text="그냥 계속 그래요").text
    with pytest.raises(ValidationError):
        IntakeAnswer(question_id="q1")
    with pytest.raises(ValidationError):
        IntakeAnswer(question_id="q1", answer={}, text="둘 다")


def test_setting_maps_to_place_and_environment():
    assert place_for(Setting.OUTDOOR) is Place.OUTDOOR
    assert place_for(Setting.OFFICE_CAFE) is Place.INDOOR and place_for(None) is None
    assert environment_for(Setting.OFFICE_CAFE, True) == Environment(
        audio=True, quiet=True, screen=True, outdoor=False
    )
    assert environment_for(Setting.TRANSIT, True).quiet is False
    assert environment_for(None, None) == Environment()


# ── ① 합치기 · State 질문 ────────────────────────────────────────────────────────────────


def test_compose_layers_text_then_chip_answers_then_request_chips():
    request = CareRequest(text="…", minutes_available=20)
    text = reading(setting=Setting.HOME, minutes=5, order_condition=InterventionOrder.POST_STRESS)
    answers = [turn(QuestionSlot.CONTEXT, 1, answer={"setting": "OFFICE_CAFE"})]
    previous = IntakeResult(goal=CareGoal.S1, goal_source=GoalSource.INFERRED)
    out = rules.compose(request, text, answers, previous=previous)
    assert out.setting is Setting.OFFICE_CAFE  # 칩 답 > 문장
    assert out.minutes == 20  # 요청 칩 > 문장
    assert out.order_condition is InterventionOrder.POST_STRESS
    assert out.goal is CareGoal.S1  # 목표 칸은 그대로
    # 옛 place 칩은 setting이 없을 때만 OUTDOOR를 옮긴다
    old = rules.compose(CareRequest(text="…", place=Place.OUTDOOR), reading(), [])
    assert old.setting is Setting.OUTDOOR
    assert rules.compose(CareRequest(text="…"), reading(minutes=0), []).minutes is None


def test_state_question_has_unknown_within_two_to_five_options():
    q = rules.state_question(reading(question=ASK_TIMING), [])
    assert q.slot is QuestionSlot.STATE and q.question_id == "q1" and q.turn == 1
    options = q.groups[0].options
    assert MIN_OPTIONS <= len(options) <= MAX_OPTIONS
    assert options[-1].label == UNKNOWN_LABEL and options[-1].set == {}
    assert q.allow_free_text and q.groups[0].select.value == "single"
    # 상태를 정했으면 묻지 않는다
    known = reading(order_condition=InterventionOrder.POST_STRESS, question=ASK_TIMING)
    assert rules.state_question(known, []) is None
    # LLM 보기가 많아도 4개까지 + 잘 모르겠어요 = 5개
    many = ASK_TIMING.model_copy(update={"options": ASK_TIMING.options * 3})
    assert len(rules.state_question(reading(question=many), []).groups[0].options) == MAX_OPTIONS


def test_invalid_draft_falls_back_to_fixed_question():
    one = StateQuestionDraft(
        followup=False,
        text="?",
        options=[StateOptionDraft(label="방금", order_condition=InterventionOrder.POST_STRESS)],
    )
    q = rules.state_question(reading(question=one), [])
    assert q.text == rules.STATE_TEXT
    assert [o.label for o in q.groups[0].options] == [
        *(label for label, _ in rules.STATE_FIXED),
        UNKNOWN_LABEL,
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


def test_followup_after_free_text_or_unknown_and_budget():
    asked_text = [turn(QuestionSlot.STATE, 1, text="그냥 계속 그래요")]
    unclear = reading(question=ASK_TIMING, stress_fatigue_ambiguous=True)
    q = rules.state_question(unclear, asked_text)
    assert q.slot is QuestionSlot.STATE_FOLLOWUP and q.question_id == "q2"
    assert (
        rules.state_question(reading(question=ASK_TIMING), asked_text) is None
    )  # 안 갈리면 안 묻는다
    assert rules.followup_after_unknown(asked_text) is None  # 글 답은 다시 읽는 쪽

    asked_unknown = [turn(QuestionSlot.STATE, 1, answer={})]
    fixed = rules.followup_after_unknown(asked_unknown)
    assert fixed.slot is QuestionSlot.STATE_FOLLOWUP and fixed.text == rules.FOLLOWUP_TEXT
    assert fixed.groups[0].options[-1].label == UNKNOWN_LABEL
    chipped = [turn(QuestionSlot.STATE, 1, answer={"order_condition": "POST_STRESS"})]
    assert rules.followup_after_unknown(chipped) is None
    # State는 2회까지
    twice = [*asked_unknown, turn(QuestionSlot.STATE_FOLLOWUP, 2, answer={})]
    assert rules.followup_after_unknown(twice) is None
    assert rules.state_question(unclear, twice) is None


def test_both_state_unknown():
    unknown = [
        turn(QuestionSlot.STATE, 1, answer={}),
        turn(QuestionSlot.STATE_FOLLOWUP, 2, answer={}),
    ]
    assert rules.both_state_unknown(unknown)
    assert not rules.both_state_unknown(unknown[:1])
    one_known = [unknown[0], turn(QuestionSlot.STATE_FOLLOWUP, 2, answer={"goal_hint": ["S3"]})]
    assert not rules.both_state_unknown(one_known)


def test_off_topic_first_input_gets_an_unbounded_restate_outside_the_budget():
    off = reading(relevance="OFF_TOPIC", restate_text="점심 고민 중이시군요! 오늘 하루는 어땠어요?")
    q = rules.restate_question(off, [])
    assert q.slot is QuestionSlot.RESTATE and q.question_id == "r1" and q.turn == 0
    assert q.groups == [] and q.allow_free_text and q.skip_label == SKIP_LABEL
    assert q.text.startswith("점심 고민")
    # 또 관계없으면 계속 되묻는다 — 몇 번이든. 문장이 비면 고정 문장
    again = [
        IntakeTurn(question_id=f"r{n}", slot=QuestionSlot.RESTATE, text="-", free_text="ㅋㅋ")
        for n in (1, 2, 3, 4)
    ]
    q5 = rules.restate_question(off.model_copy(update={"restate_text": " "}), again)
    assert q5.question_id == "r5" and q5.text == rules.RESTATE_FIXED
    assert rules.counted(again) == []  # 예산에 세지 않는다
    # 관련 있는 답이 오면 시점 질문이 q1부터
    state_q = rules.state_question(reading(question=ASK_TIMING), again)
    assert state_q.question_id == "q1" and state_q.turn == 1
    # 관련 있음 · 예산 질문을 시작한 뒤에는 되묻지 않는다
    assert rules.restate_question(reading(), []) is None
    assert rules.restate_question(off, [turn(QuestionSlot.STATE, 1, text="몰라요")]) is None


def test_restate_skip_starts_with_default_goal():
    q = rules.restate_question(reading(relevance="OFF_TOPIC"), [])
    values, recorded = rules.apply_answer(q, IntakeAnswer(question_id="r1", skip=True))
    assert values == {} and recorded.skipped and rules.entry_skipped([recorded])
    out = rules.apply_goal(
        IntakeResult(),
        rules.GoalInference(has_cues=True, goal=CareGoal.S1, secondary_goal=None, reason="-"),
        CareRequest(text="ㅋㅋ"),
        [recorded],
    )
    assert out.goal is CareGoal.S2 and out.goal_source is GoalSource.DEFAULT
    # 되묻기가 아닌 질문은 건너뛸 수 없고, 답은 하나만
    state_q = rules.fixed_question(QuestionSlot.STATE, 1)
    with pytest.raises(ValueError):
        rules.apply_answer(state_q, IntakeAnswer(question_id="q1", skip=True))
    with pytest.raises(ValidationError):
        IntakeAnswer(question_id="r1", skip=True, text="그냥")
    with pytest.raises(ValidationError):  # 되묻기는 보기 없이 turn 0
        IntakeQuestion(
            question_id="r1", slot=QuestionSlot.RESTATE, turn=1, text="?", allow_free_text=True
        )


def test_safety_is_read_by_the_llm_not_a_word_list():
    assert not hasattr(rules, "safety_hit") and not hasattr(rules, "SAFETY_PATTERNS")
    assert "safety" in StateReading.model_json_schema()["required"]


# ── ④ 환경 질문 ─────────────────────────────────────────────────────────────────────────────


def test_context_asks_only_slots_that_change_the_top_activity():
    s2_home = IntakeResult(goal=CareGoal.S2, setting=Setting.HOME)
    assert rules.context_groups(s2_home) == []  # 시간이 달라도 1순위 PR-06
    s1 = IntakeResult(goal=CareGoal.S1)
    assert rules.context_groups(s1) == ["setting", "earphones"]  # 시간은 1순위를 안 바꾼다
    s1_office = IntakeResult(goal=CareGoal.S1, setting=Setting.OFFICE_CAFE)
    assert rules.context_groups(s1_office) == ["earphones"]
    assert rules.top_activity(s1_office, earphones=False) == "PR-15"  # 소리 활동은 빠진다
    assert rules.top_activity(s1_office, earphones=True) == "PR-09"
    s3_home = IntakeResult(goal=CareGoal.S3, setting=Setting.HOME)
    assert rules.context_groups(s3_home) == ["minutes"]  # 10분 PR-01 · 20분 PR-10

    q = rules.context_question(["setting", "earphones", "minutes"], 2)
    assert q.slot is QuestionSlot.CONTEXT and q.question_id == "q2" and not q.allow_free_text
    assert [(g.key, g.select.value, len(g.options)) for g in q.groups] == [
        ("setting", "single", 4),
        ("earphones", "single", 2),
        ("minutes", "single", 3),
    ]
    assert [o.set for o in q.groups[1].options] == [{"earphones": True}, {"earphones": False}]
    assert all(o.label != UNKNOWN_LABEL for g in q.groups for o in g.options)
    assert q.groups[2].options[-1].label == "20분 이상"
    assert rules.context_question([], 2) is None


def test_apply_answer_validates_and_records_json_values():
    q = rules.context_question(["setting", "minutes"], 3)
    values, record = rules.apply_answer(
        q, IntakeAnswer(question_id="q3", answer={"setting": "TRANSIT", "minutes": 5})
    )
    assert values == {"setting": Setting.TRANSIT, "minutes": 5}
    assert record.answer == {"setting": "TRANSIT", "minutes": 5} and record.free_text is None
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


def inference(**update) -> GoalInference:
    base = {
        "has_cues": True,
        "goal": CareGoal.S1,
        "secondary_goal": CareGoal.S2,
        "reason": "계속 생각나요 — 반복 사고",
    }
    return GoalInference(**(base | update))


def test_goal_from_inference_with_target_outcomes():
    out = rules.apply_goal(IntakeResult(), inference(), CareRequest(text="…"))
    assert (out.goal, out.secondary_goal, out.goal_source) == (
        CareGoal.S1,
        CareGoal.S2,
        GoalSource.INFERRED,
    )
    assert out.target_outcomes == [Outcome.RUMINATION]
    assert out.goal_evidence == []  # 개념 정의 JSON(②) 전에는 빈다
    same = rules.apply_goal(
        IntakeResult(), inference(secondary_goal=CareGoal.S1), CareRequest(text="…")
    )
    assert same.secondary_goal is None


def test_goal_defaults_and_priorities():
    unknown = [
        turn(QuestionSlot.STATE, 1, answer={}),
        turn(QuestionSlot.STATE_FOLLOWUP, 2, answer={}),
    ]
    out = rules.apply_goal(IntakeResult(), inference(), CareRequest(text="…"), unknown)
    assert out.goal is CareGoal.S2 and out.goal_source is GoalSource.DEFAULT  # 두 질문 모두 모름
    assert out.target_outcomes == [Outcome.SUBJECTIVE_STRESS, Outcome.AFFECT]

    hinted = IntakeResult(goal_hint=[CareGoal.S3, CareGoal.S2])
    no_cues = inference(has_cues=False)
    out = rules.apply_goal(hinted, no_cues, CareRequest(text="…"))
    assert (out.goal, out.secondary_goal, out.goal_source) == (
        CareGoal.S3,
        CareGoal.S2,
        GoalSource.INFERRED,
    )
    out = rules.apply_goal(IntakeResult(), no_cues, CareRequest(text="…"))
    assert out.goal is CareGoal.S2 and out.goal_source is GoalSource.DEFAULT
    out = rules.apply_goal(IntakeResult(), inference(), CareRequest(text="…", goal=CareGoal.S3))
    assert out.goal is CareGoal.S3 and out.goal_source is GoalSource.CHIP


def test_finish_fills_defaults_environment_place_and_end_reason():
    out = rules.finish(IntakeResult(goal=CareGoal.S2))
    assert out.minutes == rules.DEFAULT_MINUTES and out.place is Place.INDOOR
    assert out.environment == Environment(audio=True, quiet=True, screen=True, outdoor=False)
    assert out.missing_slots == ["minutes", "setting"]
    assert out.end_reason is IntakeEndReason.SUFFICIENT
    out = rules.finish(IntakeResult(goal=CareGoal.S3, setting=Setting.OUTDOOR, minutes=30))
    assert (
        out.place is Place.OUTDOOR and out.environment.outdoor is True and out.missing_slots == []
    )
    out = rules.finish(IntakeResult(setting=Setting.TRANSIT, earphones=True, minutes=5))
    assert out.environment == Environment(audio=True, quiet=False, screen=True, outdoor=False)
    assert out.goal is CareGoal.S2 and out.goal_source is GoalSource.DEFAULT
    three = [turn(QuestionSlot.STATE, n, answer={}) for n in (1, 2, 3)]
    assert rules.finish(IntakeResult(), three).end_reason is IntakeEndReason.MAX_TURNS
    assert rules.end_for_safety(IntakeResult()).end_reason is IntakeEndReason.SAFETY


# ── 단서표 ────────────────────────────────────────────────────────────────────────────────


def test_cue_table_covers_every_goal_and_state():
    table = intake_cues.load_cues()
    assert set(table.goals) == set(CareGoal)
    assert all(g.cues and g.boundary and g.sources for g in table.goals.values())
    assert {h.order_condition for h in table.state_hints} == set(InterventionOrder)
    hint = intake_cues.state_hint(InterventionOrder.POST_FATIGUE)
    assert hint.lean == [CareGoal.S3] and hint.strength == "STRONG"
    assert intake_cues.state_hint(None) is None
