"""intake의 규칙 — 무엇을 묻고, 답을 어떻게 합치고, 언제 끝내는지. LLM · LangGraph 없이 테스트한다.

노드(app/agent/nodes/intake.py)는 이 모듈의 함수만 부른다.

    ① 상황 읽기(LLM) → compose()로 State · Context 채움 → state_question()으로 시점 질문 여부
    ② 개념 정의 검색 → ③ 목표 추론(LLM) → apply_goal()
    ④ context_groups()로 추천을 바꾸는 환경 칸만 → context_question()
    답 → apply_answer() · merge_values()     끝 → finish()

우선순위: 요청 칩 > 질문 답(칩) > 문장에서 읽은 값 > 기본값. 질문은 합계 MAX_TURNS회, State는
MAX_STATE_TURNS회. 무엇을 물을지는 "그 칸이 1순위 활동을 바꾸는가"로 정하고, 그 판단은 플래너와
같은 protocols.candidates()로 한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from app import protocols
from app.agent.state import IntakeResult, default_environment
from app.schemas.care import CareGoal, CareRequest, Location, Place, environment_for
from app.schemas.evidence import InterventionOrder
from app.schemas.intake import (
    DEFAULT_GOAL,
    MAX_STATE_TURNS,
    MAX_TURNS,
    MINUTES_CHOICES,
    UNKNOWN_LABEL,
    GoalSource,
    IntakeAnswer,
    IntakeQuestion,
    IntakeTurn,
    QuestionGroup,
    QuestionOption,
    QuestionSlot,
    SelectMode,
    StateQuestionDraft,
    parse_set,
)

DEFAULT_MINUTES = 10
STATE_SLOTS = (QuestionSlot.STATE, QuestionSlot.STATE_FOLLOWUP)

# ── LLM 구조화 출력. 필드 설명이 LLM에 보인다 ──────────────────────────────────────────────


class StateReading(BaseModel):
    """① 상황 읽기 — 문장(과 앞선 질문·답)에서 State와 여건을 읽고, 검색어와 질문 초안을 낸다."""

    order_condition: InterventionOrder | None = Field(
        description="PRE_STRESS 앞으로 있을 구체적 사건(발표·면접·시험 직전) · "
        "POST_STRESS 방금·오늘 "
        "겪은 사건 · POST_FATIGUE 오래 이어진 정신적 작업 뒤 · NO_INDUCTION 계기 없이 일상적. "
        "말에 없으면 null — 추측하지 않는다"
    )
    event: str | None = Field(description="사건·계기를 몇 단어로(발표 · 시험공부 …). 없으면 null")
    rumination_cue: bool = Field(
        description="같은 생각·장면이 반복해서 떠오른다는 표현이 있으면 true "
        "(계속 생각나요 · 곱씹어요)"
    )
    stress_fatigue_ambiguous: bool = Field(
        description="지친 원인이 압박·사건인지 오래 이어진 집중인지 말로 갈리지 않으면 true"
    )
    cues: list[str] = Field(description="판단에 쓴 사용자 표현 그대로 최대 3개")
    constraints: list[str] = Field(description="활동 제약 — 이명 · 멀미 · 눈 피로 … 없으면 []")
    location: Location | None = Field(
        description="말에 나온 장소만 — HOME 집·혼자 있는 방 · OFFICE_CAFE 사무실·카페 · "
        "TRANSIT 지하철·버스 · OUTDOOR 밖·공원. 없으면 null"
    )
    earphones: bool | None = Field(
        description="이어폰이 있다/없다고 말했으면 true/false, 아니면 null"
    )
    minutes: int | None = Field(description="쓸 수 있는 시간(분)을 말했으면 그 숫자, 아니면 null")
    search_query: str = Field(
        description="개념 정의를 찾을 영어 검색어 4~8단어 — 사용자 상태를 심리학 용어로. "
        "예: repetitive negative thinking after presentation · state anxiety tense nervous · "
        "mental fatigue after prolonged concentration"
    )
    summary: str = Field(description="사용자 상황 한 줄(한국어, 40자 이내, 진단 용어 금지)")
    question: StateQuestionDraft | None = Field(
        description="order_condition을 정하지 못했을 때만 시점 질문 초안. 정했으면 null"
    )


class GoalInference(BaseModel):
    """③ 목표 추론 — 개념 정의 근거와 상황 단서로 이번 세션의 결과 지표를 정한다."""

    has_cues: bool = Field(description="문장이나 답에 목표를 가를 단서가 하나라도 있으면 true")
    goal: CareGoal = Field(description="S1 반추 · S2 긴장·스트레스 · S3 주의·피로 중 주 목표")
    secondary_goal: CareGoal | None = Field(
        description="함께 보인 다른 목표(S1·S2 혼합 등). goal과 같으면 안 된다. 없으면 null"
    )
    evidence_ids: list[int] = Field(
        description="판단에 쓴 개념 정의 근거의 ID — 근거 목록에 있는 것만. 없으면 []"
    )
    reason: str = Field(description="판단 이유 한 문장(한국어) — 어떤 단서가 어느 정의에 맞았나")


@dataclass(frozen=True)
class DefinitionHit:
    """② 개념 정의 검색 결과 하나(app/agent/definitions.find_definitions)."""

    evidence_record_id: int
    paper_id: str
    goal: CareGoal | None
    authors: str | None
    year: int | None
    excerpt: str


# ── ① State · Context 합치기 ──────────────────────────────────────────────────────────────


def chip_answers(turns: Sequence[IntakeTurn]) -> dict[str, Any]:
    """칩 답을 순서대로 합친 값(enum으로). 뒤의 답이 앞의 답을 덮는다. 글 답은 빼고 본다."""
    out: dict[str, Any] = {}
    for turn in turns:
        if turn.answer:
            out |= parse_set(turn.slot, turn.answer)
    return out


def request_chips(request: CareRequest) -> dict[str, Any]:
    """요청의 환경 칩. 옛 place 칩은 location이 없을 때만 OUTDOOR를 옮긴다."""
    out: dict[str, Any] = {}
    if request.location is not None:
        out["location"] = request.location
    elif request.place is Place.OUTDOOR:
        out["location"] = Location.OUTDOOR
    if request.earphones is not None:
        out["earphones"] = request.earphones
    if request.minutes_available is not None:
        out["minutes"] = request.minutes_available
    return out


def compose(
    request: CareRequest,
    reading: StateReading,
    turns: Sequence[IntakeTurn],
    previous: IntakeResult | None = None,
) -> IntakeResult:
    """State · Context = 문장에서 읽은 값 < 칩 답 < 요청 칩. Goal 칸은 previous에서 그대로 둔다."""
    minutes = reading.minutes if reading.minutes and reading.minutes > 0 else None
    values: dict[str, Any] = {
        "order_condition": reading.order_condition,
        "location": reading.location,
        "earphones": reading.earphones,
        "minutes": minutes,
    }
    values |= chip_answers(turns)
    values |= request_chips(request)
    base = previous or IntakeResult()
    return base.model_copy(
        update={
            "order_condition": values.get("order_condition"),
            "goal_hint": values.get("goal_hint", base.goal_hint),
            "event": reading.event,
            "rumination_cue": reading.rumination_cue,
            "cues": reading.cues[:3],
            "constraints": reading.constraints,
            "summary": reading.summary,
            "location": values.get("location"),
            "earphones": values.get("earphones"),
            "minutes": values.get("minutes"),
        }
    )


def merge_values(intake: IntakeResult, values: dict[str, Any]) -> IntakeResult:
    """검증된 칩 답(parse_set 결과)을 intake에 덮는다."""
    return intake.model_copy(update=values)


# ── 질문 ──────────────────────────────────────────────────────────────────────────────────

STATE_FALLBACK: tuple[tuple[str, dict[str, Any]], ...] = (
    ("방금 있었던 일 때문이에요", {"order_condition": "POST_STRESS"}),
    ("곧 있을 일이 걱정돼요", {"order_condition": "PRE_STRESS"}),
    ("오래 집중해서 지쳤어요", {"order_condition": "POST_FATIGUE"}),
    ("딱히 계기는 없어요", {"order_condition": "NO_INDUCTION"}),
)
FOLLOWUP_FALLBACK: tuple[tuple[str, dict[str, Any]], ...] = (
    ("오래 집중하는 일이 많았어요", {"order_condition": "POST_FATIGUE", "goal_hint": ["S3"]}),
    ("신경 쓰이는 일이 있었어요", {"order_condition": "POST_STRESS", "goal_hint": ["S2"]}),
    ("둘 다예요", {"order_condition": "POST_STRESS", "goal_hint": ["S2", "S3"]}),
)
STATE_TEXT = "방금 있었던 일 때문인가요, 곧 있을 일 때문인가요?"
FOLLOWUP_TEXT = "요즘 오래 집중해야 하는 일이 많았나요, 신경 쓰이는 일이 있었나요?"
MAX_OPTIONS = 4  # LLM 보기 상한. 잘 모르겠어요는 따로 붙는다


def state_slot(reading: StateReading, turns: Sequence[IntakeTurn]) -> QuestionSlot | None:
    """이번에 State 질문을 해도 되는가, 한다면 어느 slot인가.

    - 시점 질문: 아직 State 질문을 안 했고 상태를 못 정했을 때.
    - 재질문: 시점 질문에 글로 답했는데 아직도 스트레스와 피로가 갈릴 때 1회만.
    """
    if reading.question is None or len(turns) >= MAX_TURNS:
        return None
    asked = [t for t in turns if t.slot in STATE_SLOTS]
    if len(asked) >= MAX_STATE_TURNS:
        return None
    if not asked:
        return QuestionSlot.STATE if reading.order_condition is None else None
    last = asked[-1]
    if (
        last.slot is QuestionSlot.STATE
        and last.free_text is not None
        and reading.stress_fatigue_ambiguous
    ):
        return QuestionSlot.STATE_FOLLOWUP
    return None


def _draft_options(draft: StateQuestionDraft, slot: QuestionSlot) -> list[QuestionOption]:
    options = []
    for o in draft.options[:MAX_OPTIONS]:
        values: dict[str, Any] = {"order_condition": o.order_condition.value}
        if slot is QuestionSlot.STATE_FOLLOWUP and o.goal_hint:
            values["goal_hint"] = [g.value for g in o.goal_hint]
        try:
            parse_set(slot, values)
        except ValueError:
            continue
        if o.label.strip():
            options.append(QuestionOption(label=o.label.strip(), set=values))
    return options


def state_question(reading: StateReading, turns: Sequence[IntakeTurn]) -> IntakeQuestion | None:
    """LLM 초안을 검증해 질문으로. 보기가 2개 미만이면 고정 보기로 바꾼다."""
    slot = state_slot(reading, turns)
    if slot is None:
        return None
    draft = reading.question
    options = _draft_options(draft, slot)
    text = draft.text.strip()
    if len(options) < 2:
        fallback = STATE_FALLBACK if slot is QuestionSlot.STATE else FOLLOWUP_FALLBACK
        options = [QuestionOption(label=label, set=values) for label, values in fallback]
        text = STATE_TEXT if slot is QuestionSlot.STATE else FOLLOWUP_TEXT
    options.append(QuestionOption(label=UNKNOWN_LABEL, set={}))
    turn = len(turns) + 1
    return IntakeQuestion(
        question_id=f"q{turn}",
        slot=slot,
        turn=turn,
        text=text or (STATE_TEXT if slot is QuestionSlot.STATE else FOLLOWUP_TEXT),
        groups=[QuestionGroup(key=slot.value, select=SelectMode.SINGLE, options=options)],
        allow_free_text=True,
    )


LOCATION_OPTIONS: tuple[tuple[str, Location], ...] = (
    ("집이나 혼자 있는 방", Location.HOME),
    ("사무실·카페", Location.OFFICE_CAFE),
    ("지하철·버스", Location.TRANSIT),
    ("밖에 나갈 수 있어요", Location.OUTDOOR),
)


def top_activity(intake: IntakeResult, **override: Any) -> str | None:
    """이 조건일 때 플래너 후보 1순위(protocols.candidates — 플래너와 같은 함수)."""
    if intake.goal is None:
        return None
    location = override.get("location", intake.location)
    earphones = override.get("earphones", intake.earphones)
    minutes = override.get("minutes", intake.minutes)
    environment = environment_for(location, earphones)
    found = protocols.candidates(
        intake.goal, order=intake.order_condition, minutes=minutes, environment=environment
    )
    return found[0].protocol_id if found else None


def context_groups(intake: IntakeResult) -> list[str]:
    """물어볼 환경 칸 — 비어 있고, 값에 따라 1순위 활동이 달라지는 것만.

    location · earphones · minutes 순서다.
    """
    groups: list[str] = []
    if intake.location is None:
        earphones = bool(intake.earphones)
        tops = {
            top_activity(intake, location=loc, earphones=earphones) for _, loc in LOCATION_OPTIONS
        }
        if len(tops) > 1:
            groups.append("location")
    if intake.earphones is None:
        places = [intake.location] if intake.location else [loc for _, loc in LOCATION_OPTIONS]
        if any(
            top_activity(intake, location=loc, earphones=True)
            != top_activity(intake, location=loc, earphones=False)
            for loc in places
        ):
            groups.append("earphones")
    if (
        intake.minutes is None
        and len({top_activity(intake, minutes=m) for m in MINUTES_CHOICES}) > 1
    ):
        groups.append("minutes")
    return groups


def context_question(groups: Sequence[str], turn: int) -> IntakeQuestion | None:
    if not groups:
        return None
    built: list[QuestionGroup] = []
    asks: list[str] = []
    if "location" in groups:
        built.append(
            QuestionGroup(
                key="location",
                select=SelectMode.SINGLE,
                options=[
                    QuestionOption(label=label, set={"location": loc.value})
                    for label, loc in LOCATION_OPTIONS
                ],
            )
        )
        asks.append("지금 어디에 계세요?")
    if "earphones" in groups:
        built.append(
            QuestionGroup(
                key="earphones",
                select=SelectMode.TOGGLE,
                options=[QuestionOption(label="이어폰 있어요", set={"earphones": True})],
            )
        )
        if not asks:
            asks.append("이어폰이 있으세요?")
    if "minutes" in groups:
        built.append(
            QuestionGroup(
                key="minutes",
                select=SelectMode.SINGLE,
                options=[
                    QuestionOption(label="20분 이상" if m == 20 else f"{m}분", set={"minutes": m})
                    for m in MINUTES_CHOICES
                ],
            )
        )
        asks.append(
            "쓸 수 있는 시간도 알려 주세요." if asks else "쓸 수 있는 시간이 얼마나 되세요?"
        )
    return IntakeQuestion(
        question_id=f"q{turn}",
        slot=QuestionSlot.CONTEXT,
        turn=turn,
        text=" ".join(asks),
        groups=built,
        allow_free_text=False,
    )


def apply_answer(
    question: IntakeQuestion, answer: IntakeAnswer
) -> tuple[dict[str, Any], IntakeTurn]:
    """답 → (intake에 덮을 값, 대화 기록). 칩 답은 parse_set으로 검증한다."""
    if answer.question_id != question.question_id:
        raise ValueError(f"{question.question_id}의 답이 아니다({answer.question_id})")
    if answer.text is not None and answer.text.strip():
        if not question.allow_free_text:
            raise ValueError(f"{question.slot.value} 질문은 직접 입력을 받지 않는다")
        turn = IntakeTurn(
            question_id=question.question_id,
            slot=question.slot,
            text=question.text,
            free_text=answer.text.strip(),
        )
        return {}, turn
    values = parse_set(question.slot, answer.answer or {})
    stored = {k: (v.value if hasattr(v, "value") else v) for k, v in values.items()}
    if "goal_hint" in stored:
        stored["goal_hint"] = [g.value for g in values["goal_hint"]]
    turn = IntakeTurn(
        question_id=question.question_id, slot=question.slot, text=question.text, answer=stored
    )
    return values, turn


# ── ③ 목표 · 끝 ──────────────────────────────────────────────────────────────────────────


def cited_ids(hits: Sequence[DefinitionHit], claimed: Sequence[int]) -> list[int]:
    """LLM이 근거로 댄 ID 중 이번 검색 결과에 실제로 있었던 것만. 지어낸 ID는 버린다."""
    found = {h.evidence_record_id for h in hits}
    return [i for i in dict.fromkeys(claimed) if i in found]


def apply_goal(
    intake: IntakeResult,
    inference: GoalInference | None,
    hits: Sequence[DefinitionHit],
    request: CareRequest,
) -> IntakeResult:
    """목표 칸 채우기. 요청 칩 > 추론 > 재질문의 목표 단서 > 기본값(S2, 화면에 밝힌다)."""
    if request.goal is not None:
        update: dict[str, Any] = {"goal": request.goal, "goal_source": GoalSource.CHIP}
    elif inference is not None and inference.has_cues:
        secondary = inference.secondary_goal
        update = {
            "goal": inference.goal,
            "secondary_goal": secondary if secondary is not inference.goal else None,
            "goal_source": GoalSource.INFERRED,
            "goal_evidence_ids": cited_ids(hits, inference.evidence_ids),
            "goal_reason": inference.reason,
        }
    elif intake.goal_hint:
        hint = intake.goal_hint
        update = {
            "goal": hint[0],
            "secondary_goal": hint[1] if len(hint) > 1 else None,
            "goal_source": GoalSource.INFERRED,
            "goal_reason": "재질문 답이 가리킨 목표",
        }
    else:
        update = {
            "goal": DEFAULT_GOAL,
            "goal_source": GoalSource.DEFAULT,
            "goal_reason": "목표를 가를 단서가 없어 가볍게 긴장을 푸는 활동부터 시작한다",
        }
    return intake.model_copy(update=update)


def finish(intake: IntakeResult) -> IntakeResult:
    """끝 — 빈 칸을 기본값으로, 환경 조건과 실내·실외를 정한다. 메운 칸은 missing_slots에."""
    missing: list[str] = []
    minutes = intake.minutes
    if minutes is None:
        minutes = DEFAULT_MINUTES
        missing.append("minutes")
    if intake.location is None:
        environment = default_environment()
        missing.append("location")
    else:
        environment = environment_for(intake.location, intake.earphones)
    place = Place.OUTDOOR if intake.location is Location.OUTDOOR else Place.INDOOR
    goal = intake.goal or DEFAULT_GOAL
    source = intake.goal_source or GoalSource.DEFAULT
    return intake.model_copy(
        update={
            "goal": goal,
            "goal_source": source,
            "minutes": minutes,
            "environment": environment,
            "place": place,
            "missing_slots": missing,
        }
    )
