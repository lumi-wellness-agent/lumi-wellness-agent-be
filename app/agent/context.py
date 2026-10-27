from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from app.schemas.care import CareGoal, ContentType
from app.schemas.evidence import InterventionOrder


@dataclass
class AgentContext:
    """그래프 실행 컨텍스트. state와 달리 체크포인트에 저장되지 않아 실행·재개 때마다 넘긴다."""

    # app.state.db_pool. 근거 검색·검증에 쓴다
    db_pool: AsyncConnectionPool | None = None


@dataclass
class PlannerContext(AgentContext):
    """plan 노드의 단계(후보 조회 · 근거 검색 · 검증)가 공유하는 세션 값.

    그래프 컨텍스트(DB 풀) + 인테이크에서 정해진 값. 목표 · 사용자 상태 · 쓸 수 있는 시간 · 이미지
    선호 · 뺄 활동은 계획 중에 바뀌지 않으므로 LLM이 정하지 않고 코드가 여기서 읽는다. plan 노드가
    인테이크로 채운다(nodes/planner.py planner_context).
    """

    goal: CareGoal | None = None
    user_order: InterventionOrder | None = None  # 인테이크 order_condition. None = 미상
    minutes_available: int | None = None
    content_type: ContentType = ContentType.CURATED_PHOTO
    # 이번 라운드에 내놓지 않을 프로토콜 — 직전 라운드에서 활동 변경(CHANGE_ACTIVITY)을 고른 것
    excluded_protocol_ids: tuple[str, ...] = ()
