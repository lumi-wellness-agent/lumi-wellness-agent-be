"""intake LLM 입력 — 상황 읽기(①)와 목표 추론(③)에 무엇이 실리는지.

노드 흐름은 tests/agent/test_graph.py에서 본다.

실제 호출 결과(녹화본)는 make plan-trial로 본다. 여기서는 프롬프트 입력과 구조화 출력 스키마만 본다.
"""

from app.agent.intake_rules import GoalInference, StateReading
from app.agent.prompts import build_goal_input, build_read_input
from app.agent.state import IntakeResult
from app.schemas.care import CareRequest
from app.schemas.evidence import InterventionOrder
from app.schemas.intake import IntakeTurn, QuestionSlot


def test_read_input_carries_previous_turns_and_asks_to_reread_free_text():
    request = CareRequest(text="요즘 너무 지치고 짜증나요")
    assert build_read_input(request, []) == "## 사용자 입력\n요즘 너무 지치고 짜증나요"
    restated = [
        IntakeTurn(
            question_id="r1", slot=QuestionSlot.RESTATE, text="오늘 하루는요?", free_text="피곤해"
        )
    ]
    assert "relevance를 다시 판단" in build_read_input(request, restated)
    turns = [
        IntakeTurn(
            question_id="q1",
            slot=QuestionSlot.STATE,
            text="방금 있었던 일 때문인가요?",
            free_text="그냥 계속 그래요",
        )
    ]
    text = build_read_input(request, turns)
    assert "[state] 방금 있었던 일 때문인가요? → 그냥 계속 그래요" in text
    assert "재질문(followup)" in text
    chip = [
        turns[0].model_copy(
            update={"free_text": None, "answer": {"order_condition": "POST_STRESS"}}
        )
    ]
    assert "재질문" not in build_read_input(request, chip)


def test_goal_input_has_state_and_cue_table():
    intake = IntakeResult(
        order_condition=InterventionOrder.POST_STRESS,
        event="발표",
        rumination_cue=True,
        cues=["계속 생각나요"],
    )
    text = build_goal_input(CareRequest(text="발표 끝났는데 계속 생각나요"), intake)
    assert "- 사용자 상태: POST_STRESS" in text and "- 반복 사고 단서: 있음" in text
    assert "## 단서표(팀 요약)" in text and '"state_hints"' in text and "계속 생각나요" in text


def test_llm_output_schemas_are_what_the_prompts_describe():
    reading = StateReading.model_json_schema()["properties"]
    assert {
        "order_condition",
        "rumination_cue",
        "stress_fatigue_ambiguous",
        "search_query",
        "question",
        "setting",
        "earphones",
        "minutes",
        "safety",
        "relevance",
        "restate_text",
    } <= set(reading)
    goal = GoalInference.model_json_schema()["properties"]
    assert set(goal) == {"has_cues", "goal", "secondary_goal", "reason"}
