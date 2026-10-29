from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from app.schemas.care import CareGoal, ContentType, Environment
from app.schemas.evidence import InterventionOrder


@dataclass
class AgentContext:
    """그래프 실행 컨텍스트. state와 달리 체크포인트에 저장되지 않아 실행·재개 때마다 넘긴다."""

    # app.state.db_pool. 근거 검색·검증에 쓴다
    db_pool: AsyncConnectionPool | None = None
    # 세션 ID(= thread_id). 후보 추천 순서(등급 가중 추첨)의 시드 — 재시도해도 같은 순서가 나온다.
    # None이면(평가 스크립트 · 테스트) 등급 순이다
    session_id: str | None = None


@dataclass
class PlannerContext(AgentContext):
    """plan 노드의 단계(후보 조회 · 근거 검색 · 검증)가 공유하는 세션 값.

    그래프 컨텍스트(DB 풀 · 세션 ID) + 인테이크에서 정해진 값. 목표 · 사용자 상태 · 쓸 수 있는
    시간 · 환경 · 이미지 선호 · 뺄 활동 · 최근에 한 활동은 계획 중에 바뀌지 않으므로 LLM이 정하지
    않고 코드가 여기서 읽는다. plan 노드가 인테이크로 채운다(nodes/planner.py planner_context).
    """

    goal: CareGoal | None = None
    user_order: InterventionOrder | None = None  # 인테이크 order_condition. None = 미상
    minutes_available: int | None = None
    environment: Environment | None = None  # 인테이크 environment — 후보를 환경으로 거른다
    content_type: ContentType = ContentType.CURATED_PHOTO
    # 이번 라운드에 내놓지 않을 프로토콜 — 이번 세션에서 활동 변경(CHANGE_ACTIVITY)을 고른 것
    excluded_protocol_ids: tuple[str, ...] = ()
    # 이 사용자가 지난 세션들에서 최근에 한 활동 — 추천 순서에서 가중치를 낮춘다
    recent_protocol_ids: tuple[str, ...] = ()
