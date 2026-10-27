"""활동 노드(Care Protocol) JSON — data/protocols/PR-*.json 하나가 레코드 하나다.

앞부분(ProtocolEvidenceSection)은 근거 연결 — evidence[]·grades. PR-A 계약 2. BE는
evidence[].evidence_record_ids만 역기입한다. 근거는 활동 노드를 제약하는 장치가 아니라 Reference
카드에 보여 줄 최소한의 출처다. 그래서 등급을 계산하지 않고 구조만 검사한다. 83편 밖 근거뿐인
노드(PR-21)는 evidence[]를 비워 둔다.

뒷부분(CareProtocol)은 본문 — steps · content_spec · measures · duration · eligibility … 활동 노드
담당이 채우고, prepare(콘텐츠) · get_care_protocols(후보) · check_plan(후검증)이 읽는다. 코드가 읽는
필드만 형을 고정하고, 나머지 블록(selection · render · personalization · generation …)은
extra="allow"로
담당자가 자유롭게 넓힌다. 시트 → JSON 초안은 scripts/scaffold_protocols.py가 만든다.
"""

from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.care import CareGoal
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
from app.schemas.measure import InstrumentId

ProtocolId = Annotated[str, StringConstraints(pattern=r"^PR-[0-9]{2}$")]

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

    protocol_id: ProtocolId
    media: list[Medium] = []  # 제공 매체(대안 포함, AI_IMAGE 가능). 단계 모듈(PR-16)은 빈 목록
    # 허용 사용자 상태. 레코드의 intervention_orders(연구가 수행된 조건)와 뜻이 다르다
    intervention_order: list[InterventionOrder] = []
    paper_id_scheme: str
    grades: dict[Outcome, GradeEntry]
    evidence: list[ProtocolEvidenceRef] = []  # 83편 밖 근거뿐이면 비운다(PR-21 Lymeus 2017)

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


# ── 본문 — prepare · get_care_protocols · check_plan이 읽는다 ──────────────────────────────


class Composition(StrEnum):
    """구성 유형(프로토콜 시트). 플래너는 프로토콜을 고를 뿐 임의로 조합하지 않는다."""

    SINGLE = "SINGLE"  # 단일
    VALIDATED_MULTIMODAL = "VALIDATED_MULTIMODAL"  # 조합 자체가 실험 자극 — PR-02 · 10 · 12
    EXPERIMENTAL = "EXPERIMENTAL"  # 조합 근거 없음. A/B로만 제공, '실험적' 표시 — PR-15 · 21 · 22
    STAGE_MODULE = "STAGE_MODULE"  # 다른 프로토콜의 NOTICE 단계 — PR-16. 단독 활동으로 쓰지 않는다


class Channel(StrEnum):
    IN_APP = "IN_APP"
    OUTDOOR = "OUTDOOR"  # 앱 밖 활동. FE는 타이머와 사전·사후만
    OUTDOOR_APP = "OUTDOOR_APP"  # 야외 활동 + 앱 기록
    IN_APP_OR_PAPER = "IN_APP_OR_PAPER"  # 디지털 또는 종이(컬러링)


class StepType(StrEnum):
    ACTIVITY = "ACTIVITY"
    NOTICE = "NOTICE"  # 한 단어 알아차리기. 점수화하지 않는다(E10)
    CHOOSE = "CHOOSE"


class SlotType(StrEnum):
    """content_spec.slots[].slot — prepare가 채우는 콘텐츠 자리."""

    VISUAL_STILL = "VISUAL_STILL"
    VISUAL_VIDEO = "VISUAL_VIDEO"
    SOUNDSCAPE = "SOUNDSCAPE"
    MUSIC = "MUSIC"
    GUIDE_AUDIO = "GUIDE_AUDIO"  # 안내 음성이 자극 자체인 경우만(호흡 · PMR · 심상 · 마음챙김)
    PACER = "PACER"  # 호흡 박자 표시
    COLORING_TEMPLATE = "COLORING_TEMPLATE"
    ARTWORK = "ARTWORK"
    WRITING_PROMPT = "WRITING_PROMPT"
    OUTDOOR_TASK = "OUTDOOR_TASK"
    NOTICE_PROMPT = "NOTICE_PROMPT"


class Step(BaseModel):
    model_config = ConfigDict(extra="allow")

    order: int = Field(ge=1)
    type: StepType
    label: str
    minutes: float = Field(gt=0)
    slots: list[SlotType] = []
    optional: bool = False
    user_can_stop: bool = True
    note: str | None = None


class Duration(BaseModel):
    model_config = ConfigDict(extra="allow")

    default: float = Field(gt=0)
    min: float | None = Field(default=None, gt=0)
    max: float | None = Field(default=None, gt=0)
    evidence: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min is not None and self.default < self.min:
            raise ValueError("duration_minutes.default가 min보다 작다")
        if self.max is not None and self.default > self.max:
            raise ValueError("duration_minutes.default가 max보다 크다")
        return self


class Eligibility(BaseModel):
    model_config = ConfigDict(extra="allow")

    min_minutes_available: float | None = None
    needs: list[str] = []  # SCREEN · AUDIO · OUTDOORS …
    place: Literal["ANY", "INDOOR", "OUTDOOR"] = "ANY"
    quiet_required: bool = False
    not_recommended_if: list[str] = []


class Guide(BaseModel):
    """모든 프로토콜 공통의 짧은 시작 안내. intro_text가 있으면 prepare가 guide_text로 쓴다."""

    model_config = ConfigDict(extra="allow")

    intro_text: str | None = None
    intro_text_ref: str | None = None
    max_seconds: int | None = None
    source_policy: dict[str, str] = {}
    during_activity: str = "NONE"
    note: str | None = None


class Slot(BaseModel):
    model_config = ConfigDict(extra="allow")

    slot: SlotType
    required: bool = True
    evidence_stimulus: str | None = None
    source_policy: dict[str, str] = {}
    note: str | None = None


class ContentSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    slots: list[Slot] = []

    @property
    def slot_types(self) -> set[SlotType]:
        return {s.slot for s in self.slots}


class Measures(BaseModel):
    """어떤 도구를 쓰는지만 가리킨다. 측정은 노드 바깥(measure_pre/post/final 코드 노드)이 한다."""

    model_config = ConfigDict(extra="allow")

    anchor: InstrumentId | None = InstrumentId.SAM
    goal_scale_by_goal: dict[CareGoal, InstrumentId | None] = {}
    schedule: dict[str, list[str]] = {}
    note: str | None = None


class Relation(BaseModel):
    model_config = ConfigDict(extra="allow")

    protocol_id: ProtocolId
    composition: Composition | None = None
    basis: str | None = None
    reason: str | None = None


class Review(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["DRAFT", "REVIEWED"] = "DRAFT"
    open_items: list[str] = []
    source: str | None = None
    reviewed_by: str | None = None
    updated_at: str | None = None


class CareProtocol(ProtocolEvidenceSection):
    """활동 노드 JSON 전체. 파일명 = protocol_id."""

    schema_version: str = "1.0"
    record_type: Literal["care_protocol", "stage_module"] = "care_protocol"
    name: str
    name_en: str | None = None
    summary: str | None = None
    goal: CareGoal
    secondary_goals: list[CareGoal] = []
    composition: Composition
    channel: Channel = Channel.IN_APP
    duration_minutes: Duration
    eligibility: Eligibility = Eligibility()
    guide: Guide = Guide()
    steps: list[Step] = []
    measures: Measures = Measures()
    content_spec: ContentSpec = ContentSpec()
    constraints: list[str] = []
    safety: list[str] = []
    unverified_claims: list[str] = []
    population_validated: list[str] = []
    population_note: str | None = None
    compatible_with: list[Relation] = []
    exclusive_with: list[Relation] = []
    user_facing_flags: dict[str, Any] = {}
    review: Review = Review()

    @model_validator(mode="after")
    def _body_invariants(self) -> Self:
        is_module = self.composition is Composition.STAGE_MODULE
        if is_module != (self.record_type == "stage_module"):
            raise ValueError("STAGE_MODULE이면 record_type은 stage_module, 아니면 care_protocol")
        if not self.steps:
            raise ValueError("steps가 비어 있다")
        if [s.order for s in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("steps.order는 1부터 이어져야 한다")
        if not is_module and not any(s.type is StepType.ACTIVITY for s in self.steps):
            raise ValueError("ACTIVITY 단계가 하나는 있어야 한다")
        declared = self.content_spec.slot_types
        for step in self.steps:
            undeclared = [s.value for s in step.slots if s not in declared]
            if undeclared:
                raise ValueError(
                    f"steps[{step.order}]의 슬롯 {undeclared}이 content_spec.slots에 없다"
                )
        if self.goal in self.secondary_goals:
            raise ValueError("goal이 secondary_goals에도 있다")
        for relation in self.compatible_with + self.exclusive_with:
            if relation.protocol_id == self.protocol_id:
                raise ValueError("자기 자신과의 관계는 적지 않는다")
        return self

    @property
    def goals(self) -> list[CareGoal]:
        return [self.goal, *self.secondary_goals]

    def serves(self, goal: CareGoal) -> bool:
        return goal in self.goals

    def allows_order(self, order: InterventionOrder | None) -> bool:
        """허용 사용자 상태. 빈 목록은 제한 없음. NO_INDUCTION은 양쪽 다 '어느 쪽이든'이다 —
        프로토콜의 NO_INDUCTION(일상 반복)은 어느 상태의 사용자에게나 쓰고, 특별한 계기가 없는
        사용자(NO_INDUCTION)는 어느 프로토콜이든 쓴다. 근거 쪽 retrieval.ORDER_COMPATIBILITY와 같은
        정책이다.
        """
        if order is None or not self.intervention_order:
            return True
        return (
            order in self.intervention_order
            or InterventionOrder.NO_INDUCTION in (order, *self.intervention_order)
        )

    def fits_minutes(self, minutes: int | None) -> bool:
        if minutes is None:
            return True
        floor = self.duration_minutes.min or self.duration_minutes.default
        return floor <= minutes
