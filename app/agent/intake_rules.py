"""intake의 규칙 — 무엇을 묻고, 답을 어떻게 합치고, 언제 끝내는지. LLM · LangGraph 없이 테스트한다.

노드(app/agent/nodes/intake.py)는 이 모듈의 함수만 부른다.

    ① 상황 읽기(LLM) → compose()로 State · Context 채움 → state_question()으로 시점 질문 여부
       시점 질문에 '잘 모르겠어요' → followup_after_unknown()으로 고정 재질문
    ③ 목표 추론(LLM, 단서표) → apply_goal()   State 두 질문이 모두 모름이면 추론 없이 S2 DEFAULT
    ④ context_groups()로 추천을 바꾸는 환경 칸만 → context_question()
    답 → safety_hit()(자유 입력) · apply_answer() · merge_values()     끝 → finish()

우선순위: 요청 칩 > 질문 답(칩) > 문장에서 읽은 값 > 기본값. 질문은 합계 MAX_TURNS회, State는
MAX_STATE_TURNS회. 무엇을 물을지는 "그 칸이 1순위 활동을 바꾸는가"로 정한다.
"""

import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from app import protocols
from app.agent.state import IntakeResult, default_environment
from app.schemas.care import (
    CareGoal,
    CareRequest,
    Place,
    Setting,
    environment_for,
    place_for,
)
from app.schemas.evidence import InterventionOrder
from app.schemas.intake import (
    DEFAULT_GOAL,
    MAX_OPTIONS,
    MAX_STATE_TURNS,
    MAX_TURNS,
    MIN_OPTIONS,
    MINUTES_CHOICES,
    UNKNOWN_LABEL,
    GoalSource,
    IntakeAnswer,
    IntakeEndReason,
    IntakeQuestion,
    IntakeTurn,
    QuestionGroup,
    QuestionOption,
    QuestionSlot,
    StateQuestionDraft,
    parse_set,
)
from app.schemas.retrieval import GOAL_OUTCOMES

DEFAULT_MINUTES = 10
STATE_SLOTS = (QuestionSlot.STATE, QuestionSlot.STATE_FOLLOWUP)

# ── LLM 구조화 출력. 필드 설명이 LLM에 보인다 ──────────────────────────────────────────────


class StateReading(BaseModel):
    """① 상황 읽기 — 문장(과 앞선 질문·답)에서 State와 여건을 읽고, 검색어와 질문 초안을 낸다."""

    order_condition: InterventionOrder | None = Field(
        description="PRE_STRESS 앞으로 있을 구체적 사건(발표·면접·시험 직전) · "
        "POST_STRESS 방금·오늘 겪은 사건 · POST_FATIGUE 오래 이어진 정신적 작업 뒤 · "
        "NO_INDUCTION 계기 없이 일상적. 말에 없으면 null — 추측하지 않는다"
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
    setting: Setting | None = Field(
        description="말에 나온 장소만 — HOME 집·혼자 있는 방 · OFFICE_CAFE 사무실·카페 · "
        "TRANSIT 지하철·버스 · OUTDOOR 밖·공원. 없으면 null"
    )
    earphones: bool | None = Field(
        description="이어폰이 있다/없다고 말했으면 true/false, 아니면 null"
    )
    minutes: int | None = Field(description="쓸 수 있는 시간(분)을 말했으면 그 숫자, 아니면 null")
    search_query: str = Field(
        description="개념 정의를 찾을 영어 검색어 4~8단어 — 사용자 상태를 심리학 용어로. "
        "예: repetitive negative thinking after presentation · state anxiety tense nervous"
    )
    summary: str = Field(description="사용자 상황 한 줄(한국어, 40자 이내, 진단 용어 금지)")
    question: StateQuestionDraft | None = Field(
        description="order_condition을 정하지 못했을 때만 시점 질문 초안. 정했으면 null"
    )


class GoalInference(BaseModel):
    """③ 목표 추론 — 단서표와 상황 단서로 이번 세션의 결과 지표를 정한다."""

    has_cues: bool = Field(description="문장이나 답에 목표를 가를 단서가 하나라도 있으면 true")
    goal: CareGoal = Field(description="S1 반추 · S2 긴장·스트레스 · S3 주의·피로 중 주 목표")
    secondary_goal: CareGoal | None = Field(
        description="함께 보인 다른 목표(S1·S2 혼합 등). goal과 같으면 안 된다. 없으면 null"
    )
    reason: str = Field(description="판단 이유 한 문장(한국어) — 어떤 표현이 어느 단서에 맞았나")


# ── 자유 입력 안전 검사 ──────────────────────────────────────────────────────────────────

# 자해·자살 의도가 드러나는 표현만. "피곤해 죽겠어"처럼 형용사 뒤 과장 표현은 걸지 않는다
SAFETY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"자살",
        r"자해",
        r"죽고\s*싶",
        r"죽어\s*버리고\s*싶",
        r"죽을\s*(거|생각)",
        r"목숨을\s*끊",
        r"살고\s*싶지\s*않",
        r"뛰어\s*내리고\s*싶",
        r"손목을?\s*긋",
        r"사라지고\s*싶",
    )
)


def safety_hit(text: str) -> bool:
    """자유 입력 답이 안전 검사에 걸리는가. 걸리면 세션을 끝낸다(end_reason = SAFETY)."""
    compact = re.sub(r"\s+", " ", text)
    return any(p.search(compact) for p in SAFETY_PATTERNS)


# ── ① 합치기 ──────────────────────────────────────────────────────────────────────────────


def chip_answers(turns: Sequence[IntakeTurn]) -> dict[str, Any]:
    """칩 답을 순서대로 합친 값(enum으로). 뒤의 답이 앞의 답을 덮는다. 글 답은 빼고 본다."""
    out: dict[str, Any] = {}
    for turn in turns:
        if turn.answer:
            out |= parse_set(turn.slot, turn.answer)
    return out


def request_chips(request: CareRequest) -> dict[str, Any]:
    """요청의 환경 칩. 옛 place 칩은 setting이 없을 때만 OUTDOOR를 옮긴다."""
    out: dict[str, Any] = {}
    if request.setting is not None:
        out["setting"] = request.setting
    elif request.place is Place.OUTDOOR:
        out["setting"] = Setting.OUTDOOR
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
        "setting": reading.setting,
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
            "setting": values.get("setting"),
            "earphones": values.get("earphones"),
            "minutes": values.get("minutes"),
        }
    )


def merge_values(intake: IntakeResult, values: dict[str, Any]) -> IntakeResult:
    """검증된 칩 답(parse_set 결과)을 intake에 덮는다."""
    return intake.model_copy(update=values)


# ── State 질문 ────────────────────────────────────────────────────────────────────────────

STATE_TEXT = "방금 있었던 일 때문인가요, 곧 있을 일 때문인가요?"
STATE_FIXED: tuple[tuple[str, dict[str, Any]], ...] = (
    ("방금 있었던 일 때문이에요", {"order_condition": "POST_STRESS"}),
    ("곧 있을 일이 걱정돼요", {"order_condition": "PRE_STRESS"}),
    ("오래 집중해서 지쳤어요", {"order_condition": "POST_FATIGUE"}),
    ("딱히 계기는 없어요", {"order_condition": "NO_INDUCTION"}),
)
FOLLOWUP_TEXT = "요즘 오래 집중해야 하는 일이 많았나요, 신경 쓰이는 일이 있었나요?"
FOLLOWUP_FIXED: tuple[tuple[str, dict[str, Any]], ...] = (
    ("오래 집중하는 일이 많았어요", {"order_condition": "POST_FATIGUE", "goal_hint": ["S3"]}),
    ("신경 쓰이는 일이 있었어요", {"order_condition": "POST_STRESS", "goal_hint": ["S2"]}),
    ("둘 다예요", {"order_condition": "POST_STRESS", "goal_hint": ["S2", "S3"]}),
)
LLM_MAX_OPTIONS = MAX_OPTIONS - 1  # 잘 모르겠어요 자리를 남긴다


def _state_turns(turns: Sequence[IntakeTurn]) -> list[IntakeTurn]:
    return [t for t in turns if t.slot in STATE_SLOTS]


def state_slot(reading: StateReading, turns: Sequence[IntakeTurn]) -> QuestionSlot | None:
    """이번 읽기 뒤에 State 질문을 해도 되는가, 한다면 어느 slot인가.

    - 시점 질문: 아직 State 질문을 안 했고 상태를 못 정했을 때.
    - 재질문(LLM 초안): 시점 질문에 글로 답했는데 아직도 스트레스와 피로가 갈릴 때.
      ('잘 모르겠어요'로 답했을 때의 재질문은 읽기 없이 followup_after_unknown이 만든다)
    """
    if reading.question is None or len(turns) >= MAX_TURNS:
        return None
    asked = _state_turns(turns)
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


def _with_unknown(options: list[QuestionOption]) -> list[QuestionOption]:
    return [*options, QuestionOption(label=UNKNOWN_LABEL, set={})]


def fixed_question(slot: QuestionSlot, turn: int) -> IntakeQuestion:
    """고정 질문 — LLM 보기가 검증 뒤 2개 미만일 때와 '잘 모르겠어요' 뒤 재질문."""
    if slot is QuestionSlot.STATE:
        text, fixed = STATE_TEXT, STATE_FIXED
    else:
        text, fixed = FOLLOWUP_TEXT, FOLLOWUP_FIXED
    options = [QuestionOption(label=label, set=values) for label, values in fixed]
    return IntakeQuestion(
        question_id=f"q{turn}",
        slot=slot,
        turn=turn,
        text=text,
        groups=[QuestionGroup(key=slot.value, options=_with_unknown(options))],
        allow_free_text=True,
    )


def _draft_options(draft: StateQuestionDraft, slot: QuestionSlot) -> list[QuestionOption]:
    options = []
    for o in draft.options[:LLM_MAX_OPTIONS]:
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
    """LLM 초안을 검증해 질문으로. 유효한 보기가 2개 미만이거나 문장이 비면 고정 질문으로 바꾼다.

    보기는 LLM 보기(2~4개) + 잘 모르겠어요 = 3~5개다.
    """
    slot = state_slot(reading, turns)
    if slot is None:
        return None
    turn = len(turns) + 1
    options = _draft_options(reading.question, slot)
    text = reading.question.text.strip()
    if len(options) < MIN_OPTIONS or not text:
        return fixed_question(slot, turn)
    return IntakeQuestion(
        question_id=f"q{turn}",
        slot=slot,
        turn=turn,
        text=text,
        groups=[QuestionGroup(key=slot.value, options=_with_unknown(options))],
        allow_free_text=True,
    )


def followup_after_unknown(turns: Sequence[IntakeTurn]) -> IntakeQuestion | None:
    """시점 질문에 '잘 모르겠어요'로 답했으면 원인을 묻는 고정 재질문(읽기 없이). 예산 안에서만."""
    asked = _state_turns(turns)
    if len(turns) >= MAX_TURNS or len(asked) != 1:
        return None
    last = asked[0]
    if last.slot is QuestionSlot.STATE and last.answer == {}:
        return fixed_question(QuestionSlot.STATE_FOLLOWUP, len(turns) + 1)
    return None


def both_state_unknown(turns: Sequence[IntakeTurn]) -> bool:
    """State 두 질문(시점 + 재질문)이 모두 '잘 모르겠어요'인가 → 추론 없이 S2 DEFAULT."""
    asked = _state_turns(turns)
    return len(asked) == MAX_STATE_TURNS and all(t.answer == {} for t in asked)


# ── ④ 환경 질문 ─────────────────────────────────────────────────────────────────────────────

SETTING_OPTIONS: tuple[tuple[str, Setting], ...] = (
    ("집이나 혼자 있는 방", Setting.HOME),
    ("사무실·카페", Setting.OFFICE_CAFE),
    ("지하철·버스", Setting.TRANSIT),
    ("밖에 나갈 수 있어요", Setting.OUTDOOR),
)
EARPHONE_OPTIONS: tuple[tuple[str, bool], ...] = (("있어요", True), ("없어요", False))


def top_activity(intake: IntakeResult, **override: Any) -> str | None:
    """이 조건일 때 후보 1순위. 플래너와 같은 후보 조회(protocols.plan_candidates — 환경 필터 ·
    완화 순서)를 봐야 '이 칸이 추천을 바꾸는가'가 실제 계획과 맞는다."""
    if intake.goal is None:
        return None
    setting = override.get("setting", intake.setting)
    earphones = override.get("earphones", intake.earphones)
    found = protocols.plan_candidates(
        intake.goal,
        order=intake.order_condition,
        minutes=override.get("minutes", intake.minutes),
        environment=environment_for(setting, earphones),
    ).protocols
    return found[0].protocol_id if found else None


def context_groups(intake: IntakeResult) -> list[str]:
    """물어볼 환경 칸 — 비어 있고, 값에 따라 1순위 활동이 달라지는 것만.

    setting · earphones · minutes 순서다.
    """
    groups: list[str] = []
    if intake.setting is None:
        earphones = bool(intake.earphones)
        tops = {
            top_activity(intake, setting=setting, earphones=earphones)
            for _, setting in SETTING_OPTIONS
        }
        if len(tops) > 1:
            groups.append("setting")
    if intake.earphones is None:
        places = [intake.setting] if intake.setting else [s for _, s in SETTING_OPTIONS]
        if any(
            top_activity(intake, setting=s, earphones=True)
            != top_activity(intake, setting=s, earphones=False)
            for s in places
        ):
            groups.append("earphones")
    if intake.minutes is None:
        if len({top_activity(intake, minutes=m) for m in MINUTES_CHOICES}) > 1:
            groups.append("minutes")
    return groups


def context_question(groups: Sequence[str], turn: int) -> IntakeQuestion | None:
    """환경 칩 질문. 보기는 고정이고 '잘 모르겠어요'는 붙이지 않는다(고르지 않으면 키가 빠진다)."""
    if not groups:
        return None
    built: list[QuestionGroup] = []
    asks: list[str] = []
    if "setting" in groups:
        options = [QuestionOption(label=lb, set={"setting": s.value}) for lb, s in SETTING_OPTIONS]
        built.append(QuestionGroup(key="setting", options=options))
        asks.append("지금 어디에 계세요?")
    if "earphones" in groups:
        options = [QuestionOption(label=lb, set={"earphones": v}) for lb, v in EARPHONE_OPTIONS]
        built.append(QuestionGroup(key="earphones", options=options))
        asks.append("이어폰이 있으세요?")
    if "minutes" in groups:
        options = [
            QuestionOption(label="20분 이상" if m == 20 else f"{m}분", set={"minutes": m})
            for m in MINUTES_CHOICES
        ]
        built.append(QuestionGroup(key="minutes", options=options))
        asks.append("쓸 수 있는 시간은 얼마나 되세요?")
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
    """답 → (intake에 덮을 값, 대화 기록). 칩 답은 parse_set으로 검증한다.

    자유 입력의 안전 검사는 노드가 따로 한다(safety_hit) — 여기서는 기록만 만든다.
    """
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


def apply_goal(
    intake: IntakeResult,
    inference: GoalInference | None,
    request: CareRequest,
    turns: Sequence[IntakeTurn] = (),
) -> IntakeResult:
    """목표 칸 채우기. 요청 칩 > State 두 질문 모두 모름(S2 DEFAULT) > 추론 > 재질문 목표 단서 >
    기본값(S2, 화면에 밝힌다). 개념 정의 JSON(②)이 오기 전에는 goal_evidence가 빈다."""
    if request.goal is not None:
        update: dict[str, Any] = {"goal": request.goal, "goal_source": GoalSource.CHIP}
    elif both_state_unknown(turns):
        update = {
            "goal": DEFAULT_GOAL,
            "goal_source": GoalSource.DEFAULT,
            "goal_reason": "상황 질문 두 개에 모두 '잘 모르겠어요'라고 답했다",
        }
    elif inference is not None and inference.has_cues:
        secondary = inference.secondary_goal
        update = {
            "goal": inference.goal,
            "secondary_goal": secondary if secondary is not inference.goal else None,
            "goal_source": GoalSource.INFERRED,
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
    update["target_outcomes"] = list(GOAL_OUTCOMES[update["goal"]].primary)
    return intake.model_copy(update=update)


def finish(intake: IntakeResult, turns: Sequence[IntakeTurn] = ()) -> IntakeResult:
    """끝 — 빈 칸을 기본값으로, 환경 조건과 Place를 정한다. 메운 칸은 missing_slots에."""
    missing: list[str] = []
    minutes = intake.minutes
    if minutes is None:
        minutes = DEFAULT_MINUTES
        missing.append("minutes")
    if intake.setting is None:
        environment = default_environment()
        missing.append("setting")
    else:
        environment = environment_for(intake.setting, intake.earphones)
    goal = intake.goal or DEFAULT_GOAL
    ended = IntakeEndReason.MAX_TURNS if len(turns) >= MAX_TURNS else IntakeEndReason.SUFFICIENT
    return intake.model_copy(
        update={
            "goal": goal,
            "goal_source": intake.goal_source or GoalSource.DEFAULT,
            "target_outcomes": intake.target_outcomes or list(GOAL_OUTCOMES[goal].primary),
            "minutes": minutes,
            "environment": environment,
            "place": place_for(intake.setting) or Place.INDOOR,
            "missing_slots": missing,
            "end_reason": ended,
        }
    )


def end_for_safety(intake: IntakeResult) -> IntakeResult:
    return intake.model_copy(update={"end_reason": IntakeEndReason.SAFETY})
