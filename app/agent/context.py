from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool


@dataclass
class AgentContext:
    """그래프 실행 컨텍스트. state와 달리 체크포인트에 저장되지 않아 실행·재개 때마다 넘긴다.

    Planner의 Tool은 state가 아니라 이 컨텍스트만 받는다(`ToolRuntime[AgentContext]`).
    """

    # app.state.db_pool. 근거 검색·프로토콜 조회에 쓴다
    db_pool: AsyncConnectionPool | None = None
