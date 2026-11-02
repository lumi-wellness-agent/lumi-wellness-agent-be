"""intake가 코드로 만드는 문장 — 고정 질문과 보기 · 입구 되묻기 · 환경 칩 · 목표 판단 이유.
세션 언어마다 하나.

한국어가 원본이다(실측으로 다듬은 문장 — 2026-10-29 intake 평가 · FE 예시 문장 실측).
영어는 같은 뜻을 옮긴 것이고 아직 실제 사용자로 재지 않았다. 보기의 set(고르면 채워질 값)은
언어와 상관없이 같아야 한다 — 테스트가 지킨다(tests/intake/test_intake_texts.py). LLM이 쓰는
문장(되묻기 초안 · 질문 초안 · 추론한 판단 이유)은 여기 없다.

남의 위험 안내는 영어 화면에도 국내 번호(109)다 — 2026-11-02 결정(FE SAFETY 화면과 같다).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.schemas.care import Lang

Fixed = tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class IntakeTexts:
    state: str  # 시점 질문
    state_fixed: Fixed
    followup: str  # '잘 모르겠어요' 뒤 원인 재질문
    followup_fixed: Fixed
    mismatch: str  # 상황과 증상이 엇갈릴 때 지금 상태 재질문
    mismatch_fixed: Fixed
    restate: str  # 입구 되묻기 — LLM 문장이 비었을 때
    third_party: str  # 남의 위험 이야기(109)
    diagnosis: str  # 진단 · 처방만 물었을 때
    settings: dict[str, str]  # Setting 값 → 보기
    earphones: tuple[str, str]  # 있어요 · 없어요
    ask_setting: str
    ask_earphones: str
    ask_minutes: str
    minutes: Callable[[int], str]
    unknown: str  # State 질문의 마지막 보기(set = {})
    skip: str  # 되묻기 화면의 출구 → 무작위 DEFAULT 목표
    # 목표 판단 이유(IntakeResult.goal_reason — Reference 카드 goal.reason)
    reason_skipped: str
    reason_both_unknown: str
    reason_hint_chosen: str  # 재질문에서 고른 상태가 추론보다 앞설 때
    reason_without_diagnosis: str  # 추론한 이유에 진단명이 섞였을 때 바꿔 쓰는 문장
    reason_hint: str
    reason_no_cues: str


KO = IntakeTexts(
    state="방금 있었던 일 때문인가요, 곧 있을 일 때문인가요?",
    state_fixed=(
        ("방금 있었던 일 때문이에요", {"order_condition": "POST_STRESS"}),
        ("곧 있을 일이 걱정돼요", {"order_condition": "PRE_STRESS"}),
        ("오래 집중해서 지쳤어요", {"order_condition": "POST_FATIGUE"}),
        ("딱히 계기는 없어요", {"order_condition": "NO_INDUCTION"}),
    ),
    followup="요즘 오래 집중해야 하는 일이 많았나요, 신경 쓰이는 일이 있었나요?",
    followup_fixed=(
        ("오래 집중하는 일이 많았어요", {"order_condition": "POST_FATIGUE", "goal_hint": ["S3"]}),
        ("신경 쓰이는 일이 있었어요", {"order_condition": "POST_STRESS", "goal_hint": ["S2"]}),
        ("둘 다예요", {"order_condition": "POST_STRESS", "goal_hint": ["S2", "S3"]}),
    ),
    # 상황은 분명한데 증상이 다른 목표를 가리킬 때(symptom_mismatch) — 원인이 아니라 지금
    # 상태를 묻는다.
    # 예: "야근하고 나니 신경이 곤두서 있어요"(피로 후 + 긴장)
    #     "곧 시험인데 머리가 안 돌아가요"(시험 전 + 멍함)
    mismatch="지금은 어느 쪽에 더 가까워요?",
    mismatch_fixed=(
        ("긴장되거나 예민해요", {"goal_hint": ["S2"]}),
        ("머리가 멍하고 지쳐요", {"goal_hint": ["S3"]}),
    ),
    restate=(
        "오늘 하루는 어떠셨어요? 지금 상황을 한두 마디로 들려주세요. 예: 발표 끝나고 계속 생각나요"
    ),
    # 남의 위험을 이야기했을 때(third_party_risk) — LLM 문장 대신 고정 문장. 도울 곳(109)을
    # 먼저 알리고 사용자 자신의 이야기를 묻는다. 109는 자살예방상담전화(24시간)
    third_party=(
        "걱정되는 분이 있다면 자살예방상담전화 109에서 함께 도울 방법을 들을 수 있어요. "
        "지금은 당신의 이야기를 듣고 싶어요. 오늘 하루는 어떠셨어요?"
    ),
    # 진단 · 처방만 물었을 때(diagnosis_request) — 진단하지 않고 지금 상태를 묻는 고정 문장
    diagnosis=(
        "LUMI는 진단은 하지 못해요. 걱정되면 전문가와 이야기해 보세요. "
        "지금 상태를 한두 마디로 들려주시면 쉬어 갈 활동을 찾아 드릴게요."
    ),
    settings={
        "HOME": "집이나 혼자 있는 방",
        "OFFICE_CAFE": "사무실·카페",
        "TRANSIT": "지하철·버스",
        "OUTDOOR": "밖에 나갈 수 있어요",
    },
    earphones=("있어요", "없어요"),
    ask_setting="지금 어디에 계세요?",
    ask_earphones="이어폰이 있으세요?",
    ask_minutes="쓸 수 있는 시간은 얼마나 되세요?",
    minutes=lambda m: "20분 이상" if m == 20 else f"{m}분",
    unknown="잘 모르겠어요",
    skip="그냥 시작할게요",
    reason_skipped="상황을 말하지 않고 '그냥 시작할게요'를 골라 목표를 무작위로 정했다",
    reason_both_unknown="상황 질문 두 개에 모두 '잘 모르겠어요'라고 답해 목표를 무작위로 정했다",
    reason_hint_chosen="재질문에서 고른 지금 상태를 따랐다",
    reason_without_diagnosis="사용자가 말한 지금 상태 표현이 이 목표의 단서에 맞았다",
    reason_hint="재질문 답이 가리킨 목표",
    reason_no_cues="목표를 가를 단서가 없어 세 목표 중 하나를 무작위로 정했다",
)

EN = IntakeTexts(
    state="Is it because of something that just happened, or something coming up?",
    state_fixed=(
        ("Something that just happened", {"order_condition": "POST_STRESS"}),
        ("I'm worried about something coming up", {"order_condition": "PRE_STRESS"}),
        ("I'm worn out from focusing for a long time", {"order_condition": "POST_FATIGUE"}),
        ("Nothing in particular", {"order_condition": "NO_INDUCTION"}),
    ),
    followup=(
        "Lately, have you had a lot of work that needed long focus, "
        "or has something been on your mind?"
    ),
    followup_fixed=(
        ("A lot of long, focused work", {"order_condition": "POST_FATIGUE", "goal_hint": ["S3"]}),
        ("Something has been on my mind", {"order_condition": "POST_STRESS", "goal_hint": ["S2"]}),
        ("Both", {"order_condition": "POST_STRESS", "goal_hint": ["S2", "S3"]}),
    ),
    mismatch="Which is closer to how you feel right now?",
    mismatch_fixed=(
        ("Tense or on edge", {"goal_hint": ["S2"]}),
        ("Foggy and worn out", {"goal_hint": ["S3"]}),
    ),
    restate=(
        "How has your day been? Tell me how things are in a word or two. "
        "For example: I keep replaying how the presentation went"
    ),
    third_party=(
        "If you're worried about someone, Korea's suicide prevention line 109 can talk with "
        "you about how to help. Right now, I'd like to hear about you. How has your day been?"
    ),
    diagnosis=(
        "LUMI can't make a diagnosis. If you're worried, please talk to a professional. "
        "Tell me how you're doing in a word or two, and I'll find an activity to help you rest."
    ),
    settings={
        "HOME": "At home or in a room by myself",
        "OFFICE_CAFE": "Office · café",
        "TRANSIT": "Subway · bus",
        "OUTDOOR": "I can go outside",
    },
    earphones=("I have them", "I don't"),
    ask_setting="Where are you right now?",
    ask_earphones="Do you have earphones?",
    ask_minutes="How much time do you have?",
    minutes=lambda m: "20 min or more" if m == 20 else f"{m} min",
    unknown="I'm not sure",
    skip="Just start",
    reason_skipped=(
        "No situation was given ('Just start' was chosen), so the goal was picked at random"
    ),
    reason_both_unknown=(
        "Both situation questions were answered 'I'm not sure', so the goal was picked at random"
    ),
    reason_hint_chosen="Followed the current state chosen in the follow-up question",
    reason_without_diagnosis="How the user described their current state matched this goal's cues",
    reason_hint="The goal pointed to by the follow-up answer",
    reason_no_cues=(
        "There were no cues to tell the goals apart, so one of the three was picked at random"
    ),
)

TEXTS: dict[Lang, IntakeTexts] = {"ko": KO, "en": EN}
