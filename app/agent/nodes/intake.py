"""intake — 자유 텍스트 → IntakeResult 구조화 출력 1회. 에이전트가 아니다(Tool 없음 · RAG 없음).

- LLM은 분류만 한다: 목표(S1/S2/S3) · 순서 조건 · 슬롯(시간·장소·제약) · 안전 신호. 점수 추정 금지.
- 되묻기 루프(ask_user 칩 interrupt)는 발표 범위에서 뺐다. 대신 FE가 goal · minutes_available ·
  place를 칩으로 받아 CareRequest에 실어 보내면 그 값이 LLM 분류보다 우선하고(resolve), 나머지
  빈 슬롯은 기본값으로 메운다. 메운 슬롯은 missing_slots에 남긴다.
- goal을 끝내 못 정하면 세션을 열지 않고 ended=NO_GOAL로 끝낸다 — FE가 목표 칩을 보여 주고
  request.goal을 채워 다시 시작한다. 안전 신호면 ended=SAFETY — 세션 대신 안전 안내.
- 2턴 이내. 감정을 길게 말로 풀면 그 자체가 중재(affect labeling, E10)라 사전 측정이 오염된다.

운영은 make_llm_intaker(구조화 출력), 테스트는 Intaker 대역을 넣는다.
"""

from collections.abc import Awaitable
from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END

from app.agent.prompts import INTAKE_SYSTEM_PROMPT, build_intake_input
from app.agent.state import CareState, IntakeResult, SessionEnd
from app.schemas.care import CareRequest, Place

DEFAULT_MINUTES = 10
DEFAULT_PLACE = Place.INDOOR


class Intaker(Protocol):
    """요청을 IntakeResult로 분류하는 쪽의 모양."""

    def __call__(self, request: CareRequest) -> Awaitable[IntakeResult]: ...


def resolve(request: CareRequest, result: IntakeResult) -> IntakeResult:
    """요청의 칩 값 > LLM 분류 > 기본값. 메운 슬롯과 못 정한 goal은 missing_slots에 남긴다."""
    missing: list[str] = []
    goal = request.goal or result.goal
    if goal is None:
        missing.append("goal")
    minutes = request.minutes_available or result.minutes
    if minutes is None:
        minutes = DEFAULT_MINUTES
        missing.append("minutes")
    place = request.place or result.place
    if place is None:
        place = DEFAULT_PLACE
        missing.append("place")
    return result.model_copy(
        update={"goal": goal, "minutes": minutes, "place": place, "missing_slots": missing}
    )


def make_llm_intaker(model: BaseChatModel) -> Intaker:
    """구조화 출력 1회(OpenAI Structured Outputs, json_schema)."""
    structured = model.with_structured_output(IntakeResult, method="json_schema")

    async def intake_with_llm(request: CareRequest) -> IntakeResult:
        messages = [SystemMessage(INTAKE_SYSTEM_PROMPT), HumanMessage(build_intake_input(request))]
        return await structured.ainvoke(messages)

    return intake_with_llm


def make_intake_node(intaker: Intaker):
    async def intake(state: CareState) -> dict:
        result = resolve(state["request"], await intaker(state["request"]))
        update: dict = {"intake": result}
        if result.safety:
            update["ended"] = SessionEnd.SAFETY
        elif result.goal is None:
            update["ended"] = SessionEnd.NO_GOAL
        return update

    return intake


def route_after_intake(state: CareState) -> Literal["measure_pre", "__end__"]:
    """안전 신호나 목표 미정이면 세션을 열지 않는다."""
    return END if state.get("ended") else "measure_pre"
