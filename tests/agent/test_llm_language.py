"""세션 언어별 LLM 입력 — 한국어 세션은 지시문 · 출력 모델이 원래 그대로이고, 영어 세션만 출력 언어
지시(*_EN_NOTE)와 언어 설명이 다른 출력 모델(*En)을 쓴다. 모델은 대역이라 비용이 들지 않는다."""

import asyncio
import json
import re

import pytest

from app import protocols
from app.agent import prompts
from app.agent.intake_rules import GoalInference, GoalInferenceEn, StateReading, StateReadingEn
from app.agent.nodes.intake import make_goal_inferrer, make_reader
from app.agent.nodes.planner import (
    PlanRationale,
    PlanRationaleEn,
    make_explainer,
)
from app.agent.nodes.replanner import (
    NextStepChoice,
    NextStepChoiceEn,
    make_recommender,
    rule_proposal,
)
from app.agent.state import IntakeResult, NextStep
from app.schemas.care import CareGoal, CareRequest
from app.schemas.evidence import Medium
from tests.agent import fakes
from tests.agent.test_planner_llm import CHOICE, S1_STATE, cited
from tests.agent.test_replanner import STOPPED

HANGUL = re.compile(r"[가-힣]")


def english(state: dict) -> dict:
    request = state["request"].model_copy(update={"lang": "en"})
    return {**state, "request": request}


def reading() -> StateReading:
    return StateReading(
        safety=False,
        relevance="RELEVANT",
        restate_text=None,
        order_condition=None,
        event=None,
        rumination_cue=False,
        stress_fatigue_ambiguous=False,
        cues=[],
        constraints=[],
        setting=None,
        earphones=None,
        minutes=None,
        summary="-",
        question=None,
    )


@pytest.mark.parametrize(
    "prompts_by_lang, base",
    [
        (prompts.READ_PROMPTS, prompts.READ_SYSTEM_PROMPT),
        (prompts.GOAL_PROMPTS, prompts.GOAL_SYSTEM_PROMPT),
        (prompts.EXPLAIN_PROMPTS, prompts.EXPLAIN_SYSTEM_PROMPT),
        (prompts.RECOMMEND_PROMPTS, prompts.RECOMMEND_SYSTEM_PROMPT),
    ],
)
def test_korean_prompts_are_the_originals_and_english_adds_a_note(prompts_by_lang, base):
    assert prompts_by_lang["ko"] is base
    assert prompts_by_lang["en"].startswith(base + "\n\n## 출력 언어 — 영어\n")


@pytest.mark.parametrize(
    "korean, english_model",
    [
        (StateReading, StateReadingEn),
        (GoalInference, GoalInferenceEn),
        (PlanRationale, PlanRationaleEn),
        (NextStepChoice, NextStepChoiceEn),
    ],
)
def test_english_output_models_keep_the_fields_but_not_the_korean_instruction(
    korean, english_model
):
    ko, en = korean.model_json_schema(), english_model.model_json_schema()
    assert list(en["properties"]) == list(ko["properties"])  # 칸과 순서는 같다
    assert "한국어" in json.dumps(ko, ensure_ascii=False)
    # 영어 출력 모델에는 '(한국어)'가 남지 않는다 — 보기 초안 · 질문 초안까지
    schema = json.dumps(en, ensure_ascii=False)
    if english_model is PlanRationaleEn:  # 콘텐츠 Factor의 고른 이유는 화면에 보이지 않는 기록
        schema = schema.replace("이렇게 고른 이유 한 문장(한국어)", "")
    assert "한국어" not in schema


@pytest.mark.parametrize("lang", ["ko", "en"])
def test_intake_calls_use_the_session_language(lang):
    request = CareRequest(
        text="발표 앞두고 긴장돼요" if lang == "ko" else "Nervous before my talk", lang=lang
    )
    reader_model = fakes.FakeStructuredModel(reading())
    asyncio.run(make_reader(reader_model)(request, []))
    goal_model = fakes.FakeStructuredModel(
        GoalInference(has_cues=True, goal=CareGoal.S2, secondary_goal=None, reason="-")
    )
    asyncio.run(make_goal_inferrer(goal_model)(request, IntakeResult()))

    # 그 언어의 출력 모델 하나만 만든다 — 한국어 세션은 원래 모델 · 지시문 그대로
    assert reader_model.schemas == [
        ({"ko": StateReading, "en": StateReadingEn}[lang], "json_schema")
    ]
    assert goal_model.schemas == [
        ({"ko": GoalInference, "en": GoalInferenceEn}[lang], "json_schema")
    ]
    assert reader_model.inputs[0][0].content == prompts.READ_PROMPTS[lang]
    assert goal_model.inputs[0][0].content == prompts.GOAL_PROMPTS[lang]


def test_english_rationale_input_carries_english_disclosures():
    model = fakes.FakeStructuredModel(PlanRationale.text("…"))
    protocol = protocols.get_protocol("PR-21")
    evidence = [cited(102, [Medium.AI_IMAGE, Medium.MINDFULNESS])]
    asyncio.run(make_explainer(model)(english(S1_STATE), CHOICE, protocol, evidence))

    assert model.schemas == [(PlanRationaleEn, "json_schema")]
    system, human = model.inputs[0]
    assert system.content == prompts.EXPLAIN_PROMPTS["en"]
    said = prompts.DISCLOSURES["en"]
    disclose = human.content.split("## 꼭 밝힐 것\n")[1].split("\n\n")[0]
    assert disclose == f"- {said['conflicting']}\n- {said['ai_image']}"


def test_disclosures_cover_the_same_cases_in_every_language():
    assert set(prompts.DISCLOSURES["en"]) == set(prompts.DISCLOSURES["ko"])
    assert not [s for s in prompts.DISCLOSURES["en"].values() if HANGUL.search(s)]


def test_english_recommendation_uses_the_english_note_and_model():
    model = fakes.FakeStructuredModel(NextStepChoice(recommended=NextStep.END, reason="…"))
    state = english(STOPPED)
    asyncio.run(make_recommender(model)(state, rule_proposal(state)))
    assert model.schemas == [(NextStepChoiceEn, "json_schema")]
    assert model.inputs[0][0].content == prompts.RECOMMEND_PROMPTS["en"]
