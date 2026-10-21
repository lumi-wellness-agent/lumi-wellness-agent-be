"""그래프 상태와 노드 입출력 스키마.

노드는 이 모듈의 타입으로만 주고받는다. 노드끼리 직접 부르지 않고 graph.py만 노드를 배선한다.
state에 새 타입을 넣으면 checkpoint.py의 STATE_TYPES에도 등록한다.
"""

import operator
from enum import StrEnum
from typing import Annotated, NotRequired, TypedDict

from pydantic import BaseModel, Field

from app.schemas.care import CareGoal, CareRequest
from app.schemas.evidence import Medium


class PlanMode(StrEnum):
    SINGLE = "SINGLE"  # 단일 세션 — S1·S2·S3 중 하나
    CONCURRENT = "CONCURRENT"  # 동시 복합 — 호환되는 활동 병행
    SEQUENTIAL = "SEQUENTIAL"  # 순차 복합 — 2~3개 활동 연계


class PlannedActivity(BaseModel):
    """계획에 넣은 활동 하나. 실행 절차는 검수된 Care Protocol에서 온다."""

    protocol_id: str = Field(description="검수된 Care Protocol의 ID (get_care_protocols 결과)")
    goal: CareGoal = Field(description="이 활동이 겨냥하는 세션")
    medium: Medium = Field(description="활동 매체")
    minutes: int = Field(gt=0, description="활동 시간(분)")
    evidence_ids: list[int] = Field(
        default_factory=list, description="validate_evidence를 통과한 근거의 Evidence Record ID"
    )


class CarePlan(BaseModel):
    """① Planner의 출력. 순차 복합은 activities 순서대로 실행한다.

    활동을 결합한 계획(동시·순차 복합)은 개별 근거가 있어도 통합 효과가 검증되지 않은 것으로
    표시한다.
    """

    mode: PlanMode = Field(description="SINGLE · CONCURRENT · SEQUENTIAL")
    activities: list[PlannedActivity] = Field(min_length=1, max_length=3)
    rationale: str = Field(description="활동 선택·조합·순서의 이유. Reference 카드의 바탕이 된다")


class ActivityContent(BaseModel):
    """② Executor가 활동마다 준비한 콘텐츠. FE 플레이어가 그대로 쓴다."""

    protocol_id: str
    image_urls: list[str] = []
    guide_text: str = ""
    # 안내 음성·자연음 URL은 Executor를 구현하며 더한다


class SessionReport(BaseModel):
    """FE가 활동을 마친 뒤 보내는 값. run_session의 interrupt를 재개한다."""

    completed: bool  # 불편하거나 어지러워 중간에 멈췄으면 False
    stopped_reason: str | None = None
    post_stress_level: int = Field(ge=0, le=10)


class OutcomeEvaluation(BaseModel):
    """③ Outcome Evaluator의 출력."""

    goal: CareGoal
    stress_change: int  # 사후 - 사전. 음수면 줄었다
    improved: bool


class NextStep(StrEnum):
    END = "END"  # 종료
    CHANGE_ACTIVITY = "CHANGE_ACTIVITY"  # 활동 변경
    ADDITIONAL_SESSION = "ADDITIONAL_SESSION"  # 추가 세션


class ReplanProposal(BaseModel):
    """④ Replanning이 사용자에게 보여 주는 제안."""

    recommended: NextStep
    reason: str


class ReplanDecision(BaseModel):
    """사용자의 선택(동의). confirm의 interrupt를 재개한다."""

    next_step: NextStep
    # 다음 라운드의 입력. 없으면 이전 입력에 이번 사후 스트레스 수준을 사전 값으로 넣어 쓴다
    request: CareRequest | None = None


class Round(BaseModel):
    """끝난 라운드 하나. Planner가 다음 계획에서 같은 활동을 되풀이하지 않는 데 쓴다."""

    plan: CarePlan
    report: SessionReport
    evaluation: OutcomeEvaluation
    next_step: NextStep


class CareState(TypedDict):
    """세션 하나(LangGraph thread 하나)의 상태. 라운드마다 plan~next_step을 덮어쓴다."""

    request: CareRequest  # 사용자 상태·목표·선호
    plan: NotRequired[CarePlan]
    contents: NotRequired[list[ActivityContent]]
    report: NotRequired[SessionReport]
    evaluation: NotRequired[OutcomeEvaluation]
    proposal: NotRequired[ReplanProposal]
    next_step: NotRequired[NextStep]
    rounds: NotRequired[Annotated[list[Round], operator.add]]  # 끝난 라운드. 뒤에 쌓인다
