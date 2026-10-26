"""Planner Tool — 근거 검색·검증.

실제 일은 app/retrieval/hybrid.py(search)와 app/agent/validation.py(validate)가 한다.
docstring과 Field(description)이 LLM이 읽는 사용 설명서다 — 코드 주석은 LLM에 보이지 않는다.
결과는 EvidenceHit · EvidenceVerdict(app/schemas/retrieval.py)를
model_dump(mode="json")한 list[dict]다. 테스트는 hybrid.search · validation.validate를
가짜로 바꿔 끼운다(tests/agent/fakes.py).

세션에서 정해진 값(목표 · 사용자 상태 · 이미지 선호)은 LLM 인자가 아니라 PlannerContext에서 읽는다.
검색 필터(medium · intervention_order · outcomes)만은 LLM이 넘긴다 — 0건이면 LLM이 필터를 빼고
다시 찾아야 하기 때문이다.
"""

from collections.abc import Sequence

from langchain.tools import ToolRuntime, tool
from pydantic import BaseModel, Field

from app import protocols
from app.agent import validation
from app.agent.context import PlannerContext
from app.retrieval import hybrid
from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType, study_medium
from app.schemas.protocol import CareProtocol
from app.schemas.retrieval import EvidenceVerdict


class SearchEvidenceArgs(BaseModel):
    """search_evidence 인자. 필드 설명이 LLM에 그대로 보인다."""

    query: str = Field(
        description="찾을 내용. 활동과 목표 지표를 논문 용어(영어)로. "
        "예: slow breathing anxiety stress · mindfulness rumination · nature images rumination"
    )
    medium: Medium | None = Field(
        default=None,
        description="고른 활동의 매체 하나로 거른다. "
        "AI_IMAGE는 연구가 없어 STATIC_IMAGE로 바꿔 찾는다",
    )
    intervention_order: InterventionOrder | None = Field(
        default=None,
        description="사용자 상태(인테이크 order_condition)에 맞는 연구 조건. 스트레스 뒤 회복이면 "
        "POST_STRESS. 유도 없이 수행한 연구(NO_INDUCTION)는 어떤 값을 줘도 함께 나온다. "
        "모르면 비운다",
    )
    outcomes: list[Outcome] = Field(
        default_factory=list,
        description="목표 1차 지표로 거른다. 여러 개면 하나라도 측정한 레코드",
    )
    record_type: RecordType | None = Field(
        default=None,
        description="레코드 종류. 비우면 ARM·SYNTHESIS·OBSERVATION만(이론 정의·참조표 제외)",
    )


@tool("search_evidence", args_schema=SearchEvidenceArgs)
async def search_evidence(
    runtime: ToolRuntime[PlannerContext],
    query: str,
    medium: Medium | None = None,
    intervention_order: InterventionOrder | None = None,
    outcomes: list[Outcome] | None = None,
    record_type: RecordType | None = None,
) -> list[dict] | str:
    """고른 활동의 연구 근거(arm 단위 Evidence Record)를 찾는다. 하이브리드 검색 상위 5건.

    결과마다 evidence_record_id, paper(제목·저자·연도·DOI), experiment_id, section,
    excerpt(원문 그대로), metadata(media · intervention_orders · results · n …), score가 있다.
    결과의 evidence_record_id를 모두 validate_evidence에 넣어 검증한다 — 검증하지 않은 근거는
    인용하지 않는다. 필터는 자동으로 완화하지 않는다 — 0건이면 필터를 줄여 다시 부른다.
    """
    hits = await hybrid.search(
        runtime.context.db_pool,
        query=query,
        medium=study_medium(medium) if medium else None,
        intervention_order=intervention_order,
        outcomes=outcomes or [],
        record_type=record_type,
    )
    if not hits:
        # 빈 목록은 ToolMessage 내용이 비어 모델이 '결과 없음'으로 읽지 못한다
        return "0건. 필터를 하나씩 빼고(outcomes → intervention_order → medium) 다시 찾는다"
    return [hit.model_dump(mode="json") for hit in hits]


async def validate_for_session(
    session: PlannerContext, protocol: CareProtocol, evidence_record_ids: Sequence[int]
) -> list[EvidenceVerdict]:
    """이번 세션의 목표 · 사용자 상태 · 제공 매체(protocols.delivered_media)로 근거를 검증한다.

    validate_evidence Tool과 plan 노드의 재검증(planner.keep_usable_evidence)이 같은 값으로
    판정하도록 한곳에 둔다. 사용자 상태가 미상이면 validate가 프로토콜의 허용 시점을 쓴다.
    """
    return await validation.validate(
        session.db_pool,
        evidence_record_ids=evidence_record_ids,
        goal=session.goal,
        media=protocols.delivered_media(protocol, session.content_type),
        protocol_id=protocol.protocol_id,
        user_order=session.user_order,
    )


class ValidateEvidenceArgs(BaseModel):
    """validate_evidence 인자. 목표 · 사용자 상태 · 제공 매체는 Tool이 세션에서 읽는다."""

    evidence_record_ids: list[int] = Field(
        min_length=1,
        description="검증할 Evidence Record ID — search_evidence 결과의 evidence_record_id. "
        "프로토콜 ID가 아니다",
    )
    protocol_id: str = Field(description="고른 활동의 Care Protocol ID (get_care_protocols 결과)")


@tool("validate_evidence", args_schema=ValidateEvidenceArgs)
async def validate_evidence(
    runtime: ToolRuntime[PlannerContext], evidence_record_ids: list[int], protocol_id: str
) -> list[dict] | str:
    """search_evidence로 찾은 근거 레코드를 고른 활동에 인용해도 되는지 검사한다. 프로토콜을
    검사하는 도구가 아니다 — 먼저 search_evidence로 근거를 찾는다. 입력 ID 순서대로 결과를 돌려준다.
    목표 · 사용자 상태와 실제로 제공할 매체(AI 생성 이미지 포함)는 이번 세션 값으로 검사한다.

    usable=False인 근거는 계획에 넣지 않는다(출처 추적 불가 · 중재 불일치 · 목표 지표 미측정).
    usable이어도 supports_goal=False면 상충·참고 근거로만 인용한다. supported_outcomes에 있는
    지표만 '개선'으로 말한다 — 정서(AFFECT) 결과를 스트레스 감소로 바꿔 말하지 않는다.
    flags의 ADAPTED_STIMULUS는 제공 콘텐츠가 연구 자극과 다르다는 뜻이다(AI 생성 이미지 등).
    인용을 막지는 않지만 Reference 카드에 그 차이를 한 줄로 밝힌다.
    """
    protocol = protocols.get_protocol(protocol_id)
    if protocol is None:
        return f"{protocol_id}는 검수된 프로토콜이 아니다. get_care_protocols 결과에서 고른다"
    verdicts = await validate_for_session(runtime.context, protocol, evidence_record_ids)
    return [verdict.model_dump(mode="json") for verdict in verdicts]
