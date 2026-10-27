"""운영 작업 — Vercel Cron이 부른다(vercel.json crons). 사용자 API가 아니다.

    GET /internal/cleanup   기한이 지난 원문, 쉬는 미완료 세션과 주인 없는 체크포인트를 지운다

Vercel Cron은 GET으로 부르고 프로젝트 환경 변수 CRON_SECRET을 `Authorization: Bearer`로 보낸다.
CRON_SECRET이 비어 있으면 503이다. 하루 한 번 돈다(Hobby 플랜 상한).
"""

import hmac
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import records

router = APIRouter(prefix="/internal", include_in_schema=False)

_bearer = HTTPBearer(auto_error=False)


@router.get("/cleanup")
async def cleanup(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> dict:
    state = request.app.state
    secret = getattr(state, "cron_secret", "")
    if not secret:
        raise HTTPException(503, {"code": "CRON_UNAVAILABLE", "message": "CRON_SECRET이 없습니다."})
    if credentials is None or not hmac.compare_digest(credentials.credentials, secret):
        raise HTTPException(401, {"code": "UNAUTHENTICATED", "message": "정리 작업 키가 아닙니다."})
    idle = timedelta(hours=state.session_idle_hours)
    return await records.cleanup(state.session_store, state.checkpointer, idle)
