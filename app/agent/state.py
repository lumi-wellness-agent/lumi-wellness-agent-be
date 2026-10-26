"""그래프 상태와 노드 입출력 스키마 (에이전트 아키텍처 v2).

노드는 이 모듈의 타입으로만 주고받는다. 노드끼리 직접 부르지 않고 graph.py만 노드를 배선한다.
state에 새 타입을 넣으면 checkpoint.py의 STATE_TYPES에도 등록한다.

라운드 = plan → prepare → run_session → measure_post → evaluate → replan → confirm 한 바퀴.
라운드당 활동 노드 1개. 다음 라운드의 사전값(round_pre)은 직전 라운드의 사후값(post)이다.
"""

import operator
from enum import StrEnum
from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import BaseModel, Field

from app.schemas.care import CareGoal, CareRequest, Place
from app.schemas.evidence import InterventionOrder
from app.schemas.measure import Measurement


class IntakeResult(BaseModel):
    """intake의 구조화 출력. LLM은 분류만 하고 점수는 추정하지 않는다."""

    goal: CareGoal | None = Field(
        default=None,
        description="S1 반복적 생각에서 벗어나기 · S2 긴장·스트레스 완화 · S3 주의·피로 회복. "
        "모호하면 null",
    )
    order_condition: InterventionOrder | None = Field(
        default=None,
        description="PRE_STRESS 스트레스가 예정됨(발표·면접 직전) · POST_STRESS 스트레스를 겪은 뒤 "
        "· "
        "POST_FATIGUE 오래 집중한 뒤 · NO_INDUCTION 특별한 유도 없음. 모르면 null",
    )
    minutes: int | None = Field(
        default=None, gt=0, description="쓸 수 있는 시간(분). 말하지 않았으면 null"
    )
    place: Place | None = Field(default=None, description="INDOOR · OUTDOOR. 말하지 않았으면 null")
    constraints: list[str] = Field(
        default=[], description="활동 제약 — 이명, 멀미, 눈 피로, 소리를 틀 수 없음 …"
    )
    safety: bool = Field(default=False, description="자해·위기 등 안전 신호가 있으면 true")
    safety_note: str | None = Field(default=None, description="안전 신호의 근거 한 줄")
    missing_slots: list[str] = Field(
        default=[],
        description="채우지 못한 슬롯 이름(goal · minutes · place). 코드가 기본값으로 메운다",
    )
    summary: str | None = Field(
        default=None, description="사용자 상태 한 줄 요약(플래너·Reference 카드용)"
    )


class PlannedActivity(BaseModel):
    """계획에 넣은 활동 하나. 실행 절차는 검수된 Care Protocol(data/protocols)에서 온다."""

    protocol_id: str = Field(description="검수된 Care Protocol의 ID (get_care_protocols 결과)")
    minutes: int = Field(gt=0, description="활동 시간(분). 프로토콜의 duration_minutes 범위 안")
    evidence_record_ids: list[int] = Field(
        default_factory=list, description="validate_evidence를 통과한 근거의 Evidence Record ID"
    )


class CarePlan(BaseModel):
    """① Planner의 출력. 라운드당 활동 노드 1개 — 검증된 다중매체 프로토콜도 노드 1개로 센다."""

    activity: PlannedActivity
    rationale: str = Field(description="활동 선택 이유. Reference 카드의 바탕이 된다")


class ContentStep(BaseModel):
    order: int
    type: str  # ACTIVITY · NOTICE · CHOOSE (app/schemas/protocol.py StepType)
    label: str
    minutes: float
    optional: bool = False


class ActivityAsset(BaseModel):
    """prepare가 공급자별 응답을 정규화한 콘텐츠 자산 하나."""

    slot: str  # VISUAL_STILL · VISUAL_VIDEO · SOUNDSCAPE · MUSIC · GUIDE_AUDIO · ARTWORK
    url: str
    provider: str
    title: str | None = None
    creator: str | None = None
    source_url: str | None = None
    license: str | None = None
    generated: bool = False
    verification: str = "VERIFIED"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActivityContent(BaseModel):
    """② prepare가 준비한 콘텐츠. FE 플레이어가 그대로 쓴다."""

    protocol_id: str
    name: str
    steps: list[ContentStep] = []
    assets: list[ActivityAsset] = []
    asset_warnings: list[str] = []
    image_urls: list[str] = []
    guide_text: str = ""
    notice_prompt: str | None = None
    # 안내 음성·자연음 URL · Reference 카드는 Executor를 구현하며 더한다


class SessionReport(BaseModel):
    """FE가 활동을 마친 뒤 보내는 값. run_session의 interrupt를 재개한다. 점수는 안 받는다."""

    completed: bool  # 불편하거나 어지러워 중간에 멈췄으면 False
    stopped_reason: str | None = None
    notice_word: str | None = None  # NOTICE 단계의 한 단어 응답. 점수화하지 않는다


class OutcomeEvaluation(BaseModel):
    """③ Outcome Evaluator의 출력. deltas는 사후 − 사전(음수면 줄었다)."""

    goal: CareGoal
    round: int
    deltas: dict[str, float]  # SAM: valence · arousal
    improved: bool | None  # None = 세션 내 판정 기준 없음(S3 목표 지표 '—')
    completed: bool


class NextStep(StrEnum):
    END = "END"  # 종료
    CHANGE_ACTIVITY = "CHANGE_ACTIVITY"  # 활동 변경 (같은 목표 · 다른 프로토콜)
    ADDITIONAL_SESSION = "ADDITIONAL_SESSION"  # 추가 세션


class ReplanProposal(BaseModel):
    """④ Replanning이 사용자에게 보여 주는 제안."""

    recommended: NextStep
    reason: str


class ReplanDecision(BaseModel):
    """사용자의 선택(동의). confirm의 interrupt를 재개한다."""

    next_step: NextStep


class Round(BaseModel):
    """끝난 라운드 하나. Planner가 다음 계획에서 같은 활동을 되풀이하지 않는 데 쓴다."""

    plan: CarePlan
    report: SessionReport
    pre: Measurement  # 이 라운드가 비교한 앵커(SAM)
    post: Measurement
    evaluation: OutcomeEvaluation
    next_step: NextStep


class SessionEnd(StrEnum):
    """intake가 세션을 열지 않고 끝낸 이유."""

    SAFETY = "SAFETY"  # 안전 신호 → 세션 대신 안전 안내
    NO_GOAL = (
        "NO_GOAL"  # 목표를 정하지 못함 → FE가 목표 칩을 보여 주고 request.goal을 채워 다시 시작
    )


class CareState(TypedDict):
    """세션 하나(LangGraph thread 하나)의 상태. 라운드마다 plan~next_step을 덮어쓴다."""

    request: CareRequest  # 사용자 자유 입력 + 칩 + 프로필
    intake: NotRequired[IntakeResult]  # 목표 · 순서 조건 · 슬롯
    ended: NotRequired[SessionEnd]
    pre: NotRequired[list[Measurement]]  # 세션 시작 측정(SAM + 목표 척도)
    round_pre: NotRequired[Measurement]  # 이번 라운드가 비교할 SAM. 첫 라운드는 pre의 SAM
    plan: NotRequired[CarePlan]
    content: NotRequired[ActivityContent]
    report: NotRequired[SessionReport]
    post: NotRequired[Measurement]  # 이번 라운드 사후 SAM
    evaluation: NotRequired[OutcomeEvaluation]
    proposal: NotRequired[ReplanProposal]
    next_step: NotRequired[NextStep]
    final: NotRequired[Measurement]  # 세션 끝 목표 척도(S3는 없다)
    rounds: NotRequired[Annotated[list[Round], operator.add]]  # 끝난 라운드. 뒤에 쌓인다
