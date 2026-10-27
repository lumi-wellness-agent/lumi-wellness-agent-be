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

from app.schemas.care import CareGoal, CareRequest, Environment, Place, Setting
from app.schemas.evidence import InterventionOrder, Outcome
from app.schemas.intake import GoalSource, IntakeEndReason, IntakeQuestion, IntakeTurn
from app.schemas.measure import Measurement


class GoalEvidence(BaseModel):
    """목표 판단의 근거 하나 — 논문과 원문 문장."""

    paper_id: str
    sentence: str


class SessionEnd(StrEnum):
    """측정·활동 전에 세션을 끝낸 이유."""

    SAFETY = "SAFETY"  # ① 상황 읽기가 안전 신호를 봤다(처음 입력 · 글 답) → 활동 대신 도움 안내


class IntakeResult(BaseModel):
    """intake의 결과 — Goal · State · Context. 코드가 단계마다 채우고, 점수는 추정하지 않는다.

    - Goal: 이번 세션에서 개선할 결과 지표(S1 반추 · S2 긴장·스트레스 · S3 주의·피로). 사용자가
      고르는 니즈가 아니라 개념 정의(원문) · 단서표와 상황 단서로 추론한다. 측정
      척도 · 후보 · 평가를 정한다.
    - State: 지금 상황(사건 전·후 · 오래 집중한 뒤 · 계기 없음). 대화로 정하고 Goal 추론의 단서가
      된다.
    - Context: 여건(장소 · 이어폰 · 시간). 칩으로 받고, 후보 활동을 거른다(environment).
    """

    # Goal
    goal: CareGoal | None = None
    secondary_goal: CareGoal | None = None  # 함께 보인 다른 목표(S1·S2 혼합 등). 측정은 goal만
    goal_source: GoalSource | None = None
    target_outcomes: list[Outcome] = []  # 개선할 지표 — retrieval.GOAL_OUTCOMES[goal].primary
    goal_evidence: list[GoalEvidence] = []  # 추론한 목표 · 부목표의 개념 정의(원문). 칩·기본값 []
    goal_reason: str | None = None  # 판단 이유 한 줄(Reference 카드 · 평가용)
    # State
    order_condition: InterventionOrder | None = None
    event: str | None = None  # 사건·계기 짧게(발표 · 시험공부 …). 자세히 묻지 않는다
    rumination_cue: bool = False  # 같은 생각·장면이 반복된다는 표현이 있다
    goal_hint: list[CareGoal] = []  # 재질문 답이 가리킨 목표(스트레스 vs 피로)
    cues: list[str] = []  # 판단에 쓴 사용자 표현(최대 3개)
    # Context
    setting: Setting | None = None  # 환경 칩(HOME · OFFICE_CAFE · TRANSIT · OUTDOOR)
    earphones: bool | None = None  # 있어요 true · 없어요 false · 모름 None
    environment: Environment = Environment()  # setting + earphones → 소리 · 조용함 · 화면 · 야외
    minutes: int | None = Field(default=None, gt=0)
    place: Place | None = None  # setting에서(care.place_for — OUTDOOR → OUTDOOR, 그 밖 → INDOOR)
    constraints: list[str] = []  # 이명 · 멀미 · 눈 피로 … (문장에서 읽은 것)
    # 기타
    missing_slots: list[str] = []  # 기본값으로 메운 칸 — FE가 "소리가 나와요" 같은 안내를 띄운다
    end_reason: IntakeEndReason | None = None  # 끝난 이유 — SUFFICIENT · MAX_TURNS · SAFETY
    summary: str | None = None  # 상태 한 줄 요약(플래너 · Reference 카드용)


class PlannedActivity(BaseModel):
    """계획에 넣은 활동 하나. 실행 절차는 검수된 Care Protocol(data/protocols)에서 온다."""

    protocol_id: str = Field(description="검수된 Care Protocol의 ID (후보 목록에서 고른다)")
    minutes: int = Field(gt=0, description="활동 시간(분). 프로토콜의 duration_minutes 범위 안")
    evidence_record_ids: list[int] = Field(
        default_factory=list,
        description="검증을 통과한(usable) 근거의 Evidence Record ID. 목표를 지지하는 것이 앞. "
        "코드가 채운다",
    )


def default_environment() -> Environment:
    """장소를 끝내 모를 때의 가정 — 혼자 조용히 소리를 들을 수 있다. 활동 시작 화면이 "소리가
    나와요" 안내와 "지금은 어려워요"(활동 변경)를 함께 보여 주는 조건이다(missing_slots)."""
    return Environment(audio=True, quiet=True, screen=True, outdoor=False)


class CarePlan(BaseModel):
    """① Planner의 출력. 라운드당 활동 노드 1개 — 검증된 다중매체 프로토콜도 노드 1개로 센다."""

    activity: PlannedActivity
    rationale: str = Field(
        description="추천 이유 2~3문장 — 사용자 상태, 고른 이유, 근거(저자·연도·지표)와 한계. "
        "사용자에게 보인다. Reference 카드의 사실(출처·플래그)은 evidence_record_ids에서 만든다"
    )
    # 다 맞는 후보가 없어 푼 조건(protocols.plan_candidates) — order면 근거의 사용자 상태와 다르고,
    # environment면 지금 환경에서 하기 어려운 활동일 수 있다
    relaxed: list[str] = []
    # 고른 활동이 지금 환경에서 어려운 이유(AUDIO · SCREEN · QUIET · OUTDOORS). FE가 "소리가 나와요"
    # 같은 안내를 띄운다. 환경을 풀지 않았으면 비어 있다
    environment_gaps: list[str] = []


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
    # 도구별 scores 키를 합친다 — SAM valence·arousal, S3는 MFVAS fatigue·concentration·energy도
    deltas: dict[str, float]
    improved: bool | None  # None = 세션 내 판정 기준 없음(evaluator.IMPROVEMENT_RULE)
    completed: bool


class NextStep(StrEnum):
    END = "END"  # 종료
    CHANGE_ACTIVITY = "CHANGE_ACTIVITY"  # 활동 변경 (같은 목표 · 다른 프로토콜)
    ADDITIONAL_SESSION = "ADDITIONAL_SESSION"  # 추가 세션


class ReplanProposal(BaseModel):
    """④ Replanning이 사용자에게 보여 주는 제안."""

    recommended: NextStep
    reason: str
    # 고를 수 있는 다음 단계 — 라운드 상한이면 END만, 바꿀 활동이 없으면 CHANGE_ACTIVITY를 뺀다
    # (replanner.next_step_options). confirm payload의 options이고 세션 API가 결정을 이것으로 거른다
    options: list[NextStep] = Field(default_factory=lambda: list(NextStep))


class ReplanDecision(BaseModel):
    """사용자의 선택(동의). confirm의 interrupt를 재개한다."""

    next_step: NextStep


class Round(BaseModel):
    """끝난 라운드 하나. Planner가 다음 계획에서 같은 활동을 되풀이하지 않는 데 쓴다."""

    plan: CarePlan
    report: SessionReport
    pre: list[Measurement]  # 이 라운드가 비교한 측정 — SAM, S3는 MFVAS도
    post: list[Measurement]
    evaluation: OutcomeEvaluation
    next_step: NextStep


class CareState(TypedDict):
    """세션 하나(LangGraph thread 하나)의 상태. 라운드마다 plan~next_step을 덮어쓴다."""

    request: CareRequest  # 사용자 자유 입력 + 환경 칩 + 프로필
    intake: NotRequired[IntakeResult]  # Goal · State · Context. intake 단계마다 갱신된다
    # intake 진행 — 질문과 답(쌓인다), 대기 중인 질문
    intake_turns: NotRequired[Annotated[list[IntakeTurn], operator.add]]
    pending_question: NotRequired[IntakeQuestion | None]
    ended: NotRequired[SessionEnd]
    pre: NotRequired[list[Measurement]]  # 세션 시작 측정(SAM + 목표 척도)
    # 이번 라운드가 비교할 측정(SAM, S3는 + MFVAS). 첫 라운드는 pre에서 고른다
    round_pre: NotRequired[list[Measurement]]
    plan: NotRequired[CarePlan]
    content: NotRequired[ActivityContent]
    report: NotRequired[SessionReport]
    post: NotRequired[list[Measurement]]  # 이번 라운드 사후 측정(SAM, S3는 + MFVAS)
    evaluation: NotRequired[OutcomeEvaluation]
    proposal: NotRequired[ReplanProposal]
    next_step: NotRequired[NextStep]
    final: NotRequired[Measurement]  # 세션 끝 목표 척도(STAI-6 · BSRI). 라운드마다 잰 S3는 없다
    rounds: NotRequired[Annotated[list[Round], operator.add]]  # 끝난 라운드. 뒤에 쌓인다
