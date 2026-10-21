"""FastAPI 앱. `DATABASE_URL`이 있으면 시작할 때 DB 커넥션 풀을 열어 `app.state.db_pool`에 둔다."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import get_settings
from app.db import create_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    pool = create_pool(settings.database_url) if settings.database_url else None
    if pool is not None:
        # 연결은 백그라운드에서 맺는다. DB에 문제가 있어도 서버는 뜨고 /health/db가 503을 돌려준다
        await pool.open()
    app.state.db_pool = pool
    try:
        yield
    finally:
        if pool is not None:
            await pool.close()


app = FastAPI(title="lumi-wellness-agent-be", lifespan=lifespan)
app.include_router(router)
