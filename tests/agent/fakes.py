"""테스트 대역 — 대본 채팅 모델과 fixture 기반 가짜 검색·검증.

가짜 검색·검증은 app/retrieval/hybrid.search · app/agent/validation.validate와 같은 시그니처라
monkeypatch로 바꿔 끼운다. tests/fixtures/evidence_hits.json은 후보 저장소이고, 가짜 검색이 필터를
적용해 상위 TOP_K건만 돌려준다. 검증 규칙은 가짜가 아니라 validation.judge()를 그대로 쓴다.
"""

import json
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from pydantic import Field

from app.agent.validation import judge, missing_verdict
from app.retrieval.hybrid import DEFAULT_RECORD_TYPES, TOP_K
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType
from app.schemas.protocol import ProtocolEvidenceSection
from app.schemas.retrieval import EvidenceHit, EvidenceVerdict

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class ScriptedChatModel(GenericFakeChatModel):
    """`messages` 순서대로 AIMessage를 돌려준다. OpenAI 등 실제 LLM을 부르지 않는다.

    create_agent가 거치는 bind_tools는 받은 Tool 이름을 남기고 자신을 돌려준다.
    """

    offered: list[list[str]] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.offered.append([tool.name for tool in tools])
        return self


@lru_cache
def load_hits() -> tuple[EvidenceHit, ...]:
    data = json.loads((FIXTURES / "evidence_hits.json").read_text(encoding="utf-8"))
    return tuple(EvidenceHit.model_validate(item) for item in data)


def protocol_orders(protocol_id: str) -> list[InterventionOrder]:
    """fixture 프로토콜의 허용 시점. 운영에서는 Protocol DB가 준다."""
    path = FIXTURES / "protocols" / f"{protocol_id}.json"
    if not path.exists():
        return []
    section = ProtocolEvidenceSection.model_validate_json(path.read_text(encoding="utf-8"))
    return section.intervention_order


async def fake_search(
    pool,
    *,
    query: str,
    medium: Medium | None = None,
    intervention_order: InterventionOrder | None = None,
    outcomes: Sequence[Outcome] = (),
    record_type: RecordType | None = None,
    limit: int = TOP_K,
) -> list[EvidenceHit]:
    allowed = {record_type} if record_type else set(DEFAULT_RECORD_TYPES)
    wanted_orders = {intervention_order, InterventionOrder.NO_INDUCTION}
    out = []
    for hit in load_hits():
        meta = hit.metadata
        if meta.record_type not in allowed:
            continue
        if medium and medium not in meta.media:
            continue
        if (
            intervention_order
            and meta.intervention_orders
            and not wanted_orders & set(meta.intervention_orders)
        ):
            continue
        if outcomes and not {r.outcome for r in meta.results} & set(outcomes):
            continue
        out.append(hit)
    out.sort(key=lambda h: h.score, reverse=True)
    return out[:limit]


async def fake_validate(
    pool,
    *,
    evidence_record_ids: Sequence[int],
    goal: CareGoal,
    media: Sequence[Medium],
    protocol_id: str | None = None,
    user_order: InterventionOrder | None = None,
) -> list[EvidenceVerdict]:
    hits = load_hits()
    by_id = {hit.evidence_record_id: hit for hit in hits}
    if user_order:
        orders: list[InterventionOrder] = [user_order]
    else:
        orders = protocol_orders(protocol_id) if protocol_id else []
    return [
        judge(by_id[i], goal=goal, media=media, orders=orders, candidates=hits)
        if i in by_id
        else missing_verdict(i)
        for i in evidence_record_ids
    ]
