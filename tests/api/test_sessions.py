"""세션 API — 대역 모델로 조립한 그래프에 HTTP로 흐름 · step · SSE · 오류를 확인한다.

intake · Planner 대역은 tests/agent/test_graph.py의 것을 쓴다(상황 읽기 · 목표 추론 대역).
"""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.checkpoint import memory_checkpointer
from app.agent.graph import build_graph
from app.api import sessions
from app.config import Settings
from app.main import add_cors
from app.schemas.care import CareGoal, Setting
from app.schemas.evidence import InterventionOrder
from tests.agent.test_graph import (
    ASK_TIMING,
    BREATHING,
    S2_READING,
    FakeIntaker,
    FakePlanner,
    inference,
    plan,
    reading,
    sam,
)

SSE = {"Accept": "text/event-stream"}
PRE_S2 = sam(3, 7)


def client_for(
    planner: FakePlanner | None = None, intaker: FakeIntaker | None = None
) -> TestClient:
    """기본은 발표 앞 · 집 · 15분이 문장에 다 있는 S2 — 질문 없이 사전 측정으로 간다."""
    app = FastAPI()
    app.include_router(sessions.router)
    planner = planner or FakePlanner(BREATHING)
    intaker = intaker or FakeIntaker(S2_READING, goal=inference(CareGoal.S2))
    app.state.session_graph = build_graph(planner, intaker, memory_checkpointer())
    app.state.db_pool = None
    return TestClient(app)


def asking_client() -> TestClient:
    """시점이 불명확한 문장 — 시점 질문(q1) → 목표 추론(S1 + S2) → 환경 질문(q2) → 사전 측정."""
    intaker = FakeIntaker(
        reading(question=ASK_TIMING, rumination_cue=True, cues=["계속 생각나요"]),
        goal=inference(CareGoal.S1, CareGoal.S2),
    )
    return client_for(intaker=intaker)


def events(response) -> list[tuple[str, dict]]:
    """SSE 본문 → [(event, data)]. 주석(: ping)은 뺀다."""
    out = []
    for frame in response.text.split("\n\n"):
        lines = [line for line in frame.splitlines() if line and not line.startswith(":")]
        if lines:
            fields = dict(line.split(": ", 1) for line in lines)
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def start(client: TestClient) -> str:
    return client.post("/sessions", json={"text": "발표 앞두고 긴장돼요"}).json()["session_id"]


def test_s2_session_runs_to_end_over_json():
    client = client_for()
    res = client.post("/sessions", json={"text": "발표 앞두고 긴장돼요"})
    assert res.status_code == 201
    body = res.json()
    sid = body["session_id"]
    assert (body["step"], body["node"]) == ("MEASURE", "measure_pre")
    assert [i["instrument_id"] for i in body["payload"]["instruments"]] == ["INS-SAM"]

    body = client.post(f"/sessions/{sid}/measure", json=PRE_S2).json()
    assert body["step"] == "RUN"
    assert body["payload"]["plan"]["activity"]["protocol_id"] == "PR-06"

    body = client.post(f"/sessions/{sid}/report", json={"completed": True}).json()
    assert (body["step"], body["payload"]["measure"]) == ("MEASURE", "POST")

    body = client.post(f"/sessions/{sid}/measure", json=sam(5, 4)).json()
    assert body["step"] == "CONFIRM"
    assert body["payload"]["proposal"]["recommended"] == "END"

    body = client.post(f"/sessions/{sid}/decision", json={"next_step": "END"}).json()
    assert (body["step"], body["payload"]["measure"]) == ("MEASURE", "FINAL")

    final = sam(5, 5)
    body = client.post(f"/sessions/{sid}/measure", json=final).json()
    assert (body["step"], body["node"]) == ("END", None)
    end = body["payload"]
    assert (end["reason"], end["goal"]) == ("DONE", "S2")
    assert [(r["protocol_id"], r["improved"], r["next_step"]) for r in end["rounds"]] == [
        ("PR-06", True, "END")
    ]
    # 시작·종료 SAM 차이는 기분 변화이며 S2 목표 점수로 해석하지 않는다.
    assert end["sam_change"]["instrument_id"] == "INS-SAM"
    assert end["sam_change"]["delta"] == {"valence": 2.0, "arousal": -2.0}
    assert client.get(f"/sessions/{sid}").json() == body


def test_s3_session_uses_sam_and_reports_no_goal_scale_change():
    outdoors = reading(
        order_condition=InterventionOrder.POST_FATIGUE, setting=Setting.OUTDOOR, minutes=30
    )
    client = client_for(
        FakePlanner(plan("PR-11", 20)), FakeIntaker(outdoors, goal=inference(CareGoal.S3))
    )
    sid = start(client)
    assert (
        client.post(f"/sessions/{sid}/measure", json=sam(4, 5)).json()[
            "step"
        ]
        == "RUN"
    )
    client.post(f"/sessions/{sid}/report", json={"completed": True})
    client.post(f"/sessions/{sid}/measure", json=sam(6, 5))

    body = client.post(f"/sessions/{sid}/decision", json={"next_step": "END"}).json()
    assert (body["step"], body["payload"]["measure"]) == ("MEASURE", "FINAL")
    end = client.post(f"/sessions/{sid}/measure", json=sam(7, 6)).json()
    change = end["payload"]["sam_change"]
    assert change["instrument_id"] == "INS-SAM"
    assert change["delta"] == {"valence": 3.0, "arousal": 1.0}


def test_sse_streams_progress_then_the_same_result():
    client = client_for()
    res = client.post("/sessions", json={"text": "발표 앞두고 긴장돼요"}, headers=SSE)
    assert res.headers["content-type"].startswith("text/event-stream")
    got = events(res)
    assert got[0][0] == "session"
    sid = got[0][1]["session_id"]
    assert got[1:] == [
        ("progress", {"node": "intake_read"}),
        ("progress", {"node": "intake_goal"}),
        ("progress", {"node": "intake_context"}),
        ("progress", {"node": "intake_finish"}),
        ("result", client.get(f"/sessions/{sid}").json()),
    ]

    got = events(client.post(f"/sessions/{sid}/measure", json=PRE_S2, headers=SSE))
    assert [data for name, data in got if name == "progress"] == [
        {"node": "measure_pre"},
        {"node": "plan", "step": "choose_activity"},
        {"node": "plan", "step": "search_evidence"},
        {"node": "plan", "step": "verify_evidence"},
        {"node": "plan", "step": "write_rationale"},
        {"node": "plan"},
        {"node": "prepare"},
    ]
    assert got[-1] == ("result", client.get(f"/sessions/{sid}").json())
    assert got[-1][1]["step"] == "RUN"


def test_request_for_another_step_is_refused_without_resuming():
    client = client_for()
    sid = start(client)
    res = client.post(f"/sessions/{sid}/report", json={"completed": True})
    assert res.status_code == 409
    assert res.json()["detail"] | {"message": "-"} == {
        "code": "STEP_MISMATCH",
        "message": "-",
        "step": "MEASURE",
        "node": "measure_pre",
    }
    assert client.get(f"/sessions/{sid}").json()["node"] == "measure_pre"
    assert client.post(f"/sessions/{sid}/retry").status_code == 409
    assert client.post("/sessions/nope/measure", json=PRE_S2).status_code == 404
    assert client.get("/sessions/nope").json()["detail"]["code"] == "SESSION_NOT_FOUND"


def test_invalid_measure_answers_never_reach_the_graph():
    client = client_for()
    sid = start(client)
    for bad in (
        {"INS-SAM": {"valence": 3}},  # SAM 문항 빠짐
        {"INS-SAM": {"valence": 0, "arousal": 7}},  # SAM 범위 밖
        {},  # 필수 SAM 응답 없음
    ):
        res = client.post(f"/sessions/{sid}/measure", json=bad)
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "INVALID_ANSWER"
    # 그래프에 들어간 적이 없어서 올바른 답으로 그대로 이어진다.
    # TODO 키를 추가로 보내도 비활성 척도 답은 검증·저장하지 않는다.
    extra = {**sam(3, 7), "INS-STAI6": {"calm": 999}}
    assert client.post(f"/sessions/{sid}/measure", json=extra).json()["step"] == "RUN"


def test_decision_outside_the_options_is_refused():
    # S1 · 이어폰 없는 카페 · 5분 — 후보가 PR-15 하나라 활동 변경이 선택지에 없다
    cafe = reading(
        order_condition=InterventionOrder.POST_STRESS,
        setting=Setting.OFFICE_CAFE,
        earphones=False,
        minutes=5,
    )
    client = client_for(
        FakePlanner(plan("PR-15", 5)), FakeIntaker(cafe, goal=inference(CareGoal.S1))
    )
    sid = start(client)
    client.post(f"/sessions/{sid}/measure", json=sam(4, 6))
    client.post(f"/sessions/{sid}/report", json={"completed": True})
    body = client.post(f"/sessions/{sid}/measure", json=sam(4, 6)).json()
    assert body["payload"]["options"] == ["END", "ADDITIONAL_SESSION"]

    res = client.post(f"/sessions/{sid}/decision", json={"next_step": "CHANGE_ACTIVITY"})
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "INVALID_ANSWER"
    # 거절된 결정은 그래프에 들어가지 않았다 — 선택지 안의 결정으로 이어진다
    body = client.post(f"/sessions/{sid}/decision", json={"next_step": "END"}).json()
    assert (body["step"], body["payload"]["measure"]) == ("MEASURE", "FINAL")


def test_plan_rejection_is_reported_and_retry_replans():
    invalid = plan("PR-09", 3)  # S1 프로토콜 — S2 세션에서 두 번 거부된다
    client = client_for(FakePlanner(invalid, invalid, BREATHING))
    sid = start(client)
    res = client.post(f"/sessions/{sid}/measure", json=PRE_S2)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "PLAN_REJECTED"
    assert res.json()["detail"]["retryable"] is True

    view = client.get(f"/sessions/{sid}").json()
    assert (view["step"], view["node"]) == ("STALLED", "plan")
    assert "S2 목표용이 아니다" in view["payload"]["error"]
    # 사전 측정은 이미 끝났다 — 다시 받지 않고 plan만 다시 돌린다
    assert client.post(f"/sessions/{sid}/measure", json=PRE_S2).status_code == 409
    got = events(client.post(f"/sessions/{sid}/retry", headers=SSE))
    assert got[-1][1]["step"] == "RUN"
    assert got[-1][1]["payload"]["plan"]["activity"]["protocol_id"] == "PR-06"


def test_sse_ends_with_an_error_event_when_the_run_fails():
    invalid = plan("PR-09", 3)
    client = client_for(FakePlanner(invalid, invalid))
    sid = start(client)
    got = events(client.post(f"/sessions/{sid}/measure", json=PRE_S2, headers=SSE))
    name, data = got[-1]
    assert name == "error"
    assert (data["code"], data["retryable"]) == ("PLAN_REJECTED", True)


def test_intake_questions_are_answered_until_the_pre_measure():
    client = asking_client()
    got = events(
        client.post("/sessions", json={"text": "발표 끝났는데 계속 생각나요"}, headers=SSE)
    )
    sid = got[0][1]["session_id"]
    assert got[1] == ("progress", {"node": "intake_read"})
    name, body = got[-1]
    assert (name, body["step"], body["node"]) == ("result", "ASK", "intake_ask")
    q1 = body["payload"]
    assert (q1["type"], q1["question_id"], q1["slot"]) == ("intake_question", "q1", "state")

    chosen = q1["groups"][0]["options"][1]["set"]  # 보기의 set을 그대로 돌려준다
    got = events(
        client.post(
            f"/sessions/{sid}/answer", json={"question_id": "q1", "answer": chosen}, headers=SSE
        )
    )
    assert [data for name, data in got if name == "progress"] == [
        {"node": "intake_ask"},
        {"node": "intake_goal"},
        {"node": "intake_context"},
    ]
    q2 = got[-1][1]["payload"]
    assert (q2["question_id"], q2["slot"], q2["allow_free_text"]) == ("q2", "context", False)

    answer = {"question_id": "q2", "answer": {"setting": "OFFICE_CAFE", "earphones": False}}
    body = client.post(f"/sessions/{sid}/answer", json=answer).json()
    assert (body["step"], body["payload"]["measure"]) == ("MEASURE", "PRE")
    assert [i["instrument_id"] for i in body["payload"]["instruments"]] == ["INS-SAM"]


def test_invalid_intake_answers_never_reach_the_graph():
    client = asking_client()
    sid = start(client)
    for bad in (
        {"question_id": "q9", "answer": {"order_condition": "POST_STRESS"}},  # 다른 질문
        {"question_id": "q1", "answer": {"order_condition": "NOPE"}},  # 없는 값
        {"question_id": "q1", "answer": {"goal_hint": ["S1"]}},  # 시점 질문이 받지 않는 칸
        {"question_id": "q1", "skip": True},  # 건너뛰기는 되묻기에만 있다
    ):
        res = client.post(f"/sessions/{sid}/answer", json=bad)
        assert res.status_code == 422, bad
        assert res.json()["detail"]["code"] == "INVALID_ANSWER"
    answer = {"question_id": "q1", "answer": {"order_condition": "POST_STRESS"}}
    assert (
        client.post(f"/sessions/{sid}/answer", json=answer).json()["payload"]["slot"] == "context"
    )
    # 환경 질문은 직접 입력을 받지 않는다
    res = client.post(f"/sessions/{sid}/answer", json={"question_id": "q2", "text": "카페예요"})
    assert res.json()["detail"]["code"] == "INVALID_ANSWER"
    assert client.post(f"/sessions/{sid}/measure", json=PRE_S2).status_code == 409


def test_safety_signal_ends_the_session_at_start_or_after_a_free_text_answer():
    ended = ("END", None, {"reason": "SAFETY"})
    # 처음 입력에서 — 상황 읽기(LLM)가 안전 신호를 본다
    client = client_for(intaker=FakeIntaker(reading(safety=True), goal=inference(CareGoal.S2)))
    body = client.post("/sessions", json={"text": "다 끝내고 싶어요"}).json()
    assert (body["step"], body["node"], body["payload"]) == ended

    # 시점 질문의 글 답에서 — 글 답은 상황 읽기를 다시 지난다
    intaker = FakeIntaker(
        reading(question=ASK_TIMING), reading(safety=True), goal=inference(CareGoal.S2)
    )
    client = client_for(intaker=intaker)
    sid = start(client)
    answer = {"question_id": "q1", "text": "살 이유를 모르겠어요"}
    body = client.post(f"/sessions/{sid}/answer", json=answer).json()
    assert (body["step"], body["node"], body["payload"]) == ended


def test_off_topic_input_is_restated_and_can_be_skipped():
    off = reading(relevance="OFF_TOPIC", restate_text="점심 고민 중이시군요! 오늘 하루는 어땠어요?")
    client = client_for(intaker=FakeIntaker(off, goal=inference(CareGoal.S1)))
    request = {"text": "점심 뭐 먹지", "setting": "HOME", "minutes_available": 5}
    body = client.post("/sessions", json=request).json()
    sid = body["session_id"]
    r1 = body["payload"]
    assert (body["step"], r1["slot"], r1["question_id"]) == ("ASK", "restate", "r1")
    assert (r1["groups"], r1["skip_label"]) == ([], "그냥 시작할게요")

    body = client.post(f"/sessions/{sid}/answer", json={"question_id": "r1", "skip": True}).json()
    assert (body["step"], body["payload"]["measure"]) == ("MEASURE", "PRE")
    # 건너뛰면 추론 없이 S2(기본값)로 시작한다 — MVP 측정은 SAM만 쓴다
    assert [i["instrument_id"] for i in body["payload"]["instruments"]] == ["INS-SAM"]


def test_session_api_is_unavailable_without_a_graph():
    app = FastAPI()
    app.include_router(sessions.router)
    res = TestClient(app).post("/sessions", json={"text": "긴장돼요"})
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "AGENT_UNAVAILABLE"


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("https://lumi-wellness-agent-fe.vercel.app", True),
        ("https://lumi-wellness-agent-fe-git-feat-20-koothemavericks-projects.vercel.app", True),
        ("http://localhost:3000", True),
        ("https://lumi-wellness-agent-fe.vercel.app.example.com", False),
        ("https://example.com", False),
    ],
)
def test_cors_allows_only_fe_origins(origin: str, allowed: bool):
    app = FastAPI()
    add_cors(app, Settings(_env_file=None))
    app.include_router(sessions.router)
    res = TestClient(app).options(
        "/sessions",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization",
        },
    )
    assert (res.headers.get("access-control-allow-origin") == origin) is allowed


def test_heartbeat_fills_silence_and_passes_errors_through():
    async def slow():
        yield "a"
        await asyncio.sleep(0.05)
        yield "b"

    async def failing():
        yield "a"
        raise ValueError("boom")

    async def collect(source):
        return [item async for item in sessions.with_heartbeat(source, 0.01)]

    out = asyncio.run(collect(slow()))
    assert out[0] == "a" and out[-1] == "b" and None in out
    with pytest.raises(ValueError, match="boom"):
        asyncio.run(collect(failing()))
