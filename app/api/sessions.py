"""세션 API — 세션 하나가 LangGraph thread 하나다(README 「세션 API」).

그래프는 사용자 입력이 필요한 자리(interrupt)에서 멈춘다. 요청마다 그 자리를 재개해 다음
interrupt(또는 끝)까지 돌리고, 멈춘 자리를 돌려준다. 응답의 step은 멈춘 노드로 정한다(STEP_BY_NODE)
— 노드 이름이 API 계약이다.

    POST /sessions                CareRequest     새 thread → intake → 질문 또는 사전 측정
    POST /sessions/{id}/answer    IntakeAnswer    intake_ask 재개 → 다음 질문 · 사전 측정 · END
    POST /sessions/{id}/measure   측정 응답       measure_pre · measure_post · measure_final 재개
    POST /sessions/{id}/report    SessionReport   run_session 재개
    POST /sessions/{id}/decision  ReplanDecision  confirm 재개
    POST /sessions/{id}/retry     —               멈춘(STALLED) 노드부터 다시
    GET  /sessions/{id}           —               지금 멈춘 자리(새로고침 · 끊긴 스트림 복구)

POST는 Accept: text/event-stream이면 SSE(session → progress … → result, 실패하면 error)로,
아니면 JSON(result와 같은 본문)으로 답한다. 둘 다 run_events 하나에서 나온다. FE는 응답 전에 LLM이
도는 호출 — POST /sessions · answer · 사전 measure · decision · retry — 만 SSE로 받는다. 사후
measure도 replan의 다음 단계 추천(LLM 1회, 실패하면 규칙 제안)이 돌지만 짧아 JSON으로 받아도 된다.

모든 요청에 Supabase 로그인 토큰이 필요하다(app/auth.py). 세션은 만든 사용자만 본다 — 남의 세션은
없는 세션과 같은 404다. 회원의 새 세션은 지금 버전의 기록 동의(PUT /me/consent)가 있어야 연다.
게스트(익명 로그인)는 1회성 체험이라 동의 없이 열고, 내용을 기록하지 않는다. 요청마다 그래프가 멈춘
뒤의 상태로 기록(app/records.py)을 다시 쓰고, 세션이 끝나면 원문이 든 체크포인트를 지운다 — 그 뒤의
GET은 기록에 남긴 END payload로 답한다.

재개 값은 그래프에 넣기 전에 검증한다. interrupt 뒤에서 노드가 예외를 내면 LangGraph는 그 재개
값을 남겨 두고, 다음 재개도 같은 값으로 다시 실패한다(interrupt id를 지정해도 같다 — 2026-10-27
확인). 그래서 측정 응답은 노드와 같은 채점 함수(instruments.score)로, intake 답은 같은 규칙
(intake_rules.apply_answer)으로, 결정은 payload의 options로 먼저 거른다.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from dataclasses import replace
from enum import StrEnum
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from app import instruments, records
from app.agent import intake_rules
from app.agent.context import AgentContext
from app.agent.llm import LlmExhaustedError
from app.agent.nodes.evaluator import deltas_between
from app.agent.nodes.planner import PlanRejected
from app.agent.state import ReplanDecision, SessionReport
from app.auth import CurrentUser, User
from app.protocols import RECENT_ROUNDS
from app.records import SessionRecord, SessionStore
from app.schemas.care import CareRequest
from app.schemas.intake import IntakeAnswer, IntakeQuestion
from app.schemas.measure import GOAL_SCALE, InstrumentId, Measurement

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sessions", tags=["sessions"])

# 이만큼 이벤트가 없으면 SSE 주석(: ping)을 보낸다 — prepare의 AI Horde 폴링은 최대 60초다
HEARTBEAT_SECONDS = 15


class Step(StrEnum):
    """FE가 그릴 화면. 멈춘 노드로 정한다."""

    ASK = "ASK"  # intake 질문 — payload는 IntakeQuestion(app/schemas/intake.py)
    MEASURE = "MEASURE"  # 측정 문항 — payload.measure가 PRE · POST · FINAL
    RUN = "RUN"  # 계획 · 콘텐츠 — FE 플레이어가 활동을 실행한다
    CONFIRM = "CONFIRM"  # 평가 · 제안 · 선택지(options)
    END = "END"  # 끝 — payload.reason이 DONE이거나 intake가 세션을 끝낸 이유
    STALLED = "STALLED"  # 노드가 실패했거나 스트림이 끊겨 interrupt 사이에 멈췄다 — retry로 잇는다


STEP_BY_NODE: dict[str, Step] = {
    "intake_ask": Step.ASK,
    "measure_pre": Step.MEASURE,
    "measure_post": Step.MEASURE,
    "measure_final": Step.MEASURE,
    "run_session": Step.RUN,
    "confirm": Step.CONFIRM,
}
MEASURE_NODES = frozenset({"measure_pre", "measure_post", "measure_final"})


def _error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message, **extra})


def _graph(request: Request) -> CompiledStateGraph:
    graph = getattr(request.app.state, "session_graph", None)
    if graph is None:
        raise _error(
            503, "AGENT_UNAVAILABLE", "세션 그래프를 조립하지 않았습니다. LLM 키를 확인하세요."
        )
    return graph


def _store(request: Request) -> SessionStore:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise _error(503, "AGENT_UNAVAILABLE", "세션 기록 저장소를 열지 않았습니다.")
    return store


def _context(request: Request, session_id: str) -> AgentContext:
    """그래프 실행 컨텍스트 — DB 풀과 세션 ID(plan의 후보 추천 순서 시드)."""
    return AgentContext(db_pool=getattr(request.app.state, "db_pool", None), session_id=session_id)


def _config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


# ── 멈춘 자리 → 응답 본문 ─────────────────────────────────────────────────────────────────


def session_view(session_id: str, snapshot: StateSnapshot) -> dict:
    """{session_id, step, node, payload}. JSON 응답과 SSE의 result가 같은 본문이다."""
    task = snapshot.tasks[0] if snapshot.tasks else None
    if task is None:  # next가 비었다 — 끝
        return _view(session_id, Step.END, None, end_payload(snapshot.values))
    if task.error is None and task.interrupts:
        return _view(session_id, STEP_BY_NODE[task.name], task.name, task.interrupts[0].value)
    error = None if task.error is None else str(task.error)
    return _view(session_id, Step.STALLED, task.name, {"error": error})


def _view(session_id: str, step: Step, node: str | None, payload: Any) -> dict:
    return {"session_id": session_id, "step": step.value, "node": node, "payload": payload}


def end_payload(values: dict) -> dict:
    """끝난 세션 요약. intake가 끝냈으면 그 이유만, 아니면 라운드와 목표 척도의 세션 변화."""
    if ended := values.get("ended"):
        return {"reason": ended.value}
    return {
        "reason": "DONE",
        "goal": values["intake"].goal.value,
        "rounds": [
            {
                "round": r.evaluation.round,
                "protocol_id": r.plan.activity.protocol_id,
                "minutes": r.plan.activity.minutes,
                "completed": r.report.completed,
                "deltas": r.evaluation.deltas,
                "improved": r.evaluation.improved,
                "next_step": r.next_step.value,
            }
            for r in values.get("rounds", [])
        ],
        "goal_scale": goal_scale_change(values),
    }


def goal_scale_change(values: dict) -> dict | None:
    """목표 척도의 세션 변화(끝 − 사전). 끝값은 종료 측정(STAI-6 · BSRI)이고, 라운드마다 재는 척도
    (S3 MFVAS)는 마지막 사후값이다(nodes/evaluator.py — 세션 Δ는 API 응답에서 계산한다)."""
    scale = GOAL_SCALE[values["intake"].goal]
    if scale is None:
        return None
    pre = _find(values.get("pre", []), scale)
    end = values.get("final")
    if end is None or end.instrument_id != scale:
        rounds = values.get("rounds", [])
        end = _find(rounds[-1].post, scale) if rounds else None
    if pre is None or end is None:
        return None
    return {
        "instrument_id": scale.value,
        "pre": pre.scores,
        "end": end.scores,
        "delta": deltas_between([pre], [end]),
    }


def _find(measurements: list[Measurement], instrument_id: InstrumentId) -> Measurement | None:
    return next((m for m in measurements if m.instrument_id == instrument_id), None)


# ── 실행 → 이벤트 ─────────────────────────────────────────────────────────────────────────


Finish = Callable[[dict, dict], Awaitable[None]]


async def run_events(
    graph: CompiledStateGraph,
    session_id: str,
    value: Any,
    context: AgentContext,
    finish: Finish | None = None,
) -> AsyncIterator[tuple[str, dict]]:
    """그래프를 다음 interrupt(또는 끝)까지 돌리며 (이벤트, 데이터)를 낸다.

    progress는 노드가 끝날 때 {"node"}, plan 안 단계가 시작될 때 {"node", "step"}(custom)이다.
    마지막은 result — 멈춘 자리(session_view)다. finish(상태, 멈춘 자리)는 result를 내기 전에 부른다
    (기록 쓰기 · 끝난 세션의 체크포인트 삭제).
    """
    config = _config(session_id)
    async for mode, chunk in graph.astream(
        value, config, context=context, stream_mode=["updates", "custom"]
    ):
        if mode == "custom":
            yield "progress", chunk
            continue
        for node in chunk:
            if node != "__interrupt__":
                yield "progress", {"node": node}
    snapshot = await graph.aget_state(config)
    view = session_view(session_id, snapshot)
    if finish is not None:
        await finish(snapshot.values, view)
    yield "result", view


def recorder(graph: CompiledStateGraph, store: SessionStore, record: SessionRecord) -> Finish:
    """멈춘 뒤의 상태로 기록을 다시 쓴다. 끝났으면 원문이 든 체크포인트를 지운다 — 기록을 먼저 남겨
    지운 뒤의 GET도 END payload(end_summary)로 답한다."""

    async def finish(values: dict, view: dict) -> None:
        await store.save(record, values, view)
        if view["step"] == Step.END:
            await graph.checkpointer.adelete_thread(record.id)

    return finish


def _mapped(exc: Exception) -> tuple[int, dict] | None:
    """실행 중 예외 → (HTTP 상태, 오류 본문). 모르는 예외는 None."""
    if isinstance(exc, PlanRejected):
        return 422, {"code": "PLAN_REJECTED", "message": str(exc), "retryable": exc.retryable}
    if isinstance(exc, LlmExhaustedError):
        message = "모델 호출이 모두 실패했습니다."
        return 503, {"code": "LLM_UNAVAILABLE", "message": message, "retryable": True}
    return None


async def _collect(events: AsyncIterator[tuple[str, dict]]) -> dict:
    """JSON 응답 — progress는 버리고 result만."""
    result: dict = {}
    try:
        async for name, data in events:
            if name == "result":
                result = data
    except Exception as exc:
        if (mapped := _mapped(exc)) is None:
            raise
        raise HTTPException(*mapped) from exc
    return result


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _sse_stream(
    session_id: str, events: AsyncIterator[tuple[str, dict]]
) -> AsyncIterator[str]:
    yield _sse("session", {"session_id": session_id})
    try:
        async for item in with_heartbeat(events, HEARTBEAT_SECONDS):
            yield ": ping\n\n" if item is None else _sse(*item)
    except Exception as exc:
        if (mapped := _mapped(exc)) is None:
            log.exception("세션 %s 실행 실패", session_id)
            body = {"code": "INTERNAL", "message": "서버 오류가 났습니다.", "retryable": True}
        else:
            log.warning("세션 %s: %s", session_id, exc)
            body = mapped[1]
        yield _sse("error", body)


async def with_heartbeat(events: AsyncIterator[Any], interval: float) -> AsyncIterator[Any]:
    """events를 그대로 넘기고, interval초 동안 아무것도 없으면 None(하트비트)을 낸다.

    wait_for가 기다리던 그래프 실행을 취소하지 않도록 events는 별도 태스크가 큐로 옮긴다. 소비가
    끝나거나 클라이언트가 끊겨 취소되면 그 태스크도 취소한다 — 그래프는 마지막 체크포인트에 남고
    retry로 잇는다.
    """
    queue: asyncio.Queue = asyncio.Queue()
    done = object()

    async def pump() -> None:
        try:
            async for item in events:
                queue.put_nowait(item)
        except Exception as exc:  # 소비하는 쪽에서 다시 던진다
            queue.put_nowait(exc)
        finally:
            queue.put_nowait(done)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), interval)
            except TimeoutError:
                yield None
                continue
            if item is done:
                return
            if isinstance(item, Exception):
                raise item
            yield item
    finally:
        task.cancel()


async def _respond(request: Request, record: SessionRecord, value: Any, status_code: int = 200):
    graph = _graph(request)
    finish = recorder(graph, _store(request), record)
    events = run_events(graph, record.id, value, _context(request, record.id), finish)
    if "text/event-stream" in request.headers.get("accept", ""):
        return StreamingResponse(
            _sse_stream(record.id, events),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(await _collect(events), status_code=status_code)


# ── 재개 전 검사 ──────────────────────────────────────────────────────────────────────────


async def _owned(request: Request, session_id: str, user: User) -> SessionRecord:
    """요청한 사용자의 세션 기록. 없거나 남의 세션이면 404 — 남의 세션이 있는지도 알리지 않는다."""
    _graph(request)  # 그래프가 없으면 기록을 보기 전에 503
    store = _store(request)
    try:
        uuid.UUID(session_id)
    except ValueError:
        record = None
    else:
        record = await store.session(session_id)
    if record is None or record.user_id != user.id:
        raise _error(404, "SESSION_NOT_FOUND", "세션이 없습니다.")
    return replace(record, guest=user.anonymous)


async def _current(request: Request, record: SessionRecord) -> dict:
    """지금 멈춘 자리. 끝난 세션은 기록의 END payload로, 쉬다 만료된 세션은 410."""
    if record.end_summary is not None:
        return _view(record.id, Step.END, None, record.end_summary)
    snapshot = await _graph(request).aget_state(_config(record.id))
    if record.expired or snapshot.created_at is None:
        message = "오래 쉬어 이어 갈 수 없는 세션입니다. 새로 시작하세요."
        raise _error(410, "SESSION_EXPIRED", message)
    return session_view(record.id, snapshot)


def _mismatch(view: dict) -> HTTPException:
    message = f"지금은 {view['step']} 단계입니다."
    return _error(409, "STEP_MISMATCH", message, step=view["step"], node=view["node"])


async def _waiting_at(
    request: Request, session_id: str, user: User, nodes: Collection[str]
) -> tuple[SessionRecord, Any]:
    """세션이 nodes 중 하나의 interrupt에서 기다리면 (기록, payload)를, 아니면 409를 낸다."""
    record = await _owned(request, session_id, user)
    view = await _current(request, record)
    if view["step"] in (Step.END, Step.STALLED) or view["node"] not in nodes:
        raise _mismatch(view)
    return record, view["payload"]


def _check_measure(payload: dict, answers: dict[str, dict[str, float]]) -> None:
    """측정 응답을 노드와 같은 채점 함수로 미리 검사한다 — 빠진 도구 · 문항, 범위 밖 값이면 422."""
    for presented in payload["instruments"]:
        key = presented["instrument_id"]
        if key not in answers:
            raise _error(422, "INVALID_ANSWER", f"{key} 응답이 없습니다.")
        try:
            instruments.score(instruments.get_instrument(InstrumentId(key)), answers[key])
        except ValueError as exc:
            raise _error(422, "INVALID_ANSWER", str(exc)) from exc


def _check_answer(payload: dict, answer: IntakeAnswer) -> None:
    """intake 답을 노드와 같은 규칙으로 미리 검사한다 — 다른 질문의 답, 직접 입력을 받지 않는 질문의
    글 답, 그 질문이 받지 않는 칸이나 값이면 422."""
    try:
        intake_rules.apply_answer(IntakeQuestion.model_validate(payload), answer)
    except (ValueError, TypeError) as exc:
        raise _error(422, "INVALID_ANSWER", str(exc)) from exc


# ── 엔드포인트 ────────────────────────────────────────────────────────────────────────────


@router.post("", status_code=201)
async def start_session(body: CareRequest, request: Request, user: CurrentUser):
    """새 세션 — intake를 돌려 첫 interrupt(질문이 필요하면 질문, 아니면 사전 측정)에서 멈춘다.

    회원은 지금 버전의 기록 동의가 없으면 403(CONSENT_REQUIRED) — FE가 동의 화면을 띄운다.
    게스트는 동의를 받지 않는다(내용을 기록하지 않는 1회성 체험).
    """
    _graph(request)  # 그래프가 없으면 기록을 쓰기 전에 503
    store = _store(request)
    if not user.anonymous:
        consent = await store.consent(user.id)
        if consent is None or not consent.current:
            message = "세션 기록 동의가 필요합니다."
            raise _error(403, "CONSENT_REQUIRED", message, version=records.CONSENT_VERSION)
    record = SessionRecord(id=str(uuid.uuid4()), user_id=user.id, guest=user.anonymous)
    # 지난 세션에서 최근에 한 활동 — plan이 추천 순서에서 가중치를 낮춘다. 게스트는 기록이 없다
    recent = [] if user.anonymous else await store.recent_protocols(user.id, RECENT_ROUNDS)
    await store.create(record.id, user.id, body, guest=user.anonymous)
    start = {"request": body, "recent_protocol_ids": recent}
    return await _respond(request, record, start, status_code=201)


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request, user: CurrentUser) -> dict:
    """지금 멈춘 자리. 새로고침하거나 스트림이 끊겼을 때 FE가 화면을 다시 그린다."""
    return await _current(request, await _owned(request, session_id, user))


@router.post("/{session_id}/answer")
async def submit_answer(session_id: str, answer: IntakeAnswer, request: Request, user: CurrentUser):
    """intake 질문의 답 — 고른 보기의 set을 합친 answer, 또는 직접 쓴 text(안전 검사를 거친다)."""
    record, payload = await _waiting_at(request, session_id, user, {"intake_ask"})
    _check_answer(payload, answer)
    return await _respond(request, record, Command(resume=answer.model_dump(mode="json")))


@router.post("/{session_id}/measure")
async def submit_measure(
    session_id: str, answers: dict[str, dict[str, float]], request: Request, user: CurrentUser
):
    """측정 응답 {instrument_id: {item_id: value}} — 사전 · 사후 · 종료 측정 모두 여기로 온다."""
    record, payload = await _waiting_at(request, session_id, user, MEASURE_NODES)
    _check_measure(payload, answers)
    return await _respond(request, record, Command(resume=answers))


@router.post("/{session_id}/report")
async def submit_report(
    session_id: str, report: SessionReport, request: Request, user: CurrentUser
):
    """활동을 마쳤거나 멈췄다. 점수는 받지 않는다 — 다음 응답이 사후 측정 문항이다."""
    record, _ = await _waiting_at(request, session_id, user, {"run_session"})
    return await _respond(request, record, Command(resume=report.model_dump(mode="json")))


@router.post("/{session_id}/decision")
async def submit_decision(
    session_id: str, decision: ReplanDecision, request: Request, user: CurrentUser
):
    """제안에 대한 사용자 선택. 활동 변경 · 추가 세션이면 다음 라운드를 계획한다."""
    record, payload = await _waiting_at(request, session_id, user, {"confirm"})
    if decision.next_step.value not in payload["options"]:
        message = f"{decision.next_step.value}는 지금 고를 수 없습니다."
        raise _error(422, "INVALID_ANSWER", message)
    return await _respond(request, record, Command(resume=decision.model_dump(mode="json")))


@router.post("/{session_id}/retry")
async def retry_session(session_id: str, request: Request, user: CurrentUser):
    """STALLED 세션을 멈춘 노드부터 다시 돌린다 — plan 재선택, 끊긴 스트림."""
    record = await _owned(request, session_id, user)
    view = await _current(request, record)
    if view["step"] != Step.STALLED:
        raise _mismatch(view)
    return await _respond(request, record, None)
