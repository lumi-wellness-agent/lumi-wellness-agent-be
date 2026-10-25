"""Planner 도구의 출력 계약 — search_evidence는 EvidenceHit, validate_evidence는 EvidenceVerdict.

도구는 `[m.model_dump(mode="json") for m in …]`를 돌려준다. 플래너 담당은 fixture
(tests/fixtures/evidence_hits.json)를 후보 저장소로 쓰는 가짜 도구로 먼저 개발한다.

검증의 목적은 콘텐츠를 막는 것이 아니다. 활동 노드는 AI 생성 콘텐츠를 포함해 자유롭게 구성한다.
검증은 (1) 출처가 실재하고 (2) 활동과 관련 있고 (3) 목표 지표를 실제로 측정한 근거만 인용하게
하고, 연구 자극과 제공 콘텐츠의 차이는 Reference 카드에 한 줄로 밝힌다(ADAPTED_STIMULUS).
그 차이는 뒤에 사용자 데이터 분석의 비교 축(연구 자극 그대로 vs AI 생성)이 된다.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, model_validator

from app.schemas.care import CareGoal
from app.schemas.evidence import (
    EvidenceMetadata,
    ExperimentId,
    FulltextStatus,
    InterventionOrder,
    Outcome,
    OutcomeResult,
    PaperId,
    Section,
)


class PaperRef(BaseModel):
    paper_id: PaperId
    title: str
    authors: str | None = None
    year: int | None = None
    doi: str | None = None
    url: str | None = None
    fulltext_status: FulltextStatus | None = None  # SOURCE_EXISTS가 본다. manifest.csv 원천


class EvidenceHit(BaseModel):
    """search_evidence 결과 한 건 = evidence_records 한 행 + 논문 + 점수."""

    evidence_record_id: int
    paper: PaperRef
    experiment_id: ExperimentId | None
    section: Section
    chunk_idx: int
    excerpt: str  # content 원문 그대로. 요약하지 않는다 — Reference 카드 · 출처 검증용
    metadata: EvidenceMetadata
    score: float  # RRF 합산. 하이브리드 순위의 근거
    dense_rank: int | None = None  # Dense 단독 기준선 비교용
    keyword_rank: int | None = None


class GoalOutcomes(BaseModel):
    primary: list[Outcome]
    secondary: list[Outcome] = []


# S2 1차에 AFFECT가 있는 것은 '정서 개선을 S2의 지지로 참고한다'는 정책이다. 어느 지표를
# 지지했는지는 supported_outcomes가 지표명으로 드러내므로, 카드 문구는 그 지표명을 쓴다
# (정서 ≠ 스트레스).
GOAL_OUTCOMES: dict[CareGoal, GoalOutcomes] = {
    CareGoal.S1: GoalOutcomes(primary=[Outcome.RUMINATION], secondary=[Outcome.AFFECT]),
    CareGoal.S2: GoalOutcomes(
        primary=[Outcome.SUBJECTIVE_STRESS, Outcome.AFFECT], secondary=[Outcome.PHYSIO]
    ),
    CareGoal.S3: GoalOutcomes(primary=[Outcome.ATTENTION]),
}

# 사용자 상태(또는 프로토콜 허용 시점) → 인용할 수 있는 연구 조건. 레코드의 NO_INDUCTION은
# '유도 없이 수행한 연구'라는 사실이고, 그것을 스트레스 뒤 사용자에게도 인용한다는 것은
# 이 표의 정책이다.
ORDER_COMPATIBILITY: dict[InterventionOrder, frozenset[InterventionOrder]] = {
    InterventionOrder.POST_STRESS: frozenset(
        {InterventionOrder.POST_STRESS, InterventionOrder.NO_INDUCTION}
    ),
    InterventionOrder.POST_FATIGUE: frozenset(
        {InterventionOrder.POST_FATIGUE, InterventionOrder.NO_INDUCTION}
    ),
    InterventionOrder.PRE_STRESS: frozenset(
        {InterventionOrder.PRE_STRESS, InterventionOrder.NO_INDUCTION}
    ),
    InterventionOrder.NO_INDUCTION: frozenset(InterventionOrder),
}


class MediaMatch(StrEnum):
    EXACT = "EXACT"  # 연구 매체 집합 == 제공 매체 집합(AI_IMAGE는 STATIC_IMAGE로 비교)
    PARTIAL = "PARTIAL"  # 겹치지만 같지 않다 — N18 [VR, NATURE_SOUND] vs 제공 [NATURE_SOUND]
    NONE = "NONE"  # 겹치지 않는다


class Check(StrEnum):
    """validation.py docstring의 6개 검사. 항상 이 순서로 6개 모두 돌려준다."""

    SOURCE_EXISTS = "SOURCE_EXISTS"  # papers에 DOI/URL + 원문 파일이 있고 verification ≠ SECONDARY
    INTERVENTION_MATCH = (
        "INTERVENTION_MATCH"  # media_match ≠ NONE + 순서가 ORDER_COMPATIBILITY에 맞다
    )
    POPULATION_FIT = "POPULATION_FIT"  # clinical_status ≠ CLINICAL (None은 통과 + 사유 '미상')
    OUTCOME_MEASURED = "OUTCOME_MEASURED"  # GOAL_OUTCOMES[goal].primary 중 하나가 results에 있다
    CONFLICT_SCAN = "CONFLICT_SCAN"  # 같은 연구 매체·1차 지표에 NOT_SIGNIFICANT·NEGATIVE arm이 없다
    STIMULUS_FIDELITY = "STIMULUS_FIDELITY"  # 제공 구성이 연구 자극과 같다(AI 생성·조합이면 실패)


BLOCKING_CHECKS: frozenset[Check] = frozenset(
    {Check.SOURCE_EXISTS, Check.INTERVENTION_MATCH, Check.OUTCOME_MEASURED}
)


class Flag(StrEnum):
    POPULATION_MISMATCH = "POPULATION_MISMATCH"  # 임상 표본(E18). 참고로만 인용
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"  # related_record_ids에 상충 arm
    ADAPTED_STIMULUS = "ADAPTED_STIMULUS"  # 제공 콘텐츠가 연구 자극의 변형. 카드에 한 줄 표기
    NOT_SUPPORTING = "NOT_SUPPORTING"  # usable이지만 goal 1차 지표에 POSITIVE가 없다


class CheckResult(BaseModel):
    check: Check
    passed: bool | None  # None = 평가 불가(레코드 없음 · 순서 미지정)
    reason: str  # 한 문장. Reference 카드와 로그에 그대로 쓴다
    related_record_ids: list[int] = []


class EvidenceVerdict(BaseModel):
    """validate_evidence 결과 한 건. usable은 '인용 가능한가', supports_goal은 '지지하는가'."""

    evidence_record_id: int
    usable: bool  # BLOCKING_CHECKS 셋 다 passed=True
    supports_goal: bool  # supported_outcomes가 비어 있지 않다
    supported_outcomes: list[Outcome] = []  # goal 1차 지표 중 POSITIVE 결과가 있는 것
    media_match: MediaMatch
    flags: list[Flag] = []
    checks: list[CheckResult] = Field(min_length=6, max_length=6)
    goal_results: list[OutcomeResult] = []  # goal 1차·2차 지표에 해당하는 결과만

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if [c.check for c in self.checks] != list(Check):
            raise ValueError("checks는 Check 순서대로 6개, 중복 없이")
        blocking_ok = all(c.passed is True for c in self.checks if c.check in BLOCKING_CHECKS)
        if self.usable != blocking_ok:
            raise ValueError("usable은 차단 검사 3개의 결과와 같아야 한다")
        if self.supports_goal != bool(self.supported_outcomes):
            raise ValueError("supports_goal은 supported_outcomes가 있는지와 같아야 한다")
        if (Flag.NOT_SUPPORTING in self.flags) != (self.usable and not self.supports_goal):
            raise ValueError("NOT_SUPPORTING은 usable이면서 supports_goal이 아닐 때만")
        return self
