"""intake 노드 — 칩 값 우선·기본값·세션 종료 분기. LLM 호출 본문은 intake 담당이 테스트를 더한다.

실제 모델 대신 ScriptedChatModel을 쓰려면 with_structured_output이 필요하므로, 여기서는 Intaker
대역(정해 둔 IntakeResult)과 resolve()·route_after_intake()만 본다.
"""

import asyncio

from app.agent.nodes.intake import (
    DEFAULT_MINUTES,
    DEFAULT_PLACE,
    make_intake_node,
    resolve,
    route_after_intake,
)
from app.agent.prompts import build_intake_input
from app.agent.state import IntakeResult, SessionEnd
from app.schemas.care import CareGoal, CareRequest, Place
from app.schemas.evidence import InterventionOrder


def test_request_chips_override_llm_and_defaults_fill_the_rest():
    llm = IntakeResult(goal=CareGoal.S1, minutes=5, constraints=["이명"])
    request = CareRequest(text="…", goal=CareGoal.S2)
    result = resolve(request, llm)
    assert result.goal is CareGoal.S2  # 칩이 LLM 분류보다 우선
    assert result.minutes == 5  # LLM이 읽은 값
    assert result.place is DEFAULT_PLACE and result.missing_slots == ["place"]
    assert result.constraints == ["이명"]  # 나머지 필드는 그대로

    empty = resolve(CareRequest(text="…"), IntakeResult())
    assert empty.goal is None and empty.minutes == DEFAULT_MINUTES
    assert empty.missing_slots == ["goal", "minutes", "place"]


def test_node_ends_session_on_safety_or_missing_goal():
    async def intaker(request):
        return IntakeResult(goal=None, safety=request.text == "위험")

    node = make_intake_node(intaker)
    out = asyncio.run(node({"request": CareRequest(text="위험")}))
    assert out["ended"] is SessionEnd.SAFETY and route_after_intake(out) == "__end__"
    out = asyncio.run(node({"request": CareRequest(text="모호")}))
    assert out["ended"] is SessionEnd.NO_GOAL
    out = asyncio.run(node({"request": CareRequest(text="모호", goal=CareGoal.S3)}))
    assert "ended" not in out and route_after_intake(out) == "measure_pre"
    assert out["intake"].goal is CareGoal.S3


def test_intake_input_carries_chips():
    text = build_intake_input(
        CareRequest(text="발표 앞두고 긴장돼요", goal=CareGoal.S2, place=Place.INDOOR)
    )
    assert "발표 앞두고 긴장돼요" in text
    assert "- goal: S2" in text and "- place: INDOOR" in text and "minutes" not in text
    assert "직접 고른 값" not in build_intake_input(CareRequest(text="…"))


def test_intake_result_schema_is_what_the_llm_fills():
    schema = IntakeResult.model_json_schema()
    assert set(schema["properties"]) == {
        "goal",
        "order_condition",
        "minutes",
        "place",
        "constraints",
        "safety",
        "safety_note",
        "missing_slots",
        "summary",
    }
    assert [o.value for o in InterventionOrder] == schema["$defs"]["InterventionOrder"]["enum"]
