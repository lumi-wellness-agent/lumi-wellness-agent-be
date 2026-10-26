"""① Adaptive Care Planning Agent — 근거를 찾고 검증해 이번 라운드의 활동 노드 1개를 고른다.

LangChain `create_agent`의 Tool 호출 루프다. 시스템에서 Tool 루프를 도는 유일한 노드.
- get_care_protocols: 검수된 Care Protocol 후보 (data/protocols)
- search_evidence: Evidence Record 하이브리드 검색 (RAG 근거 검색)
- validate_evidence: EvidenceValidationTool
그 결과로 CarePlan(활동 1개 + rationale)을 제출한다(ToolStrategy).

LLM의 계획은 그대로 믿지 않는다. check_plan(후검증)이 규칙을 확인하고, 어기면 거부 사유를 붙여
한 번 다시 묻는다. 그래도 어기면 PlanRejected다.

모델 조립은 app/agent/assembly.py(OpenAI, 모델 하나).
"""

from collections.abc import Awaitable
from typing import Protocol

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from app.agent.context import AgentContext
from app.agent.prompts import PLANNER_SYSTEM_PROMPT, build_planner_input
from app.agent.state import CarePlan, CareState, NextStep
from app.agent.tools import PLANNER_TOOLS
from app.protocols import get_protocol
from app.schemas.protocol import Composition


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
    TODO(plan 담당): evidence_record_ids를 validate_evidence 결과와 대조해 usable만 남긴다.
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


def build_planner_agent(model: BaseChatModel):
    return create_agent(
        model,
        tools=PLANNER_TOOLS,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        response_format=ToolStrategy(CarePlan),
        context_schema=AgentContext,
    )


def make_agent_planner(model: BaseChatModel) -> Planner:
    """LangChain 에이전트로 계획을 세우는 Planner."""
    agent = build_planner_agent(model)

    async def plan_with_agent(
        state: CareState, context: AgentContext, problems: list[str]
    ) -> CarePlan:
        message = HumanMessage(build_planner_input(state, problems))
        result = await agent.ainvoke({"messages": [message]}, context=context)
        return result["structured_response"]

    return plan_with_agent


def make_plan_node(planner: Planner):
    async def plan(state: CareState, runtime: Runtime[AgentContext]) -> dict:
        problems: list[str] = []
        for _ in range(2):  # 처음 한 번 + 거부 사유를 붙여 한 번 더
            care_plan = await planner(state, runtime.context, problems)
            problems = check_plan(care_plan, state)
            if not problems:
                return {"plan": care_plan}
        raise PlanRejected("; ".join(problems))

    return plan
