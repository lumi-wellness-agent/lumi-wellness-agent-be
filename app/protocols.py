"""검수된 활동 노드(Care Protocol) 저장소 — data/protocols/PR-*.json을 읽는다.

Protocol DB 테이블은 두지 않는다(발표 범위). 프로토콜은 검수된 정적 콘텐츠라 저장소에 함께 배포하고
시작할 때 한 번 읽는다. 테이블이 필요해지면 이 모듈의 함수 시그니처를 유지한 채 바꾼다.

- get_care_protocols Tool(app/agent/tools/protocol.py)은 candidates()의 compact()를 LLM에 준다.
- prepare(app/agent/nodes/executor.py)는 get_protocol()로 steps · guide를 읽는다.
- check_plan(app/agent/nodes/planner.py)은 get_protocol()로 검수 ID · 목표 · 순서 · 시간을 확인한다.
"""

from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

from app.schemas.care import CareGoal, ContentType
from app.schemas.evidence import InterventionOrder, Medium
from app.schemas.protocol import CareProtocol, Composition, Grade, SlotType
from app.schemas.retrieval import GOAL_OUTCOMES

PROTOCOL_DIR = Path(__file__).resolve().parents[1] / "data" / "protocols"

# 후보 정렬용 등급 순서. 미확인(측정했으나 원문 확인 전)은 C와 D 사이, —(측정한 근거 없음)이 끝
GRADE_ORDER: tuple[Grade, ...] = (Grade.A, Grade.B, Grade.C, Grade.PENDING, Grade.D, Grade.NONE)


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


def goal_grade_rank(protocol: CareProtocol, goal: CareGoal) -> tuple[int, ...]:
    """목표 1차 지표(retrieval.GOAL_OUTCOMES)의 등급 순위를 좋은 것부터. 작을수록 근거가 강하다.

    S2(SUBJECTIVE_STRESS · AFFECT): PR-06 A·A → (0, 0), PR-18 B·B → (1, 1), PR-03 —·— → (5, 5).
    """
    primary = GOAL_OUTCOMES[goal].primary
    return tuple(sorted(GRADE_ORDER.index(protocol.grades[o].grade) for o in primary))


def candidates(
    goal: CareGoal,
    *,
    order: InterventionOrder | None = None,
    minutes: int | None = None,
    exclude: Iterable[str] = (),
) -> list[CareProtocol]:
    """목표에 맞는 검수 프로토콜. 단계 모듈과 exclude(직전 라운드에서 바꾸기로 한 활동)는 뺀다.

    주 목표가 앞, 부 목표가 뒤이고, 같은 쪽에서는 목표 1차 지표 등급이 높은 순이다. 플래너(LLM)는
    목록의 앞쪽을 고르는 경향이 있어, 등급 순으로 두어야 근거가 강한 활동이 먼저 고려된다.
    """
    excluded = set(exclude)
    picked = [
        p
        for p in load_protocols().values()
        if p.composition is not Composition.STAGE_MODULE
        and p.protocol_id not in excluded
        and p.serves(goal)
        and p.allows_order(order)
        and p.fits_minutes(minutes)
    ]
    return sorted(
        picked, key=lambda p: (p.goal is not goal, goal_grade_rank(p, goal), p.protocol_id)
    )


def delivered_media(protocol: CareProtocol, content_type: ContentType) -> list[Medium]:
    """이번 세션에 실제로 제공할 구성 요소 — 근거 검증(validation.validate)의 media.

    media에 STATIC_IMAGE와 AI_IMAGE가 함께 있으면(대안, PR-01) 사용자 선호에 맞는 하나만 남긴다.
    선호가 AI 생성 이미지이고 정지 이미지 슬롯이 생성을 막지 않으면(source_policy.generated가
    FORBIDDEN이 아니면) STATIC_IMAGE를 AI_IMAGE로 바꾼다 — 검증이 ADAPTED_STIMULUS로 표시한다.
    """
    media = list(protocol.media)
    wants_generated = content_type is ContentType.AI_IMAGE
    if Medium.STATIC_IMAGE in media and Medium.AI_IMAGE in media:
        media.remove(Medium.STATIC_IMAGE if wants_generated else Medium.AI_IMAGE)
    elif wants_generated and Medium.STATIC_IMAGE in media:
        still_slots = [s for s in protocol.content_spec.slots if s.slot is SlotType.VISUAL_STILL]
        if not any(s.source_policy.get("generated") == "FORBIDDEN" for s in still_slots):
            media[media.index(Medium.STATIC_IMAGE)] = Medium.AI_IMAGE
    return media


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
