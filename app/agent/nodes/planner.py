"""① Adaptive Care Planning Agent — 근거를 찾고 검증해 이번 라운드의 CarePlan을 만든다.

LangChain `create_agent`의 Tool 호출 루프다. LLM이 Tool을 골라 부른다.
- search_evidence: Evidence Record 하이브리드 검색 (RAG 근거 검색)
- validate_evidence: EvidenceValidationTool
- get_care_protocols: 검수된 Care Protocol 조회 (Protocol DB 검증)
그 결과로 활동 선택 · 조합 적합성 · 실행 순서를 정해 CarePlan을 제출한다(ToolStrategy).

LLM의 계획은 그대로 믿지 않는다. check_plan(후검증)이 규칙을 확인하고, 어기면 거부 사유를 붙여
한 번 다시 묻는다. 그래도 어기면 PlanRejected다.
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
from app.agent.state import CarePlan, CareState, PlanMode
from app.agent.tools import PLANNER_TOOLS


class Planner(Protocol):
    """계획을 세우는 쪽의 모양. 운영은 make_agent_planner, 테스트는 대역을 넣는다."""

    def __call__(
        self, state: CareState, context: AgentContext, problems: list[str]
    ) -> Awaitable[CarePlan]: ...


class PlanRejected(Exception):
    """후검증을 두 번 통과하지 못한 계획."""


def check_plan(plan: CarePlan) -> list[str]:
    """CarePlan 후검증. 어긴 규칙을 돌려준다 (비어 있으면 통과).

    동시 복합의 활동 수는 아키텍처에 없어 순차 복합과 같은 2~3개로 둔다.
    Protocol DB가 생기면 protocol_id가 검수된 프로토콜인지, 묶은 활동끼리 함께 실행할 수
    있는지도 여기서 확인한다.
    """
    problems = []
    count = len(plan.activities)
    if plan.mode is PlanMode.SINGLE and count != 1:
        problems.append(f"단일 세션(SINGLE)은 활동이 1개여야 한다. 지금 {count}개다")
    if plan.mode is not PlanMode.SINGLE and not 2 <= count <= 3:
        problems.append(f"복합 세션({plan.mode})은 활동이 2~3개여야 한다. 지금 {count}개다")
    protocol_ids = [activity.protocol_id for activity in plan.activities]
    if len(set(protocol_ids)) != count:
        problems.append("같은 프로토콜을 두 번 넣었다")
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
    """LangChain 에이전트로 계획을 세우는 Planner. 모델 공급자는 아직 정하지 않았다."""
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
            problems = check_plan(care_plan)
            if not problems:
                return {"plan": care_plan}
        raise PlanRejected("; ".join(problems))

    return plan
