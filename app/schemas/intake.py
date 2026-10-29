"""intake 질문·답 계약 — FE와 주고받는 JSON의 키는 이 모듈이 원천이다.

intake는 State(대화) → Goal(추론) → Context(환경 칩) 순서로 정보를 모으고, 부족하면 묻는다
(app/agent/nodes/intake.py). 질문은 `intake_ask` 노드의 interrupt payload로 FE에 가고, 답은 resume
값으로 온다. measure의 payload에는 type이 없다 — FE는 API 응답의 step으로 분기한다.

    BE → FE  IntakeQuestion  {"type": "intake_question", "question_id", "slot", "turn",
                              "max_turns", "text", "groups": [{"key", "select", "options":
                              [{"label", "set"}]}], "allow_free_text", "skip_label"}
    FE → BE  IntakeAnswer    {"question_id", "answer": {...}}  또는  {"question_id", "text": "..."}
                             또는  {"question_id", "skip": true}  (되묻기의 '그냥 시작할게요')

입구 되묻기(restate)
- 처음 질문 "오늘 상황이 어떠세요?"(OPENING_TEXT)는 FE 시작 화면에 고정이다.
  그 답이 CareRequest.text다.
- 답이 지금 상태·상황과 관계없으면(점심 메뉴 · 코드 질문 · 아무 글자) ① 상황 읽기가
  OFF_TOPIC으로 보고, 사용자 말을 받아 주며 오늘 상황을 가볍게 다시 묻는 문장을 만든다.
  slot = restate, 글로만 답하고(groups = []), skip_label '그냥 시작할게요'를 누르면 goal = S2,
  goal_source = DEFAULT로 시작한다.
- 되묻기는 관련 있는 답이 올 때까지 제한 없이 돈다. 질문 3회 예산에 세지 않는다(turn = 0,
  question_id = r1 · r2 …). 예산은 관련 있는 답 뒤의 질문부터 센다.

보기 규칙
- select는 single만 쓴다. 한 묶음의 보기는 2~5개다.
- '잘 모르겠어요'(set = {})는 State 질문(state · state_followup)의 마지막 보기로 코드가 붙이고,
  2~5개에 포함된다(LLM 보기 최대 4개 + 잘 모르겠어요). Context 묶음에는 붙이지 않는다 —
  setting 4개, earphones 2개(있어요 true / 없어요 false), minutes 3개. 묶음에서 아무것도 안 고르면
  그 키가 answer에 없다.
- 보기의 set은 "이 보기를 고르면 어떤 칸이 어떤 값이 되는지"다. FE는 묶음마다 고른 보기의 set을 합쳐
  answer로 돌려준다. slot마다 받을 수 있는 키와 값이 정해져 있다(SLOT_KEYS · parse_set) — LLM이 만든
  보기도, FE가 보낸 답도 같은 검사를 지난다. 목표 단서(goal_hint)는 state_followup에서만 받는다.
- LLM 보기가 검증 뒤 2개 미만이면 고정 질문으로 바꾼다(app/agent/intake_rules.STATE_FIXED ·
  FOLLOWUP_FIXED).

질문 수와 끝
- 질문은 합계 MAX_TURNS(3)회, 그중 State는 MAX_STATE_TURNS(2)회.
- State 두 질문(시점 + 재질문)이 모두 '잘 모르겠어요'면 추론 없이 goal = S2, goal_source = DEFAULT.
- 안전 판단은 ① 상황 읽기(LLM)의 safety 칸이다. 처음 입력과 모든 글 답(되묻기 · State 질문)이
  ①을 지나므로 모두 판단된다. 걸리면 세션을 여기서 끝낸다(end_reason = SAFETY, 측정·활동으로 가지
  않는다). 정규식 목록은 쓰지 않는다 — 표현이 다양해 놓치는 것이 많다.
"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.schemas.care import CareGoal, Setting
from app.schemas.evidence import InterventionOrder

MAX_TURNS = 3  # 질문 합계 상한(State + Context)
MAX_STATE_TURNS = 2  # 시점 질문 + 재질문 1회
MIN_OPTIONS = 2  # single 묶음 보기 수(잘 모르겠어요 포함)
MAX_OPTIONS = 5
# 시간 칩 · 보기. 3 = 짧은 활동(PR-20 1–2분 · PR-09 3분), 20 = 20분 이상
MINUTES_CHOICES = (3, 5, 10, 20)
# 시간을 되물을지 판단하는 경계. 3분은 보기에만 둔다 — 넣으면 S2마다 시간을 더 묻게 된다
MINUTES_ASK_BOUNDARIES = (5, 10, 20)
UNKNOWN_LABEL = "잘 모르겠어요"
OPENING_TEXT = "오늘 상황이 어떠세요?"  # FE 시작 화면의 고정 질문(답 = CareRequest.text)
SKIP_LABEL = "그냥 시작할게요"  # 되묻기 화면의 출구 → 무작위 DEFAULT 목표


class QuestionSlot(StrEnum):
    RESTATE = "restate"  # 입구 되묻기 — 관계없는 답 뒤. 글로만 답하고 예산에 세지 않는다
    STATE = "state"  # 시점 질문 — 사건 전·후 · 오래 집중한 뒤 · 계기 없음
    # 재질문 — 원인(압박 vs 오래 집중), 또는 상황 · 증상이 엇갈리면 지금 상태. goal_hint도 받는다
    STATE_FOLLOWUP = "state_followup"
    CONTEXT = "context"  # 환경 칩 — setting · earphones · minutes


class SelectMode(StrEnum):
    SINGLE = "single"  # 하나만 선택


class GoalSource(StrEnum):
    """목표를 어떻게 정했나. 추천 이유 문구와 평가 해석(정확도 분석)에 쓴다."""

    CHIP = "CHIP"  # 요청의 goal(리허설용)
    INFERRED = "INFERRED"  # 단서표 + 상황 단서로 추론
    DEFAULT = "DEFAULT"  # 단서가 없거나 State 두 질문이 모두 '잘 모르겠어요' → S2. 화면에 밝힌다


class IntakeEndReason(StrEnum):
    """intake가 끝난 이유."""

    SUFFICIENT = "SUFFICIENT"  # 더 물을 것이 없다(추천을 바꾸는 빈 칸이 없음)
    MAX_TURNS = "MAX_TURNS"  # 질문 3회를 채웠다 — 남은 빈 칸은 기본값
    SAFETY = "SAFETY"  # 처음 입력이나 글 답에 안전 신호(① safety) — 세션 종료


DEFAULT_GOAL = CareGoal.S2  # finish의 마지막 안전망. 단서 없음은 intake_rules.default_goal(무작위)

# slot → set에 쓸 수 있는 키
SLOT_KEYS: dict[QuestionSlot, frozenset[str]] = {
    QuestionSlot.RESTATE: frozenset(),  # 글 답만
    QuestionSlot.STATE: frozenset({"order_condition"}),
    QuestionSlot.STATE_FOLLOWUP: frozenset({"order_condition", "goal_hint"}),
    QuestionSlot.CONTEXT: frozenset({"setting", "earphones", "minutes"}),
}


def parse_set(slot: QuestionSlot, values: dict[str, Any]) -> dict[str, Any]:
    """set(또는 답) → 검증된 값. slot에 없는 키나 허용 밖의 값이면 ValueError.

    돌려주는 값은 enum으로 바꾼 것이다 — {"order_condition": InterventionOrder.POST_STRESS}.
    """
    allowed = SLOT_KEYS[slot]
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"{slot.value} 질문에는 {sorted(unknown)}를 받지 않는다")
    out: dict[str, Any] = {}
    for key, value in values.items():
        if key == "order_condition":
            out[key] = InterventionOrder(value)
        elif key == "goal_hint":
            hints = [CareGoal(v) for v in value]
            if not 1 <= len(set(hints)) <= 2:
                raise ValueError("goal_hint는 S1 · S2 · S3 중 1~2개다")
            out[key] = list(dict.fromkeys(hints))
        elif key == "setting":
            out[key] = Setting(value)
        elif key == "earphones":
            if not isinstance(value, bool):
                raise ValueError("earphones는 true · false다")
            out[key] = value
        elif key == "minutes":
            if value not in MINUTES_CHOICES:
                raise ValueError(f"minutes는 {MINUTES_CHOICES} 중 하나다")
            out[key] = int(value)
    return out


class QuestionOption(BaseModel):
    label: str = Field(min_length=1, description="화면에 보일 문구")
    set: dict[str, Any] = Field(
        default_factory=dict, description='고르면 채워질 칸과 값. "잘 모르겠어요"는 {}'
    )


class QuestionGroup(BaseModel):
    key: str = Field(description="state · state_followup · setting · earphones · minutes")
    select: SelectMode = SelectMode.SINGLE
    options: list[QuestionOption] = Field(min_length=MIN_OPTIONS, max_length=MAX_OPTIONS)


class IntakeQuestion(BaseModel):
    """BE → FE. intake_ask 노드의 interrupt payload."""

    type: Literal["intake_question"] = "intake_question"
    question_id: str  # q1 · q2 · q3. 되묻기는 r1 · r2 …
    slot: QuestionSlot
    turn: int = Field(ge=0)  # 예산 안 몇 번째 질문. 되묻기는 0(세지 않는다)
    max_turns: int = MAX_TURNS
    text: str = Field(min_length=1)
    groups: list[QuestionGroup] = []  # 되묻기는 [](글로만 답한다)
    allow_free_text: bool
    skip_label: str | None = None  # 되묻기만 — '그냥 시작할게요'

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.slot is QuestionSlot.RESTATE:
            if self.groups or not self.allow_free_text or self.turn != 0:
                raise ValueError("되묻기는 보기 없이 글로만 받고 turn = 0이다")
        elif not self.groups or self.turn < 1 or self.skip_label is not None:
            raise ValueError(f"{self.slot.value} 질문은 보기 묶음이 있고 turn ≥ 1이다")
        return self


class IntakeAnswer(BaseModel):
    """FE → BE. intake_ask의 resume 값. answer · text · skip 중 하나만."""

    question_id: str
    answer: dict[str, Any] | None = None  # 고른 보기들의 set을 합친 것. 잘 모르겠어요 = {}
    text: str | None = None  # 직접 입력(allow_free_text일 때만). ① 상황 읽기가 안전도 판단한다
    skip: bool = False  # 되묻기의 '그냥 시작할게요'

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        given = [
            self.answer is not None,
            self.text is not None and bool(self.text.strip()),
            self.skip,
        ]
        if sum(given) != 1:
            raise ValueError("answer · text · skip 중 하나만 보낸다")
        return self


class IntakeTurn(BaseModel):
    """질문 하나와 그 답. 그래프 state의 intake_turns에 쌓인다.

    글 답을 다시 읽을 때 LLM에 넘긴다. answer = {}는 '잘 모르겠어요'다.
    """

    question_id: str
    slot: QuestionSlot
    text: str
    answer: dict[str, Any] | None = None  # 검증된 칩 답(JSON 값). 글 답이면 None
    free_text: str | None = None
    skipped: bool = False  # 되묻기에서 '그냥 시작할게요'


# ── LLM 구조화 출력의 질문 초안. set 대신 타입이 정해진 칸을 쓴다(json_schema strict에서 안전) ──


class StateOptionDraft(BaseModel):
    label: str = Field(description="보기 문구(한국어, 짧게). 예: 방금 있었던 일 때문이에요")
    order_condition: InterventionOrder = Field(description="이 보기를 고르면 정해지는 사용자 상태")
    goal_hint: list[CareGoal] = Field(
        default=[],
        description="재질문(followup)에서만 — 이 보기가 가리키는 목표 1~2개. "
        "시점 질문에서는 비운다",
    )


class StateQuestionDraft(BaseModel):
    followup: bool = Field(
        description="false = 시점 질문. true = 재질문(글 답이 여전히 불명확하고, 압박 때문인지 "
        "오래 집중한 탓인지가 갈릴 때만)"
    )
    text: str = Field(
        description="질문 한 문장(한국어). 사건을 자세히 묻지 않고 시점·원인만 묻는다"
    )
    options: list[StateOptionDraft] = Field(
        description="보기 2~4개. '잘 모르겠어요'는 넣지 않는다(코드가 붙인다)"
    )
