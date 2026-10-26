"""Planner Tool — 검수된 Care Protocol 조회. data/protocols/*.json(app/protocols.py)을 읽는다."""

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app import protocols
from app.agent.context import AgentContext
from app.schemas.care import CareGoal


class GetCareProtocolsArgs(BaseModel):
    """get_care_protocols 인자."""

    goal: CareGoal = Field(description="세션 목표 (S1 반추 · S2 긴장·스트레스 · S3 주의·피로)")


@tool("get_care_protocols", args_schema=GetCareProtocolsArgs)
async def get_care_protocols(runtime: ToolRuntime[AgentContext], goal: CareGoal) -> list[dict]:
    """목표에 쓸 수 있는 검수된 Care Protocol 목록을 돌려준다. 계획의 활동은 이 목록에서만 고른다.

    프로토콜마다 protocol_id, 매체(media), 구성(composition: SINGLE · VALIDATED_MULTIMODAL ·
    EXPERIMENTAL), 시간(duration_minutes default·min·max), 허용 사용자 상태(intervention_order;
    비어 있으면 제한 없음), 지표별 등급(grades: A·B·C·D·—·미확인), 필수 제한(constraints),
    안전(safety),
    함께 쓸 수 있는 프로토콜(compatible_with)과 섞지 말아야 할 프로토콜(exclusive_with)이 있다.
    주 목표(goal)가 맞는 것이 앞에 온다. EXPERIMENTAL은 근거가 없어 '실험적'으로 표시해야 한다.
    """
    return [protocols.compact(p) for p in protocols.candidates(goal)]
