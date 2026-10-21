"""Planner Tool — 검수된 Care Protocol 조회. Protocol DB(테이블 미정)를 읽는다."""

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.schemas.care import CareGoal


class GetCareProtocolsArgs(BaseModel):
    """get_care_protocols 인자."""

    goal: CareGoal = Field(description="세션 (S1·S2·S3)")


@tool("get_care_protocols", args_schema=GetCareProtocolsArgs)
async def get_care_protocols(runtime: ToolRuntime[AgentContext], goal: CareGoal) -> list[dict]:
    """세션에 쓸 수 있는 검수된 Care Protocol 목록을 돌려준다. 계획의 활동은 이 목록에서만 고른다.

    프로토콜마다 protocol_id, 활동 매체, 시간, 필수 제한(예: 숨 참기·과호흡 강요 금지),
    함께 실행할 수 있는 프로토콜이 있다. 동시·순차 복합은 서로 함께 실행할 수 있는 것끼리만 묶는다.
    """
    raise NotImplementedError("Protocol DB 테이블을 정한 뒤 연결한다")
