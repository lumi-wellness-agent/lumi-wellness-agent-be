"""Postgres 체크포인터를 실제 DB로 확인한다. TEST_DATABASE_URL이 없으면 건너뛴다(CI에는 DB가 없다).

TEST_DATABASE_URL에는 supabase/migrations를 적용한 DB를 쓴다. Supabase transaction pooler처럼
prepared statement가 없는 조건은 PgBouncer transaction 모드로 만든다
(max_prepared_statements = 0, track_extra_parameters에 search_path 추가).
"""

import asyncio
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from starlette.datastructures import State

from app.agent.checkpoint import (
    LATEST_MIGRATION,
    checkpoint_version,
    create_checkpoint_pool,
    postgres_checkpointer,
    setup_checkpoint_tables,
)
from app.agent.context import AgentContext
from app.agent.graph import build_graph
from app.agent.state import CarePlan
from app.config import get_settings
from app.main import app
from app.schemas.care import CareGoal, CareRequest
from tests.agent.test_graph import (
    BREATHING,
    S2_READING,
    STAI6,
    FakeIntaker,
    FakePlanner,
    inference,
    sam,
)

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL이 없다")


def s2_intaker() -> FakeIntaker:
    return FakeIntaker(S2_READING, goal=inference(CareGoal.S2))


async def resume_on_another_instance() -> None:
    await setup_checkpoint_tables(DATABASE_URL)
    await setup_checkpoint_tables(DATABASE_URL)  # 다시 돌려도 된다
    thread_id = f"test-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    # 인스턴스 A: 세션을 시작해 사전 측정을 넣고 run_session에서 멈춘다
    async with create_checkpoint_pool(DATABASE_URL) as pool:
        graph = build_graph(FakePlanner(BREATHING), s2_intaker(), postgres_checkpointer(pool))
        request = {"request": CareRequest(text="발표 앞두고 긴장돼요")}
        out = await graph.ainvoke(request, config, context=AgentContext())
        assert out["__interrupt__"][0].value["measure"] == "PRE"
        resume = Command(resume={**sam(3, 7), "INS-STAI6": STAI6})
        out = await graph.ainvoke(resume, config, context=AgentContext())
        assert out["__interrupt__"][0].value["plan"]["activity"]["protocol_id"] == "PR-06"

    # 인스턴스 B: 풀과 체크포인터를 새로 만들어 같은 thread를 이어 간다.
    # 계획이 없는 Planner를 넣어, 재개할 때 plan을 다시 돌면 실패하게 한다
    async with create_checkpoint_pool(DATABASE_URL) as pool:
        checkpointer = postgres_checkpointer(pool)
        graph = build_graph(FakePlanner(), s2_intaker(), checkpointer)
        out = await graph.ainvoke(
            Command(resume={"completed": True}), config, context=AgentContext()
        )
        assert out["__interrupt__"][0].value["measure"] == "POST"
        out = await graph.ainvoke(Command(resume=sam(5, 4)), config, context=AgentContext())
        assert out["evaluation"].deltas == {"valence": 2.0, "arousal": -3.0}
        assert out["__interrupt__"][0].value["proposal"]["recommended"] == "END"
        assert isinstance(out["request"], CareRequest)
        assert isinstance(out["plan"], CarePlan)

        async with pool.connection() as conn:
            assert await checkpoint_version(conn) == LATEST_MIGRATION
        await checkpointer.adelete_thread(thread_id)


def test_session_resumes_on_another_instance():
    asyncio.run(resume_on_another_instance())


def test_app_stores_sessions_in_postgres(monkeypatch):
    asyncio.run(setup_checkpoint_tables(DATABASE_URL))
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    # lifespan이 남기는 풀(종료 후 닫힘)이 다른 테스트의 app.state로 새지 않게 한다
    monkeypatch.setattr(app, "state", State())
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            assert isinstance(app.state.checkpointer, AsyncPostgresSaver)
            response = client.get("/health/db")
    finally:
        get_settings.cache_clear()

    assert response.status_code == 200
    assert response.json()["checkpoint_migration"] == LATEST_MIGRATION
