"""get_care_protocols → search_evidence → validate_evidence → CarePlan 흐름을 대본 모델로 확인한다.

도구 본체가 부르는 hybrid.search · validation.validate를 fixture 기반 가짜로 바꿔 끼운다.
도구 출력(JSON)의 모양, 도구가 세션 값(목표 · 사용자 상태 · 시간 · 이미지 선호)을 PlannerContext에서
읽는지, 검증 결과의 usable·flags, 계획의 evidence_record_ids가 usable 근거에 속하는지를 본다.
대본은 gpt-4o-mini 실측 궤적(2026-10-26)과 같은 모양이다 — 세션 값은 인자로 넘기지 않는다.
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import protocols
from app.agent.context import AgentContext
from app.agent.nodes.planner import build_planner_agent, check_plan, planner_context
from app.agent.prompts import build_planner_input
from app.agent.state import IntakeResult
from app.schemas.care import CareGoal, CareRequest, ContentType, UserProfile
from app.schemas.evidence import InterventionOrder
from app.schemas.retrieval import Check
from tests.agent import fakes

# AI 생성 이미지를 고른 사용자의 S1(반추) 세션. 20분이 있어 PR-21(실내 자연 이미지 + 15분 명상,
# EXPERIMENTAL)을 고르는 대본이다. medium=STATIC_IMAGE로 찾고, 검증은 세션의 이미지 선호로
# 제공 매체를 [AI_IMAGE, MINDFULNESS]로 본다
REQUEST = CareRequest(
    text="같은 생각이 계속 돌아요", profile=UserProfile(content_type=ContentType.AI_IMAGE)
)
INTAKE = IntakeResult(goal=CareGoal.S1, order_condition=InterventionOrder.POST_STRESS, minutes=20)
STATE = {"request": REQUEST, "intake": INTAKE}
SEARCH_ARGS = {
    "query": "nature images rumination",
    "medium": "STATIC_IMAGE",
    "intervention_order": "POST_STRESS",
    "outcomes": ["RUMINATION"],
}
VALIDATE_ARGS = {
    "evidence_record_ids": [102, 112, 999],  # P24-E1 RESULTS · P33-E1 RESULTS · 없는 ID
    "protocol_id": "PR-21",
}
PLAN = {
    "activity": {"protocol_id": "PR-21", "minutes": 15, "evidence_record_ids": [102]},
    "rationale": "P24-E1 자연 이미지 후 상태 반추 감소. AI 생성 이미지는 연구 자극과 다르다",
}


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


@pytest.fixture
def fake_backends(monkeypatch):
    monkeypatch.setattr("app.retrieval.hybrid.search", fakes.fake_search)
    monkeypatch.setattr("app.agent.validation.validate", fakes.fake_validate)


def run_agent(*calls: AIMessage, state: dict = STATE) -> dict:
    model = fakes.ScriptedChatModel(messages=iter(calls))
    agent = build_planner_agent(model)
    message = HumanMessage(build_planner_input(state, []))
    context = planner_context(AgentContext(), state)
    return asyncio.run(agent.ainvoke({"messages": [message]}, context=context))


def tool_messages(result: dict) -> list[ToolMessage]:
    # 구조화 출력(CarePlan)도 ToolStrategy가 ToolMessage로 남긴다
    return [m for m in result["messages"] if isinstance(m, ToolMessage)]


def test_planner_calls_tools_and_cites_usable_evidence(fake_backends):
    result = run_agent(
        _tool_call("get_care_protocols", {}, "0"),
        _tool_call("search_evidence", SEARCH_ARGS, "1"),
        _tool_call("validate_evidence", VALIDATE_ARGS, "2"),
        _tool_call("CarePlan", PLAN, "3"),
    )
    messages = tool_messages(result)
    assert [m.name for m in messages] == [
        "get_care_protocols",
        "search_evidence",
        "validate_evidence",
        "CarePlan",
    ]

    # 세션(S1 · POST_STRESS · 20분)으로 거른 후보. 주 목표가 앞, 같은 쪽은 반추 등급 순
    protocols = json.loads(messages[0].content)
    assert [p["protocol_id"] for p in protocols] == ["PR-09", "PR-15", "PR-21", "PR-14"]
    assert {"grades", "duration_minutes", "intervention_order", "composition"} <= set(protocols[0])

    hits = json.loads(messages[1].content)
    assert 1 <= len(hits) <= 5
    assert {h["evidence_record_id"] for h in hits} == {101, 102, 111, 112}
    assert all(h["metadata"]["media"] == ["STATIC_IMAGE"] for h in hits)
    assert all(any(r["outcome"] == "RUMINATION" for r in h["metadata"]["results"]) for h in hits)
    assert {"paper", "excerpt", "section", "score"} <= set(hits[0])

    verdicts = {v["evidence_record_id"]: v for v in json.loads(messages[2].content)}
    assert list(verdicts) == [102, 112, 999]
    p24 = verdicts[102]
    assert [c["check"] for c in p24["checks"]] == [c.value for c in Check]
    assert p24["usable"] and p24["supports_goal"]
    assert p24["supported_outcomes"] == ["RUMINATION"]
    # 제공 매체는 세션의 이미지 선호로 [AI_IMAGE, MINDFULNESS] — 연구 [STATIC_IMAGE]와 일부만 겹친다
    assert p24["media_match"] == "PARTIAL"
    assert set(p24["flags"]) == {"CONFLICTING_EVIDENCE", "ADAPTED_STIMULUS"}
    intervention = next(c for c in p24["checks"] if c["check"] == "INTERVENTION_MATCH")
    assert "AI_IMAGE" in intervention["reason"] and "POST_STRESS" in intervention["reason"]
    assert verdicts[112]["usable"] and not verdicts[112]["supports_goal"]
    assert "NOT_SUPPORTING" in verdicts[112]["flags"]
    assert not verdicts[999]["usable"]

    plan = result["structured_response"]
    usable = {i for i, v in verdicts.items() if v["usable"]}
    cited = plan.activity.evidence_record_ids
    assert cited and set(cited) <= usable
    assert check_plan(plan, STATE) == []


def test_tool_mistakes_go_back_to_the_model(fake_backends):
    """빈 ID · 검수되지 않은 프로토콜 · 0건 검색은 예외가 아니라 고칠 방법을 담은 ToolMessage다."""
    result = run_agent(
        _tool_call("validate_evidence", {**VALIDATE_ARGS, "evidence_record_ids": []}, "0"),
        _tool_call("validate_evidence", {**VALIDATE_ARGS, "protocol_id": "PR-99"}, "1"),
        _tool_call("search_evidence", {**SEARCH_ARGS, "medium": "VR"}, "2"),
        _tool_call("CarePlan", PLAN, "3"),
    )
    empty_ids, unknown_protocol, no_hits, _ = tool_messages(result)
    assert empty_ids.status == "error" and "evidence_record_ids" in empty_ids.content
    assert "PR-99는 검수된 프로토콜이 아니다" in unknown_protocol.content
    assert no_hits.content.startswith("0건")
    assert result["structured_response"].activity.protocol_id == "PR-21"


def test_demo_s2_session_offers_breathing_first():
    """발표 시나리오(TEAM.md): '발표 앞두고 긴장돼요' → S2 · PRE_STRESS · 기본 10분 → PR-06.

    플래너는 목록 맨 앞을 고른다. PR-03(스트레스 전 예방)은 이름이 상황과 맞아 보여도 S2 1차 지표
    등급이 —라 뒤에 온다. JSON 등급이 바뀌어 이 테스트가 깨지면 발표 경로가 바뀐 것이다.
    """
    found = protocols.candidates(CareGoal.S2, order=InterventionOrder.PRE_STRESS, minutes=10)
    ids = [p.protocol_id for p in found]
    assert ids[0] == "PR-06"
    assert ids.index("PR-03") > ids.index("PR-06")


def test_get_care_protocols_says_so_when_nothing_fits():
    state = {"request": REQUEST, "intake": INTAKE.model_copy(update={"minutes": 1})}
    result = run_agent(
        _tool_call("get_care_protocols", {}, "0"), _tool_call("CarePlan", PLAN, "1"), state=state
    )
    assert "맞는 프로토콜이 없다" in tool_messages(result)[0].content
