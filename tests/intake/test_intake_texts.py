"""intake가 코드로 만드는 문장의 언어판 — 보기를 골랐을 때 채워질 값은 언어와 상관없이 같고, 영어
세션은 고정 질문 · 보기 · 되묻기 · 판단 이유를 영어로 받는다."""

import random
import re
from dataclasses import fields

from app.agent import intake_rules as rules
from app.agent.intake_rules import GoalInference, StateReading
from app.agent.intake_texts import EN, KO, TEXTS
from app.agent.state import IntakeResult
from app.schemas.care import LANGS, CareGoal, CareRequest
from app.schemas.intake import (
    MINUTES_CHOICES,
    SKIP_LABEL,
    UNKNOWN_LABEL,
    GoalSource,
    IntakeAnswer,
    QuestionSlot,
)

HANGUL = re.compile(r"[가-힣]")


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


def strings(texts) -> list[str]:
    """한 언어판의 모든 문장 — 보기 라벨 · 시간 보기 포함."""
    out = []
    for f in fields(texts):
        value = getattr(texts, f.name)
        if isinstance(value, str):
            out.append(value)
        elif f.name.endswith("_fixed"):
            out += [label for label, _ in value]
        elif isinstance(value, dict):
            out += list(value.values())
        elif isinstance(value, tuple):
            out += list(value)
    out += [texts.minutes(m) for m in MINUTES_CHOICES]
    return out


def test_choices_fill_the_same_values_in_every_language():
    for lang in LANGS:
        texts = TEXTS[lang]
        for name in ("state_fixed", "followup_fixed", "mismatch_fixed"):
            assert [v for _, v in getattr(texts, name)] == [v for _, v in getattr(KO, name)]
        assert set(texts.settings) == {s.value for s in rules.SETTINGS}
        assert len(texts.earphones) == 2


def test_korean_texts_keep_the_schema_labels():
    # 테스트 · 문서가 쓰는 한국어 라벨(schemas/intake)과 문장 원천이 어긋나지 않는다
    assert KO.unknown == UNKNOWN_LABEL and KO.skip == SKIP_LABEL


def test_english_texts_have_no_korean_and_name_the_domestic_crisis_line():
    assert not [s for s in strings(EN) if HANGUL.search(s)]
    assert "109" in EN.third_party  # 영어 화면도 국내 번호(2026-11-02 결정)


def test_english_fixed_questions_and_context_chips():
    q = rules.fixed_question(QuestionSlot.STATE, 1, "en")
    assert q.text == EN.state
    labels = [o.label for o in q.groups[0].options]
    assert labels == [label for label, _ in EN.state_fixed] + ["I'm not sure"]
    assert q.groups[0].options[-1].set == {}

    ko = rules.context_question(["setting", "earphones", "minutes"], 2)
    en = rules.context_question(["setting", "earphones", "minutes"], 2, "en")
    assert en.text == "Where are you right now? Do you have earphones? How much time do you have?"
    assert [[o.set for o in g.options] for g in en.groups] == [
        [o.set for o in g.options] for g in ko.groups
    ]
    assert [o.label for o in en.groups[2].options] == ["3 min", "5 min", "10 min", "20 min or more"]


def test_english_restate_uses_english_fixed_sentences_and_skip_label():
    blank = rules.restate_question(reading(relevance="OFF_TOPIC"), [], "en")
    assert blank.text == EN.restate and blank.skip_label == "Just start"
    risk = rules.restate_question(reading(third_party_risk=True), [], "en")
    assert risk.text == EN.third_party
    diagnosis = rules.restate_question(
        reading(relevance="OFF_TOPIC", diagnosis_request=True), [], "en"
    )
    assert diagnosis.text == EN.diagnosis


def test_english_goal_reasons_written_by_code():
    q = rules.restate_question(reading(relevance="OFF_TOPIC"), [], "en")
    _, skipped = rules.apply_answer(q, IntakeAnswer(question_id="r1", skip=True))
    request = CareRequest(text="hmm", lang="en")
    out = rules.apply_goal(IntakeResult(), None, request, [skipped], random.Random(0))
    assert out.goal_source is GoalSource.DEFAULT and out.goal_reason == EN.reason_skipped
    no_cues = rules.apply_goal(IntakeResult(), None, request, [], random.Random(0))
    assert no_cues.goal_reason == EN.reason_no_cues
    # 추론한 이유에 진단명이 섞이면 바꿔 쓴다 — 영어 진단명도
    inference = GoalInference(
        has_cues=True,
        goal=CareGoal.S2,
        secondary_goal=None,
        reason="Sounds like an anxiety disorder",
    )
    inferred = rules.apply_goal(IntakeResult(), inference, request)
    assert inferred.goal_reason == EN.reason_without_diagnosis
