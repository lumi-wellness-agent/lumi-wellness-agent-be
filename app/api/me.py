"""로그인한 사용자 자신 — 기록 동의 · 활동 기록 · 탈퇴.

    GET    /me/consent   지금 동의 상태. agreed가 false면 FE가 동의 화면을 띄운다
    PUT    /me/consent   {"records": true, "raw_text": bool} — 필수 동의와 원문 동의(선택)
    GET    /me/sessions  활동 기록 — 활동을 한 세션을 최근 것부터(?limit=, 기본 20 · 최대 100)
    DELETE /me           탈퇴 — 계정과 기록 · 원문 · 동의 · 체크포인트를 지운다(되돌릴 수 없다)

records(필수)는 세션 기록 — 상황 분류 · 질문 답 · 측정값 · 활동 · 결과. 심리 상태 측정값이 들어 있어
민감정보로 보고 따로 받는다. false로는 보낼 수 없다(거두려면 탈퇴). raw_text(선택)는 원문 보관 —
처음 입력과 직접 쓴 답 등을 text_retention_days일 동안 남긴다. false로 바꾸면 남긴 원문을 바로
지운다. 동의 문구는 FE에 있고 BE는 버전(records.CONSENT_VERSION)만 맞춘다.

게스트(익명 로그인)는 동의를 받지 않는다 — 내용을 기록하지 않는 1회성 체험이라 PUT은 403
(MEMBERS_ONLY)이다. FE는 게스트 시작 화면에 동의 대신 고지 한 줄을 띄운다(README 「인증 · 세션
기록」).

활동 기록은 세션 기록(분석 테이블)만 읽는다 — 시작 · 끝 시각, 목표, 라운드(활동 · 시간 ·
끝까지 했는지 · 변화 · 나아졌는지 · 고른 다음 단계)와 목표 척도의 세션 변화. 원문은 싣지 않는다.
END payload처럼 protocol_id만 보내면 FE가 지난 활동의 이름을 모르므로 라운드마다 활동 이름(name)을
붙인다. 게스트는 기록이 없어 403(MEMBERS_ONLY)이다.

탈퇴는 Supabase Auth의 사용자 행(auth.users)을 지운다. 로그인 세션도 함께 지워져 같은 토큰은 바로
401(SESSION_REVOKED)이 된다. FE는 204를 받으면 브라우저에 남은 세션을 지우고 첫 화면으로 간다.
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from app import protocols, records
from app.auth import CurrentUser
from app.records import Consent, SessionStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])

HISTORY_LIMIT = 20  # 활동 기록이 한 번에 돌려주는 세션 수(기본값)


class ConsentBody(BaseModel):
    records: Literal[True]  # 필수 — 세션 기록(민감정보 포함)
    raw_text: bool = False  # 선택 — 원문 보관


def _store(request: Request) -> SessionStore:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        message = "세션 기록 저장소를 열지 않았습니다."
        raise HTTPException(503, {"code": "AGENT_UNAVAILABLE", "message": message})
    return store


def consent_view(consent: Consent | None, store: SessionStore) -> dict:
    return {
        "version": records.CONSENT_VERSION,  # 지금 받는 동의 문구 버전
        "agreed": consent is not None and consent.current,  # 세션을 시작할 수 있다
        "agreed_version": consent.version if consent else None,
        "raw_text": bool(consent and consent.raw_text),
        "text_retention_days": store.retention_days,
    }


@router.get("/consent")
async def get_consent(request: Request, user: CurrentUser) -> dict:
    store = _store(request)
    return consent_view(await store.consent(user.id), store)


@router.put("/consent")
async def put_consent(body: ConsentBody, request: Request, user: CurrentUser) -> dict:
    if user.anonymous:
        message = "게스트 체험은 기록하지 않아 동의를 받지 않습니다. 로그인한 뒤 동의하세요."
        raise HTTPException(403, {"code": "MEMBERS_ONLY", "message": message})
    store = _store(request)
    return consent_view(await store.save_consent(user.id, body.raw_text), store)


@router.get("/sessions")
async def get_sessions(
    request: Request,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = HISTORY_LIMIT,
) -> dict:
    """활동 기록 {"sessions": [...]} — 최근 세션부터. 라운드마다 활동 이름을 붙인다."""
    if user.anonymous:
        message = "게스트 체험은 기록을 남기지 않습니다. 로그인하면 활동 기록을 볼 수 있습니다."
        raise HTTPException(403, {"code": "MEMBERS_ONLY", "message": message})
    sessions = await _store(request).history(user.id, limit)
    return {
        "sessions": [
            {**session, "rounds": [_named(r) for r in session["rounds"]]} for session in sessions
        ]
    }


def _named(round_row: dict) -> dict:
    """활동 이름. 활동 노드 JSON에서 빠진 활동이면 None — FE는 protocol_id를 보인다."""
    protocol = protocols.get_protocol(round_row["protocol_id"])
    return {**round_row, "name": protocol.name if protocol else None}


@router.delete("", status_code=204)
async def delete_me(request: Request, user: CurrentUser) -> Response:
    """탈퇴. 계정과 기록을 지운 뒤 그 사용자의 체크포인트(원문이 들어 있다)를 지운다."""
    session_ids = await _store(request).delete_user(user.id)
    checkpointer = request.app.state.checkpointer
    for session_id in session_ids:
        try:
            await checkpointer.adelete_thread(session_id)
        except (
            Exception
        ):  # 계정은 이미 지웠다 — 남은 체크포인트는 정리 작업이 주인 없는 것으로 지운다
            log.exception("탈퇴한 사용자의 체크포인트 %s를 지우지 못했습니다", session_id)
    return Response(status_code=204)
