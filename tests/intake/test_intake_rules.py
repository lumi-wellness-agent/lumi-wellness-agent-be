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


def test_context_asks_only_slots_that_change_the_candidates():
    # plan이 후보를 등급 가중 추첨 순서로 세워 어느 후보든 추천될 수 있다 — 1순위가 아니라 후보
    # 묶음을 바꾸는 칸을 묻는다
    s2_home = IntakeResult(goal=CareGoal.S2, setting=Setting.HOME)
    assert rules.context_groups(s2_home) == ["minutes"]  # 5분 · 10분 · 20분의 후보가 다르다
    # 집에서는 이어폰이 환경을 바꾸지 않는다 — 모르는 칸이어도 묻지 않는다
    assert rules.context_groups(s2_home.model_copy(update={"minutes": 10})) == []
    s1 = IntakeResult(goal=CareGoal.S1)
    assert rules.context_groups(s1) == ["setting", "earphones", "minutes"]  # 20분이면 PR-21
    s1_office = IntakeResult(goal=CareGoal.S1, setting=Setting.OFFICE_CAFE, minutes=10)
    assert rules.context_groups(s1_office) == ["earphones"]
    assert rules.candidate_ids(s1_office, earphones=False) == {"PR-15"}  # 소리 활동은 빠진다
    assert rules.candidate_ids(s1_office, earphones=True) == {"PR-09", "PR-15"}
    s3_home = IntakeResult(goal=CareGoal.S3, setting=Setting.HOME)
    assert rules.context_groups(s3_home) == ["minutes"]
    assert rules.candidate_ids(s3_home, minutes=10) == {"PR-01", "PR-04"}
    assert rules.candidate_ids(s3_home, minutes=20) == {"PR-01", "PR-04", "PR-10"}

    q = rules.context_question(["setting", "earphones", "minutes"], 2)
    assert q.slot is QuestionSlot.CONTEXT and q.question_id == "q2" and not q.allow_free_text
    assert [(g.key, g.select.value, len(g.options)) for g in q.groups] == [
        ("setting", "single", 4),
        ("earphones", "single", 2),
        ("minutes", "single", 4),  # 3분 · 5분 · 10분 · 20분 이상
    ]
    assert [o.set for o in q.groups[1].options] == [{"earphones": True}, {"earphones": False}]
    assert all(o.label != UNKNOWN_LABEL for g in q.groups for o in g.options)
    assert q.groups[2].options[0].label == "3분" and q.groups[2].options[-1].label == "20분 이상"
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
    # 추론한 목표 · 부목표 순으로 그 개념 정의(원문)를 붙인다
    assert [e.paper_id for e in out.goal_evidence] == ["P12", "N26", "N25", "N04", "N07", "N01"]
    assert out.goal_evidence[0].sentence.startswith("Repetitive negative thinking (RNT)")
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
    assert [e.paper_id for e in out.goal_evidence] == [
        "P06",
        "P06",
        "N27",
        "N27",
        "N04",
        "N07",
        "N01",
    ]
    out = rules.apply_goal(IntakeResult(), no_cues, CareRequest(text="…"))
    assert out.goal is CareGoal.S2 and out.goal_source is GoalSource.DEFAULT
    assert out.goal_evidence == []  # 정의로 판단한 것이 아니다
    out = rules.apply_goal(IntakeResult(), inference(), CareRequest(text="…", goal=CareGoal.S3))
    assert out.goal is CareGoal.S3 and out.goal_source is GoalSource.CHIP
    assert out.goal_evidence == []


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


def test_goal_definitions_quote_the_papers_the_cue_table_cites():
    """개념 정의는 목표마다 둘 이상이고, 그 목표의 단서표가 인용한 R1 논문에서만 온다."""
    cues, definitions = intake_cues.load_cues(), intake_cues.load_definitions()
    assert set(definitions.goals) == set(CareGoal)
    for goal, defs in definitions.goals.items():
        assert len(defs) >= 2
        assert {d.paper_id for d in defs} <= set(cues.goals[goal].sources)
        assert all(d.concept and d.page and d.sentence.endswith(".") for d in defs)


# ── 상황과 증상이 엇갈릴 때 · 남의 위험 이야기 (10-29) ─────────────────────────────────────────


def test_mismatch_asks_current_state_when_situation_and_symptom_disagree():
    # "야근하고 나니 신경이 곤두서 있어요" — 피로 후인데 긴장이 중심
    r = reading(order_condition=InterventionOrder.POST_FATIGUE, symptom_mismatch=True)
    assert rules.state_question(r, []) is None  # 상황은 정했으니 시점 질문은 없다
    q = rules.mismatch_question(r, [])
    assert q is not None and q.slot is QuestionSlot.STATE_FOLLOWUP and q.turn == 1
    labels = [o.label for o in q.groups[0].options]
    assert labels[-1] == UNKNOWN_LABEL
    hints = [o.set.get("goal_hint") for o in q.groups[0].options]
    assert hints == [["S2"], ["S3"], None]
    for option in q.groups[0].options:  # 보기 값이 재질문 slot 계약 안이다
        parse_set(QuestionSlot.STATE_FOLLOWUP, option.set)


def test_mismatch_question_only_once_and_only_when_situation_is_known():
    assert rules.mismatch_question(reading(symptom_mismatch=True), []) is None  # 상황 미상
    known = reading(order_condition=InterventionOrder.PRE_STRESS, symptom_mismatch=True)
    assert (
        rules.mismatch_question(reading(order_condition=InterventionOrder.PRE_STRESS), []) is None
    )
    asked = [turn(QuestionSlot.STATE_FOLLOWUP, 1, answer={})]
    assert rules.mismatch_question(known, asked) is None  # 이미 물었다
    timing = [turn(QuestionSlot.STATE, 1, text="곧 시험이에요")]
    assert rules.mismatch_question(known, timing) is None  # 시점 질문 뒤는 기존 재질문 몫
    full = [turn(QuestionSlot.CONTEXT, n, answer={}) for n in (1, 2, 3)]
    assert rules.mismatch_question(known, full) is None  # 예산 초과


def test_chosen_current_state_wins_over_inference():
    picked = IntakeResult(goal_hint=[CareGoal.S2])
    out = rules.apply_goal(
        picked, inference(goal=CareGoal.S3, secondary_goal=None), CareRequest(text="…")
    )
    assert (out.goal, out.secondary_goal, out.goal_source) == (
        CareGoal.S2,
        CareGoal.S3,
        GoalSource.INFERRED,
    )
    assert "재질문" in out.goal_reason
    # 둘 다 골랐거나 추론과 같으면 추론 그대로
    both = IntakeResult(goal_hint=[CareGoal.S3, CareGoal.S2])
    assert (
        rules.apply_goal(both, inference(goal=CareGoal.S3), CareRequest(text="…")).goal
        is CareGoal.S3
    )


def test_third_party_risk_restates_with_fixed_help_text():
    r = reading(relevance="OFF_TOPIC", third_party_risk=True, restate_text="LLM 문장")
    q = rules.restate_question(r, [])
    assert q is not None and q.slot is QuestionSlot.RESTATE
    assert q.text == rules.THIRD_PARTY_TEXT
    assert "109" in q.text and "당신의 이야기" in q.text
    # LLM이 RELEVANT로 읽어도 남의 위험 이야기면 109를 알리고 되묻는다(2026-10-29 평가)
    relevant = rules.restate_question(reading(third_party_risk=True), [])
    assert relevant is not None and relevant.text == rules.THIRD_PARTY_TEXT
    # 질문을 시작한 뒤에는 되묻지 않는다
    assert rules.restate_question(r, [turn(QuestionSlot.STATE, 1, answer={})]) is None


def test_diagnosis_question_only_restates_without_diagnosing():
    r = reading(relevance="OFF_TOPIC", diagnosis_request=True, restate_text="우울증일 수 있어요")
    q = rules.restate_question(r, [])
    assert q is not None and q.text == rules.DIAGNOSIS_TEXT  # LLM 문장을 쓰지 않는다
    assert "진단은 하지 못해요" in q.text and q.skip_label == SKIP_LABEL
    # 자기 상태도 말했으면(RELEVANT) 상태만 읽고 이어 간다
    assert rules.restate_question(reading(diagnosis_request=True), []) is None
