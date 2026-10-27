"""세션 그래프(v2)의 배선을 LLM 없이 확인한다.

intake 자리에 정해 둔 IntakeResult를 내는 대역을, Planner 자리에 정해 둔 계획을 내는 대역을 넣고
interrupt 다섯 곳(measure_pre · run_session · measure_post · confirm · measure_final)을 대본으로
재개한다.
"""

import asyncio

import pytest
from langgraph.graph import END
from langgraph.types import Command

from app.agent.checkpoint import memory_checkpointer
from app.agent.context import AgentContext
from app.agent.graph import build_graph
from app.agent.nodes.intake import route_after_intake
from app.agent.nodes.planner import ActivityChoice, PlanRejected, check_plan
from app.agent.nodes.replanner import route_after_confirm
from app.agent.state import (
    ActivityContent,
    CarePlan,
    IntakeResult,
    NextStep,
    OutcomeEvaluation,
    PlannedActivity,
    ReplanProposal,
    Round,
    SessionEnd,
    SessionReport,
)
from app.schemas.care import CareGoal, CareRequest, Place
from app.schemas.evidence import InterventionOrder
from app.schemas.measure import InstrumentId, Measurement, Timepoint

CONFIG = {"configurable": {"thread_id": "session-1"}}


def plan(protocol_id: str, minutes: int = 5) -> CarePlan:
    return CarePlan(
        activity=PlannedActivity(protocol_id=protocol_id, minutes=minutes), rationale="-"
    )


BREATHING = plan("PR-06", 5)  # S2 · NO_INDUCTION · 5–20분
GROUNDING = plan("PR-17", 10)  # S2 · 허용 상태 제한 없음 · 10분
PHOTO = plan("PR-01", 6)  # S2 · POST_STRESS/POST_FATIGUE · 5–10분
S2_INTAKE = IntakeResult(
    goal=CareGoal.S2, order_condition=InterventionOrder.PRE_STRESS, minutes=15, place=Place.INDOOR
)


class FakeIntaker:
    def __init__(self, result: IntakeResult):
        self.result = result

    async def __call__(self, request: CareRequest) -> IntakeResult:
        return self.result


class FakePlanner:
    """계획의 활동을 차례로 고르고, choose 호출마다 (지난 라운드 수, 거부 사유)를 남긴다.

    시간은 코드가 정한다(protocols.planned_minutes). 근거 검색은 DB 풀이 없어 0건이다.
    """

    def __init__(self, *plans: CarePlan):
        self.plans = list(plans)
        self.calls: list[tuple[int, list[str]]] = []

    async def choose(self, state, candidates, problems):
        self.calls.append((len(state.get("rounds", [])), list(problems)))
        protocol_id = self.plans.pop(0).activity.protocol_id
        return ActivityChoice(protocol_id=protocol_id, search_query="-", reason="-")

    async def explain(self, state, choice, protocol, evidence):
        return "-"


def run(graph, value):
    return asyncio.run(graph.ainvoke(value, CONFIG, context=AgentContext()))


def sam(valence: int, arousal: int) -> dict:
    return {"INS-SAM": {"valence": valence, "arousal": arousal}}


STAI6 = {"calm": 2, "tense": 3, "upset": 2, "relaxed": 2, "content": 2, "worried": 3}


def test_session_loops_until_user_ends(caplog):
    planner = FakePlanner(BREATHING, GROUNDING)
    graph = build_graph(planner, FakeIntaker(S2_INTAKE), memory_checkpointer())

    # intake → measure_pre에서 멈춘다. S2라 SAM + STAI-6를 묻는다
    out = run(graph, {"request": CareRequest(text="발표 앞두고 긴장돼요")})
    pre = out["__interrupt__"][0].value
    assert pre["measure"] == "PRE" and pre["round"] == 0
    assert [i["instrument_id"] for i in pre["instruments"]] == ["INS-SAM", "INS-STAI6"]
    assert pre["instruments"][0]["items"][0]["id"] == "valence"

    # 사전 측정 → ① 계획 → ② 콘텐츠 준비 → FE 실행을 기다린다
    out = run(graph, Command(resume={**sam(3, 7), "INS-STAI6": STAI6}))
    session = out["__interrupt__"][0].value
    assert session["plan"]["activity"]["protocol_id"] == "PR-06"
    assert session["content"]["name"] == "5분 구조화 호흡(주기적 한숨·느린 호흡)"
    assert session["content"]["steps"][0]["minutes"] == 5  # 계획한 시간으로 맞춘다
    assert out["pre"][1].scores == {"total": 56.67}  # 역채점 후 (3,3,2,3,3,3) 합 17 × 20/6

    # 활동 끝 → measure_post(SAM만)
    out = run(graph, Command(resume={"completed": True, "notice_word": "차분"}))
    post = out["__interrupt__"][0].value
    assert post["measure"] == "POST" and post["round"] == 1
    assert [i["instrument_id"] for i in post["instruments"]] == ["INS-SAM"]

    # 사후 측정 → ③ 평가(각성 7→4) → ④ 제안 → 사용자 선택을 기다린다
    out = run(graph, Command(resume=sam(5, 4)))
    assert out["evaluation"].deltas == {"valence": 2.0, "arousal": -3.0}
    assert out["evaluation"].improved is True
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "END"

    # 추가 세션에 동의 → Planner로 돌아간다. 다음 라운드의 사전값은 이번 사후값이다
    out = run(graph, Command(resume={"next_step": "ADDITIONAL_SESSION"}))
    assert out["__interrupt__"][0].value["plan"]["activity"]["protocol_id"] == "PR-17"
    assert out["round_pre"].scores == {"valence": 5.0, "arousal": 4.0}

    # 2라운드: 중간에 멈춤 → 활동 변경 제안
    out = run(graph, Command(resume={"completed": False, "stopped_reason": "어지러움"}))
    out = run(graph, Command(resume=sam(4, 6)))
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "CHANGE_ACTIVITY"

    # 종료 → S2라 목표 척도(STAI-6)를 한 번 더 묻고 끝난다
    out = run(graph, Command(resume={"next_step": "END"}))
    final = out["__interrupt__"][0].value
    assert final["measure"] == "FINAL" and final["round"] == 2
    assert [i["instrument_id"] for i in final["instruments"]] == ["INS-STAI6"]
    out = run(graph, Command(resume={"INS-STAI6": {**STAI6, "tense": 1, "worried": 1}}))
    assert "__interrupt__" not in out
    assert out["final"].scores == {"total": 43.33}  # 합 13 × 20/6
    assert [r.next_step for r in out["rounds"]] == [NextStep.ADDITIONAL_SESSION, NextStep.END]
    assert [r.evaluation.improved for r in out["rounds"]] == [True, False]
    assert planner.calls == [(0, []), (1, [])]

    # 체크포인트에서 복원한 값이 원래 타입이다. checkpoint.STATE_TYPES에 빠진 타입이 있으면
    # 역직렬화가 막히고(로그) dict나 원시 값으로 돌아온다
    assert not [r for r in caplog.records if "Blocked deserialization" in r.getMessage()]
    values = asyncio.run(graph.aget_state(CONFIG)).values
    assert isinstance(values["request"], CareRequest)
    assert isinstance(values["intake"], IntakeResult)
    assert isinstance(values["pre"][0], Measurement)
    assert values["pre"][0].instrument_id is InstrumentId.SAM
    assert values["pre"][0].timepoint is Timepoint.PRE
    assert isinstance(values["plan"], CarePlan)
    assert isinstance(values["content"], ActivityContent)
    assert isinstance(values["report"], SessionReport)
    assert isinstance(values["evaluation"], OutcomeEvaluation)
    assert isinstance(values["proposal"], ReplanProposal)
    assert values["next_step"] is NextStep.END
    assert all(isinstance(r, Round) for r in values["rounds"])
    assert isinstance(values["final"], Measurement)


def test_s3_skips_goal_scale_and_has_no_improvement_rule():
    intake = IntakeResult(goal=CareGoal.S3, minutes=30, place=Place.OUTDOOR)
    graph = build_graph(FakePlanner(plan("PR-11", 20)), FakeIntaker(intake), memory_checkpointer())
    out = run(graph, {"request": CareRequest(text="하루 종일 집중했더니 머리가 멍해요")})
    assert [i["instrument_id"] for i in out["__interrupt__"][0].value["instruments"]] == ["INS-SAM"]
    run(graph, Command(resume=sam(4, 5)))
    run(graph, Command(resume={"completed": True}))
    out = run(graph, Command(resume=sam(6, 5)))
    assert out["evaluation"].improved is None
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "END"
    out = run(graph, Command(resume={"next_step": "END"}))
    assert "__interrupt__" not in out and "final" not in out


def test_intake_ends_session_without_goal_or_on_safety_signal():
    graph = build_graph(FakePlanner(), FakeIntaker(IntakeResult(goal=None)), memory_checkpointer())
    out = run(graph, {"request": CareRequest(text="그냥 힘들어요")})
    assert out["ended"] is SessionEnd.NO_GOAL and "__interrupt__" not in out
    assert out["intake"].missing_slots == ["goal", "minutes", "place"]

    # 요청의 칩 값이 있으면 분류 없이도 목표가 정해진다
    graph = build_graph(
        FakePlanner(BREATHING), FakeIntaker(IntakeResult(goal=None)), memory_checkpointer()
    )
    out = run(
        graph,
        {"request": CareRequest(text="그냥 힘들어요", goal=CareGoal.S2, minutes_available=10)},
    )
    assert out["intake"].goal is CareGoal.S2 and out["intake"].minutes == 10
    assert out["intake"].missing_slots == ["place"]
    assert out["__interrupt__"][0].value["measure"] == "PRE"

    safety = IntakeResult(goal=CareGoal.S2, safety=True, safety_note="자해 언급")
    graph = build_graph(FakePlanner(), FakeIntaker(safety), memory_checkpointer())
    out = run(graph, {"request": CareRequest(text="…")})
    assert out["ended"] is SessionEnd.SAFETY and "__interrupt__" not in out


def test_plan_is_retried_once_with_problems_then_rejected():
    invalid = plan("PR-09", 3)  # S1 프로토콜을 S2 세션에
    planner = FakePlanner(invalid, invalid)
    graph = build_graph(planner, FakeIntaker(S2_INTAKE), memory_checkpointer())
    run(graph, {"request": CareRequest(text="긴장돼요")})
    with pytest.raises(PlanRejected, match="S2 목표용이 아니다"):
        run(graph, Command(resume={**sam(3, 7), "INS-STAI6": STAI6}))
    assert planner.calls[0] == (0, [])
    assert planner.calls[1][1] == ["PR-09는 후보 목록에 없다", "PR-09는 S2 목표용이 아니다"]


def activity(protocol_id: str, minutes: int = 5):
    return plan(protocol_id, minutes).activity


def test_check_plan_rules():
    state = {"intake": S2_INTAKE}
    assert check_plan(BREATHING.activity, state) == []
    assert check_plan(activity("PR-99"), state) == ["PR-99는 검수된 프로토콜이 아니다"]
    assert "단계 모듈" in check_plan(activity("PR-16", 1), state)[0]
    assert any("최대 20" in p for p in check_plan(activity("PR-06", 25), state))
    assert any("최소 5" in p for p in check_plan(activity("PR-06", 3), state))
    assert any("가용 시간 15분" in p for p in check_plan(activity("PR-07", 20), state))
    # PR-01은 POST_STRESS·POST_FATIGUE만 허용 — PRE_STRESS 사용자에게는 맞지 않는다
    assert any("허용 상태" in p for p in check_plan(PHOTO.activity, state))
    post_stress = {
        "intake": S2_INTAKE.model_copy(update={"order_condition": InterventionOrder.POST_STRESS})
    }
    assert check_plan(PHOTO.activity, post_stress) == []
    # 활동 변경을 골랐으면 같은 프로토콜을 다시 넣을 수 없다
    changed = Round(
        plan=BREATHING,
        report=SessionReport(completed=False),
        pre=Measurement(
            instrument_id=InstrumentId.SAM, timepoint=Timepoint.PRE, round=0, answers={}, scores={}
        ),
        post=Measurement(
            instrument_id=InstrumentId.SAM, timepoint=Timepoint.POST, round=1, answers={}, scores={}
        ),
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S2, round=1, deltas={}, improved=False, completed=False
        ),
        next_step=NextStep.CHANGE_ACTIVITY,
    )
    with_round = {**state, "rounds": [changed]}
    assert any("같은 프로토콜" in p for p in check_plan(BREATHING.activity, with_round))
    assert check_plan(activity("PR-07", 15), with_round) == []


def test_routes():
    assert route_after_intake({"ended": SessionEnd.NO_GOAL}) == END
    assert route_after_intake({}) == "measure_pre"
    s2 = {"intake": S2_INTAKE}
    assert route_after_confirm({**s2, "next_step": NextStep.END}) == "measure_final"
    assert route_after_confirm({**s2, "next_step": NextStep.CHANGE_ACTIVITY}) == "plan"
    assert route_after_confirm({**s2, "next_step": NextStep.ADDITIONAL_SESSION}) == "plan"
    s3 = {"intake": IntakeResult(goal=CareGoal.S3), "next_step": NextStep.END}
    assert route_after_confirm(s3) == END
