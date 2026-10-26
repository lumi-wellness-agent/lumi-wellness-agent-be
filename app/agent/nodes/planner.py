"""① Adaptive Care Planning Agent — 근거를 찾고 검증해 이번 라운드의 활동 노드 1개를 고른다.

LangChain `create_agent`의 Tool 호출 루프다. 시스템에서 Tool 루프를 도는 유일한 노드.
- get_care_protocols: 검수된 Care Protocol 후보 (data/protocols)
- search_evidence: Evidence Record 하이브리드 검색 (RAG 근거 검색)
- validate_evidence: EvidenceValidationTool
그 결과로 CarePlan(활동 1개 + rationale)을 제출한다(ToolStrategy). Tool은 세션에서 정해진 값(목표 ·
사용자 상태 · 시간 · 이미지 선호)을 PlannerContext에서 읽고, LLM은 고르는 값만 넘긴다.

LLM의 계획은 그대로 믿지 않는다. check_plan(후검증)이 규칙을 확인하고, 어기면 거부 사유를 붙여
한 번 다시 묻는다. 그래도 어기면 PlanRejected다. 규칙을 통과한 계획은 keep_usable_evidence가
인용 근거를 다시 검증해 usable만 남긴다 — 근거는 활동을 막지 않으므로 다시 묻지 않는다.

Tool 루프는 끝이 보장되지 않는다(LangGraph 기본 재귀 한도 10007 — 2026-10-26 실측에서 10분 넘게
돈 일이 있다). 모델 호출이 MAX_MODEL_CALLS번째가 되면 SubmitWithinBudget이 일반 Tool을 빼
CarePlan 제출만 남기고, RECURSION_LIMIT를 넘으면 GraphRecursionError로 끊는다.

모델 조립은 app/agent/assembly.py(OpenAI 1순위 · Anthropic 폴백).
"""

import logging
from collections.abc import Awaitable
from typing import Protocol

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime

from app.agent.context import AgentContext, PlannerContext
from app.agent.prompts import PLANNER_SYSTEM_PROMPT, build_planner_input
from app.agent.state import CarePlan, CareState, NextStep
from app.agent.tools import PLANNER_TOOLS
from app.agent.tools.evidence import validate_for_session
from app.protocols import get_protocol
from app.schemas.protocol import Composition

log = logging.getLogger(__name__)

# 정상 흐름은 모델 호출 4~5회(목록 → 검색 → 검증 → 제출). 인자 오류·재검색 여유를 두고
# 8번째에는 제출시킨다
MAX_MODEL_CALLS = 8
# 그래프 step 상한. 모델·Tool 노드가 번갈아 돌아 호출 1회에 2 step. 제출을 강제한 뒤 CarePlan 인자
# 오류로 다시 묻는 몇 번까지 둔다
RECURSION_LIMIT = 2 * (MAX_MODEL_CALLS + 3)


class Planner(Protocol):
    """계획을 세우는 쪽의 모양. 운영은 make_agent_planner, 테스트는 대역을 넣는다."""

    def __call__(
        self, state: CareState, context: AgentContext, problems: list[str]
    ) -> Awaitable[CarePlan]: ...


class PlanRejected(Exception):
    """후검증을 두 번 통과하지 못한 계획."""


def check_plan(plan: CarePlan, state: CareState) -> list[str]:
    """CarePlan 후검증. 어긴 규칙을 돌려준다 (비어 있으면 통과).

    검수 ID · 단계 모듈 제외 · 목표 일치 · 순서 조건 · 시간 범위 · 활동 변경 시 같은 프로토콜 금지.
    인용 근거는 여기서 보지 않는다 — DB가 필요해 keep_usable_evidence가 따로 다시 검증한다.
    """
    intake, activity = state["intake"], plan.activity
    protocol = get_protocol(activity.protocol_id)
    if protocol is None:
        return [f"{activity.protocol_id}는 검수된 프로토콜이 아니다"]
    problems = []
    if protocol.composition is Composition.STAGE_MODULE:
        problems.append(f"{activity.protocol_id}는 단계 모듈이라 단독 활동으로 쓸 수 없다")
    if intake.goal and not protocol.serves(intake.goal):
        problems.append(f"{activity.protocol_id}는 {intake.goal.value} 목표용이 아니다")
    if not protocol.allows_order(intake.order_condition):
        allowed = [o.value for o in protocol.intervention_order]
        problems.append(f"사용자 상태 {intake.order_condition}는 허용 상태 {allowed}에 맞지 않는다")
    duration = protocol.duration_minutes
    if duration.min is not None and activity.minutes < duration.min:
        problems.append(f"활동 시간 {activity.minutes}분이 최소 {duration.min}분보다 짧다")
    if duration.max is not None and activity.minutes > duration.max:
        problems.append(f"활동 시간 {activity.minutes}분이 최대 {duration.max}분보다 길다")
    if intake.minutes and activity.minutes > intake.minutes:
        problems.append(f"활동 시간 {activity.minutes}분이 가용 시간 {intake.minutes}분을 넘는다")
    rounds = state.get("rounds") or []
    if (
        rounds
        and rounds[-1].next_step is NextStep.CHANGE_ACTIVITY
        and rounds[-1].plan.activity.protocol_id == activity.protocol_id
    ):
        problems.append("활동 변경을 골랐는데 같은 프로토콜을 다시 넣었다")
    return problems


def excluded_protocols(state: CareState) -> tuple[str, ...]:
    """이번 라운드에 내놓지 않을 프로토콜 — 직전 라운드에서 바꾸기로 한 활동.

    check_plan의 '활동 변경 시 같은 프로토콜 금지'와 같은 규칙이다.
    """
    rounds = state.get("rounds") or []
    if rounds and rounds[-1].next_step is NextStep.CHANGE_ACTIVITY:
        return (rounds[-1].plan.activity.protocol_id,)
    return ()


def planner_context(context: AgentContext | None, state: CareState) -> PlannerContext:
    """그래프 컨텍스트에 이번 세션의 정해진 값(인테이크 · 이미지 선호 · 뺄 활동)을 더한 컨텍스트."""
    intake = state["intake"]
    return PlannerContext(
        db_pool=context.db_pool if context else None,
        goal=intake.goal,
        user_order=intake.order_condition,
        minutes_available=intake.minutes,
        content_type=state["request"].profile.content_type,
        excluded_protocol_ids=excluded_protocols(state),
    )


async def keep_usable_evidence(plan: CarePlan, session: PlannerContext) -> CarePlan:
    """계획이 인용한 근거를 다시 검증해 usable만 남긴다. 목표를 지지하는 근거(supports_goal)가
    앞, 그 안에서는 LLM이 적은 순서다. 중복은 뺀다.

    LLM이 검증하지 않은 ID나 usable=False인 ID를 넣었으면 빼고 로그에 남긴다. validate_evidence
    Tool과 같은 값(validate_for_session)으로 판정한다. check_plan을 통과한 계획만 받는다. 풀이
    없으면 출처를 확인할 수 없어 전부 빠진다(validation.validate).
    """
    activity = plan.activity
    cited = list(dict.fromkeys(activity.evidence_record_ids))
    if not cited:
        return plan
    protocol = get_protocol(activity.protocol_id)
    if protocol is None:  # check_plan이 막지만 방어한다
        raise ValueError(f"검수되지 않은 프로토콜 {activity.protocol_id}")
    verdicts = await validate_for_session(session, protocol, cited)
    usable = [v.evidence_record_id for v in verdicts if v.usable and v.supports_goal]
    usable += [v.evidence_record_id for v in verdicts if v.usable and not v.supports_goal]
    if dropped := [i for i in cited if i not in usable]:
        log.warning("계획 %s의 인용 근거 %s는 usable이 아니라 뺐다", protocol.protocol_id, dropped)
    if usable == activity.evidence_record_ids:
        return plan
    return plan.model_copy(
        update={"activity": activity.model_copy(update={"evidence_record_ids": usable})}
    )


class SubmitWithinBudget(AgentMiddleware):
    """모델 호출이 max_calls번째가 되면 일반 Tool을 빼서 CarePlan 제출만 남긴다.

    ToolStrategy는 Tool 사용을 강제하므로(tool_choice="any") 남은 Tool이 CarePlan뿐이면 모델은
    지금까지 찾은 것으로 계획을 낸다. 예외로 끊는 것보다 낫다 — 근거가 덜 모였어도 활동은 정해진다.
    """

    def __init__(self, max_calls: int = MAX_MODEL_CALLS):
        super().__init__()
        self.max_calls = max_calls

    def _within_budget(self, request: ModelRequest) -> ModelRequest:
        made = sum(isinstance(m, AIMessage) for m in request.messages)
        if made + 1 < self.max_calls:
            return request
        log.warning("Planner 모델 호출 %d회째 — Tool을 빼고 CarePlan 제출만 남긴다", made + 1)
        return request.override(tools=[])

    def wrap_model_call(self, request, handler):
        return handler(self._within_budget(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._within_budget(request))


def build_planner_agent(model: BaseChatModel, *, max_model_calls: int = MAX_MODEL_CALLS):
    return create_agent(
        model,
        tools=PLANNER_TOOLS,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        middleware=[SubmitWithinBudget(max_model_calls)],
        response_format=ToolStrategy(CarePlan),
        context_schema=PlannerContext,
    )


def make_agent_planner(model: BaseChatModel) -> Planner:
    """LangChain 에이전트로 계획을 세우는 Planner."""
    agent = build_planner_agent(model)

    async def plan_with_agent(
        state: CareState, context: AgentContext, problems: list[str]
    ) -> CarePlan:
        message = HumanMessage(build_planner_input(state, problems))
        result = await agent.ainvoke(
            {"messages": [message]},
            {"recursion_limit": RECURSION_LIMIT},
            context=planner_context(context, state),
        )
        return result["structured_response"]

    return plan_with_agent


def make_plan_node(planner: Planner):
    async def plan(state: CareState, runtime: Runtime[AgentContext]) -> dict:
        problems: list[str] = []
        for _ in range(2):  # 처음 한 번 + 거부 사유를 붙여 한 번 더
            care_plan = await planner(state, runtime.context, problems)
            problems = check_plan(care_plan, state)
            if not problems:
                session = planner_context(runtime.context, state)
                return {"plan": await keep_usable_evidence(care_plan, session)}
        raise PlanRejected("; ".join(problems))

    return plan
