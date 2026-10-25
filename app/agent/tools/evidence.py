"""Planner Tool — 근거 검색·검증.

실제 일은 app/retrieval/hybrid.py(search)와 app/agent/validation.py(validate)가 한다.
docstring과 Field(description)이 LLM이 읽는 사용 설명서다 — 코드 주석은 LLM에 보이지 않는다.
결과는 EvidenceHit · EvidenceVerdict(app/schemas/retrieval.py)를
model_dump(mode="json")한 list[dict]다. 테스트는 hybrid.search · validation.validate를
가짜로 바꿔 끼운다(tests/agent/fakes.py).
"""

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app.agent import validation
from app.agent.context import AgentContext
from app.retrieval import hybrid
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType, study_medium


class SearchEvidenceArgs(BaseModel):
    """search_evidence 인자. 필드 설명이 LLM에 그대로 보인다."""

    query: str = Field(description="찾을 내용. 논문 용어(영어)로. 예: nature images rumination")
    medium: Medium | None = Field(
        default=None,
        description="연구 매체로 거른다. AI_IMAGE는 연구가 없어 STATIC_IMAGE로 바꿔 찾는다",
    )
    intervention_order: InterventionOrder | None = Field(
        default=None,
        description="사용자 상태에 맞는 연구 조건. 스트레스 뒤 회복이면 POST_STRESS. "
        "유도 없이 수행한 연구(NO_INDUCTION)는 어떤 값을 줘도 함께 나온다",
    )
    outcomes: list[Outcome] = Field(
        default_factory=list,
        description="측정한 결과 지표로 거른다. 여러 개면 하나라도 측정한 레코드",
    )
    record_type: RecordType | None = Field(
        default=None,
        description="레코드 종류. 비우면 ARM·SYNTHESIS·OBSERVATION만(이론 정의·참조표 제외)",
    )


@tool("search_evidence", args_schema=SearchEvidenceArgs)
async def search_evidence(
    runtime: ToolRuntime[AgentContext],
    query: str,
    medium: Medium | None = None,
    intervention_order: InterventionOrder | None = None,
    outcomes: list[Outcome] | None = None,
    record_type: RecordType | None = None,
) -> list[dict]:
    """연구 근거(arm 단위 Evidence Record)를 찾는다. 하이브리드 검색 상위 5건.

    결과마다 evidence_record_id, paper(제목·저자·연도·DOI), experiment_id, section,
    excerpt(원문 그대로), metadata(media · intervention_orders · results · n …), score가 있다.
    필터는 자동으로 완화하지 않는다 — 0건이면 필터를 바꿔 다시 부른다.
    인용하기 전에 validate_evidence로 검증한다.
    """
    hits = await hybrid.search(
        runtime.context.db_pool,
        query=query,
        medium=study_medium(medium) if medium else None,
        intervention_order=intervention_order,
        outcomes=outcomes or [],
        record_type=record_type,
    )
    return [hit.model_dump(mode="json") for hit in hits]


class ValidateEvidenceArgs(BaseModel):
    """validate_evidence 인자."""

    evidence_record_ids: list[int] = Field(
        description="검증할 Evidence Record ID (search_evidence 결과)"
    )
    goal: CareGoal = Field(description="계획 중인 세션")
    media: list[Medium] = Field(
        min_length=1, description="실제로 제공할 구성 요소 전부. AI 생성 이미지는 AI_IMAGE"
    )
    protocol_id: str | None = Field(
        default=None,
        description="활동의 Care Protocol ID. 주면 허용 시점·시간 조건을 프로토콜에서 읽는다",
    )
    user_order: InterventionOrder | None = Field(
        default=None,
        description="사용자 상태에서 본 시점. 높은 스트레스 뒤 회복이면 POST_STRESS. "
        "비우면 protocol_id의 허용 시점을 쓰고, 둘 다 없으면 매체만 검사한다",
    )


@tool("validate_evidence", args_schema=ValidateEvidenceArgs)
async def validate_evidence(
    runtime: ToolRuntime[AgentContext],
    evidence_record_ids: list[int],
    goal: CareGoal,
    media: list[Medium],
    protocol_id: str | None = None,
    user_order: InterventionOrder | None = None,
) -> list[dict]:
    """근거를 이번 활동에 인용해도 되는지 검사한다. 입력 ID 순서대로 하나씩 결과를 돌려준다.

    usable=False인 근거는 계획에 넣지 않는다(출처 추적 불가 · 중재 불일치 · 목표 지표 미측정).
    usable이어도 supports_goal=False면 상충·참고 근거로만 인용한다. supported_outcomes에 있는
    지표만 '개선'으로 말한다 — 정서(AFFECT) 결과를 스트레스 감소로 바꿔 말하지 않는다.
    flags의 ADAPTED_STIMULUS는 제공 콘텐츠가 연구 자극과 다르다는 뜻이다(AI 생성 이미지 등).
    인용을 막지는 않지만 Reference 카드에 그 차이를 한 줄로 밝힌다.
    """
    verdicts = await validation.validate(
        runtime.context.db_pool,
        evidence_record_ids=evidence_record_ids,
        goal=goal,
        media=media,
        protocol_id=protocol_id,
        user_order=user_order,
    )
    return [verdict.model_dump(mode="json") for verdict in verdicts]
