"""세션 그래프(v2)의 배선을 LLM 없이 확인한다.

intake 자리에 정해 둔 상황 읽기 · 목표 추론을 내는 대역을, Planner 자리에 정해 둔 계획을 내는 대역을
넣고 interrupt(intake_ask · measure_pre · run_session · measure_post · confirm · measure_final)를
대본으로 재개한다. 개념 정의 검색도 대역이다(DB 풀이 없으면 원래도 빈 목록).
"""

import asyncio

import pytest
from langgraph.graph import END
from langgraph.types import Command

from app.agent.checkpoint import memory_checkpointer
from app.agent.context import AgentContext
from app.agent.graph import build_graph
from app.agent.intake_rules import DEFAULT_CHOICES, FOLLOWUP_TEXT, GoalInference, StateReading
from app.agent.nodes.intake import route_after_ask, route_after_context, route_after_read
from app.agent.nodes.planner import ActivityChoice, PlanRationale, PlanRejected, check_plan
from app.agent.nodes.replanner import NextStepChoice, next_step_options, route_after_confirm
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
from app.schemas.care import CareGoal, CareRequest, Place, Setting, environment_for
from app.schemas.evidence import InterventionOrder, Outcome
from app.schemas.intake import (
    GoalSource,
    IntakeEndReason,
    IntakeQuestion,
    IntakeTurn,
    QuestionSlot,
    StateOptionDraft,
    StateQuestionDraft,
)
from app.schemas.measure import InstrumentId, Measurement, Timepoint

CONFIG = {"configurable": {"thread_id": "session-1"}}


def plan(protocol_id: str, minutes: int = 5) -> CarePlan:
    return CarePlan(
        activity=PlannedActivity(protocol_id=protocol_id, minutes=minutes), rationale="-"
    )


BREATHING = plan("PR-06", 5)  # S2 · NO_INDUCTION · 5–20분
PHOTO = plan("PR-01", 6)  # S2 · POST_STRESS/POST_FATIGUE · 5–10분
S2_INTAKE = IntakeResult(
    goal=CareGoal.S2, order_condition=InterventionOrder.PRE_STRESS, minutes=15, place=Place.INDOOR
)


def reading(**update) -> StateReading:
    base = {
        "safety": False,
        "relevance": "RELEVANT",
        "restate_text": None,
        "order_condition": None,
        "event": None,
        "rumination_cue": False,
        "stress_fatigue_ambiguous": False,
        "cues": [],
        "constraints": [],
        "setting": None,
        "earphones": None,
        "minutes": None,
        "summary": "-",
        "question": None,
    }
    return StateReading(**(base | update))


def inference(goal: CareGoal, secondary: CareGoal | None = None, **update) -> GoalInference:
    base = {
        "has_cues": True,
        "goal": goal,
        "secondary_goal": secondary,
        "reason": "-",
    }
    return GoalInference(**(base | update))


ASK_TIMING = StateQuestionDraft(
    followup=False,
    text="방금 있었던 일 때문인가요, 곧 있을 일 때문인가요?",
    options=[
        StateOptionDraft(label="방금 있었던 일", order_condition=InterventionOrder.POST_STRESS),
        StateOptionDraft(label="곧 있을 일", order_condition=InterventionOrder.PRE_STRESS),
    ],
)
ASK_CAUSE = StateQuestionDraft(
    followup=True,
    text="오래 집중해야 하는 일이 많았나요, 신경 쓰이는 일이 있었나요?",
    options=[
        StateOptionDraft(
            label="오래 집중",
            order_condition=InterventionOrder.POST_FATIGUE,
            goal_hint=[CareGoal.S3],
        ),
        StateOptionDraft(
            label="신경 쓰이는 일",
            order_condition=InterventionOrder.POST_STRESS,
            goal_hint=[CareGoal.S2],
        ),
    ],
)
# 발표 앞 · 집 · 15분이 문장에 다 있어 질문 없이 측정으로 간다
S2_READING = reading(
    order_condition=InterventionOrder.PRE_STRESS,
    setting=Setting.HOME,
    minutes=15,
    cues=["긴장돼요"],
)
S2_INTAKE = IntakeResult(
    goal=CareGoal.S2, order_condition=InterventionOrder.PRE_STRESS, minutes=15, place=Place.INDOOR
)


class FakeIntaker:
    """상황 읽기는 readings를 차례로(마지막 것을 반복), 목표 추론은 정해 둔 값을 낸다."""

    def __init__(self, *readings: StateReading, goal: GoalInference):
        self.readings = list(readings)
        self.goal = goal
        self.read_turns: list[int] = []  # read 호출마다 넘어온 질문·답 수
        self.goal_calls = 0

    async def read(self, request, turns):
        self.read_turns.append(len(turns))
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    async def infer_goal(self, request, intake):
        self.goal_calls += 1
        return self.goal


def graph_with(planner, intaker):
    return build_graph(planner, intaker, memory_checkpointer())


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

    async def explain(self, state, choice, protocol, evidence, problems=()):
        return PlanRationale.text("-")


class FakeReplanner:
    """다음 단계 추천을 차례로 낸다(예외면 던진다). recommend 호출마다 받은 규칙 제안을 남긴다."""

    def __init__(self, *outputs: NextStepChoice | BaseException):
        self.outputs = list(outputs)
        self.rules: list[ReplanProposal] = []

    async def recommend(self, state, rule):
        self.rules.append(rule)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return output


def run(graph, value):
    return asyncio.run(graph.ainvoke(value, CONFIG, context=AgentContext()))


def sam(valence: int, arousal: int) -> dict:
    return {"INS-SAM": {"valence": valence, "arousal": arousal}}


def mfvas(fatigue: int, concentration: int, energy: int) -> dict:
    return {"INS-MFVAS": {"fatigue": fatigue, "concentration": concentration, "energy": energy}}


def anchor(timepoint: Timepoint, round_no: int) -> list[Measurement]:
    sam = {"instrument_id": InstrumentId.SAM, "answers": {}, "scores": {}}
    return [Measurement(timepoint=timepoint, round=round_no, **sam)]


STAI6 = {"calm": 2, "tense": 3, "upset": 2, "relaxed": 2, "content": 2, "worried": 3}


def test_session_loops_until_user_ends(caplog):
    planner = FakePlanner(BREATHING)
    graph = graph_with(planner, FakeIntaker(S2_READING, goal=inference(CareGoal.S2)))

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
    assert session["content"]["name"] == "주기적 한숨 호흡 5분"
    assert session["content"]["steps"][0]["minutes"] == 5  # 계획한 시간으로 맞춘다
    assert out["pre"][1].scores == {"total": 56.67}  # 역채점 후 (3,3,2,3,3,3) 합 17 × 20/6

    # 활동 끝 → measure_post(SAM만)
    out = run(graph, Command(resume={"completed": True, "notice_words": ["차분"]}))
    post = out["__interrupt__"][0].value
    assert post["measure"] == "POST" and post["round"] == 1
    assert [i["instrument_id"] for i in post["instruments"]] == ["INS-SAM"]

    # 사후 측정 → ③ 평가(각성 7→4) → ④ 제안 → 사용자 선택을 기다린다
    out = run(graph, Command(resume=sam(5, 4)))
    assert out["evaluation"].deltas == {"valence": 2.0, "arousal": -3.0}
    assert out["evaluation"].improved is True
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "END"

    # 추가 세션(한 번 더)에 동의 → Planner로 돌아가 활동을 다시 고르지 않고 같은 활동을 한다.
    # 다음 라운드의 사전값은 이번 사후값이다
    out = run(graph, Command(resume={"next_step": "ADDITIONAL_SESSION"}))
    again = out["__interrupt__"][0].value["plan"]
    assert again["activity"]["protocol_id"] == "PR-06"
    assert again["activity"]["minutes"] == 5
    assert [m.scores for m in out["round_pre"]] == [{"valence": 5.0, "arousal": 4.0}]

    # 2라운드: 중간에 멈춤 → 활동 변경 제안. 멈춘 활동은 한 번 더를 내놓지 않는다
    out = run(graph, Command(resume={"completed": False, "stopped_reason": "어지러움"}))
    out = run(graph, Command(resume=sam(4, 6)))
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "CHANGE_ACTIVITY"
    assert out["__interrupt__"][0].value["options"] == ["END", "CHANGE_ACTIVITY"]

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
    assert planner.calls == [(0, [])]  # 한 번 더는 LLM 선택(호출 1)을 건너뛴다

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
    assert values["rounds"][0].report.notice_words == ["차분"]  # 라운드마다 감정 단어가 남는다
    assert isinstance(values["evaluation"], OutcomeEvaluation)
    assert isinstance(values["proposal"], ReplanProposal)
    assert values["next_step"] is NextStep.END
    assert all(isinstance(r, Round) for r in values["rounds"])
    assert isinstance(values["post"][0], Measurement)
    assert isinstance(values["final"], Measurement)


def test_s3_measures_fatigue_every_round_and_ends_without_final():
    outdoors = reading(
        order_condition=InterventionOrder.POST_FATIGUE, setting=Setting.OUTDOOR, minutes=30
    )
    graph = graph_with(
        FakePlanner(plan("PR-11", 20)), FakeIntaker(outdoors, goal=inference(CareGoal.S3))
    )

    # 사전: SAM + 정신 피로 VAS(임시 도구). 둘 다 라운드마다 다시 재므로 round_pre에 같이 들어간다
    out = run(graph, {"request": CareRequest(text="하루 종일 집중했더니 머리가 멍해요")})
    pre = out["__interrupt__"][0].value
    assert [i["instrument_id"] for i in pre["instruments"]] == ["INS-SAM", "INS-MFVAS"]
    assert pre["instruments"][1]["scale"]["max"] == 100
    out = run(graph, Command(resume={**sam(4, 5), **mfvas(80, 70, 20)}))
    assert [m.instrument_id for m in out["round_pre"]] == [InstrumentId.SAM, InstrumentId.MFVAS]

    # 사후: 같은 두 도구. 피로 80 → 45로 줄어 개선 → 종료 제안
    out = run(graph, Command(resume={"completed": True}))
    post = out["__interrupt__"][0].value
    assert post["measure"] == "POST" and post["round"] == 1
    assert [i["instrument_id"] for i in post["instruments"]] == ["INS-SAM", "INS-MFVAS"]
    out = run(graph, Command(resume={**sam(6, 5), **mfvas(45, 40, 55)}))
    assert out["evaluation"].deltas == {
        "valence": 2.0,
        "arousal": 0.0,
        "fatigue": -35.0,
        "concentration": -30.0,
        "energy": 35.0,
    }
    assert out["evaluation"].improved is True
    assert out["__interrupt__"][0].value["proposal"]["recommended"] == "END"

    # 종료: MFVAS는 라운드마다 쟀으니 measure_final 없이 끝난다. 마지막 사후값이 종료값이다
    out = run(graph, Command(resume={"next_step": "END"}))
    assert "__interrupt__" not in out and "final" not in out
    assert out["rounds"][-1].post[1].scores["fatigue"] == 45.0


def answer(question_id: str, **values) -> Command:
    return Command(resume={"question_id": question_id, "answer": values})


def test_intake_asks_timing_then_environment_then_measures(caplog):
    # 발표 뒤 반복 사고 · 장소 모름 → 시점 질문 → 목표 추론(S1 + S2) → 환경 질문 → 측정
    intaker = FakeIntaker(
        reading(question=ASK_TIMING, rumination_cue=True, cues=["계속 생각나요"]),
        goal=inference(CareGoal.S1, CareGoal.S2),
    )
    graph = graph_with(FakePlanner(), intaker)
    out = run(graph, {"request": CareRequest(text="발표 끝났는데 계속 생각나요")})
    q1 = out["__interrupt__"][0].value
    assert q1["type"] == "intake_question" and q1["question_id"] == "q1"
    assert q1["slot"] == "state" and q1["turn"] == 1 and q1["max_turns"] == 3
    assert q1["groups"][0]["options"][-1] == {"label": "잘 모르겠어요", "set": {}}

    out = run(graph, answer("q1", order_condition="POST_STRESS"))
    q2 = out["__interrupt__"][0].value
    assert q2["slot"] == "context" and q2["question_id"] == "q2" and not q2["allow_free_text"]
    # S1 · 스트레스 뒤는 20분이면 PR-21 · PR-14도 후보라 시간도 묻는다. 이어폰은 single
    assert [g["key"] for g in q2["groups"]] == ["setting", "earphones", "minutes"]
    assert [o["set"] for o in q2["groups"][1]["options"]] == [
        {"earphones": True},
        {"earphones": False},
    ]
    assert intaker.read_turns == [0]  # 칩 답은 다시 읽지 않는다
    assert intaker.goal_calls == 1

    out = run(graph, answer("q2", setting="OFFICE_CAFE", earphones=False))
    pre = out["__interrupt__"][0].value
    assert pre["measure"] == "PRE"
    assert [i["instrument_id"] for i in pre["instruments"]] == ["INS-SAM", "INS-BSRI"]
    intake = out["intake"]
    assert (intake.goal, intake.secondary_goal, intake.goal_source) == (
        CareGoal.S1,
        CareGoal.S2,
        GoalSource.INFERRED,
    )
    assert intake.target_outcomes == [Outcome.RUMINATION]
    assert [e.paper_id for e in intake.goal_evidence] == ["P12", "N26", "N25", "N04", "N07", "N01"]
    assert intake.order_condition is InterventionOrder.POST_STRESS
    assert intake.setting is Setting.OFFICE_CAFE and intake.place is Place.INDOOR
    assert intake.earphones is False and intake.environment.audio is False
    assert intake.missing_slots == ["minutes"]
    assert intake.end_reason is IntakeEndReason.SUFFICIENT
    assert [t.question_id for t in out["intake_turns"]] == ["q1", "q2"]

    assert not [r for r in caplog.records if "Blocked deserialization" in r.getMessage()]
    values = asyncio.run(graph.aget_state(CONFIG)).values
    assert isinstance(values["intake"], IntakeResult)
    assert all(isinstance(t, IntakeTurn) for t in values["intake_turns"])
    assert values["intake_turns"][0].slot is QuestionSlot.STATE
    assert values["intake"].setting is Setting.OFFICE_CAFE
    assert values["intake"].end_reason is IntakeEndReason.SUFFICIENT


def test_free_text_rereads_then_followup_then_goal_hint():
    intaker = FakeIntaker(
        reading(question=ASK_TIMING),
        reading(
            question=ASK_CAUSE, stress_fatigue_ambiguous=True, setting=Setting.HOME, minutes=10
        ),
        goal=inference(CareGoal.S2, has_cues=False),
    )
    graph = graph_with(FakePlanner(), intaker)
    run(graph, {"request": CareRequest(text="요즘 너무 지치고 짜증나요")})
    out = run(graph, Command(resume={"question_id": "q1", "text": "그냥 계속 그래요"}))
    q2 = out["__interrupt__"][0].value
    assert q2["slot"] == "state_followup" and q2["question_id"] == "q2"
    assert q2["groups"][0]["options"][0]["set"] == {
        "order_condition": "POST_FATIGUE",
        "goal_hint": ["S3"],
    }
    assert intaker.read_turns == [0, 1]  # 글 답이라 한 번 더 읽었다

    out = run(graph, answer("q2", order_condition="POST_FATIGUE", goal_hint=["S3"]))
    # 목표 단서 없음 → 재질문의 목표 단서 S3. 집 · 10분이라 환경 질문 없이 측정
    assert out["__interrupt__"][0].value["measure"] == "PRE"
    intake = out["intake"]
    assert intake.goal is CareGoal.S3 and intake.goal_source is GoalSource.INFERRED
    assert intake.order_condition is InterventionOrder.POST_FATIGUE


def test_two_unknown_answers_start_with_default_goal_without_inference():
    intaker = FakeIntaker(
        reading(question=ASK_TIMING, setting=Setting.HOME, minutes=5),
        goal=inference(CareGoal.S1),
    )
    graph = graph_with(FakePlanner(), intaker)
    run(graph, {"request": CareRequest(text="그냥 그래요")})
    out = run(graph, answer("q1"))  # 잘 모르겠어요 → 읽기 없이 고정 재질문
    q2 = out["__interrupt__"][0].value
    assert q2["slot"] == "state_followup" and q2["text"] == FOLLOWUP_TEXT
    assert q2["groups"][0]["options"][-1]["label"] == "잘 모르겠어요"
    assert intaker.read_turns == [0]

    out = run(graph, answer("q2"))  # 또 잘 모르겠어요
    assert out["__interrupt__"][0].value["measure"] == "PRE"
    intake = out["intake"]
    assert intake.goal in DEFAULT_CHOICES and intake.goal_source is GoalSource.DEFAULT
    assert intaker.goal_calls == 0  # 추론을 부르지 않는다


def test_safety_signal_in_first_input_or_free_text_ends_session():
    # 처음 입력에서
    intaker = FakeIntaker(reading(safety=True), goal=inference(CareGoal.S2))
    graph = graph_with(FakePlanner(), intaker)
    out = run(graph, {"request": CareRequest(text="다 끝내고 싶어요")})
    assert "__interrupt__" not in out and out["ended"] is SessionEnd.SAFETY
    assert out["intake"].end_reason is IntakeEndReason.SAFETY
    assert "pre" not in out and intaker.goal_calls == 0

    # 시점 질문의 글 답에서 — 다시 읽을 때 ①이 본다
    intaker = FakeIntaker(
        reading(question=ASK_TIMING), reading(safety=True), goal=inference(CareGoal.S2)
    )
    graph = graph_with(FakePlanner(), intaker)
    run(graph, {"request": CareRequest(text="요즘 너무 힘들어요")})
    out = run(graph, Command(resume={"question_id": "q1", "text": "살 이유를 모르겠어요"}))
    assert "__interrupt__" not in out and out["ended"] is SessionEnd.SAFETY
    assert intaker.read_turns == [0, 1] and intaker.goal_calls == 0


def test_off_topic_input_loops_restate_then_starts_budget():
    off = reading(relevance="OFF_TOPIC", restate_text="점심 고민 중이시군요! 오늘 하루는 어땠어요?")
    intaker = FakeIntaker(
        off,
        off,
        reading(question=ASK_TIMING),
        goal=inference(CareGoal.S2),
    )
    graph = graph_with(FakePlanner(), intaker)
    out = run(graph, {"request": CareRequest(text="점심 뭐 먹지")})
    r1 = out["__interrupt__"][0].value
    assert r1["slot"] == "restate" and r1["question_id"] == "r1" and r1["turn"] == 0
    assert r1["groups"] == [] and r1["skip_label"] == "그냥 시작할게요"

    out = run(graph, Command(resume={"question_id": "r1", "text": "ㅋㅋㅋ"}))
    assert out["__interrupt__"][0].value["question_id"] == "r2"  # 또 되묻는다

    out = run(graph, Command(resume={"question_id": "r2", "text": "회의 끝나고 좀 지쳤어요"}))
    q1 = out["__interrupt__"][0].value
    assert q1["slot"] == "state" and q1["question_id"] == "q1" and q1["turn"] == 1
    assert intaker.read_turns == [0, 1, 2]


def test_restate_skip_starts_with_default_goal_without_inference():
    off = reading(relevance="OFF_TOPIC", restate_text="오늘 하루는 어땠어요?")
    intaker = FakeIntaker(off, goal=inference(CareGoal.S1))
    graph = graph_with(FakePlanner(), intaker)
    run(graph, {"request": CareRequest(text="asdf", setting=Setting.HOME, minutes_available=5)})
    out = run(graph, Command(resume={"question_id": "r1", "skip": True}))
    assert out["__interrupt__"][0].value["measure"] == "PRE"
    intake = out["intake"]
    assert intake.goal in DEFAULT_CHOICES and intake.goal_source is GoalSource.DEFAULT
    assert intaker.goal_calls == 0 and intaker.read_turns == [0]
    assert out["intake_turns"][0].skipped


def test_no_cues_starts_with_default_goal_and_bad_answers_are_rejected():
    intaker = FakeIntaker(
        reading(setting=Setting.HOME, minutes=5), goal=inference(CareGoal.S1, has_cues=False)
    )
    graph = graph_with(FakePlanner(), intaker)
    out = run(graph, {"request": CareRequest(text="그냥 그래요")})
    assert out["__interrupt__"][0].value["measure"] == "PRE"
    assert out["intake"].goal in DEFAULT_CHOICES
    assert out["intake"].goal_source is GoalSource.DEFAULT

    graph = graph_with(
        FakePlanner(), FakeIntaker(reading(question=ASK_TIMING), goal=inference(CareGoal.S2))
    )
    run(graph, {"request": CareRequest(text="…")})
    with pytest.raises(ValueError):  # 시점 질문에 목표 단서를 실을 수 없다
        run(graph, answer("q1", goal_hint=["S1"]))


def test_request_goal_chip_skips_inference():
    intaker = FakeIntaker(S2_READING, goal=inference(CareGoal.S1))
    graph = graph_with(FakePlanner(), intaker)
    out = run(graph, {"request": CareRequest(text="…", goal=CareGoal.S3, minutes_available=10)})
    assert out["intake"].goal is CareGoal.S3 and out["intake"].goal_source is GoalSource.CHIP
    assert intaker.goal_calls == 0  # 추론을 부르지 않는다
    assert out["intake"].minutes == 10  # 요청 칩 > 문장


def test_plan_is_retried_once_with_problems_then_rejected():
    invalid = plan("PR-09", 3)  # S1 프로토콜을 S2 세션에
    planner = FakePlanner(invalid, invalid)
    graph = graph_with(planner, FakeIntaker(S2_READING, goal=inference(CareGoal.S2)))
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
    assert any("최대 5" in p for p in check_plan(activity("PR-06", 25), state))
    assert any("최소 5" in p for p in check_plan(activity("PR-06", 3), state))
    assert any("가용 시간 15분" in p for p in check_plan(activity("PR-07", 20), state))
    # PR-01은 POST_STRESS·POST_FATIGUE만 허용 — PRE_STRESS 사용자에게는 맞지 않는다
    assert any("허용 상태" in p for p in check_plan(PHOTO.activity, state))
    post_stress = {
        "intake": S2_INTAKE.model_copy(update={"order_condition": InterventionOrder.POST_STRESS})
    }
    assert check_plan(PHOTO.activity, post_stress) == []
    # 활동 변경을 골랐으면 바꾸기로 한 프로토콜을 다시 넣을 수 없다
    changed = Round(
        plan=BREATHING,
        report=SessionReport(completed=False),
        pre=anchor(Timepoint.PRE, 0),
        post=anchor(Timepoint.POST, 1),
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S2, round=1, deltas={}, improved=False, completed=False
        ),
        next_step=NextStep.CHANGE_ACTIVITY,
    )
    with_round = {**state, "rounds": [changed]}
    assert check_plan(BREATHING.activity, with_round) == [
        "PR-06는 이번 세션에서 바꾸기로 한 활동이다"
    ]
    assert check_plan(activity("PR-07", 9), with_round) == []
    # 두 번 바꿨으면 둘 다 — 3라운드에 1라운드 활동이 돌아오지 않는다
    changed_again = changed.model_copy(update={"plan": plan("PR-07", 9)})
    twice = {**state, "rounds": [changed, changed_again]}
    assert check_plan(BREATHING.activity, twice) == ["PR-06는 이번 세션에서 바꾸기로 한 활동이다"]
    assert check_plan(activity("PR-19", 5), twice) == []


def test_check_plan_follows_the_environment_and_what_the_search_relaxed():
    cafe = IntakeResult(
        goal=CareGoal.S1, minutes=10, environment=environment_for(Setting.OFFICE_CAFE, False)
    )
    state = {"intake": cafe}
    assert check_plan(activity("PR-09", 3), state) == [
        "PR-09는 지금 환경에서 하기 어렵다(AUDIO, QUIET)"
    ]
    assert check_plan(activity("PR-09", 3), state, relaxed=("environment",)) == []
    assert check_plan(activity("PR-15"), state) == []
    pre_stress = {"intake": S2_INTAKE}
    assert any("허용 상태" in p for p in check_plan(PHOTO.activity, pre_stress))
    assert check_plan(PHOTO.activity, pre_stress, relaxed=("order",)) == []


BSRI = {f"item{i}": 60 for i in range(1, 9)}


def test_change_is_not_offered_when_no_other_activity_fits():
    # S1 · 이어폰 없는 카페 · 5분 — 후보가 PR-15 하나라 활동을 바꿀 수 없다
    cafe = reading(
        order_condition=InterventionOrder.POST_STRESS,
        setting=Setting.OFFICE_CAFE,
        earphones=False,
        minutes=5,
    )
    graph = graph_with(
        FakePlanner(plan("PR-15", 5)), FakeIntaker(cafe, goal=inference(CareGoal.S1))
    )
    run(graph, {"request": CareRequest(text="회의 끝나고 같은 말이 계속 떠올라요")})
    out = run(graph, Command(resume={**sam(4, 6), "INS-BSRI": BSRI}))
    assert out["__interrupt__"][0].value["plan"]["activity"]["protocol_id"] == "PR-15"
    run(graph, Command(resume={"completed": True}))
    out = run(graph, Command(resume=sam(4, 6)))  # 쾌가 그대로 — 나아지지 않았다
    confirm = out["__interrupt__"][0].value
    assert confirm["options"] == ["END", "ADDITIONAL_SESSION"]
    assert confirm["proposal"]["recommended"] == "END"
    assert (
        confirm["proposal"]["reason"] == "사전보다 나아지지 않았다 — 지금 여건에서 바꿀 활동이 없다"
    )


def test_options_leave_out_every_changed_activity_and_a_repeat_after_a_stop():
    # S1 · 집 · 10분 후보는 PR-09 · PR-15뿐 — PR-09를 바꾸고 PR-15를 하는 중이면 바꿀 활동이 없다
    s1 = IntakeResult(
        goal=CareGoal.S1,
        order_condition=InterventionOrder.POST_STRESS,
        minutes=10,
        environment=environment_for(Setting.HOME, None),
    )
    first = Round(
        plan=plan("PR-09", 3),
        report=SessionReport(completed=True),
        pre=anchor(Timepoint.PRE, 0),
        post=anchor(Timepoint.POST, 1),
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S1, round=1, deltas={}, improved=False, completed=True
        ),
        next_step=NextStep.CHANGE_ACTIVITY,
    )
    state = {
        "request": CareRequest(text="-"),
        "intake": s1,
        "plan": plan("PR-15", 3),
        "report": SessionReport(completed=True),
        "rounds": [first],
    }
    assert next_step_options(state, 2) == [NextStep.END, NextStep.ADDITIONAL_SESSION]
    # 첫 라운드라면 PR-09로 바꿀 수 있다. 중간에 멈췄으면 같은 활동을 한 번 더 내놓지 않는다
    stopped = {**state, "rounds": [], "report": SessionReport(completed=False)}
    assert next_step_options(stopped, 1) == [NextStep.END, NextStep.CHANGE_ACTIVITY]


def test_third_round_offers_only_to_end():
    graph = graph_with(
        FakePlanner(BREATHING, BREATHING, BREATHING),
        FakeIntaker(S2_READING, goal=inference(CareGoal.S2)),
    )
    run(graph, {"request": CareRequest(text="발표 앞두고 긴장돼요")})
    run(graph, Command(resume={**sam(3, 7), "INS-STAI6": STAI6}))
    for round_no in (1, 2, 3):
        run(graph, Command(resume={"completed": True}))
        confirm = run(graph, Command(resume=sam(3, 7)))["__interrupt__"][0].value
        if round_no < 3:
            assert confirm["options"] == ["END", "CHANGE_ACTIVITY", "ADDITIONAL_SESSION"]
            run(graph, Command(resume={"next_step": "ADDITIONAL_SESSION"}))
    assert confirm["options"] == ["END"]
    assert confirm["proposal"] == {
        "recommended": "END",
        "reason": "라운드 3회를 채웠다",
        "options": ["END"],
        "rule_recommended": "END",
        "by_llm": False,
    }


def test_the_replanner_recommends_on_the_confirm_screen():
    choice = NextStepChoice(
        recommended=NextStep.ADDITIONAL_SESSION, reason="긴장이 내려갔어요. 한 번 더 해 볼까요?"
    )
    replanner = FakeReplanner(choice)
    intaker = FakeIntaker(S2_READING, goal=inference(CareGoal.S2))
    graph = build_graph(FakePlanner(BREATHING), intaker, memory_checkpointer(), replanner)
    run(graph, {"request": CareRequest(text="발표 앞두고 긴장돼요")})
    run(graph, Command(resume={**sam(3, 7), "INS-STAI6": STAI6}))
    run(graph, Command(resume={"completed": True}))
    confirm = run(graph, Command(resume=sam(5, 4)))["__interrupt__"][0].value
    assert confirm["proposal"] == {
        "recommended": "ADDITIONAL_SESSION",
        "reason": choice.reason,
        "options": ["END", "CHANGE_ACTIVITY", "ADDITIONAL_SESSION"],
        "rule_recommended": "END",  # 규칙은 나아졌으면 종료
        "by_llm": True,
    }
    assert [r.recommended for r in replanner.rules] == [NextStep.END]


def test_routes():
    question = IntakeQuestion(
        question_id="q1",
        slot=QuestionSlot.STATE,
        turn=1,
        text="?",
        groups=[
            {
                "key": "state",
                "select": "single",
                "options": [{"label": "a", "set": {}}, {"label": "b", "set": {}}],
            }
        ],
        allow_free_text=True,
    )
    assert route_after_read({"pending_question": question}) == "intake_ask"
    assert route_after_read({"pending_question": None}) == "intake_goal"
    assert route_after_read({"pending_question": None, "ended": SessionEnd.SAFETY}) == END
    state_chip = IntakeTurn(question_id="q1", slot=QuestionSlot.STATE, text="?", answer={})
    state_text = state_chip.model_copy(update={"answer": None, "free_text": "몰라요"})
    context = IntakeTurn(question_id="q2", slot=QuestionSlot.CONTEXT, text="?", answer={})
    assert route_after_ask({"intake_turns": [state_chip]}) == "intake_goal"
    assert route_after_ask({"intake_turns": [state_text]}) == "intake_read"
    assert route_after_ask({"intake_turns": [state_chip, context]}) == "intake_finish"
    assert route_after_ask({"intake_turns": [state_chip], "pending_question": question}) == (
        "intake_ask"
    )  # 잘 모르겠어요 뒤 고정 재질문
    skipped = IntakeTurn(question_id="r1", slot=QuestionSlot.RESTATE, text="?", skipped=True)
    restated = skipped.model_copy(update={"skipped": False, "free_text": "피곤해요"})
    assert route_after_ask({"intake_turns": [skipped]}) == "intake_goal"
    assert route_after_ask({"intake_turns": [restated]}) == "intake_read"
    assert route_after_context({"pending_question": question}) == "intake_ask"
    assert route_after_context({}) == "intake_finish"
    s2 = {"intake": S2_INTAKE}
    assert route_after_confirm({**s2, "next_step": NextStep.END}) == "measure_final"
    assert route_after_confirm({**s2, "next_step": NextStep.CHANGE_ACTIVITY}) == "plan"
    assert route_after_confirm({**s2, "next_step": NextStep.ADDITIONAL_SESSION}) == "plan"
    s1 = {"intake": IntakeResult(goal=CareGoal.S1), "next_step": NextStep.END}
    assert route_after_confirm(s1) == "measure_final"
    # S3의 MFVAS는 라운드마다 재므로 종료에 다시 묻지 않는다
    s3 = {"intake": IntakeResult(goal=CareGoal.S3), "next_step": NextStep.END}
    assert route_after_confirm(s3) == END
