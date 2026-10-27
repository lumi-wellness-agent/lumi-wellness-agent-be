"""세션 API — 대역 모델로 조립한 그래프에 HTTP로 흐름 · step · SSE · 오류를 확인한다.

intake · Planner 대역은 tests/agent/test_graph.py의 것을 쓴다(상황 읽기 · 목표 추론 대역).
로그인은 current_user를 바꿔 끼우고(토큰 검증은 tests/test_auth.py), 기록은 MemoryStore에 쓴다.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agent.checkpoint import memory_checkpointer
from app.agent.graph import build_graph
from app.api import internal, me, sessions
from app.auth import User, current_user
from app.config import Settings
from app.main import add_cors
from app.records import MemoryStore
from app.schemas.care import CareGoal, Setting
from app.schemas.evidence import InterventionOrder
from tests.agent.test_graph import (
    ASK_TIMING,
    BREATHING,
    S2_READING,
    STAI6,
    FakeIntaker,
    FakePlanner,
    inference,
    mfvas,
    plan,
    reading,
    sam,
)

SSE = {"Accept": "text/event-stream"}
PRE_S2 = {**sam(3, 7), "INS-STAI6": STAI6}
ALICE = User(id=uuid.UUID("00000000-0000-4000-8000-00000000a11c"), provider="kakao")
BOB = User(id=uuid.UUID("00000000-0000-4000-8000-000000000b0b"), provider="google")


def client_for(
    planner: FakePlanner | None = None,
    intaker: FakeIntaker | None = None,
    *,
    raw_text: bool = False,
    consent: bool = True,
) -> TestClient:
    """기본은 발표 앞 · 집 · 15분이 문장에 다 있는 S2 — 질문 없이 사전 측정으로 간다.

    ALICE로 로그인했고 기록 동의(consent)를 했다. raw_text면 원문 보관에도 동의했다.
    """
    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(me.router)
    app.include_router(internal.router)
    planner = planner or FakePlanner(BREATHING)
    intaker = intaker or FakeIntaker(S2_READING, goal=inference(CareGoal.S2))
    app.state.checkpointer = memory_checkpointer()
    app.state.session_graph = build_graph(planner, intaker, app.state.checkpointer)
    app.state.db_pool = None
    app.state.session_store = MemoryStore()
    if consent:
        asyncio.run(app.state.session_store.save_consent(ALICE.id, raw_text))
    login(app, ALICE)
    return TestClient(app)


def login(app: FastAPI, user: User) -> None:
    app.dependency_overrides[current_user] = lambda: user


def store_of(client: TestClient) -> MemoryStore:
    return client.app.state.session_store


def has_checkpoint(client: TestClient, sid: str) -> bool:
    graph = client.app.state.session_graph
    snapshot = asyncio.run(graph.aget_state({"configurable": {"thread_id": sid}}))
    return snapshot.created_at is not None


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
    assert [i["instrument_id"] for i in body["payload"]["instruments"]] == ["INS-SAM", "INS-STAI6"]

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

    final = {"INS-STAI6": {**STAI6, "tense": 1, "worried": 1}}
    body = client.post(f"/sessions/{sid}/measure", json=final).json()
    assert (body["step"], body["node"]) == ("END", None)
    end = body["payload"]
    assert (end["reason"], end["goal"]) == ("DONE", "S2")
    assert [(r["protocol_id"], r["improved"], r["next_step"]) for r in end["rounds"]] == [
        ("PR-06", True, "END")
    ]
    # 세션 Δ는 사전과 종료 측정의 STAI-6(56.67 → 43.33)
    assert end["goal_scale"]["instrument_id"] == "INS-STAI6"
    assert end["goal_scale"]["delta"]["total"] == pytest.approx(43.33 - 56.67)
    assert client.get(f"/sessions/{sid}").json() == body


def test_s3_session_ends_without_final_measure_and_uses_last_post():
    outdoors = reading(
        order_condition=InterventionOrder.POST_FATIGUE, setting=Setting.OUTDOOR, minutes=30
    )
    client = client_for(
        FakePlanner(plan("PR-11", 20)), FakeIntaker(outdoors, goal=inference(CareGoal.S3))
    )
    sid = start(client)
    assert (
        client.post(f"/sessions/{sid}/measure", json={**sam(4, 5), **mfvas(80, 70, 20)}).json()[
            "step"
        ]
        == "RUN"
    )
    client.post(f"/sessions/{sid}/report", json={"completed": True})
    client.post(f"/sessions/{sid}/measure", json={**sam(6, 5), **mfvas(45, 40, 55)})

    body = client.post(f"/sessions/{sid}/decision", json={"next_step": "END"}).json()
    assert body["step"] == "END"
    scale = body["payload"]["goal_scale"]
    assert scale["instrument_id"] == "INS-MFVAS"
    assert scale["delta"] == {"fatigue": -35.0, "concentration": -30.0, "energy": 35.0}


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
        sam(3, 7),  # STAI-6 없음
        {**sam(3, 7), "INS-STAI6": {**STAI6, "calm": 9}},  # 범위 밖
        {"INS-SAM": {"valence": 3}, "INS-STAI6": STAI6},  # 문항 빠짐
    ):
        res = client.post(f"/sessions/{sid}/measure", json=bad)
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "INVALID_ANSWER"
    # 그래프에 들어간 적이 없어서 올바른 답으로 그대로 이어진다
    assert client.post(f"/sessions/{sid}/measure", json=PRE_S2).json()["step"] == "RUN"


def test_report_takes_one_to_three_notice_words():
    client = client_for()
    sid = start(client)
    client.post(f"/sessions/{sid}/measure", json=PRE_S2)
    for bad in (["차분", "피곤", "답답", "멍함"], ["가" * 21], [" "]):  # 4개 · 20자 초과 · 빈 칸
        res = client.post(f"/sessions/{sid}/report", json={"completed": True, "notice_words": bad})
        assert res.status_code == 422, bad
    report = {"completed": True, "notice_words": [" 차분 ", "조금 피곤"]}
    assert client.post(f"/sessions/{sid}/report", json=report).json()["step"] == "MEASURE"
    graph = client.app.state.session_graph
    values = asyncio.run(graph.aget_state({"configurable": {"thread_id": sid}})).values
    assert values["report"].notice_words == ["차분", "조금 피곤"]  # 서버에 남는다


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
    bsri = {f"item{i}": 60 for i in range(1, 9)}
    client.post(f"/sessions/{sid}/measure", json={**sam(4, 6), "INS-BSRI": bsri})
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
    assert [i["instrument_id"] for i in body["payload"]["instruments"]] == ["INS-SAM", "INS-BSRI"]


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
    # 건너뛰면 추론 없이 S2(기본값)로 시작한다 — 목표 척도가 STAI-6다
    assert [i["instrument_id"] for i in body["payload"]["instruments"]] == ["INS-SAM", "INS-STAI6"]


def test_session_api_is_unavailable_without_a_graph():
    app = FastAPI()
    app.include_router(sessions.router)
    login(app, ALICE)
    res = TestClient(app).post("/sessions", json={"text": "긴장돼요"})
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "AGENT_UNAVAILABLE"


def test_session_api_is_closed_without_a_login_verifier():
    # SUPABASE_URL이 없으면 검증기를 만들지 않는다 — 인증 없이 열리지 않는다
    app = FastAPI()
    app.include_router(sessions.router)
    res = TestClient(app).post("/sessions", json={"text": "긴장돼요"})
    assert res.status_code == 503
    assert res.json()["detail"]["code"] == "AUTH_UNAVAILABLE"


def test_a_new_session_needs_the_records_consent():
    client = client_for(consent=False)
    res = client.post("/sessions", json={"text": "발표 앞두고 긴장돼요"})
    assert res.status_code == 403
    assert res.json()["detail"]["code"] == "CONSENT_REQUIRED"
    assert store_of(client).sessions == {}

    view = client.get("/me/consent").json()
    assert (view["agreed"], view["raw_text"], view["agreed_version"]) == (False, False, None)
    # 필수 동의는 거절로 보낼 수 없다
    assert client.put("/me/consent", json={"records": False}).status_code == 422
    view = client.put("/me/consent", json={"records": True}).json()
    assert (view["agreed"], view["raw_text"], view["text_retention_days"]) == (True, False, 90)
    assert client.post("/sessions", json={"text": "발표 앞두고 긴장돼요"}).status_code == 201


def test_sessions_are_visible_only_to_their_owner():
    client = client_for()
    sid = start(client)
    asyncio.run(store_of(client).save_consent(BOB.id, raw_text=False))
    login(client.app, BOB)
    for res in (
        client.get(f"/sessions/{sid}"),
        client.post(f"/sessions/{sid}/measure", json=PRE_S2),
        client.post(f"/sessions/{sid}/retry"),
    ):
        # 남의 세션은 없는 세션과 같다
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "SESSION_NOT_FOUND"
    login(client.app, ALICE)
    assert client.post(f"/sessions/{sid}/measure", json=PRE_S2).json()["step"] == "RUN"
    assert store_of(client).sessions[sid]["user_id"] == ALICE.id


def run_s2_to_end(client: TestClient, **report) -> str:
    sid = start(client)
    client.post(f"/sessions/{sid}/measure", json=PRE_S2)
    client.post(f"/sessions/{sid}/report", json={"completed": True, **report})
    client.post(f"/sessions/{sid}/measure", json=sam(5, 4))
    client.post(f"/sessions/{sid}/decision", json={"next_step": "END"})
    final = {"INS-STAI6": {**STAI6, "tense": 1, "worried": 1}}
    assert client.post(f"/sessions/{sid}/measure", json=final).json()["step"] == "END"
    return sid


def test_records_follow_the_session_and_the_checkpoint_goes_at_the_end():
    client = client_for()
    sid = start(client)
    row = store_of(client).sessions[sid]
    assert (row["step"], row["node"], row["goal"]) == ("MEASURE", "measure_pre", "S2")
    assert row["request"] == {
        "setting": None,
        "earphones": None,
        "minutes_available": None,
        "goal": None,
        "place": None,
        "content_type": "CURATED_PHOTO",
    }
    assert row["intake"]["goal_source"] == "INFERRED"
    assert "summary" not in row["intake"] and "cues" not in row["intake"]  # 글은 분석 기록에 없다

    client.post(f"/sessions/{sid}/measure", json=PRE_S2)
    client.post(f"/sessions/{sid}/report", json={"completed": True})
    client.post(f"/sessions/{sid}/measure", json=sam(5, 4))
    client.post(f"/sessions/{sid}/decision", json={"next_step": "END"})
    assert has_checkpoint(client, sid)
    final = {"INS-STAI6": {**STAI6, "tense": 1, "worried": 1}}
    body = client.post(f"/sessions/{sid}/measure", json=final).json()

    # 끝나면 원문이 든 체크포인트를 지우고, GET은 기록의 END payload로 답한다
    assert not has_checkpoint(client, sid)
    assert client.get(f"/sessions/{sid}").json() == body
    assert client.post(f"/sessions/{sid}/retry").json()["detail"]["code"] == "STEP_MISMATCH"
    row = store_of(client).sessions[sid]
    assert (row["step"], row["end_reason"], row["end_summary"]) == ("END", "DONE", body["payload"])
    assert row["ended_at"] is not None
    assert [m["instrument_id"] for m in row["pre"]] == ["INS-SAM", "INS-STAI6"]
    assert row["final"]["instrument_id"] == "INS-STAI6"
    (key, round_row), *rest = store_of(client).rounds.items()
    assert key == (sid, 1) and rest == []
    assert (round_row["protocol_id"], round_row["completed"], round_row["improved"]) == (
        "PR-06",
        True,
        True,
    )
    assert (round_row["recommended"], round_row["next_step"]) == ("END", "END")
    assert round_row["options"] == ["END", "CHANGE_ACTIVITY", "ADDITIONAL_SESSION"]
    assert store_of(client).texts == {}  # 원문 동의가 없다


def test_raw_text_is_kept_only_with_its_consent_and_goes_when_withdrawn():
    client = client_for(raw_text=True)
    sid = run_s2_to_end(client, notice_words=["편안", "졸림"])
    text = store_of(client).texts[sid]
    assert text["request_text"] == "발표 앞두고 긴장돼요"
    assert text["user_id"] == ALICE.id
    assert text["rounds"][0]["notice_words"] == ["편안", "졸림"]
    assert text["intake"].keys() == {"event", "cues", "summary", "goal_reason", "constraints"}
    assert text["expires_at"] - store_of(client).sessions[sid]["created_at"] == pytest.approx(
        timedelta(days=90), abs=timedelta(seconds=5)
    )
    # 원문 동의를 거두면 남긴 원문을 바로 지운다
    view = client.put("/me/consent", json={"records": True, "raw_text": False}).json()
    assert view["raw_text"] is False
    assert store_of(client).texts == {}


def test_free_text_answers_are_kept_with_the_question():
    client = asking_client()
    asyncio.run(store_of(client).save_consent(ALICE.id, raw_text=True))
    sid = start(client)
    answer = {"question_id": "q1", "text": "발표가 아까 끝났어요"}
    client.post(f"/sessions/{sid}/answer", json=answer)
    turns = store_of(client).sessions[sid]["turns"]
    assert turns[0] | {"answer": None} == {
        "question_id": "q1",
        "slot": "state",
        "answer": None,
        "skipped": False,
        "wrote": True,
    }
    (written,) = [a for a in store_of(client).texts[sid]["answers"] if a["free_text"]]
    assert (written["question_id"], written["free_text"]) == ("q1", "발표가 아까 끝났어요")
    assert written["question"]  # 질문 문장도 함께(LLM이 사용자 말을 되받았을 수 있다)


def test_a_safety_ended_session_keeps_only_the_fact():
    intaker = FakeIntaker(
        reading(question=ASK_TIMING), reading(safety=True), goal=inference(CareGoal.S2)
    )
    client = client_for(intaker=intaker, raw_text=True)
    sid = client.post("/sessions", json={"text": "요즘 좀 힘들어요", "setting": "HOME"}).json()[
        "session_id"
    ]
    assert sid in store_of(client).texts  # 질문을 기다리는 동안은 원문이 있다
    answer = {"question_id": "q1", "text": "살 이유를 모르겠어요"}
    body = client.post(f"/sessions/{sid}/answer", json=answer).json()
    assert body["payload"] == {"reason": "SAFETY"}
    row = store_of(client).sessions[sid]
    assert (row["end_reason"], row["intake"], row["turns"], row["request"], row["goal"]) == (
        "SAFETY",
        None,
        [],
        {},
        None,
    )
    assert store_of(client).texts == {}
    assert not has_checkpoint(client, sid)
    assert client.get(f"/sessions/{sid}").json() == body


def test_cleanup_drops_idle_and_orphan_checkpoints_and_expired_texts():
    now = [datetime(2026, 10, 28, 12, tzinfo=UTC)]
    client = client_for(raw_text=True)
    store_of(client).clock = lambda: now[0]
    app = client.app
    app.state.cron_secret = "s3cret"
    app.state.session_idle_hours = 24
    idle = start(client)  # 사전 측정을 기다린 채 쉰다
    finished = run_s2_to_end(client)
    # 기록 없는 체크포인트 — 인증 전 세션이나 지운 사용자의 세션
    orphan = str(uuid.uuid4())
    graph = app.state.session_graph
    asyncio.run(graph.aupdate_state({"configurable": {"thread_id": orphan}}, {"ended": None}))
    assert has_checkpoint(client, orphan)

    assert client.get("/internal/cleanup").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/internal/cleanup", headers=bad).status_code == 401

    now[0] += timedelta(hours=25)
    auth = {"Authorization": "Bearer s3cret"}
    assert client.get("/internal/cleanup", headers=auth).json() == {
        "texts": 0,
        "threads": 2,
        "expired_sessions": 1,
    }
    assert not has_checkpoint(client, idle) and not has_checkpoint(client, orphan)
    res = client.get(f"/sessions/{idle}")
    assert (res.status_code, res.json()["detail"]["code"]) == (410, "SESSION_EXPIRED")
    assert client.get(f"/sessions/{finished}").json()["step"] == "END"  # 끝난 세션은 기록으로

    now[0] += timedelta(days=90)
    assert client.get("/internal/cleanup", headers=auth).json()["texts"] == 2
    assert store_of(client).texts == {}


def test_account_deletion_removes_the_user_their_records_and_checkpoints():
    client = client_for(raw_text=True)
    asyncio.run(store_of(client).save_consent(BOB.id, raw_text=False))
    idle = start(client)  # 진행 중 — 체크포인트에 원문이 있다
    finished = run_s2_to_end(client)
    login(client.app, BOB)
    bobs = start(client)
    login(client.app, ALICE)

    res = client.delete("/me")
    assert (res.status_code, res.content) == (204, b"")
    store = store_of(client)
    assert ALICE.id not in store.consents
    assert set(store.sessions) == {bobs}
    assert store.texts == {} and store.rounds == {}
    assert not has_checkpoint(client, idle)
    assert has_checkpoint(client, bobs)  # 다른 사용자의 세션은 그대로
    for sid in (idle, finished):
        assert client.get(f"/sessions/{sid}").status_code == 404
    # 운영에서는 로그인 세션도 지워져 같은 토큰이 401(SESSION_REVOKED)이다 — tests/test_auth.py
    assert client.get("/me/consent").json()["agreed"] is False


def test_cleanup_is_closed_without_a_cron_secret():
    client = client_for()
    res = client.get("/internal/cleanup", headers={"Authorization": "Bearer x"})
    assert (res.status_code, res.json()["detail"]["code"]) == (503, "CRON_UNAVAILABLE")


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


def test_cors_allows_every_method_the_fe_calls():
    app = FastAPI()
    add_cors(app, Settings(_env_file=None))
    app.include_router(me.router)
    res = TestClient(app).options(
        "/me",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert {"GET", "POST", "PUT", "DELETE"} <= set(
        res.headers["access-control-allow-methods"].split(", ")
    )


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
