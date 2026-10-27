"""FastAPI 앱. 시작할 때 세션 그래프와 그 체크포인터를 `app.state`에 둔다.

`DATABASE_URL`이 있으면 DB 커넥션 풀(`app.state.db_pool`)과 체크포인터 전용 풀
(`app.state.checkpoint_pool`)을 열고, 세션 상태를 Postgres에 저장한다. 없으면 DB 없이 뜨고
세션 상태는 프로세스 메모리에 둔다(app/agent/checkpoint.py).

세션 그래프(`app.state.session_graph`)는 그 체크포인터로 조립한다. LLM 키가 하나도 없으면 조립하지
않고 세션 API만 503을 돌려준다(app/api/sessions.py). FE 오리진은 CORS_ORIGINS ·
CORS_ORIGIN_REGEX로 허용한다.

세션 기록(`app.state.session_store`)은 DB가 있으면 Postgres, 없으면 프로세스 메모리에 둔다
(app/records.py). 로그인 토큰 검증기(`app.state.auth`)는 SUPABASE_URL이 있을 때만 만들고, 없으면
세션 API가 503이다(app/auth.py).
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.agent.assembly import build_session_graph
from app.agent.checkpoint import (
    create_checkpoint_pool,
    memory_checkpointer,
    postgres_checkpointer,
)
from app.api import internal, me, sessions
from app.api.routes import router
from app.auth import AuthSessions, SupabaseVerifier
from app.config import Settings, get_settings
from app.db import create_pool
from app.records import MemoryStore, PostgresStore

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    pools = []
    if settings.database_url:
        db_pool = create_pool(settings.database_url)
        checkpoint_pool = create_checkpoint_pool(settings.database_url)
        pools = [db_pool, checkpoint_pool]
        app.state.db_pool = db_pool
        app.state.checkpoint_pool = checkpoint_pool
        app.state.checkpointer = postgres_checkpointer(checkpoint_pool)
        app.state.session_store = PostgresStore(
            db_pool, settings.text_retention_days, settings.guest_retention_days
        )
        app.state.auth_sessions = AuthSessions(db_pool)  # 로그아웃 · 탈퇴한 토큰을 바로 거절한다
    else:
        app.state.db_pool = None
        app.state.checkpoint_pool = None
        app.state.checkpointer = memory_checkpointer()
        app.state.session_store = MemoryStore(
            settings.text_retention_days, settings.guest_retention_days
        )
        app.state.auth_sessions = None
    app.state.auth = SupabaseVerifier(settings.supabase_url) if settings.supabase_url else None
    if app.state.auth is None:
        log.warning("SUPABASE_URL이 없어 세션 API가 503을 돌려줍니다(로그인 토큰을 검증할 수 없음)")
    app.state.cron_secret = settings.cron_secret
    app.state.session_idle_hours = settings.session_idle_hours
    try:
        app.state.session_graph = build_session_graph(app.state.checkpointer, settings)
    except RuntimeError as exc:  # LLM 키가 없다 — 세션 API만 503
        log.warning("세션 그래프를 조립하지 않았습니다: %s", exc)
        app.state.session_graph = None
    # 연결은 백그라운드에서 맺는다. DB에 문제가 있어도 서버는 뜨고 /health/db가 503을 돌려준다
    for pool in pools:
        await pool.open()
    try:
        yield
    finally:
        for pool in pools:
            await pool.close()


def add_cors(app: FastAPI, settings: Settings) -> None:
    """FE(다른 오리진)가 세션 API를 부르게 한다. Authorization은 인증(Supabase Auth)이 쓴다."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_origin_regex=settings.cors_origin_regex or None,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )


app = FastAPI(title="lumi-wellness-agent-be", lifespan=lifespan)
add_cors(app, get_settings())
app.include_router(router)
app.include_router(sessions.router)
app.include_router(me.router)
app.include_router(internal.router)
app.mount(
    "/assets",
    StaticFiles(directory=Path(__file__).resolve().parent / "static"),
    name="assets",
)
