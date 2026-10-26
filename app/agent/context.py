from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from app.schemas.care import CareGoal, ContentType
from app.schemas.evidence import InterventionOrder


@dataclass
class AgentContext:
    """그래프 실행 컨텍스트. state와 달리 체크포인트에 저장되지 않아 실행·재개 때마다 넘긴다."""

    # app.state.db_pool. 근거 검색·프로토콜 조회에 쓴다
    db_pool: AsyncConnectionPool | None = None


@dataclass
class PlannerContext(AgentContext):
    """Planner 에이전트(create_agent)의 실행 컨텍스트 = 그래프 컨텍스트 + 이번 세션에서 정해진 값.

    Planner의 Tool은 state가 아니라 이 컨텍스트만 받는다(`ToolRuntime[PlannerContext]`).
    목표 · 사용자 상태 · 쓸 수 있는 시간 · 이미지 선호는 계획 중에 바뀌지 않으므로 LLM이 Tool 인자로
    넘기지 않고 Tool이 여기서 읽는다. LLM은 고르는 값(프로토콜 · 검색 질의 · 인용할 근거)만 넘긴다 —
    gpt-4o-mini가 validate_evidence의 goal을 빠뜨린 호출을 다섯 번 되풀이한 일이 있다(2026-10-26).
    plan 노드가 인테이크로 채운다(nodes/planner.py planner_context).
    """

    goal: CareGoal | None = None
    user_order: InterventionOrder | None = None  # 인테이크 order_condition. None = 미상
    minutes_available: int | None = None
    content_type: ContentType = ContentType.CURATED_PHOTO
    # 이번 라운드에 내놓지 않을 프로토콜 — 직전 라운드에서 활동 변경(CHANGE_ACTIVITY)을 고른 것.
    # LLM 지시로만 막으면 gpt-4o-mini가 같은 것을 다시 고른다(2026-10-26 실측) → 목록에서 뺀다
    excluded_protocol_ids: tuple[str, ...] = ()
