"""get_care_protocols → search_evidence → validate_evidence → CarePlan 흐름을 대본 모델로 확인한다.

도구 본체가 부르는 hybrid.search · validation.validate를 fixture 기반 가짜로 바꿔 끼운다.
도구 출력(JSON)의 모양, 검증 결과의 usable·flags, 계획의 evidence_record_ids가 usable 근거에
속하는지를 본다.
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.context import AgentContext
from app.agent.nodes.planner import build_planner_agent
from app.agent.prompts import build_planner_input
from app.agent.state import IntakeResult
from app.schemas.care import CareGoal, CareRequest, ContentType, UserProfile
from app.schemas.evidence import InterventionOrder
from app.schemas.retrieval import Check
from tests.agent import fakes

# AI 생성 이미지로 S1(반추) 세션을 계획하는 대본.
# medium=AI_IMAGE는 도구가 STATIC_IMAGE로 바꿔 찾는다
SEARCH_ARGS = {
    "query": "nature images rumination",
    "medium": "AI_IMAGE",
    "intervention_order": "POST_STRESS",
    "outcomes": ["RUMINATION"],
}
VALIDATE_ARGS = {
    "evidence_record_ids": [102, 112, 999],  # P24-E1 RESULTS · P33-E1 RESULTS · 없는 ID
    "goal": "S1",
    "media": ["AI_IMAGE"],
    "user_order": "POST_STRESS",
}
PLAN = {
    "activity": {"protocol_id": "PR-01", "minutes": 6, "evidence_record_ids": [102]},
    "rationale": "P24-E1 자연 이미지 후 상태 반추 감소. AI 생성 이미지는 연구 자극과 다름을 표시",
}


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture
def fake_backends(monkeypatch):
    monkeypatch.setattr("app.retrieval.hybrid.search", fakes.fake_search)
    monkeypatch.setattr("app.agent.validation.validate", fakes.fake_validate)


def test_planner_calls_tools_and_cites_usable_evidence(fake_backends):
    model = fakes.ScriptedChatModel(
        messages=iter(
            [
                _tool_call("get_care_protocols", {"goal": "S1"}, "0"),
                _tool_call("search_evidence", SEARCH_ARGS, "1"),
                _tool_call("validate_evidence", VALIDATE_ARGS, "2"),
                _tool_call("CarePlan", PLAN, "3"),
            ]
        )
    )
    agent = build_planner_agent(model)
    request = CareRequest(
        text="같은 생각이 계속 돌아요", profile=UserProfile(content_type=ContentType.AI_IMAGE)
    )
    intake = IntakeResult(
        goal=CareGoal.S1, order_condition=InterventionOrder.POST_STRESS, minutes=10
    )
    message = HumanMessage(build_planner_input({"request": request, "intake": intake}, []))

    result = asyncio.run(agent.ainvoke({"messages": [message]}, context=AgentContext()))

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    # 구조화 출력(CarePlan)도 ToolStrategy가 ToolMessage로 남긴다
    assert [m.name for m in tool_messages] == [
        "get_care_protocols",
        "search_evidence",
        "validate_evidence",
        "CarePlan",
    ]

    protocols = json.loads(tool_messages[0].content)
    assert [p["protocol_id"] for p in protocols][:1] == ["PR-09"]  # S1 주 목표가 앞
    assert {"grades", "duration_minutes", "intervention_order", "composition"} <= set(protocols[0])
    assert "PR-16" not in {p["protocol_id"] for p in protocols}  # 단계 모듈 제외

    hits = json.loads(tool_messages[1].content)
    assert 1 <= len(hits) <= 5
    assert {h["evidence_record_id"] for h in hits} == {101, 102, 111, 112}
    assert all(h["metadata"]["media"] == ["STATIC_IMAGE"] for h in hits)
    assert all(any(r["outcome"] == "RUMINATION" for r in h["metadata"]["results"]) for h in hits)
    assert {"paper", "excerpt", "section", "score"} <= set(hits[0])

    verdicts = {v["evidence_record_id"]: v for v in json.loads(tool_messages[2].content)}
    assert list(verdicts) == [102, 112, 999]
    assert [c["check"] for c in verdicts[102]["checks"]] == [c.value for c in Check]
    assert verdicts[102]["usable"] and verdicts[102]["supports_goal"]
    assert verdicts[102]["supported_outcomes"] == ["RUMINATION"]
    assert verdicts[102]["media_match"] == "EXACT"
    assert set(verdicts[102]["flags"]) == {"CONFLICTING_EVIDENCE", "ADAPTED_STIMULUS"}
    assert verdicts[112]["usable"] and not verdicts[112]["supports_goal"]
    assert "NOT_SUPPORTING" in verdicts[112]["flags"]
    assert not verdicts[999]["usable"]

    plan = result["structured_response"]
    usable = {i for i, v in verdicts.items() if v["usable"]}
    cited = plan.activity.evidence_record_ids
    assert cited and set(cited) <= usable
