"""intake 단서표와 개념 정의 — ③ 목표 추론(S1 · S2 · S3)의 판단 기준. data/intake_cues.json ·
data/goal_definitions.json을 읽는다.

목표마다 사용자 표현 단서(cues)와 헷갈리는 목표와의 경계(boundary), 그리고 사용자 상태가 목표를 어느
쪽으로 기울이는지(state_hints)다. 팀이 R1(상태 분석) 논문을 읽고 요약한 것이고 원문 인용이 아니다.

state_hints는 규칙이 아니라 단서다 — 피로 후 → S3(P06 이론 + 실험 설계 일관), 스트레스 후 →
S1·S2(반복 사고 단서로 가른다, P33), 스트레스 전 → S2(근거 arm 1건이라 약하게), 계기 없음 →
기울이지 않음.

개념 정의(②)는 목표마다 R1 논문의 원문 문장이다 — 단서표가 팀 요약이라면 정의는 인용이다. ③ 입력에
통째로 넣고(definitions_json), 추론한 목표(와 부목표)의 정의가 IntakeResult.goal_evidence가 되어
Reference 카드에 실린다(intake_rules.apply_goal). 문장이 열 개 남짓이라 검색(RAG)하지 않는다.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder

CUES_PATH = Path(__file__).resolve().parents[1] / "data" / "intake_cues.json"
DEFINITIONS_PATH = CUES_PATH.with_name("goal_definitions.json")


class GoalCues(BaseModel):
    name: str
    cues: list[str]
    boundary: str
    sources: list[str]  # 요약에 쓴 논문 ID(R1)


class StateHint(BaseModel):
    order_condition: InterventionOrder
    lean: list[CareGoal]
    strength: Literal["STRONG", "SPLIT", "WEAK", "NONE"]
    sources: list[str]
    note: str


class CueTable(BaseModel):
    goals: dict[CareGoal, GoalCues]
    state_hints: list[StateHint]


@lru_cache
def load_cues() -> CueTable:
    return CueTable.model_validate_json(CUES_PATH.read_text(encoding="utf-8"))


def state_hint(order: InterventionOrder | None) -> StateHint | None:
    if order is None:
        return None
    return next((h for h in load_cues().state_hints if h.order_condition is order), None)


def cues_json() -> str:
    """LLM 입력용 — 목표 단서 · 경계와 상태 단서."""
    table = load_cues()
    payload = {
        "goals": {
            goal.value: {"name": g.name, "cues": g.cues, "boundary": g.boundary}
            for goal, g in table.goals.items()
        },
        "state_hints": [
            {
                "order_condition": h.order_condition.value,
                "lean": [g.value for g in h.lean],
                "strength": h.strength,
                "note": h.note,
            }
            for h in table.state_hints
        ],
    }
    return json.dumps(payload, ensure_ascii=False)


class GoalDefinition(BaseModel):
    """개념 정의 한 문장 — 원문 그대로(줄바꿈 · 하이픈 · 쪽 넘김만 정리)."""

    paper_id: str
    concept: str  # 정의하는 개념 — rumination · state anxiety · mental fatigue …
    page: str  # PDF 쪽 번호. 쪽을 넘기는 문장은 "1–2"
    sentence: str


class DefinitionTable(BaseModel):
    goals: dict[CareGoal, list[GoalDefinition]]


@lru_cache
def load_definitions() -> DefinitionTable:
    return DefinitionTable.model_validate_json(DEFINITIONS_PATH.read_text(encoding="utf-8"))


def definitions_for(goal: CareGoal) -> list[GoalDefinition]:
    return load_definitions().goals.get(goal, [])


def definitions_json() -> str:
    """LLM 입력용 — 목표마다 개념과 원문 정의(출처 ID)."""
    payload = {
        goal.value: [
            {"paper_id": d.paper_id, "concept": d.concept, "sentence": d.sentence} for d in defs
        ]
        for goal, defs in load_definitions().goals.items()
    }
    return json.dumps(payload, ensure_ascii=False)
