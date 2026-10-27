"""intake 단서표 — ③ 목표 추론(S1 · S2 · S3)의 판단 기준. data/intake_cues.json을 읽는다.

목표마다 사용자 표현 단서(cues)와 헷갈리는 목표와의 경계(boundary), 그리고 사용자 상태가 목표를 어느
쪽으로 기울이는지(state_hints)다. 팀이 R1(상태 분석) 논문을 읽고 요약한 것이고 원문 인용이 아니다.

state_hints는 규칙이 아니라 단서다 — 피로 후 → S3(P06 이론 + 실험 설계 일관), 스트레스 후 →
S1·S2(반복 사고 단서로 가른다, P33), 스트레스 전 → S2(근거 arm 1건이라 약하게), 계기 없음 →
기울이지 않음.

개념 정의 JSON(② — 근거 paper_id와 원문 문장)은 팀원이 따로 준다. 그전까지 ③은 이 단서표만으로 돌고
IntakeResult.goal_evidence는 빈 목록이다.
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder

CUES_PATH = Path(__file__).resolve().parents[1] / "data" / "intake_cues.json"


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
