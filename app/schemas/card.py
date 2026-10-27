"""Reference 카드 계약 — "왜 추천했나요?" 화면. FE가 그대로 그린다.

카드는 plan 노드가 코드로 만든다(app/agent/reference_card.py). LLM을 쓰지 않고 추천 이유(rationale)
문장에 기대지 않는다 — 카드의 사실은 모두 검증한 근거 · 프로토콜 · intake 결과에서 온다. 근거마다
이론 → 논문(DOI) → 대상 · 실험 조건 → 결과 → 한계 순서로 보여 주고, 원문 구간(excerpts)은 요약하지
않은 원문(영어) 그대로다. 연구 결과를 진단 · 치료 효과로 넓히지 않는다.

    ReferenceCard          CarePlan.reference_card — RUN payload의 plan에 실린다
      protocol_id · name · minutes · composition
      badges[]             화면 배지(CardBadge). 문장은 limits에 있다
      goal                 목표 · 정한 방식 · 목표 근거(개념 정의 원문) · 근거 지표와 측정 척도
      grades               이 활동의 목표 1차 지표별 근거 등급(프로토콜 시트, A–D · — · 미확인)
      evidence[]           검증을 통과한 실험 하나씩 — 목표를 지지하는 것이 앞
      limits[]             카드 끝 한계 문장
"""

from enum import StrEnum

from pydantic import BaseModel

from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Outcome, OutcomeResult, Section, StudyDesign
from app.schemas.intake import GoalSource
from app.schemas.measure import InstrumentId
from app.schemas.protocol import Composition, Grade
from app.schemas.retrieval import CheckResult, Flag


class CardBadge(StrEnum):
    """화면 배지. FE가 문구 · 색을 정하고, 설명 문장은 ReferenceCard.limits · goal.notes에 있다."""

    EXPERIMENTAL = "EXPERIMENTAL"  # 조합 근거가 없는 실험적 구성(또는 이 목표에서는 실험적)
    LIMITED_EVIDENCE = "LIMITED_EVIDENCE"  # 프로토콜 시트가 근거가 적다고 표시했다
    NO_VERIFIED_EVIDENCE = "NO_VERIFIED_EVIDENCE"  # 검증을 통과한 근거가 이번에는 없다
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"  # 효과가 없거나 반대인 연구도 있다
    ADAPTED_STIMULUS = "ADAPTED_STIMULUS"  # 연구 자극을 바꾼 콘텐츠(각색 대본 · 다른 음원 · 조합)
    AI_IMAGE_UNVERIFIED = "AI_IMAGE_UNVERIFIED"  # AI 생성 이미지 — 사진 자극과 효과 동등성 미검증
    RELAXED_ORDER = "RELAXED_ORDER"  # 지금 상황에 맞춘 활동이 없어 다른 상황용 활동을 골랐다
    RELAXED_ENVIRONMENT = "RELAXED_ENVIRONMENT"  # 지금 환경에 맞는 활동이 없었다
    DEFAULT_GOAL = "DEFAULT_GOAL"  # 상황 단서가 부족해 기본 목표로 시작했다
    PROVISIONAL_MEASURE = "PROVISIONAL_MEASURE"  # 목표 척도가 팀이 만든 임시 도구(타당화 전)


class PaperCitation(BaseModel):
    paper_id: str
    title: str
    authors: str | None = None
    year: int | None = None
    doi: str | None = None
    url: str | None = None  # 원문 링크 — DOI 주소 또는 원문 URL


class GoalDefinitionRef(BaseModel):
    """목표 근거 — 목표가 가리키는 개념의 정의 한 문장(R1 논문 원문)."""

    goal: CareGoal  # 목표 · 부목표 중 어느 쪽의 정의인가
    concept: str | None = None  # rumination · state anxiety · mental fatigue …
    page: str | None = None  # PDF 쪽
    sentence: str  # 원문 그대로(영어)
    paper: PaperCitation | None = None


class MeasureRef(BaseModel):
    """LUMI가 세션 전후로 재는 목표 척도."""

    instrument_id: InstrumentId
    name: str
    concept: str
    provisional: bool  # 팀이 쓴 임시 문항 — 타당화 전


class GoalSection(BaseModel):
    goal: CareGoal
    label: str
    secondary_goal: CareGoal | None = None
    source: GoalSource | None = None  # INFERRED · DEFAULT · CHIP
    reason: str | None = None  # intake의 판단 이유 한 줄(LLM)
    # 추론한 목표 · 부목표의 개념 정의. 기본값 · 칩으로 정했으면 []
    definitions: list[GoalDefinitionRef] = []
    evidence_outcomes: list[Outcome]  # 근거가 보는 1차 지표(retrieval.GOAL_OUTCOMES)
    measured_by: MeasureRef | None = None
    notes: list[str] = []  # 기본 목표 · 근거 지표와 측정 지표의 차이 · 임시 척도


class Excerpt(BaseModel):
    evidence_record_id: int
    section: Section
    text: str  # 원문 그대로


class CardEvidence(BaseModel):
    """실험(arm) 하나 — 같은 실험의 METHODS · RESULTS chunk를 묶는다."""

    paper: PaperCitation
    experiment_id: str | None = None
    arm_label: str | None = None  # "Study 2" · "컬러링 vs 독서"
    theories: list[str] = []  # 이론 — ART · SRT …
    study_design: StudyDesign
    population: str | None = None  # 대상 — "건강 대학생(중국)"
    n: int | None = None
    n_detail: str | None = None
    exposure_minutes: float | None = None  # 실험 조건 — 단회 노출 시간
    regimen: str | None = None  # 반복 설계
    studied_orders: list[InterventionOrder] = []  # 연구가 수행된 상황(스트레스 뒤 · 피로 뒤 …)
    results: list[OutcomeResult] = []  # 목표 지표의 결과 — 척도 · 비교 · 시점 · 통계(원문)
    supports_goal: bool  # false = 참고 근거(목표 지표의 개선이 없다)
    supported_outcomes: list[Outcome] = []
    flags: list[Flag] = []
    limits: list[str] = []  # 이 근거의 한계 문장(플래그마다)
    excerpts: list[Excerpt]
    checks: list[CheckResult] = []  # 검증 6항목과 사유(자세히 보기용, 로그 문장)
    evidence_record_ids: list[int]


class ReferenceCard(BaseModel):
    protocol_id: str
    name: str
    minutes: int
    composition: Composition
    badges: list[CardBadge] = []
    goal: GoalSection
    grades: dict[Outcome, Grade] = {}
    evidence: list[CardEvidence] = []
    limits: list[str] = []
