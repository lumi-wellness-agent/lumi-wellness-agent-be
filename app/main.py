"""FastAPI 앱. 시작할 때 세션 그래프의 체크포인터를 `app.state.checkpointer`에 둔다.

`DATABASE_URL`이 있으면 DB 커넥션 풀(`app.state.db_pool`)과 체크포인터 전용 풀
(`app.state.checkpoint_pool`)을 열고, 세션 상태를 Postgres에 저장한다. 없으면 DB 없이 뜨고
세션 상태는 프로세스 메모리에 둔다(app/agent/checkpoint.py).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.checkpoint import (
    create_checkpoint_pool,
    memory_checkpointer,
    postgres_checkpointer,
)
from app.api.routes import router
from app.config import get_settings
from app.db import create_pool


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
    else:
        app.state.db_pool = None
        app.state.checkpoint_pool = None
        app.state.checkpointer = memory_checkpointer()
    # 연결은 백그라운드에서 맺는다. DB에 문제가 있어도 서버는 뜨고 /health/db가 503을 돌려준다
    for pool in pools:
        await pool.open()
    try:
        yield
    finally:
        for pool in pools:
            await pool.close()


app = FastAPI(title="lumi-wellness-agent-be", lifespan=lifespan)
app.include_router(router)
