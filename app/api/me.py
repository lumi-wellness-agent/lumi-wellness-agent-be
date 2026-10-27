"""로그인한 사용자 자신 — 기록 동의와 탈퇴.

    GET    /me/consent   지금 동의 상태. agreed가 false면 FE가 동의 화면을 띄운다
    PUT    /me/consent   {"records": true, "raw_text": bool} — 필수 동의와 원문 동의(선택)
    DELETE /me           탈퇴 — 계정과 기록 · 원문 · 동의 · 체크포인트를 지운다(되돌릴 수 없다)

records(필수)는 세션 기록 — 상황 분류 · 질문 답 · 측정값 · 활동 · 결과. 심리 상태 측정값이 들어 있어
민감정보로 보고 따로 받는다. false로는 보낼 수 없다(거두려면 탈퇴). raw_text(선택)는 원문 보관 —
처음 입력과 직접 쓴 답 등을 text_retention_days일 동안 남긴다. false로 바꾸면 남긴 원문을 바로
지운다. 동의 문구는 FE에 있고 BE는 버전(records.CONSENT_VERSION)만 맞춘다.

탈퇴는 Supabase Auth의 사용자 행(auth.users)을 지운다. 로그인 세션도 함께 지워져 같은 토큰은 바로
401(SESSION_REVOKED)이 된다. FE는 204를 받으면 브라우저에 남은 세션을 지우고 첫 화면으로 간다.
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app import records
from app.auth import CurrentUser
from app.records import Consent, SessionStore

log = logging.getLogger(__name__)

router = APIRouter(prefix="/me", tags=["me"])


class ConsentBody(BaseModel):
    records: Literal[True]  # 필수 — 세션 기록(민감정보 포함)
    raw_text: bool = False  # 선택 — 원문 보관


def _store(request: Request) -> SessionStore:
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        message = "세션 기록 저장소를 열지 않았습니다."
        raise HTTPException(503, {"code": "AGENT_UNAVAILABLE", "message": message})
    return store


def consent_view(consent: Consent | None, retention_days: int) -> dict:
    return {
        "version": records.CONSENT_VERSION,  # 지금 받는 동의 문구 버전
        "agreed": consent is not None and consent.current,  # 세션을 시작할 수 있다
        "agreed_version": consent.version if consent else None,
        "raw_text": bool(consent and consent.raw_text),
        "text_retention_days": retention_days,
    }


@router.get("/consent")
async def get_consent(request: Request, user: CurrentUser) -> dict:
    store = _store(request)
    return consent_view(await store.consent(user.id), store.retention_days)


@router.put("/consent")
async def put_consent(body: ConsentBody, request: Request, user: CurrentUser) -> dict:
    store = _store(request)
    consent = await store.save_consent(user.id, body.raw_text)
    return consent_view(consent, store.retention_days)


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
