"""intake — State(대화) → Goal(추론) → Context(환경 칩). 규칙은 app/agent/intake_rules.py.

    intake_read ─(시점 질문)→ intake_ask ⏸ ─(글 답)→ intake_read
        │                       ├─(잘 모르겠어요)→ intake_ask ⏸ (고정 재질문)
        │                       ├─(칩 답)→ intake_goal
        │                       └─(자유 입력이 안전 검사에 걸림)→ END (ended = SAFETY)
        └→ intake_goal → intake_context ─(환경 질문)→ intake_ask ⏸ → intake_finish
                              └→ intake_finish → measure_pre

- ① intake_read (LLM): 문장(과 앞선 질문·답)에서 사용자 상태 · 사건 · 반복 사고 단서 · 문장에 나온
  여건을 읽고, 개념 정의 검색어와 시점 질문 초안을 낸다. 초안은 코드가 검증해 질문으로 만든다.
- ③ intake_goal (LLM): 단서표(data/intake_cues.json)와 상황 단서로 목표 S1 · S2 · S3와 부목표를
  정한다. State 두 질문이 모두 '잘 모르겠어요'면 LLM 없이 S2 DEFAULT. ② 개념 정의 검색은 팀원이
  JSON을 주면 이 노드 앞에 붙인다(그전까지 goal_evidence는 빈다).
- ④ intake_context (코드): setting · earphones · minutes 중 비어 있고 1순위 활동을 바꾸는 칸만
  묻는다.
- intake_ask ⏸: 질문 interrupt(이 노드 하나). payload는 IntakeQuestion, resume은 IntakeAnswer
  (app/schemas/intake.py). 자유 입력 답은 안전 검사를 거쳐 걸리면 세션을 끝낸다. interrupt가 있는
  노드는 재개될 때 처음부터 다시 돌므로 LLM 호출을 두지 않는다(그래서 노드를 나눴다).
- intake_finish (코드): 빈 칸을 기본값으로, 환경 조건과 Place, 끝난 이유를 정한다.

질문은 합계 3회(State 최대 2회). LLM은 질문 수와 상관없이 2회(①·③)이고, 시점 질문에 글로 답할
때만 ①이 한 번 더 돈다.

운영은 LlmIntaker(구조화 출력, 재시도·폴백), 테스트는 Intaker 대역을 넣는다.
"""

from collections.abc import Awaitable, Sequence
from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import interrupt

from app.agent import intake_rules as rules
from app.agent.intake_rules import GoalInference, StateReading
from app.agent.llm import with_fallback
from app.agent.prompts import (
    GOAL_SYSTEM_PROMPT,
    READ_SYSTEM_PROMPT,
    build_goal_input,
    build_read_input,
)
from app.agent.state import CareState, IntakeResult, SessionEnd
from app.schemas.care import CareRequest
from app.schemas.intake import MAX_TURNS, IntakeAnswer, IntakeTurn, QuestionSlot


class Intaker(Protocol):
    """LLM이 하는 두 판단. 운영은 LlmIntaker, 테스트는 대역을 넣는다."""

    def read(
        self, request: CareRequest, turns: Sequence[IntakeTurn]
    ) -> Awaitable[StateReading]: ...

    def infer_goal(
        self, request: CareRequest, intake: IntakeResult
    ) -> Awaitable[GoalInference]: ...


def make_reader(model: BaseChatModel):
    structured = model.with_structured_output(StateReading, method="json_schema")

    async def read(request: CareRequest, turns: Sequence[IntakeTurn]) -> StateReading:
        messages = [
            SystemMessage(READ_SYSTEM_PROMPT),
            HumanMessage(build_read_input(request, turns)),
        ]
        return await structured.ainvoke(messages)

    return read


def make_goal_inferrer(model: BaseChatModel):
    structured = model.with_structured_output(GoalInference, method="json_schema")

    async def infer_goal(request: CareRequest, intake: IntakeResult) -> GoalInference:
        messages = [
            SystemMessage(GOAL_SYSTEM_PROMPT),
            HumanMessage(build_goal_input(request, intake)),
        ]
        return await structured.ainvoke(messages)

    return infer_goal


class LlmIntaker:
    """공급자별 모델로 만든 read · infer_goal에 재시도·폴백(app/agent/llm.py)을 두른 Intaker."""

    def __init__(self, models: Sequence[tuple[str, BaseChatModel]]):
        self._read = with_fallback([(name, make_reader(m)) for name, m in models])
        self._infer = with_fallback([(name, make_goal_inferrer(m)) for name, m in models])

    async def read(self, request: CareRequest, turns: Sequence[IntakeTurn]) -> StateReading:
        return await self._read(request, turns)

    async def infer_goal(self, request: CareRequest, intake: IntakeResult) -> GoalInference:
        return await self._infer(request, intake)


def make_intake_nodes(intaker: Intaker) -> dict:
    """노드 이름 → 함수. graph.py가 배선한다."""

    async def intake_read(state: CareState) -> dict:
        request, turns = state["request"], state.get("intake_turns") or []
        reading = await intaker.read(request, turns)
        intake = rules.compose(request, reading, turns, previous=state.get("intake"))
        return {
            "intake": intake,
            "pending_question": rules.state_question(reading, turns),
            "search_query": reading.search_query,
        }

    def intake_ask(state: CareState) -> dict:
        question = state["pending_question"]
        raw = interrupt(question.model_dump(mode="json"))
        answer = IntakeAnswer.model_validate(raw)
        values, turn = rules.apply_answer(question, answer)
        intake = rules.merge_values(state["intake"], values)
        if turn.free_text is not None and rules.safety_hit(turn.free_text):
            return {
                "intake": rules.end_for_safety(intake),
                "intake_turns": [turn],
                "pending_question": None,
                "ended": SessionEnd.SAFETY,
            }
        turns = [*(state.get("intake_turns") or []), turn]
        return {
            "intake": intake,
            "intake_turns": [turn],
            # 시점 질문에 '잘 모르겠어요'면 읽기 없이 고정 재질문(예산 안에서)
            "pending_question": rules.followup_after_unknown(turns),
        }

    async def intake_goal(state: CareState) -> dict:
        request, intake = state["request"], state["intake"]
        turns = state.get("intake_turns") or []
        if request.goal is not None or rules.both_state_unknown(turns):
            return {"intake": rules.apply_goal(intake, None, request, turns)}
        inference = await intaker.infer_goal(request, intake)
        return {"intake": rules.apply_goal(intake, inference, request, turns)}

    def intake_context(state: CareState) -> dict:
        turns = state.get("intake_turns") or []
        if len(turns) >= MAX_TURNS or any(t.slot is QuestionSlot.CONTEXT for t in turns):
            return {"pending_question": None}
        groups = rules.context_groups(state["intake"])
        return {"pending_question": rules.context_question(groups, len(turns) + 1)}

    def intake_finish(state: CareState) -> dict:
        turns = state.get("intake_turns") or []
        return {"intake": rules.finish(state["intake"], turns), "pending_question": None}

    return {
        "intake_read": intake_read,
        "intake_ask": intake_ask,
        "intake_goal": intake_goal,
        "intake_context": intake_context,
        "intake_finish": intake_finish,
    }


def route_after_read(state: CareState) -> Literal["intake_ask", "intake_goal"]:
    return "intake_ask" if state.get("pending_question") else "intake_goal"


def route_after_ask(
    state: CareState,
) -> Literal["intake_ask", "intake_read", "intake_goal", "intake_finish", "__end__"]:
    """안전 검사에 걸리면 끝. 다음 질문(고정 재질문)이 있으면 다시 묻는다. State 질문의 글 답은
    다시 읽고, 칩 답은 목표로. 환경 질문 뒤에는 끝."""
    if state.get("ended"):
        return END
    if state.get("pending_question"):
        return "intake_ask"
    last = state["intake_turns"][-1]
    if last.slot is QuestionSlot.CONTEXT:
        return "intake_finish"
    return "intake_read" if last.free_text is not None else "intake_goal"


def route_after_context(state: CareState) -> Literal["intake_ask", "intake_finish"]:
    return "intake_ask" if state.get("pending_question") else "intake_finish"


async def intake_once(intaker: Intaker, request: CareRequest) -> tuple[IntakeResult, list[str]]:
    """질문 없이 한 번에 — ① 읽기 → ③ 목표 → 끝. 리허설(scripts/plan_trial.py)용.

    질문할 차례였던 slot 이름을 함께 돌려준다(세션이었다면 무엇을 물었을지).
    """
    reading = await intaker.read(request, [])
    intake = rules.compose(request, reading, [])
    would_ask = []
    if question := rules.state_question(reading, []):
        would_ask.append(question.slot.value)
    inference = None if request.goal is not None else await intaker.infer_goal(request, intake)
    intake = rules.apply_goal(intake, inference, request)
    if groups := rules.context_groups(intake):
        would_ask.append("context:" + "+".join(groups))
    return rules.finish(intake), would_ask
