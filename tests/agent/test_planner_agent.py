"""Planner의 LangChain 배선(create_agent + ToolStrategy)을 대본 모델로 확인한다."""

import asyncio

from langchain_core.messages import AIMessage

from app.agent.context import AgentContext
from app.agent.nodes.planner import make_agent_planner
from app.agent.state import IntakeResult
from app.schemas.care import CareGoal, CareRequest
from tests.agent.fakes import ScriptedChatModel


def test_agent_planner_returns_submitted_plan():
    submitted = {"activity": {"protocol_id": "PR-09", "minutes": 3}, "rationale": "-"}
    model = ScriptedChatModel(
        messages=iter(
            [AIMessage(content="", tool_calls=[{"name": "CarePlan", "args": submitted, "id": "1"}])]
        )
    )
    planner = make_agent_planner(model)

    state = {
        "request": CareRequest(text="생각이 멈추지 않아요"),
        "intake": IntakeResult(goal=CareGoal.S1, minutes=10),
    }
    plan = asyncio.run(planner(state, AgentContext(), []))

    assert plan.activity.protocol_id == "PR-09"
    assert plan.activity.evidence_record_ids == []
    assert model.offered[0] == [
        "search_evidence",
        "validate_evidence",
        "get_care_protocols",
        "CarePlan",
    ]
