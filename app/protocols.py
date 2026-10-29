"""검수된 활동 노드(Care Protocol) 저장소 — data/protocols/PR-*.json을 읽는다.

Protocol DB 테이블은 두지 않는다(발표 범위). 프로토콜은 검수된 정적 콘텐츠라 저장소에 함께 배포하고
시작할 때 한 번 읽는다. 테이블이 필요해지면 이 모듈의 함수 시그니처를 유지한 채 바꾼다.

- plan(app/agent/nodes/planner.py)은 plan_candidates()를 recommendation_order()로 세운 compact()를
  LLM에 주고, planned_minutes()로 시간을, delivered_media()로 검색·검증할 매체를 정하고,
  check_plan이 get_protocol()로 후검증한다. intake의 환경 질문 판정(intake_rules.candidate_ids)과
  replan의 활동 변경 가능 여부도 같은 plan_candidates()를 본다 — 셋이 다른 후보를 보면 묻는 질문 ·
  고르는 활동 · 선택지가 어긋난다.
- prepare(app/agent/nodes/executor.py)는 get_protocol()로 steps · guide · content_spec을 읽고,
  NOTICE 단계가 있으면 stage_module()의 화면 명세를 함께 싣는다.
"""

import random
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from app.schemas.care import CareGoal, ContentType, Environment
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


def stage_module() -> CareProtocol | None:
    """단계 모듈(PR-16 감정에 이름 붙이기) — 다른 활동의 NOTICE 단계를 그린다. 후보가 아니다."""
    return next(
        (p for p in load_protocols().values() if p.composition is Composition.STAGE_MODULE), None
    )


def goal_grade_rank(protocol: CareProtocol, goal: CareGoal) -> tuple[int, ...]:
    """목표 1차 지표(retrieval.GOAL_OUTCOMES)의 등급 순위를 좋은 것부터. 작을수록 근거가 강하다.

    S2(SUBJECTIVE_STRESS · AFFECT): PR-06 A·A → (0, 0), PR-18 B·B → (1, 1), PR-03 —·— → (5, 5).
    """
    primary = GOAL_OUTCOMES[goal].primary
    return tuple(sorted(GRADE_ORDER.index(protocol.grades[o].grade) for o in primary))


def environment_gaps(protocol: CareProtocol, environment: Environment | None) -> list[str]:
    """이 환경에서 이 활동을 하기 어려운 이유 — AUDIO · SCREEN · QUIET · OUTDOORS. 비면 할 수 있다.

    활동 노드의 eligibility(needs · quiet_required · place)와 환경(schemas/care.environment_for)을
    맞댄다. 모르는 조건(None)으로는 거르지 않는다.
    """
    if environment is None:
        return []
    eligibility = protocol.eligibility
    needs = set(eligibility.needs)
    gaps = []
    if "AUDIO" in needs and environment.audio is False:
        gaps.append("AUDIO")
    if "SCREEN" in needs and environment.screen is False:
        gaps.append("SCREEN")
    if eligibility.quiet_required and environment.quiet is False:
        gaps.append("QUIET")
    if ("OUTDOORS" in needs or eligibility.place == "OUTDOOR") and environment.outdoor is False:
        gaps.append("OUTDOORS")
    return gaps


def candidates(
    goal: CareGoal,
    *,
    order: InterventionOrder | None = None,
    minutes: int | None = None,
    environment: Environment | None = None,
    exclude: Iterable[str] = (),
) -> list[CareProtocol]:
    """목표에 맞는 검수 프로토콜. 단계 모듈과 exclude(직전 라운드에서 바꾸기로 한 활동)는 뺀다.

    environment를 주면 그 환경에서 할 수 있는 것만 남긴다(environment_gaps). 주 목표가 앞, 부 목표가
    뒤이고, 같은 쪽에서는 목표 1차 지표 등급이 높은 순이다(기준 순서). plan이 LLM에 보일 순서는
    recommendation_order()가 이 목록으로 다시 정한다(등급 가중 추첨).
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
        and not environment_gaps(p, environment)
    ]
    return sorted(
        picked, key=lambda p: (p.goal is not goal, goal_grade_rank(p, goal), p.protocol_id)
    )


@dataclass(frozen=True)
class CandidateSearch:
    protocols: list[CareProtocol]
    relaxed: tuple[str, ...] = ()  # 후보를 찾으려고 푼 조건 — "order" · "environment"


# 다 맞는 후보가 없을 때 푸는 순서. 사용자 상태(order)는 근거와 맞는지의 문제라 풀어도 활동은 할
# 수 있고, 환경(소리 · 조용함 · 화면 · 야외)은 풀면 지금 자리에서 하기 어렵다 — 그래서 상태를 먼저
# 푼다. 시간은 맨 마지막에만 푼다(아래). 그 전까지 가용 시간을 넘는 활동은 check_plan이 막는다
RELAX_STEPS: tuple[tuple[str, ...], ...] = (
    (),
    ("order",),
    ("environment",),
    ("order", "environment"),
    # 시간은 맨 마지막에 푼다 — 짧은 시간 칩(3분)을 골랐는데 그 목표에 그만큼 짧은 활동이 없을 때
    # (S3: PR-01 6분). 활동 시간은 줄이지 않고 근거 용량을 그대로 쓴다(plan이 planned_minutes(None))
    ("minutes",),
    ("order", "minutes"),
    ("environment", "minutes"),
    ("order", "environment", "minutes"),
)


def plan_candidates(
    goal: CareGoal,
    *,
    order: InterventionOrder | None = None,
    minutes: int | None = None,
    environment: Environment | None = None,
    exclude: Iterable[str] = (),
) -> CandidateSearch:
    """플래너가 고를 후보. 다 맞는 것이 없으면 RELAX_STEPS 순서로 조건을 풀고, 푼 조건을 남긴다.

    2026-10-27 프로토콜 기준 S3 + 사건 전(PRE_STRESS)은 상태를 맞는 S3 활동이 없어 상태를 푼다.
    """
    exclude = tuple(exclude)
    for relaxed in RELAX_STEPS:
        found = candidates(
            goal,
            order=None if "order" in relaxed else order,
            minutes=None if "minutes" in relaxed else minutes,
            environment=None if "environment" in relaxed else environment,
            exclude=exclude,
        )
        if found:
            if "minutes" in relaxed:
                # 시간을 풀었으면 가장 짧은 활동부터. 같은 길이면 원래 순서(목표 · 등급 순)
                found = sorted(found, key=_shortest)
            return CandidateSearch(found, relaxed)
    return CandidateSearch([])


def _shortest(protocol: CareProtocol) -> float:
    duration = protocol.duration_minutes
    return duration.min or duration.default


# 추천 순서의 등급 가중치 — 목표 1차 지표 중 가장 좋은 등급으로 정한다. 모두 0보다 커서 어느
# 후보든 맨 앞에 설 수 있고, 근거가 강한 활동이 더 자주 앞에 선다. 후보를 등급 순으로만 세우면
# 목표마다 같은 활동이 1순위로 고정돼 20개 중 11개만 고를 수 있었다(2026-10-29, 입력 조합 420개).
# 이 가중치로 S2 · 발표 앞둠 · 10분의 첫 추천이 D · — 활동일 확률은 균등 추첨 38% → 17%,
# PR-06(A · A)은 12% → 22%
GRADE_WEIGHT: dict[Grade, float] = {
    Grade.A: 4.0,
    Grade.B: 3.0,
    Grade.C: 2.0,
    Grade.PENDING: 1.0,
    Grade.D: 1.0,
    Grade.NONE: 1.0,
}
RECENT_FACTOR = 0.3  # 이 사용자가 최근 세션에서 한 활동의 가중치 배율 — 빼지 않고 덜 나오게
RECENT_ROUNDS = 5  # '최근' = 이 사용자의 지난 라운드 몇 개(세션 API가 시작할 때 기록에서 읽는다)

OrderMethod = Literal["WEIGHTED", "RANKED", "SHORTEST"]  # state.SelectionMethod 값


def selection_weight(protocol: CareProtocol, goal: CareGoal, recent: Collection[str] = ()) -> float:
    """추천 순서의 가중치 — 목표 1차 지표 최고 등급의 GRADE_WEIGHT, 최근에 한 활동이면 낮춘다."""
    best = GRADE_ORDER[min(goal_grade_rank(protocol, goal))]
    weight = GRADE_WEIGHT[best]
    return weight * RECENT_FACTOR if protocol.protocol_id in recent else weight


@dataclass(frozen=True)
class RecommendationOrder:
    """LLM에 보일 후보 순서. weights는 순서를 정한 가중치 — 가중 추첨일 때만 있다."""

    protocols: list[CareProtocol]
    method: OrderMethod
    weights: dict[str, float] = field(default_factory=dict)

    def probability(self, protocol_id: str) -> float | None:
        """그 활동이 맨 앞에 설 확률(가중 추첨) = 가중치 / 가중치 합. 가중치가 없으면 None."""
        if not self.weights:
            return None
        return self.weights[protocol_id] / sum(self.weights.values())


def recommendation_order(
    search: CandidateSearch,
    goal: CareGoal,
    *,
    seed: str | None = None,
    recent: Collection[str] = (),
) -> RecommendationOrder:
    """후보의 추천 순서. 플래너(LLM)는 맨 앞을 고르고, 제약 · 장소에 걸릴 때만 건너뛴다.

    - 시간 조건을 풀었으면(relaxed에 minutes) 짧은 활동부터(plan_candidates 순서) — 가용 시간을
      덜 넘는 활동이 먼저다.
    - seed가 있으면(운영 — 세션 ID) 등급 가중 추첨이다. 활동마다 (seed, protocol_id)로 고정한 난수
      u에서 키 u^(1/w)를 만들어 큰 순으로 세운다(Efraimidis–Spirakis 가중 무작위 순열). 활동이 맨
      앞에 설 확률이 w / Σw이고, 후보 하나를 빼도(활동 변경) 나머지 순서가 그대로라 다음 라운드는
      같은 순열의 다음 활동이다. 같은 세션은 재시도해도 같은 순서다.
    - seed가 없으면(평가 스크립트 · 테스트) 등급 순(plan_candidates 순서) 그대로다.
    """
    found = search.protocols
    if "minutes" in search.relaxed:
        return RecommendationOrder(list(found), "SHORTEST")
    if seed is None:
        return RecommendationOrder(list(found), "RANKED")
    weights = {p.protocol_id: selection_weight(p, goal, recent) for p in found}

    def key(protocol: CareProtocol) -> float:
        u = random.Random(f"{seed}:{protocol.protocol_id}").random()
        return u ** (1 / weights[protocol.protocol_id])

    return RecommendationOrder(sorted(found, key=key, reverse=True), "WEIGHTED", weights)


def planned_minutes(protocol: CareProtocol, available: int | None) -> int:
    """활동 시간. 프로토콜 기본값(근거가 있는 용량)을 쓰되 쓸 수 있는 시간을 넘기지 않고, 최소 시간
    아래로는 줄이지 않는다(그러면 check_plan이 가용 시간 초과로 거른다)."""
    duration = protocol.duration_minutes
    minutes = duration.default if available is None else min(duration.default, available)
    if duration.min is not None:
        minutes = max(minutes, duration.min)
    return max(1, round(minutes))


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
