"""검수된 활동 노드(Care Protocol) 저장소 — data/protocols/PR-*.json을 읽는다.

Protocol DB 테이블은 두지 않는다(발표 범위). 프로토콜은 검수된 정적 콘텐츠라 저장소에 함께 배포하고
시작할 때 한 번 읽는다. 테이블이 필요해지면 이 모듈의 함수 시그니처를 유지한 채 바꾼다.

- get_care_protocols Tool(app/agent/tools/protocol.py)은 candidates()의 compact()를 LLM에 준다.
- prepare(app/agent/nodes/executor.py)는 get_protocol()로 steps · guide를 읽는다.
- check_plan(app/agent/nodes/planner.py)은 get_protocol()로 검수 ID · 목표 · 순서 · 시간을 확인한다.
"""

from functools import lru_cache
from pathlib import Path

from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder
from app.schemas.protocol import CareProtocol, Composition

PROTOCOL_DIR = Path(__file__).resolve().parents[1] / "data" / "protocols"


@lru_cache
def load_protocols() -> dict[str, CareProtocol]:
    """파일명 순. 파일명과 protocol_id가 다르면 ValueError(tests/protocols가 잡는다)."""
    out: dict[str, CareProtocol] = {}
    for path in sorted(PROTOCOL_DIR.glob("PR-*.json")):
        protocol = CareProtocol.model_validate_json(path.read_text(encoding="utf-8"))
        if protocol.protocol_id != path.stem:
            raise ValueError(f"{path.name}: protocol_id {protocol.protocol_id}와 파일명이 다르다")
        out[protocol.protocol_id] = protocol
    return out


def get_protocol(protocol_id: str) -> CareProtocol | None:
    return load_protocols().get(protocol_id)


def candidates(
    goal: CareGoal,
    *,
    order: InterventionOrder | None = None,
    minutes: int | None = None,
) -> list[CareProtocol]:
    """목표에 맞는 검수 프로토콜. 단계 모듈은 뺀다. 주 목표가 앞, 부 목표가 뒤."""
    picked = [
        p
        for p in load_protocols().values()
        if p.composition is not Composition.STAGE_MODULE
        and p.serves(goal)
        and p.allows_order(order)
        and p.fits_minutes(minutes)
    ]
    return sorted(picked, key=lambda p: (p.goal is not goal, p.protocol_id))


def compact(protocol: CareProtocol) -> dict:
    """LLM용 압축본. 판단에 쓸 것만 — 본문 전체(content_spec · evidence)는 넣지 않는다."""
    duration = protocol.duration_minutes
    return {
        "protocol_id": protocol.protocol_id,
        "name": protocol.name,
        "summary": protocol.summary,
        "goal": protocol.goal.value,
        "secondary_goals": [g.value for g in protocol.secondary_goals],
        "media": [m.value for m in protocol.media],
        "composition": protocol.composition.value,
        "channel": protocol.channel.value,
        "duration_minutes": {"default": duration.default, "min": duration.min, "max": duration.max},
        "intervention_order": [o.value for o in protocol.intervention_order],
        "grades": {outcome.value: entry.grade.value for outcome, entry in protocol.grades.items()},
        "constraints": protocol.constraints,
        "safety": protocol.safety,
        "compatible_with": [r.protocol_id for r in protocol.compatible_with],
        "exclusive_with": [r.protocol_id for r in protocol.exclusive_with],
        "user_facing_flag": protocol.user_facing_flags.get("default"),
    }
