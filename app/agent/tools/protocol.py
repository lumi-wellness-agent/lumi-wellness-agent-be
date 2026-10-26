"""Planner Tool — 검수된 Care Protocol 조회. data/protocols/*.json(app/protocols.py)을 읽는다."""

from langchain.tools import ToolRuntime, tool

from app import protocols
from app.agent.context import PlannerContext


@tool("get_care_protocols")
async def get_care_protocols(runtime: ToolRuntime[PlannerContext]) -> list[dict] | str:
    """이번 세션(목표 · 사용자 상태 · 쓸 수 있는 시간)에 쓸 수 있는 검수된 Care Protocol 목록.
    계획의 활동은 이 목록에서만 고른다. 지난 라운드에서 바꾸기로 한 활동은 이미 빠져 있다.

    프로토콜마다 protocol_id, 매체(media), 구성(composition: SINGLE · VALIDATED_MULTIMODAL ·
    EXPERIMENTAL), 시간(duration_minutes default·min·max), 허용 사용자 상태(intervention_order;
    비어 있으면 제한 없음), 지표별 등급(grades: A·B·C·D·—·미확인), 필수 제한(constraints),
    안전(safety), 함께 쓸 수 있는 프로토콜(compatible_with)과 섞지 말아야 할
    프로토콜(exclusive_with)이 있다. 주 목표(goal)가 맞는 것이 앞에 오고, 같은 쪽에서는 목표 1차
    지표의 등급이 높은 순이다. EXPERIMENTAL은 근거가 없어 '실험적'으로 표시해야 한다.
    """
    session = runtime.context
    if session.goal is None:  # plan 노드는 목표가 정해진 세션에서만 돈다
        raise ValueError("PlannerContext에 세션 목표가 없다")
    found = protocols.candidates(
        session.goal,
        order=session.user_order,
        minutes=session.minutes_available,
        exclude=session.excluded_protocol_ids,
    )
    if not found:
        return "이번 세션의 목표·사용자 상태·시간에 맞는 프로토콜이 없다"
    return [protocols.compact(p) for p in found]
