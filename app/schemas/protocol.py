"""활동 노드(Care Protocol) JSON 중 근거 연결 부분 — 21개 JSON의 evidence[]·grades를 읽고 검증한다.

프로토콜 본문(steps · content_spec · measures …)은 활동 노드 담당이 정하므로 여기서 다루지 않는다.
BE는 evidence[].evidence_record_ids만 역기입한다.

근거는 활동 노드를 제약하는 장치가 아니라 Reference 카드에 보여 줄 최소한의 출처다. 그래서
등급을 계산하지 않고 구조만 검사한다. 82편 밖 근거뿐인 노드(PR-21)는 evidence[]를 비워 둔다.
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.evidence import (
    DESIGN_LABELS,
    GRADED_OUTCOMES,
    VERIFICATION_LABELS,
    ExperimentId,
    InterventionOrder,
    Medium,
    Outcome,
    PaperId,
    StudyDesign,
    Verification,
    parse_orders,
)

EvidenceKey = Annotated[str, StringConstraints(pattern=r"^[PEN][0-9]{2}(-E[1-9][0-9]*)?$")]
"""grades.support/conflicts 항목. ARM은 experiment_id(P24-E1), SYNTHESIS는 paper_id(P01)."""

_PRIMARY_DESIGNS = frozenset({StudyDesign.RANDOMIZED_EXPERIMENT, StudyDesign.QUASI_EXPERIMENT})


class EvidenceRole(StrEnum):
    """프로토콜 ↔ 레코드 링크의 대표 역할. 지표별 지지·상충은 grades와 레코드 results가 정한다.

    P24-E1은 PR-01 AFFECT에는 지지, PHYSIO에는 상충이고 다른 프로토콜에서는 또 다를 수 있다.
    """

    PRIMARY = "PRIMARY"  # 매체·순서가 맞는 ARM. 무작위면 A/B, 준실험이면 C의 지지 근거
    CONFLICT = "CONFLICT"  # 같은 매체·지표에서 NOT_SIGNIFICANT·NEGATIVE. ARM 또는 SYNTHESIS(E08)
    META = "META"  # SYNTHESIS. 등급 A 조건의 '메타분석·체계적 고찰 지지'
    CONTEXT = "CONTEXT"  # 등급 밖 참고 — 제약 출처(P15), 선별 참고(N15 · P11), 기전(E10)


class Grade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"
    NONE = "—"  # 해당 지표를 측정한 근거 없음
    PENDING = "미확인"  # 측정했으나 원문 확인 전(PR-04 정서 · PR-07 생리). NONE과 다르다


GRADE_LABELS: dict[str, Grade] = {
    "-": Grade.NONE,
    "–": Grade.NONE,
    "원문 확인": Grade.PENDING,
}


class GradeEntry(BaseModel):
    grade: Grade
    support: list[EvidenceKey] = []
    conflicts: list[EvidenceKey] = []
    note: str | None = None

    @field_validator("grade", mode="before")
    @classmethod
    def _grade_label(cls, v: object) -> object:
        return GRADE_LABELS.get(v.strip(), v.strip()) if isinstance(v, str) else v


class ProtocolEvidenceRef(BaseModel):
    """JSON evidence[] 항목 하나. 키 이름은 JSON 그대로 두고 라벨만 enum으로 정규화한다."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    paper_id: PaperId
    experiment_id: ExperimentId | None = None
    role: EvidenceRole
    design: StudyDesign  # "RCT" · "META_ANALYSIS" 라벨 허용
    n: int | str | None = None  # "10편, 886명" 같은 문자열 허용. 정규화(n · n_studies)는 레코드 쪽
    # JSON 키는 order. "POST_STRESS" · "POST_FATIGUE / 무관"(P07) · null 모두 받는다
    orders: list[InterventionOrder] = Field(default=[], alias="order")
    population: str | None = None
    stimulus: str | None = None
    outcomes: list[Outcome]
    result: str | None = None
    verification: Verification  # "본문" · "초록" · "2차" 라벨 허용
    # BE 역기입 — 그 arm의 chunk ID 전부. 순서는 (Section 순, chunk_idx, id)
    evidence_record_ids: list[int] = []

    @field_validator("design", mode="before")
    @classmethod
    def _design_label(cls, v: object) -> object:
        return DESIGN_LABELS.get(v, v) if isinstance(v, str) else v

    @field_validator("verification", mode="before")
    @classmethod
    def _verification_label(cls, v: object) -> object:
        return VERIFICATION_LABELS.get(v, v) if isinstance(v, str) else v

    @field_validator("orders", mode="before")
    @classmethod
    def _orders_label(cls, v: object) -> object:
        return parse_orders(v) if v is None or isinstance(v, str | list) else v

    @model_validator(mode="after")
    def _role_rules(self) -> Self:
        if self.role is EvidenceRole.PRIMARY:
            if self.experiment_id is None:
                raise ValueError("PRIMARY는 experiment_id가 있는 arm이어야 한다(리뷰 본문 금지)")
            if self.design not in _PRIMARY_DESIGNS:
                raise ValueError("PRIMARY는 무작위·준실험만. 관찰·리뷰는 CONTEXT 또는 META")
        if self.role is EvidenceRole.META:
            if self.design is not StudyDesign.SYSTEMATIC_REVIEW_META:
                raise ValueError("META는 체계적 고찰·메타분석만")
            if self.experiment_id is not None:
                raise ValueError("META는 experiment_id를 갖지 않는다")
        if self.experiment_id and not self.experiment_id.startswith(f"{self.paper_id}-E"):
            raise ValueError("experiment_id는 paper_id로 시작한다")
        return self

    @property
    def key(self) -> str:
        return self.experiment_id or self.paper_id


class ProtocolEvidenceSection(BaseModel):
    """계약 테스트가 읽는 부분만. 나머지 키는 무시한다."""

    protocol_id: Annotated[str, StringConstraints(pattern=r"^PR-[0-9]{2}$")]
    media: list[Medium] = []  # 제공 매체(대안 포함, AI_IMAGE 가능). 단계 모듈(PR-16)은 빈 목록
    # 허용 사용자 상태. 레코드의 intervention_orders(연구가 수행된 조건)와 뜻이 다르다
    intervention_order: list[InterventionOrder] = []
    paper_id_scheme: str
    grades: dict[Outcome, GradeEntry]
    evidence: list[ProtocolEvidenceRef] = []  # 82편 밖 근거뿐이면 비운다(PR-21 Lymeus 2017)

    @field_validator("intervention_order", mode="before")
    @classmethod
    def _order_labels(cls, v: object) -> object:
        return parse_orders(v) if v is None or isinstance(v, str | list) else v

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.paper_id_scheme != "RC_MASTER":
            raise ValueError("paper_id_scheme은 RC_MASTER")
        if set(self.grades) != set(GRADED_OUTCOMES):
            raise ValueError(f"grades 키는 {[o.value for o in GRADED_OUTCOMES]}와 같아야 한다")
        by_key: dict[str, ProtocolEvidenceRef] = {}
        for ref in self.evidence:
            if ref.key in by_key:
                raise ValueError(f"evidence[]에 {ref.key}가 두 번 있다")
            by_key[ref.key] = ref
        for outcome, entry in self.grades.items():
            for key in entry.support + entry.conflicts:
                ref = by_key.get(key)
                if ref is None:
                    raise ValueError(f"grades.{outcome}: {key}가 evidence[]에 없다")
                if outcome not in ref.outcomes:
                    raise ValueError(f"grades.{outcome}: {key}는 {outcome}를 측정하지 않았다")
            if entry.grade is Grade.NONE and (entry.support or entry.conflicts):
                raise ValueError(f"grades.{outcome}: 등급 —인데 근거가 있다")
            if entry.grade in {Grade.A, Grade.B, Grade.C} and not entry.support:
                raise ValueError(f"grades.{outcome}: 등급 {entry.grade}인데 support가 없다")
            if entry.grade is Grade.D and not entry.conflicts:
                raise ValueError(f"grades.{outcome}: 등급 D인데 conflicts가 없다")
        return self
