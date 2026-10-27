"""개념 정의 카드 — intake 목표 추론(S1 · S2 · S3)의 판단 기준.

data/construct_definitions.json을 읽는다.

카드는 팀이 R1(상태 분석) 논문을 읽고 쓴 요약(정의 · 단서 · 경계)이고, 원문 구간은 sources의
anchor로 가리킨다. 원문 구간 자체는 scripts/seed_definitions.py가 evidence_records에
record_type = DEFINITION으로 적재하고, intake가 하이브리드 검색으로 찾아 근거 ID와 함께 LLM에 준다
(app/agent/definitions.py). 검색 결과가 어느 목표의 정의인지는 이 카드의 paper_id로 안다.

state_hints는 State(사용자 상태)가 Goal을 어느 쪽으로 기울이는지다. 규칙이 아니라 단서다 —
피로 후 → S3(P06 이론 + 실험 설계 일관), 스트레스 후 → S1·S2(반복 사고 단서로 가른다),
스트레스 전 → S2(근거 arm 1건이라 약하게), 계기 없음 → 기울이지 않음.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Outcome

CARDS_PATH = Path(__file__).resolve().parents[1] / "data" / "construct_definitions.json"


class DefinitionSource(BaseModel):
    paper_id: str
    anchor: str  # 원문 문장의 첫머리. 공백·하이픈을 정규화해 대조한다


class GoalCard(BaseModel):
    name: str
    outcome: Outcome
    definition: str
    cues: list[str]
    boundary: str
    sources: list[DefinitionSource]


class StateHint(BaseModel):
    order_condition: InterventionOrder
    lean: list[CareGoal]
    strength: Literal["STRONG", "SPLIT", "WEAK", "NONE"]
    sources: list[str]
    note: str


class ConstructCards(BaseModel):
    goals: dict[CareGoal, GoalCard]
    state_hints: list[StateHint]
    boundary_source: DefinitionSource


@lru_cache
def load_cards() -> ConstructCards:
    return ConstructCards.model_validate_json(CARDS_PATH.read_text(encoding="utf-8"))


def goal_for_paper(paper_id: str) -> CareGoal | None:
    """이 논문이 어느 목표의 정의 출처인가. 경계 출처(P06)는 S3 카드에도 있어 S3다."""
    for goal, card in load_cards().goals.items():
        if any(s.paper_id == paper_id for s in card.sources):
            return goal
    return None


def state_hint(order: InterventionOrder | None) -> StateHint | None:
    if order is None:
        return None
    return next((h for h in load_cards().state_hints if h.order_condition is order), None)


def all_sources() -> list[DefinitionSource]:
    """적재 대상 — 목표 카드의 출처 전부와 경계 출처. 같은 (논문, anchor)는 한 번만."""
    cards = load_cards()
    seen: dict[tuple[str, str], DefinitionSource] = {}
    for card in cards.goals.values():
        for source in card.sources:
            seen.setdefault((source.paper_id, source.anchor), source)
    boundary = cards.boundary_source
    seen.setdefault((boundary.paper_id, boundary.anchor), boundary)
    return list(seen.values())


def cards_json() -> str:
    """LLM 입력용 — 목표 카드(이름 · 정의 · 단서 · 경계)와 상태 단서. 출처 anchor는 넣지 않는다."""
    cards = load_cards()
    payload = {
        "goals": {
            goal.value: {
                "name": card.name,
                "definition": card.definition,
                "cues": card.cues,
                "boundary": card.boundary,
            }
            for goal, card in cards.goals.items()
        },
        "state_hints": [
            {
                "order_condition": h.order_condition.value,
                "lean": [g.value for g in h.lean],
                "strength": h.strength,
                "note": h.note,
            }
            for h in cards.state_hints
        ],
    }
    return json.dumps(payload, ensure_ascii=False)
