"""Planner Tool — 근거 검색·검증.

실제 일은 app/retrieval/hybrid.py와 app/agent/validation.py가 한다.
docstring과 인자 설명이 LLM이 읽는 사용 설명서다. 결과는 LLM에 필요한 필드만 압축해 돌려준다.
"""

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Medium, Outcome


class SearchEvidenceArgs(BaseModel):
    """search_evidence 인자. 필드 설명이 LLM에 그대로 보인다."""

    query: str = Field(description="찾을 내용. 논문 용어(영어)로. 예: nature images rumination")
    medium: Medium | None = Field(default=None, description="자극 매체로 거른다")
    intervention_order: InterventionOrder | None = Field(
        default=None, description="중재 시점으로 거른다. 스트레스 뒤의 회복이면 POST_STRESS"
    )
    outcomes: list[Outcome] = Field(
        default_factory=list, description="실험에서 측정한 결과 지표로 거른다"
    )


@tool("search_evidence", args_schema=SearchEvidenceArgs)
async def search_evidence(
    runtime: ToolRuntime[AgentContext],
    query: str,
    medium: Medium | None = None,
    intervention_order: InterventionOrder | None = None,
    outcomes: list[Outcome] | None = None,
) -> list[dict]:
    """연구 근거(실험 단위 Evidence Record)를 찾는다.

    메타데이터로 거른 뒤 벡터·키워드 검색을 합친다.
    결과마다 evidence_id, 논문(저자·연도·DOI), 섹션, 원문 구간, 메타데이터가 있다.
    찾은 근거는 계획에 쓰기 전에 validate_evidence로 검증한다.
    """
    raise NotImplementedError("app/retrieval/hybrid.py를 구현한 뒤 연결한다")


class ValidateEvidenceArgs(BaseModel):
    """validate_evidence 인자."""

    evidence_ids: list[int] = Field(description="검증할 Evidence Record ID (search_evidence 결과)")
    goal: CareGoal = Field(description="계획 중인 세션")
    medium: Medium = Field(description="실제로 제공할 매체. AI 생성 이미지면 AI_IMAGE")


@tool("validate_evidence", args_schema=ValidateEvidenceArgs)
async def validate_evidence(
    runtime: ToolRuntime[AgentContext],
    evidence_ids: list[int],
    goal: CareGoal,
    medium: Medium,
) -> list[dict]:
    """EvidenceValidationTool — 근거를 이번 세션에 써도 되는지 검사한다.

    DOI·원문 존재, 중재 일치(이미지·노출 방식), 대상 적합성, 주장 검증(효과를 실제로 측정했는지),
    상충 근거, 생성 이미지(원본 자극과의 차이)를 본다. 통과하지 못한 근거는 계획에 쓰지 않는다.
    """
    raise NotImplementedError("app/agent/validation.py를 구현한 뒤 연결한다")
