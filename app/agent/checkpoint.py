"""체크포인터 — interrupt 사이에 세션 상태를 저장한다.

state에 Pydantic 모델과 Enum을 그대로 둔다. LangGraph의 기본 직렬화기는 등록하지 않은 타입을
체크포인트에서 복원할 때 경고하고, 앞으로는 막겠다고 예고했다(LANGGRAPH_STRICT_MSGPACK).
그래서 허용 목록(STATE_TYPES)을 직접 넘긴다. 목록 밖의 타입은 복원이 막혀 dict나 원시 값으로
돌아오므로, state에 들어가는 모델과 Enum은 모델 안에 든 것까지 등록한다.
빠뜨리면 tests/agent/test_graph.py가 실패한다.

서버는 세션 상태를 Supabase Postgres의 `langgraph` 스키마에 저장한다. 서버리스에서는 한 세션의
요청을 서로 다른 인스턴스가 받으므로 프로세스 메모리에 둘 수 없다. 테이블은 라이브러리의
setup()이 만든다(`make checkpoint-setup`). public 스키마는 Data API로 노출되므로 스키마를 따로 둔다.

라이브러리는 테이블 이름에 스키마를 붙이지 않는다. 그래서 search_path로 `langgraph`를 가리키는데,
transaction 모드 풀러에서는 SET이 다음 트랜잭션까지 남지 않아 연결 옵션(`-c search_path=`)으로
넘긴다. Supavisor는 search_path마다 풀을 따로 두고 서버 연결을 그 값으로 연다.

테스트와 DATABASE_URL 없이 띄운 서버는 InMemorySaver를 쓴다. 프로세스 메모리라 서버를 다시 띄우면
진행 중인 세션이 사라진다.
"""

import asyncio

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection
from psycopg.rows import dict_row, scalar_row
from psycopg_pool import AsyncConnectionPool

from app.agent.state import (
    ActivityContent,
    CarePlan,
    ContentStep,
    IntakeResult,
    NextStep,
    OutcomeEvaluation,
    PlannedActivity,
    ReplanProposal,
    Round,
    SessionEnd,
    SessionReport,
)
from app.config import get_settings
from app.schemas.care import CareGoal, CareRequest, ContentType, Place, UserProfile
from app.schemas.evidence import InterventionOrder
from app.schemas.measure import InstrumentId, Measurement, Timepoint

SCHEMA = "langgraph"
# 라이브러리 마이그레이션의 마지막 버전. 테이블이 이보다 낮으면 make checkpoint-setup을 다시 돌린다
LATEST_MIGRATION = len(AsyncPostgresSaver.MIGRATIONS) - 1

STATE_TYPES = (
    CareRequest,
    UserProfile,
    CareGoal,
    ContentType,
    Place,
    IntakeResult,
    InterventionOrder,
    SessionEnd,
    Measurement,
    InstrumentId,
    Timepoint,
    CarePlan,
    PlannedActivity,
    ActivityContent,
    ContentStep,
    SessionReport,
    OutcomeEvaluation,
    NextStep,
    ReplanProposal,
    Round,
)


def serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)


def memory_checkpointer() -> InMemorySaver:
    return InMemorySaver(serde=serializer())


def create_checkpoint_pool(database_url: str) -> AsyncConnectionPool:
    """체크포인터 전용 풀. 열지 않은 풀을 돌려주고, 호출한 쪽에서 `await pool.open()`으로 연다.

    autocommit·dict_row는 라이브러리가 요구한다. prepare는 app/db.py와 같은 까닭으로 끈다.
    체크포인터는 락을 잡고 연결을 하나씩만 쓰므로 연결 하나를 둔다.
    """
    return AsyncConnectionPool(
        database_url,
        open=False,
        min_size=1,
        # 서버리스 인스턴스가 쉬는 동안 끊긴 연결을 빌려주지 않게 빌려줄 때마다 확인한다
        check=AsyncConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": None,
            "options": f"-c search_path={SCHEMA}",
        },
    )


def postgres_checkpointer(pool: AsyncConnectionPool) -> AsyncPostgresSaver:
    return AsyncPostgresSaver(pool, serde=serializer())


async def setup_checkpoint_tables(database_url: str) -> None:
    """`langgraph` 스키마와 체크포인트 테이블을 만들고 라이브러리 마이그레이션을 적용한다.

    이미 적용한 마이그레이션은 건너뛰므로 다시 돌려도 된다.
    """
    async with create_checkpoint_pool(database_url) as pool:
        async with pool.connection() as conn:
            await conn.execute(f"create schema if not exists {SCHEMA}")
        await postgres_checkpointer(pool).setup()


async def checkpoint_version(conn: AsyncConnection) -> int | None:
    """적용된 체크포인트 마이그레이션 버전. 체크포인터 풀의 연결로 부른다.

    연결이 search_path로 `langgraph` 스키마를 가리키지 않거나 테이블이 없으면 None이다.
    """
    async with conn.cursor(row_factory=scalar_row) as cur:
        await cur.execute(
            "select current_schema() = %s and to_regclass('checkpoint_migrations') is not null",
            (SCHEMA,),
        )
        if not await cur.fetchone():
            return None
        await cur.execute("select max(v) from checkpoint_migrations")
        return await cur.fetchone()


if __name__ == "__main__":
    # make checkpoint-setup: .env의 DATABASE_URL에 체크포인트 테이블을 만든다
    database_url = get_settings().database_url
    if not database_url:
        raise SystemExit("DATABASE_URL이 비어 있습니다. .env를 채우세요.")
    asyncio.run(setup_checkpoint_tables(database_url))
    print(f"체크포인트 테이블 준비 완료 ({SCHEMA} 스키마, 마이그레이션 {LATEST_MIGRATION})")
