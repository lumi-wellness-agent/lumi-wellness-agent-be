"""근거 메타데이터 — 실험 단위 Evidence Record에 붙여 검색 전에 거르는 값.

기획서 8.1의 메타데이터(theory, clinical_status, modality, medium, population, outcome,
effect, doi)에 '논문 수집 방식'의 intervention_order·duration·evidence_role을 더한 초안이다.
두 자료에서 modality의 뜻이 다르다. 여기서는 기획서를 따라 modality를 감각 경로(시각·청각)로
두고, '논문 수집 방식'의 modality 예시(정적 이미지·영상·야외 보행·호흡·CBT)는 medium에 넣었다.
doi는 논문 단위 값이라 papers 테이블에 둔다.

예: S2의 이미지 감상 근거는 medium=STATIC_IMAGE, intervention_order=POST_STRESS,
outcomes에 AFFECT나 RUMINATION이 있는 레코드로 먼저 거른다. 키워드는 맞아도 실험 순서가
다른 논문(P08 — 사진을 스트레스 과제 전에 보여 준 연구)을 근거로 잘못 고르지 않기 위해서다.

DB에는 evidence_records.metadata(jsonb)에 이 모델을 그대로 저장한다. 값 목록을 바꿔도
마이그레이션이 필요 없다.
"""

from enum import StrEnum

from pydantic import BaseModel


class ClinicalStatus(StrEnum):
    CLINICAL = "CLINICAL"  # 임상 (환자 대상)
    NON_CLINICAL = "NON_CLINICAL"  # 비임상 (건강한 성인·대학생 등)


class Modality(StrEnum):
    VISUAL = "VISUAL"  # 시각
    AUDITORY = "AUDITORY"  # 청각
    AUDIOVISUAL = "AUDIOVISUAL"  # 시청각


class Medium(StrEnum):
    STATIC_IMAGE = "STATIC_IMAGE"  # 정적 이미지·사진
    AI_IMAGE = "AI_IMAGE"  # AI 생성 이미지
    VIDEO = "VIDEO"  # 영상
    VR = "VR"
    NATURE_SOUND = "NATURE_SOUND"  # 자연음
    OUTDOOR_WALK = "OUTDOOR_WALK"  # 야외 보행
    BREATHING = "BREATHING"  # 호흡
    PMR = "PMR"  # 점진적 근육 이완
    MINDFULNESS = "MINDFULNESS"  # 마음챙김
    COLORING = "COLORING"  # 컬러링
    CBT = "CBT"


class InterventionOrder(StrEnum):
    PRE_STRESS = "PRE_STRESS"  # 스트레스 전
    POST_STRESS = "POST_STRESS"  # 스트레스 후
    POST_FATIGUE = "POST_FATIGUE"  # 피로 유도 후


class Population(StrEnum):
    GENERAL_ADULT = "GENERAL_ADULT"  # 일반 성인
    UNIVERSITY_STUDENT = "UNIVERSITY_STUDENT"  # 대학생
    CLINICAL_PATIENT = "CLINICAL_PATIENT"  # 임상 환자


class Outcome(StrEnum):
    RUMINATION = "RUMINATION"  # 반추
    AFFECT = "AFFECT"  # 정서
    SUBJECTIVE_STRESS = "SUBJECTIVE_STRESS"  # 주관적 스트레스
    HRV = "HRV"  # 심박 변이도
    ATTENTION = "ATTENTION"  # 주의력


class Effect(StrEnum):
    POSITIVE = "POSITIVE"  # 효과 있음
    NO_EFFECT = "NO_EFFECT"  # 무효 (유의한 차이 없음)
    NEGATIVE = "NEGATIVE"  # 부정적 결과


class EvidenceRole(StrEnum):
    DIRECT_INTERVENTION = "DIRECT_INTERVENTION"  # 직접 중재
    METHOD_GUIDE = "METHOD_GUIDE"  # 방법론 가이드
    BACKGROUND_THEORY = "BACKGROUND_THEORY"  # 배경 이론
    EXCLUDED = "EXCLUDED"  # 제외


class EvidenceMetadata(BaseModel):
    """실험 하나의 메타데이터. 근거 추출(ingestion/extract.py)이 LLM 구조화 출력으로 채운다.

    원문에 없는 값은 None이나 빈 목록으로 둔다 — LLM이 추측해 채우면 맞지 않는 근거가 필터를
    통과한다.
    """

    theories: list[str] = []  # 근거 이론 (예: ART, SRT, Biophilia)
    clinical_status: ClinicalStatus | None = None
    modality: Modality | None = None
    medium: Medium | None = None
    intervention_order: InterventionOrder | None = None
    duration: str | None = None  # 예: "단회 3분", "매일 5분 × 1개월", "5주 프로그램"
    population: Population | None = None
    outcomes: list[Outcome] = []
    effect: Effect | None = None
    evidence_role: EvidenceRole | None = None
