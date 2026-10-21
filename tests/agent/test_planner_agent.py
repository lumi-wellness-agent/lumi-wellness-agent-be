"""Planner의 LangChain 배선(create_agent + ToolStrategy)을 대본 모델로 확인한다."""

import asyncio

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import Field

from app.agent.context import AgentContext
from app.agent.nodes.planner import make_agent_planner
from app.schemas.care import CareGoal, CareRequest


class ScriptedChatModel(GenericFakeChatModel):
    """`messages` 순서대로 AIMessage를 돌려준다. OpenAI 등 실제 LLM을 부르지 않는다.

    create_agent가 거치는 bind_tools는 받은 Tool 이름을 남기고 자신을 돌려준다.
    """

    offered: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.offered.append([tool.name for tool in tools])
        return self


def test_agent_planner_returns_submitted_plan():
    submitted = {
        "mode": "SINGLE",
        "activities": [
            {"protocol_id": "s1-mindfulness", "goal": "S1", "medium": "MINDFULNESS", "minutes": 3}
        ],
        "rationale": "-",
    }
    model = ScriptedChatModel(
        messages=iter(
            [AIMessage(content="", tool_calls=[{"name": "CarePlan", "args": submitted, "id": "1"}])]
        )
    )
    planner = make_agent_planner(model)

    state = {"request": CareRequest(stress_level=7, goal=CareGoal.S1)}
    plan = asyncio.run(planner(state, AgentContext(), []))

    assert [a.protocol_id for a in plan.activities] == ["s1-mindfulness"]
    assert model.offered[0] == [
        "search_evidence",
        "validate_evidence",
        "get_care_protocols",
        "CarePlan",
    ]
