"""Supabase Postgres 커넥션 풀. 연결마다 pgvector의 vector 타입을 등록한다.

트랜잭션 모드 풀러(포트 6543)는 prepared statement를 지원하지 않는다. 어느 연결 방식이든
같은 코드로 돌도록 psycopg의 자동 prepare(`prepare_threshold`)를 끈다.
vector 확장이 없는 DB(마이그레이션 전)에서는 타입 등록이 실패해 연결이 만들어지지 않는다.
"""

from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool


async def _register_types(conn: AsyncConnection) -> None:
    await register_vector_async(conn)


def create_pool(database_url: str) -> AsyncConnectionPool:
    """열지 않은 풀을 돌려준다. 호출한 쪽에서 `await pool.open()`으로 연다."""
    return AsyncConnectionPool(
        database_url,
        open=False,
        configure=_register_types,
        kwargs={"prepare_threshold": None},
    )
