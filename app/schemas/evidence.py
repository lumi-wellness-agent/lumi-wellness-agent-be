"""근거 메타데이터 — Evidence Record(evidence_records.metadata jsonb)에 붙는 값.

LUMI의 근거 레이어는 '최소한의 논문 근거'다. 활동 노드 21개는 AI 생성 콘텐츠를 포함해 자유롭게
구성하고, 이 레이어는 그 활동이 어떤 연구에 기대는지와 연구 자극과 얼마나 다른지를 Reference
카드에 정직하게 보여 준다. 등급 계산·효과 판정 엔진이 아니다.

레코드 단위는 'arm' — 원문의 중재 조건 하나와 그 비교(P24-E1 자연 vs 도시 이미지)다. 단위는
시트 행이 아니라 원문의 조건이다. 결과가 갈리는 조건은 매체가 같아도 나눈다(P16 과호흡 조건은
PR-06이 제외한다). 한 행에 조건이 여럿이면 적재 때 나눈다(N18 숲 VR+소리 / 숲 VR). 'P31-E1/E2'처럼
두 실험이 한 행에 합쳐 보고된 경우는 원문에서 실험별 표본·결과가 대응될 때만 나누고, 아니면
한 레코드로 두고 arm_label에 원문 표기를 남긴다. 연구 수는 paper_id 기준으로 센다.
열 이름 experiment_id와 접미사 -E는 근거 arm 시트의 관례를 그대로 둔 것이고 뜻은 arm이다.

같은 arm의 Methods·Results chunk들은 연구 메타데이터(media · results · n …)를 공유한다.
verification만 chunk마다 정한다 — P05처럼 PDF는 있지만 결과는 2차 자료에서 온 경우가 있다.

메타분석·체계적 고찰의 결론은 SYNTHESIS, 관찰·질적 결과는 OBSERVATION, 이론 정의는 DEFINITION,
색–정서 표 같은 참조표는 LOOKUP으로 두고 experiment_id는 null이다.

값 목록은 LUMI_측정도구_프로토콜_2026-10-23.xlsx(근거 arm 65행 · 등급 규칙 · 프로토콜 21),
LUMI_R_C_논문목록_2026-10-22.xlsx('논문 유형 기준'), 활동 노드 JSON(grades 키 · evidence[])의
실제 값에서 뽑았다. 시트·JSON 라벨 → enum 변환은 이 모듈 끝의 *_LABELS 표와 parse_* 함수가
맡고, 계약 테스트는 변환 함수의 출력을 시트의 distinct 값 전부에 대해 검사한다.
원문에 없는 값은 None·빈 목록으로 둔다(LLM 추측 금지).
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

PaperId = Annotated[str, StringConstraints(pattern=r"^[PEN][0-9]{2}$")]
"""R/C 마스터 ID(82편). P01…P39 · E01…E21 · N01…N26. 36편 시트 ID는 쓰지 않는다.
형식만 본다 — 실재하는 82개인지는 data/manifest.csv 대조 테스트가 본다."""

ExperimentId = Annotated[str, StringConstraints(pattern=r"^[PEN][0-9]{2}-E[1-9][0-9]*$")]
"""arm ID. paper_id + '-E' + 1부터 시작하는 번호(E0 · E01 금지)."""


class RecordType(StrEnum):
    ARM = "ARM"  # 중재 조건 하나와 그 비교. experiment_id 필수. PRIMARY 링크는 이것만
    SYNTHESIS = "SYNTHESIS"  # 메타분석 통합 효과 · 체계적 고찰 결론 · 스코핑 리뷰 조건
    OBSERVATION = "OBSERVATION"  # 관찰·설문·질적 결과(N15 면담). 등급 C 이하
    DEFINITION = "DEFINITION"  # 이론·개념 정의 chunk(ART · SRT …). 효과 근거 아님
    LOOKUP = "LOOKUP"  # 참조표(P11 색–정서, N13 지각 차원). 효과 근거 아님


class StudyDesign(StrEnum):
    """연구 설계 6범주 — 논문목록 '논문 유형 기준' 시트의 값.

    papers.study_design은 논문 전체의 유형이고, 레코드의 study_design은 *그 결과를 낳은 설계*다.
    보통 같지만 P09(이론 리뷰 + 보조 실증)처럼 다르면 레코드 쪽을 원문대로 적는다.
    """

    THEORY_REVIEW = "THEORY_REVIEW"  # 이론·개념 리뷰 (5편)
    SYSTEMATIC_REVIEW_META = "SYSTEMATIC_REVIEW_META"  # 체계적 고찰·메타분석 (17편)
    SCALE_DEVELOPMENT = "SCALE_DEVELOPMENT"  # 척도·측정도구 개발/검증 (13편)
    RANDOMIZED_EXPERIMENT = "RANDOMIZED_EXPERIMENT"  # 무작위 실험 (35편)
    QUASI_EXPERIMENT = "QUASI_EXPERIMENT"  # 준실험·비무작위 비교 (7편)
    OBSERVATIONAL = "OBSERVATIONAL"  # 관찰·설문·질적 연구 (5편)


class Medium(StrEnum):
    """자극·활동의 구성 요소. 프로토콜 시트 '매체(BE Medium)' 열과 1:1.

    레코드의 media는 '그 arm의 중재 조건을 이루는 구성 요소 전부'다(N18-E1: [VR, NATURE_SOUND]).
    구성 요소 하나만의 효과를 뜻하지 않는다 — 비교는 retrieval.MediaMatch가 EXACT/PARTIAL로 가른다.
    """

    STATIC_IMAGE = "STATIC_IMAGE"
    AI_IMAGE = "AI_IMAGE"  # 제공 전용. 82편에 AI 생성 이미지 실험은 없다 → STUDY_MEDIUM_FOR
    VIDEO = "VIDEO"
    VR = "VR"
    NATURE_SOUND = "NATURE_SOUND"
    MUSIC = "MUSIC"  # 추가 — PR-05 (E08 · E09)
    OUTDOOR_WALK = "OUTDOOR_WALK"
    BREATHING = "BREATHING"
    PMR = "PMR"
    GUIDED_IMAGERY = "GUIDED_IMAGERY"  # 추가 — PR-08 (P18 유도 심상 arm)
    MINDFULNESS = "MINDFULNESS"
    NOTICING = "NOTICING"  # 추가 — 알아차리기·기록. PR-12 좋은 것 3가지, PR-16 감정 명명(E10)
    COLORING = "COLORING"
    WRITING = "WRITING"  # 추가 — PR-18 · PR-19 (E14 · E11 · E12)
    GROUNDING = "GROUNDING"  # 추가 — PR-17 (E20)
    ART_VIEWING = "ART_VIEWING"  # 추가 — PR-20 (E06 · E07)
    CBT = "CBT"


# 제공 전용 매체 → 그 근거를 찾을 연구 매체. 검색은 이 표로 바꿔 찾고, 검증은 바꿔 비교한 뒤
# ADAPTED_STIMULUS로 차이를 남긴다. 생성 음성·생성 영상이 생기면 여기에 더한다.
STUDY_MEDIUM_FOR: dict[Medium, Medium] = {Medium.AI_IMAGE: Medium.STATIC_IMAGE}


def study_medium(medium: Medium) -> Medium:
    return STUDY_MEDIUM_FOR.get(medium, medium)


class InterventionOrder(StrEnum):
    """연구가 수행된 조건. 레코드에서는 사실이고, 프로토콜 JSON에서는 '허용 사용자 상태'다.

    어떤 연구 조건을 어떤 사용자 상태에 인용할지는 retrieval.ORDER_COMPATIBILITY가 정한다.
    """

    PRE_STRESS = "PRE_STRESS"  # 스트레스 전 제시(예방) — P28, E09
    POST_STRESS = "POST_STRESS"  # 스트레스·반추·불안 유도 후 회복
    POST_FATIGUE = "POST_FATIGUE"  # 주의 피로 유도 후 회복
    NO_INDUCTION = "NO_INDUCTION"  # 유도 없음·일상 반복 — 시트 '무관'(18행). 구 ANY. 빈 목록 = 미상


class Population(StrEnum):
    GENERAL_ADULT = "GENERAL_ADULT"  # 성인 · 젊은 성인 · 온라인 성인 · 건강 여성
    UNIVERSITY_STUDENT = "UNIVERSITY_STUDENT"  # 대학생 · 간호대생
    MIXED = "MIXED"  # 리뷰·메타처럼 여러 집단을 합친 경우


class ClinicalStatus(StrEnum):
    """임상 여부. Population과 별개 축 — 대학생도 진단 집단일 수 있고(P38 호소), 리뷰는 섞인다."""

    NON_CLINICAL = "NON_CLINICAL"  # 건강 · 비임상 · 비진단 호소
    CLINICAL = "CLINICAL"  # 진단 집단 — E18 우울·불안 진단자, E16 임상
    MIXED = "MIXED"


class Outcome(StrEnum):
    """결과 지표. 앞 다섯이 등급 축(GRADED_OUTCOMES) = JSON grades{} 키 = 프로토콜 시트 등급 5열."""

    AFFECT = "AFFECT"  # 정서·기분 — PA/NA, ZIPERS, SAM
    SUBJECTIVE_STRESS = "SUBJECTIVE_STRESS"  # 주관적 스트레스 + 상태 불안 — STAI, TAQ
    RUMINATION = "RUMINATION"  # 반추 — BSRI, RRQ, SRQ, Thoughts Questionnaire
    ATTENTION = "ATTENTION"  # 주의·인지 — SART, BDS/DSB, DSF, TMT-B, LDST, Posner, n-back
    PHYSIO = "PHYSIO"  # 생리·신경 — HRV, 혈압, SCL/SCR, 코르티솔, sAA, 호흡수, fMRI (구 HRV 확장)
    OTHER = "OTHER"  # 등급 밖 — 웰빙, 자연 연결감, 우울, 몰입. measure 필수. 상충 판정에 안 쓴다


GRADED_OUTCOMES: tuple[Outcome, ...] = (
    Outcome.AFFECT,
    Outcome.SUBJECTIVE_STRESS,
    Outcome.RUMINATION,
    Outcome.ATTENTION,
    Outcome.PHYSIO,
)


class Effect(StrEnum):
    """comparison에 적은 비교에서 그 결과가 어느 쪽이었나. 숫자 증감이 아니라 비교 기준이다."""

    POSITIVE = "POSITIVE"  # 명시한 비교에서 가설 방향으로 유의
    NOT_SIGNIFICANT = "NOT_SIGNIFICANT"  # 유의 차이 미확인(효과 없음의 입증이 아님). 구 NO_EFFECT
    NEGATIVE = "NEGATIVE"  # 반대 방향으로 유의 — E09 음악 조건 코르티솔 최고
    UNCLEAR = "UNCLEAR"  # 측정했으나 방향 판단 불가 — P37. 미추출(빈 results)과 다르다


class Verification(StrEnum):
    """이 chunk content의 출처 수준. 파이프라인이 chunk마다 정하고 LLM은 건드리지 않는다."""

    FULL_TEXT = "FULL_TEXT"  # 본문(OCR 포함)에서 추출 — 시트 '본문' · '본문(일부)'
    ABSTRACT = "ABSTRACT"  # 초록만 — 시트 '초록'
    SECONDARY = "SECONDARY"  # 다른 자료(리뷰·2차 보고)에서 확인 — 시트 '2차'. SOURCE_EXISTS 실패


class FulltextStatus(StrEnum):
    """papers.fulltext_status — 논문 원문을 어떤 형태로 확보했나. data/manifest.csv가 원천."""

    TEXT = "TEXT"  # 텍스트 층이 있는 PDF 또는 JATS XML
    IMAGE_PDF = "IMAGE_PDF"  # 스캔 PDF — OCR 대상(N02 N03 N09 P05 P26 P27)
    ABSTRACT_ONLY = "ABSTRACT_ONLY"  # 초록만 확보
    MISSING = "MISSING"  # 미확보(E02 E13 N06 N10 N23 P30)


class Section(StrEnum):
    """evidence_records.section의 정규 값. Docling 헤딩을 매핑한다. 순서 = 카드·역기입 순서."""

    ABSTRACT = "ABSTRACT"
    INTRODUCTION = "INTRODUCTION"
    METHODS = "METHODS"
    RESULTS = "RESULTS"
    DISCUSSION = "DISCUSSION"
    TABLE = "TABLE"
    OTHER = "OTHER"


def _blank_to_none(v: object) -> object:
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


class OutcomeResult(BaseModel):
    """지표 하나의 결과. (outcome, measure, comparison)마다 하나 — 같은 outcome이 반복된다.

    예: N14 → (PHYSIO, "SCL", POSITIVE), (PHYSIO, "HF-HRV", NOT_SIGNIFICANT)
        P16 → (SUBJECTIVE_STRESS, "STAI", "모든 조건 사전→사후", POSITIVE),
              (AFFECT, "PA", "호흡법 vs 명상", POSITIVE)
    """

    outcome: Outcome
    effect: Effect
    measure: str | None = None  # 원문 척도·지표명 그대로 — "PANAS NA", "SART", "salivary cortisol"
    comparison: str | None = None  # 무엇과 비교했나 — "자연 vs 도시", "사전→사후", "vs 대기 통제"
    timepoint: str | None = None  # 언제 측정했나 — "직후", "회복기 5분", "6개월 추적"
    stat: str | None = None  # 원문 통계 그대로 — "p=.003", "g=0.15, n.s.", "SMD −0.01"
    note: str | None = None

    _strip = field_validator("measure", "comparison", "timepoint", "stat", "note", mode="before")(
        _blank_to_none
    )

    @model_validator(mode="after")
    def _other_needs_measure(self) -> Self:
        if self.outcome is Outcome.OTHER and not self.measure:
            raise ValueError("OTHER 지표는 measure에 원문 구성개념을 적는다")
        return self


_DESIGNS_BY_TYPE: dict[RecordType, frozenset[StudyDesign]] = {
    RecordType.ARM: frozenset({StudyDesign.RANDOMIZED_EXPERIMENT, StudyDesign.QUASI_EXPERIMENT}),
    RecordType.SYNTHESIS: frozenset({StudyDesign.SYSTEMATIC_REVIEW_META}),
    RecordType.OBSERVATION: frozenset({StudyDesign.OBSERVATIONAL}),
}


class ExtractedFields(BaseModel):
    """LLM 구조화 출력이 채우는 부분. 이것만 LLM에 요청하고, 나머지는 파이프라인이 붙인다.

    시트 씨앗(media · intervention_orders · population …)이 LLM 출력에 들어 있지 않으므로
    덮어쓸 수 없다. 시트의 N·확인 수준은 교차 검사용 — 추출값과 다르면 review.csv에 적고
    사람이 정하기 전에는 적재하지 않는다.
    """

    results: list[OutcomeResult] = []
    n: int | None = Field(default=None, ge=1)  # 분석 표본 총수. 모르면 None
    n_detail: str | None = None  # "131 (68/63)", "k=14(원문 확인)"
    n_studies: int | None = Field(default=None, ge=1)  # SYNTHESIS의 포함 연구 수 k
    exposure_minutes: float | None = Field(default=None, gt=0)  # 단회 노출 시간(분). P03 → 6.25
    regimen: str | None = None  # 반복 설계 원문 그대로 — "매일 5분 × 1개월", "20분 × 3일"
    population_detail: str | None = None  # "건강 대학생(중국)", "우울·불안 진단자(영국)"
    theories: list[str] = []  # ART, SRT, Biophilia …

    _strip = field_validator("n_detail", "regimen", "population_detail", mode="before")(
        _blank_to_none
    )


class EvidenceMetadata(ExtractedFields):
    """arm(또는 통합·관찰·정의·참조표) 하나의 메타데이터. 같은 experiment_id의 chunk들이 공유한다.

    파이프라인이 정하는 필드: record_type, study_design, verification(chunk마다), arm_label, media,
    intervention_orders, population, clinical_status. 나머지(ExtractedFields)는 LLM 추출.
    """

    record_type: RecordType
    study_design: StudyDesign  # 이 결과를 낳은 설계. 기본값은 papers.study_design
    verification: Verification
    arm_label: str | None = None  # 시트·원문 표기 그대로 — "P31-E1/E2 컬러링 vs 독서", "Study 2"
    media: list[Medium] = []  # 이 arm 중재 조건의 구성 요소 전부. 제공 전용 매체(AI_IMAGE) 불가
    intervention_orders: list[InterventionOrder] = []  # 연구가 수행된 조건. 빈 목록 = 미상
    population: Population | None = None
    clinical_status: ClinicalStatus | None = None

    _strip_label = field_validator("arm_label", mode="before")(_blank_to_none)

    @model_validator(mode="after")
    def _rules(self) -> Self:
        allowed = _DESIGNS_BY_TYPE.get(self.record_type)
        if allowed and self.study_design not in allowed:
            raise ValueError(f"{self.record_type}에 {self.study_design}는 맞지 않는다")
        provided_only = [m for m in self.media if m in STUDY_MEDIUM_FOR]
        if provided_only:
            raise ValueError(f"연구 매체에 제공 전용 매체 {provided_only}는 올 수 없다")
        return self


class EvidenceRecordIn(BaseModel):
    """evidence_records 한 행의 쓰기 모델. ingestion/extract.py가 만든다."""

    paper_id: PaperId
    experiment_id: ExperimentId | None = None
    section: Section
    chunk_idx: int = Field(ge=0, default=0)
    content: str = Field(min_length=1)  # 원문 그대로. 요약 금지
    metadata: EvidenceMetadata

    @model_validator(mode="after")
    def _experiment_id_rule(self) -> Self:
        is_arm = self.metadata.record_type is RecordType.ARM
        if is_arm != (self.experiment_id is not None):
            raise ValueError("ARM 레코드만 experiment_id를 가진다")
        if self.experiment_id and not self.experiment_id.startswith(f"{self.paper_id}-E"):
            raise ValueError("experiment_id는 paper_id로 시작한다")
        return self


# ── 시트·JSON 라벨 → enum. 적재 스크립트와 계약 테스트가 같은 표·함수를 쓴다 ──────────────────

DESIGN_LABELS: dict[str, StudyDesign] = {
    # 활동 노드 JSON
    "RCT": StudyDesign.RANDOMIZED_EXPERIMENT,
    "QUASI": StudyDesign.QUASI_EXPERIMENT,
    "META_ANALYSIS": StudyDesign.SYSTEMATIC_REVIEW_META,
    "SYSTEMATIC_REVIEW": StudyDesign.SYSTEMATIC_REVIEW_META,
    # 논문목록 '논문 유형 기준' 정식 라벨
    "이론·개념 리뷰": StudyDesign.THEORY_REVIEW,
    "체계적 고찰·메타분석": StudyDesign.SYSTEMATIC_REVIEW_META,
    "척도·측정도구 개발/검증": StudyDesign.SCALE_DEVELOPMENT,
    "무작위 실험": StudyDesign.RANDOMIZED_EXPERIMENT,
    "준실험·비무작위 비교": StudyDesign.QUASI_EXPERIMENT,
    "관찰·설문·질적 연구": StudyDesign.OBSERVATIONAL,
    # 근거 arm 시트 · 요약 시트의 줄인 라벨
    "관찰·설문·질적": StudyDesign.OBSERVATIONAL,
    "척도": StudyDesign.SCALE_DEVELOPMENT,
}
VERIFICATION_LABELS: dict[str, Verification] = {
    "본문": Verification.FULL_TEXT,
    "본문(일부)": Verification.FULL_TEXT,
    "초록": Verification.ABSTRACT,
    "2차": Verification.SECONDARY,
}
OUTCOME_LABELS: dict[str, Outcome] = {  # 등급 규칙 시트 '지표 ↔ BE Outcome' 행
    "정서·기분": Outcome.AFFECT,
    "불안·주관적 스트레스": Outcome.SUBJECTIVE_STRESS,
    "반추": Outcome.RUMINATION,
    "주의·인지": Outcome.ATTENTION,
    "생리": Outcome.PHYSIO,
}
ORDER_LABELS: dict[str, InterventionOrder | None] = {
    "무관": InterventionOrder.NO_INDUCTION,  # "무관(일상)" "무관(일상 반복)"은 괄호 앞으로 매핑
    "과제 전후": None,  # N16 — 시점 미상. 원문 확인 뒤 값을 정한다
    "—": None,
    "-": None,
}
MEDIA_LABELS: dict[str, Medium] = {"NOTICE": Medium.NOTICING}


def parse_orders(value: str | list[str] | None) -> list[InterventionOrder]:
    """시트·JSON의 순서 라벨을 enum 목록으로. 모르는 라벨은 ValueError — 계약 테스트가 잡는다.

    "POST_FATIGUE / 무관"(P07) → [POST_FATIGUE, NO_INDUCTION]. "POST_STRESS(P24·P33)"처럼
    괄호 설명이 붙은 값은 괄호 앞만 본다. None · "—" · "과제 전후"는 빈 목록 = 미상.
    """
    if value is None:
        return []
    if isinstance(value, list):
        merged: list[InterventionOrder] = []
        for item in value:
            merged.extend(o for o in parse_orders(item) if o not in merged)
        return merged
    out: list[InterventionOrder] = []
    for token in value.split("/"):
        token = token.strip()
        if not token:
            continue
        base = token.split("(", 1)[0].strip()
        if base in InterventionOrder.__members__:
            mapped: InterventionOrder | None = InterventionOrder[base]
        elif token in ORDER_LABELS:
            mapped = ORDER_LABELS[token]
        elif base in ORDER_LABELS:
            mapped = ORDER_LABELS[base]
        else:
            raise ValueError(f"모르는 순서 라벨: {token!r}")
        if mapped is not None and mapped not in out:
            out.append(mapped)
    return out


def parse_media(value: str | None) -> list[Medium]:
    """프로토콜 시트 '매체(BE Medium)' 라벨을 enum 목록으로.

    "VIDEO + NATURE_SOUND" → [VIDEO, NATURE_SOUND]. "MUSIC (enum 추가 필요)" → [MUSIC].
    "OUTDOOR_WALK + NOTICE" → [OUTDOOR_WALK, NOTICING]. "— (단계)" → [] (단계 모듈).
    """
    if value is None:
        return []
    out: list[Medium] = []
    for token in value.split("+"):
        base = token.split("(", 1)[0].strip()
        if not base or base in {"—", "-"}:
            continue
        if base in MEDIA_LABELS:
            medium = MEDIA_LABELS[base]
        elif base in Medium.__members__:
            medium = Medium[base]
        else:
            raise ValueError(f"모르는 매체 라벨: {token!r}")
        if medium not in out:
            out.append(medium)
    return out
