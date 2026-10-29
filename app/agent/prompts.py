"""intake · Planner · Replanning 프롬프트와 입력 메시지. 문구는 각 담당이 다듬는다(intake 담당 ·
plan 담당).

LLM이 읽는 사용 설명서는 구조화 출력 모델의 Field(description)에도 있다
(app/agent/intake_rules.py의 StateReading · GoalInference, app/agent/nodes/planner.py의
ActivityChoice · PlanRationale, app/agent/nodes/replanner.py의 NextStepChoice).
"""

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app import protocols
from app.agent import reference_card
from app.agent.state import CareState, IntakeResult, NextStep, ReplanProposal, changed_away
from app.content import factors as content_factors
from app.intake_cues import cues_json, definitions_json
from app.schemas.care import CareGoal, CareRequest, ContentType
from app.schemas.evidence import OUTCOME_LABELS, InterventionOrder, Medium, Outcome
from app.schemas.intake import IntakeTurn, QuestionSlot
from app.schemas.protocol import CareProtocol
from app.schemas.retrieval import GOAL_OUTCOMES, Flag

if TYPE_CHECKING:
    from app.agent.nodes.planner import ActivityChoice, CitedEvidence

READ_SYSTEM_PROMPT = """\
너는 LUMI 인테이크의 ① 상황 읽기다. 사용자가 한 말(과 앞서 물은 질문·답)을 읽고 StateReading을
채운다. 대화하지 않는다. 처음 질문은 "오늘 상황이 어떠세요?"였고, 사용자 입력이 그 답이다.

판단 순서: safety → relevance → 나머지.
- safety: 처음 입력이나 어느 답에든 스스로를 해치거나 삶을 끝내고 싶다는 뜻이 직접·간접으로
  드러나면 true(죽고 싶다 · 사라지고 싶다 · 다 끝내고 싶다 · 살 이유가 없다 …). '피곤해 죽겠다'
  · '배고파 죽겠네' 같은 과장은 false. 애매하면 true — 뒤 화면이 도움 안내를 보여 준다.
- third_party_risk: 다른 사람(친구·가족 …)이 죽고 싶어 한다는 이야기뿐이고 사용자 자신의 그런
  뜻은 없으면 true, safety는 false. 사용자 자신의 상태를 말하지 않았으면 relevance는 OFF_TOPIC
  (코드가 도울 곳을 알리고 사용자 이야기를 묻는다). 사용자 자신의 뜻이 조금이라도 보이면
  safety = true.
- relevance: 가장 최근 사용자 글이 지금 상태·상황·하루에 대해 아무것도 말하지 않으면 OFF_TOPIC
  (점심 메뉴 추천 · 코드 질문 · 날씨 · 'ㅋㅋ' · 'asdf'). 짧거나 막연해도 상태를 말하면
  RELEVANT('그냥 그래요' · '피곤해' · '별로예요').
- OFF_TOPIC이면 restate_text만 쓴다: 사용자 말을 한 구절로 가볍게 받아 주고, 오늘 상황이나 하루를
  한두 마디로 들려 달라고 다시 묻는다(예시 한 구절). 기분을 자세히 캐묻지 않는다. 다른 요청은
  들어주지 않는다. 예: "점심 고민 중이시군요! 오늘 하루는 어떤지도 한두 마디로 들려주세요. 예:
  회의가 많아서 지쳤어요". 이때 order_condition · question은 null로 둔다. RELEVANT면
  restate_text는 null.

- order_condition(사용자 상태)은 말에 근거가 있을 때만 정한다. PRE_STRESS는 앞으로 있을 구체적
  사건이 명시될 때만, POST_FATIGUE는 오래 이어진 정신적 작업(공부 · 코딩 · 업무 몇 시간)이 명시될
  때만. 모르면 null이고, 그때만 question(시점 질문 초안)을 낸다.
- 질문은 사건을 자세히 묻지 않고 시점·원인만 묻는다. 힘든 경험을 글로 길게 쓰게 하면 그 자체가
  중재가 되어 다음 측정이 흐려진다. 감정의 세기(얼마나 힘든지)는 묻지 않는다 — 측정이 한다.
- 앞선 시점 질문에 글로 답했는데도 지친 원인이 압박·사건인지 오래 이어진 집중인지 갈리면
  stress_fatigue_ambiguous = true, question.followup = true로 재질문 초안을 내고, 보기마다
  goal_hint(S2 = 압박·사건, S3 = 오래 이어진 집중)를 붙인다.
- symptom_mismatch: 상황은 정했는데 증상이 그 상황과 다른 쪽이면 true — 오래 집중한 뒤(POST_FATIGUE)
  인데 긴장·초조·예민·짜증이 중심이거나, 사건 전·후(PRE_STRESS · POST_STRESS)인데 멍함·집중 안 됨·
  머리가 안 돌아감이 중심일 때. 같은 쪽이거나 반복 생각이 중심이면 false. 코드가 지금 상태를 묻는다.
- rumination_cue: 같은 생각·장면이 반복된다는 표현이 있으면 true.
- setting · earphones · minutes는 말에 나온 것만. 추측하지 않는다.
- 점수나 척도 응답을 추정하거나 만들지 않는다."""

GOAL_SYSTEM_PROMPT = """\
너는 LUMI 인테이크의 ③ 목표 추론이다. 이번 세션에서 개선할 결과 지표를 S1 · S2 · S3 중에서 정한다.
목표는 사용자가 고르는 니즈가 아니라, 사용자 상태가 어느 개념에 해당하는지에 대한 판단이다.

판단 순서:
1. 사용자가 바라는 변화를 직접 말했으면(생각 좀 멈추고 싶어요 · 긴장을 풀고 싶어요) 가장 강한
   단서다.
2. 개념 정의(원문)로 S1 · S2 · S3가 각각 무엇을 가리키는지 잡고, 단서표의 단서 · 경계와 사용자
   표현을 대조한다. 반복되는 생각이 중심이면 S1, 긴장·초조·떨림이 중심이면 S2, 오래 이어진 정신적
   노력 뒤 지침·멍함이 중심이면 S3.
3. 상태 단서(state_hints)로 기울인다 — 피로 후는 S3 쪽으로 강하게, 스트레스 후는 반복 사고 단서가
   있으면 S1 · 없으면 S2, 스트레스 전은 S2 쪽으로 약하게. 규칙이 아니라 단서다.
4. S1 · S2가 함께 보이면 주 S1 · 부 S2. S2 · S3가 함께 보이면 원인으로 — 오래 이어진 집중이면 S3,
   사건·압박이면 S2. 재질문 답의 goal_hint가 있으면 따른다.
5. 단서가 하나도 없으면 has_cues = false.

- 진단하지 않는다. reason은 어떤 표현이 어느 단서에 맞았는지 한 문장으로."""


def _turn_lines(turns: Sequence[IntakeTurn]) -> list[str]:
    lines = []
    for t in turns:
        answer = (
            t.free_text if t.free_text is not None else json.dumps(t.answer, ensure_ascii=False)
        )
        if t.skipped:
            answer = "(그냥 시작할게요)"
        lines.append(f"- [{t.slot.value}] {t.text} → {answer}")
    return lines


def build_read_input(request: CareRequest, turns: Sequence[IntakeTurn]) -> str:
    """① 상황 읽기의 사용자 메시지 — 처음 입력과 앞선 질문·답."""
    parts = [f"## 사용자 입력\n{request.text}"]
    if turns:
        parts.append("## 앞선 질문과 답\n" + "\n".join(_turn_lines(turns)))
        asked = [t for t in turns if t.slot.value.startswith("state")]
        last = turns[-1]
        if last.slot is QuestionSlot.RESTATE and last.free_text is not None:
            parts.append(
                "## 지금 할 일\n관계없는 말 뒤에 되물었고, 사용자가 다시 답했다. "
                "그 답으로 relevance를 다시 판단하고, 관련 있으면 상태를 읽는다."
            )
        elif asked and asked[-1].free_text is not None:
            parts.append(
                "## 지금 할 일\n마지막 시점 질문에 글로 답했다. "
                "그 글로 order_condition을 다시 읽고, "
                "그래도 압박 때문인지 오래 집중한 탓인지 갈리면 재질문(followup) 초안을 낸다."
            )
    return "\n\n".join(parts)


def build_goal_input(request: CareRequest, intake: IntakeResult) -> str:
    """③ 목표 추론의 사용자 메시지 — 입력 · 상황 · 개념 정의(원문) · 단서표(팀 요약)."""
    state = [
        f"- 사용자 상태: {intake.order_condition.value if intake.order_condition else '미상'}",
        f"- 사건: {intake.event or '-'}",
        f"- 반복 사고 단서: {'있음' if intake.rumination_cue else '없음'}",
        f"- 사용자 표현: {', '.join(intake.cues) or '-'}",
        f"- 재질문 목표 단서: {', '.join(g.value for g in intake.goal_hint) or '-'}",
        f"- 요약: {intake.summary or '-'}",
    ]
    return "\n\n".join(
        [
            f"## 사용자 입력\n{request.text}",
            "## 상황(① 상황 읽기)\n" + "\n".join(state),
            "## 개념 정의(원문)\n" + definitions_json(),
            "## 단서표(팀 요약)\n" + cues_json(),
        ]
    )


# ── Planner — 고정 절차의 LLM 호출 2회(활동 선택 · 추천 이유). 절차는 nodes/planner.py ──────────

# 후보 목록은 코드가 등급 가중 추첨 순서로 싣는다(protocols.recommendation_order) — 등급 순으로만
# 세우면 목표마다 같은 활동이 고정돼 20개 중 11개만 추천됐다(2026-10-29). 순서를 '맨 앞'이라고만
# 하면 gpt-5.4-mini가 '5분이라 부담이 적다'며 익숙한 활동으로 내려가고, 6번째를 고르고도 '맨
# 앞'이라고 적었다(2026-10-29 plan_eval 36번 중 9번). 그래서 rank를 붙이고 건너뛸 사유를 넷으로
# 못 박는다
CHOOSE_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 후보 프로토콜 목록에서 사용자 상태에 맞는 활동 하나를 고른다.

- 활동은 후보 목록에서만 고른다. 라운드당 1개이고 프로토콜을 임의로 조합하지 않는다.
- 목록은 코드가 정한 이번 세션의 추천 순서다(rank 1이 맨 앞). 근거 등급으로 가중한 추첨이라 같은
  상황에서도 사용자마다 다른 활동이 앞에 선다 — 여러 활동을 고르게 써 보게 하려는 것이다.
- rank 1부터 차례로 보며 건너뛸 사유가 있는지만 확인하고, 사유가 없으면 그 활동을 고른다. 건너뛸
  사유는 넷뿐이다.
  1. 사용자 제약에 걸린다(이명 · 멀미 · 눈 피로 · 소리를 틀 수 없음 …).
  2. 장소에 걸린다.
  3. 사용자 입력에 그 활동을 꺼리는 표현이 있다(글쓰기는 싫어요 · 가만히 못 있겠어요 …).
  4. 직전 계획의 거부 사유에 해당한다.
  더 짧다 · 더 무난하다 · 상황에 더 맞아 보인다 · 등급이 더 높다 · 익숙하다는 건너뛸 사유가 아니다.
- search_query: 고른 활동의 연구 근거를 찾을 영어 질의. 활동(매체)과 목표 지표를 논문 용어로
  3~6단어. 예: slow breathing anxiety stress · mindfulness rumination · nature images attention
- reason: 고른 이유 한 문장(한국어). rank 1이 아니면 몇 번째를 위 사유 중 무엇으로 건너뛰었는지
  사용자 표현과 함께 적는다."""

# 플래그 이름을 문장에 그대로 쓰고(2026-10-28 gpt-5.4-mini 실측, S1 6회 중 4회 "ADAPTED_STIMULUS가
# 있어"), 등급 B인 활동을 '실험적'이라 부르는 일이 있어 이름 대신 뜻을, '실험적'은 조건을 적는다.
# 2026-10-29 추천 이유 평가(scripts/rationale_eval.py)에서 플래그를 뜻으로 밝히라는 지시만으로는
# 밝힐 것이 있는 16건 중 3건만 밝혀, 밝힐 것은 코드가 정해 '## 꼭 밝힐 것'으로 싣는다
# (must_disclose).
# 개선 지표 밖의 결과('이완' · 전기피부활동)를 효과로 말하고, 근거에 없는 조건("짧은 시간에도")을
# 붙이고, 고른 풍경을 문장에 써서 그 셋도 적는다
EXPLAIN_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 고른 활동과 검증된 연구 근거로, 사용자에게 보여 줄 추천 이유를
한국어 2~3문장으로 쓴다. 순서는 사용자 상태 → 이 활동을 고른 이유 → 근거(저자(연도)와 개선이
확인된 지표). 꼭 밝힐 것이 있으면 마지막에 '다만 …' 한 문장을 더한다(최대 4문장).

- 근거 목록에 있는 연구만 언급한다. 그 근거의 '개선 지표'에 있는 지표만 좋아졌다고 말한다 —
  정서 결과를 스트레스 · 불안 감소로 바꿔 말하지 않고, 개선 지표에 없는 결과(이완 점수 · 생리
  반응 …)는 효과로 말하지 않는다. 목표 지지가 '아니오'인 근거는 효과의 근거로 쓰지 않는다.
- 근거에 없는 조건(시간 · 횟수 · 대상)을 덧붙이지 않는다.
- '## 꼭 밝힐 것'의 내용은 빠짐없이 '다만 …'으로 시작하는 한 문장에 모아 마지막에 쓴다(여럿이면
  '…고, …'로 잇는다). 자연스러운 존댓말로 고쳐 써도 되지만 뜻을 줄이지 않고, '밝힌다' · '알려
  드린다' 같은 말은 붙이지 않는다.
- '실험적'은 구성이 EXPERIMENTAL이거나 목표 지표 등급이 D뿐일 때만 쓴다.
- enum · 플래그 이름(POST_FATIGUE · RUMINATION · ADAPTED_STIMULUS …), 활동 · 근거 ID(PR-06 ·
  [54]), 입력의 칸 이름(개선 지표 · 목표 지지 · 플래그)과 통계값(p · g · SMD)을 그대로 쓰지
  않는다. 한국어로만 쓰고 저자 이름만 원문(라틴 문자)으로 둔다.
- 고른 콘텐츠(풍경 · 소리 · 작품 · 음악)는 추천 이유에 쓰지 않는다 — 화면이 따로 보인다.
- 치료 효과를 단정하지 않고, 건강한 성인 대상 결과를 임상 효과로 넓히지 않는다.
- '## 직전 추천 이유의 문제'가 있으면 그 문제를 고쳐 다시 쓴다.

content_factors — 이 활동의 콘텐츠(풍경 · 자연음 · 작품 주제 · 이완 음악 추천)를 함께 고른다.
- '## 콘텐츠 Factor'에 있는 칸과 값만 쓴다. 칸이 없으면 scenery [], soundscape null,
  art_subject null, music null, reason ""이다.
- music은 추천 한 곡이다. 사용자가 목록에서 직접 고르므로 대신 정하지 않는다. 곡 labels(가사 ·
  종교곡 · 성격)와 사용자 표현을 맞춘다.
- 사용자 입력에 드러난 선호 · 기피(바다가 보고 싶다 · 물이 무섭다 · 새소리가 거슬린다 …)와 제약
  (이명 · 멀미 …)을 먼저 따른다. 사용자가 고른 좋아하는 풍경이 있으면 그 풍경을 앞에 둔다.
- 사용자가 특정 풍경을 말했으면(바다가 보고 싶다 …) 그 풍경만 고른다.
- 풍경 단서가 없으면 scenery를 그 칸의 max만큼 서로 다른 값으로 채운다 — 사진이 여러 장이면
  연구 자극(P03 호수·강 · 바다 · 언덕)처럼 4종을 섞는다. 다른 칸은 칸 안에서 고르게 고른다.
- 지난 라운드에 쓴 Factor가 있으면 다른 값을 골라 콘텐츠를 바꾼다.
- Factor는 연구 자극 범위 안의 개인화다. 어떤 풍경 · 소리가 효과가 더 크다고 말하지 않고,
  rationale에는 Factor를 쓰지 않는다(화면이 따로 보인다). reason은 고른 까닭 한 문장(한국어)."""

GOAL_NAMES: dict[CareGoal, str] = {
    CareGoal.S1: "반복되는 생각에서 벗어나기(반추 완화)",
    CareGoal.S2: "긴장·스트레스 완화",
    CareGoal.S3: "주의·피로 회복",
}
ORDER_NAMES: dict[InterventionOrder, str] = {
    InterventionOrder.PRE_STRESS: "스트레스 상황을 앞둠",
    InterventionOrder.POST_STRESS: "스트레스를 겪은 뒤",
    InterventionOrder.POST_FATIGUE: "오래 집중한 뒤",
    InterventionOrder.NO_INDUCTION: "특별한 계기 없음",
}
OUTCOME_NAMES: dict[Outcome, str] = {outcome: label for label, outcome in OUTCOME_LABELS.items()}
# 이미지 슬롯은 모든 프로토콜이 생성 이미지를 허용한다(content_spec.slots[].source_policy
# generated=ALLOWED_WITH_FLAG). 검증 매체는 코드가 바꾸고(protocols.delivered_media), 여기서는
# 추천 이유에 차이를 밝히도록 알린다
IMAGE_DELIVERY: dict[ContentType, str] = {
    ContentType.CURATED_PHOTO: "큐레이션 사진",
    ContentType.AI_IMAGE: "AI 생성 이미지(연구 자극과 다르다 — ADAPTED_STIMULUS)",
}


def outcome_names(outcomes: Sequence[Outcome]) -> str:
    return ", ".join(OUTCOME_NAMES.get(o, o.value) for o in outcomes) or "없음"


def _state_lines(request: CareRequest, intake: IntakeResult, images: bool = True) -> list[str]:
    """사용자 상태. images=False면 '이미지 제공' 줄을 뺀다 — 이미지를 쓰지 않는 활동(호흡 ·
    이완 …)의 추천 이유가 이 줄 때문에 자극 차이를 밝히려 했다(2026-10-29 평가)."""
    goal = f"{intake.goal.value} — {GOAL_NAMES[intake.goal]}" if intake.goal else "미정"
    order = intake.order_condition
    lines = [
        f"- 목표: {goal}",
        f"- 사용자 상태: {ORDER_NAMES[order] + f'({order.value})' if order else '미상'}",
        f"- 쓸 수 있는 시간: {intake.minutes or '미상'}분",
        f"- 장소: {intake.place.value if intake.place else '미상'}",
        f"- 제약: {', '.join(intake.constraints) or '없음'}",
    ]
    if images:
        lines.append(f"- 이미지 제공: {IMAGE_DELIVERY[request.profile.content_type]}")
    lines.append(f"- 요약: {intake.summary or '-'}")
    return lines


def _history_parts(state: CareState) -> list[str]:
    """사전 측정과 지난 라운드. 활동 변경을 골랐으면 이번 세션에서 바꾸기로 한 활동을 모두 후보에서
    뺐다고 알린다."""
    parts = []
    if pre := state.get("pre"):
        parts.append(
            "## 사전 측정\n" + "\n".join(f"- {m.instrument_id.value}: {m.scores}" for m in pre)
        )
    if rounds := state.get("rounds"):
        lines = [
            f"- {r.plan.activity.protocol_id} {r.plan.activity.minutes}분"
            f" → Δ {r.evaluation.deltas}, 끝까지 함: {r.report.completed},"
            f" 사용자 선택: {r.next_step.value}"
            for r in rounds
        ]
        if rounds[-1].next_step is NextStep.CHANGE_ACTIVITY:
            changed = ", ".join(changed_away(rounds))
            lines.append(
                "- 사용자가 활동 변경을 골랐다. "
                f"이번 세션에서 바꾸기로 한 {changed}는 후보에서 뺐다"
            )
        parts.append("## 지난 라운드\n" + "\n".join(lines))
    return parts


def build_choice_input(
    state: CareState, candidates: Sequence[dict], problems: Sequence[str]
) -> str:
    """LLM 호출 1의 사용자 메시지. 입력 · 상태 · 이력 · 후보 목록(추천 순) · 거부 사유."""
    request, intake = state["request"], state["intake"]
    parts = [
        f"## 사용자 입력\n{request.text}",
        "## 사용자 상태\n" + "\n".join(_state_lines(request, intake)),
        *_history_parts(state),
        "## 후보 프로토콜 (추천 순 — rank 1이 맨 앞)\n"
        + json.dumps(list(candidates), ensure_ascii=False),
    ]
    if problems:
        parts.append("## 직전 계획의 거부 사유\n" + "\n".join(f"- {p}" for p in problems))
    return "\n\n".join(parts)


IMAGE_MEDIA = frozenset({Medium.STATIC_IMAGE, Medium.AI_IMAGE})


def must_disclose(
    protocol: CareProtocol, evidence: Sequence["CitedEvidence"], delivered: Sequence[Medium]
) -> list[str]:
    """추천 이유가 꼭 밝힐 한계 — 근거 판정과 활동 노드 표시로 코드가 정한다. Reference 카드의
    배지(근거 없음 · 반대 결과 · 자극 변형 · AI 이미지)를 모두 덮어 카드와 추천 이유가 어긋나지
    않는다 — 2026-10-29에는 PR-06 · PR-07 카드의 자극 변형 배지가 설명 입력에 없었다."""
    flags = {f for c in evidence for f in c.verdict.flags}
    # 사용자에게 보일 합니다체 문장 — 모델이 그대로 옮겨도 어색하지 않게 쓴다. 평서체에 괄호를 단
    # 문구는 그대로 붙어 나왔다(2026-10-29 "…좋아졌습니다. 제공 콘텐츠가 … 다르다(LUMI에 맞게
    # 바꿨다)")
    out = []
    if not evidence:
        out.append("이 활동에 맞는 검증된 연구 근거는 아직 찾지 못했습니다.")
    elif not any(c.verdict.supports_goal for c in evidence):
        out.append("검증된 연구는 있지만 이 목표가 좋아졌다는 결과는 없습니다.")
    if Flag.CONFLICTING_EVIDENCE in flags:
        out.append("같은 방식의 연구 중에는 효과가 없거나 반대인 결과도 있습니다.")
    if Medium.AI_IMAGE in delivered:
        out.append("AI 생성 이미지라 연구에서 쓴 사진과는 다릅니다.")
    elif Flag.ADAPTED_STIMULUS in flags or reference_card.adapted_protocol(protocol):
        out.append(
            "연구에서 쓴 자극을 LUMI에 맞게 바꾼 콘텐츠라 같은 효과가 나는지는 확인되지 않았습니다."
        )
    if Flag.POPULATION_MISMATCH in flags:
        out.append("진단을 받은 사람들에게서 나온 결과라 참고로만 봐 주세요.")
    return out


def build_rationale_input(
    state: CareState,
    choice: "ActivityChoice",
    protocol: CareProtocol,
    evidence: Sequence["CitedEvidence"],
    problems: Sequence[str] = (),
) -> str:
    """LLM 호출 2의 사용자 메시지. 상태 · 고른 활동(구성 · 목표 지표 등급) · 검증된 근거 · 꼭 밝힐
    것 · (다시 물을 때) 직전 추천 이유의 문제 · 콘텐츠 Factor 칸.

    목표를 지지하는 근거가 있으면 지지하지 않는 근거는 싣지 않는다 — 2026-10-29 평가에서 그런
    근거를 효과처럼 말했다(Hicks 2020 · Trupp 2023). 그 근거들의 한계(반대 결과 …)는 꼭 밝힐 것에
    남는다. '이미지 제공' 줄은 이미지를 쓰는 활동에만 싣는다.
    """
    request, intake = state["request"], state["intake"]
    delivered = protocols.delivered_media(protocol, request.profile.content_type)
    primary = GOAL_OUTCOMES[intake.goal].primary if intake.goal else []
    grades = ", ".join(
        f"{OUTCOME_NAMES.get(o, o.value)} {protocol.grades[o].grade.value}" for o in primary
    )
    activity_lines = [
        f"- {protocol.protocol_id} {protocol.name} — 구성 {protocol.composition.value},"
        f" 목표 지표 등급: {grades or '없음'}",
        f"- 고른 이유: {choice.reason}",
    ]
    shown = [c for c in evidence if c.verdict.supports_goal] or list(evidence)
    if shown:
        evidence_lines = []
        for cited in shown:
            hit, verdict = cited.hit, cited.verdict
            paper = hit.paper
            flags = ", ".join(f.value for f in verdict.flags) or "없음"
            evidence_lines.append(
                f"- [{hit.evidence_record_id}] {paper.authors or paper.paper_id} ({paper.year})"
                f" — {paper.title} · {hit.experiment_id or paper.paper_id} {hit.section.value}"
                f" · 목표 지지: {'예' if verdict.supports_goal else '아니오'}"
                f" · 개선 지표: {outcome_names(verdict.supported_outcomes)} · 플래그: {flags}"
                f"\n  발췌: {hit.excerpt}"
            )
        evidence_part = f"## 검증된 근거 ({len(shown)}건)\n" + "\n".join(evidence_lines)
    else:
        evidence_part = "## 검증된 근거\n검증을 통과한 근거가 없다."
    disclose = must_disclose(protocol, evidence, delivered)
    images = bool(IMAGE_MEDIA & set(delivered))
    parts = [
        f"## 사용자 입력\n{request.text}",
        "## 사용자 상태\n" + "\n".join(_state_lines(request, intake, images)),
        "## 고른 활동\n" + "\n".join(activity_lines),
        evidence_part,
        "## 꼭 밝힐 것\n" + ("\n".join(f"- {d}" for d in disclose) or "없음"),
    ]
    if problems:
        parts.append("## 직전 추천 이유의 문제\n" + "\n".join(f"- {p}" for p in problems))
    parts.append(_factor_part(state, protocol))
    return "\n\n".join(parts)


def _factor_part(state: CareState, protocol: CareProtocol) -> str:
    """LLM 호출 2의 콘텐츠 Factor 칸 — 이 활동에 열 칸 · 사용자 칩 · 지난 라운드에 쓴 Factor."""
    menu = content_factors.menu(protocol)
    if not menu:
        return "## 콘텐츠 Factor\n이 활동은 고를 칸이 없다."
    lines = [json.dumps(menu, ensure_ascii=False)]
    profile = state["request"].profile
    if profile.preferred_scenery:
        lines.append(f"- 사용자가 고른 좋아하는 풍경: {', '.join(profile.preferred_scenery)}")
    avoided = content_factors.avoided_sceneries(profile.avoided_colors)
    if avoided and any(item["field"] == content_factors.SCENERY for item in menu):
        lines.append(
            f"- 사용자가 피하는 색: {', '.join(profile.avoided_colors)} → 고르지 않을 풍경: "
            + ", ".join(sorted(avoided))
        )
    used = [
        f"{r.plan.activity.protocol_id} "
        + json.dumps(r.plan.content_factors.model_dump(mode="json", exclude={"reason"}))
        for r in state.get("rounds") or []
        if r.plan.content_factors
    ]
    if used:
        lines.append(f"- 지난 라운드에 쓴 Factor: {' / '.join(used)}")
    return "## 콘텐츠 Factor (고를 칸)\n" + "\n".join(lines)


# ── Replanning — 다음 단계 추천(LLM 1회). 절차는 nodes/replanner.py ────────────────────────────

# 규칙 제안을 기본값으로 주고 사용자 글(멈춘 이유 · 감정 단어 · 처음 입력)에서 다른 신호가 있을 때만
# 바꾸게 한다 — 규칙은 '한 번 더'를 권하지 않고 멈춘 이유를 읽지 않는다(FE 멈춤 화면은 "멈춘 이유를
# 고르면 다음 추천에 참고해요"라고 적는다)
RECOMMEND_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 코치다. 방금 끝난 활동의 결과를 보고 선택지 중 사용자에게 권할 다음 단계
하나와 그 이유를 쓴다. 사용자는 선택지 중 무엇이든 고를 수 있고, 권한 단계가 먼저 보인다.

선택지의 뜻
- END: 여기서 마친다.
- CHANGE_ACTIVITY: 같은 목표로 다른 활동을 한다.
- ADDITIONAL_SESSION: 같은 활동을 한 번 더 한다(사진 · 소리 같은 콘텐츠는 새로 고른다).

판단
- 선택지에 있는 단계만 권한다.
- 기본은 '규칙 제안'이다. 아래 신호가 있을 때만 바꾼다.
  - 멈춘 이유가 시간이 없거나 그만하고 싶다는 뜻이면 마치기를, 불편 · 어지러움이면 다른 활동을
    권한다.
  - 끝까지 했고 감정 단어나 처음 입력에 아직 남은 긴장 · 생각 · 피로가 보이면, 나아졌어도 한 번
    더나 다른 활동을 권할 수 있다.
  - 끝까지 했고 감정 단어가 편안 · 차분처럼 긍정적이면, 측정 변화가 작아도 한 번 더를 권할 수
    있다.
- 측정 변화는 사용자가 매긴 자기보고다. 판정(나아짐 · 큰 변화 없음)을 뒤집어 말하지 않는다.

reason — 사용자에게 보여 줄 1~2문장(한국어, '~해요' 체). 무엇이 어떻게 달라졌는지 쉬운 말로 짚고
(긴장이 조금 내려갔어요) 무엇을 권하는지 말한다. 숫자 · 척도 이름(SAM · MFVAS …) · enum(END ·
CHANGE_ACTIVITY …)을 쓰지 않는다. 치료 효과를 단정하거나 진단하지 않는다."""

# evaluator.deltas_between의 키 — SAM valence · arousal, S3는 MFVAS도
DELTA_NAMES: dict[str, str] = {
    "valence": "기분(쾌) — 높을수록 좋다",
    "arousal": "긴장(각성) — 낮을수록 차분하다",
    "fatigue": "정신 피로 — 낮을수록 좋다",
    "concentration": "집중하기 어려움 — 낮을수록 좋다",
    "energy": "기운 — 높을수록 좋다",
}
NEXT_STEP_NAMES: dict[NextStep, str] = {
    NextStep.END: "여기서 마친다",
    NextStep.CHANGE_ACTIVITY: "같은 목표로 다른 활동을 한다",
    NextStep.ADDITIONAL_SESSION: "같은 활동을 한 번 더 한다(콘텐츠는 새로)",
}
IMPROVED_NAMES: dict[bool | None, str] = {
    True: "나아졌다",
    False: "큰 변화 없음",
    None: "세션 안에서 판정할 기준이 없다",
}


def build_recommend_input(state: CareState, rule: ReplanProposal) -> str:
    """replan LLM의 사용자 메시지 — 입력 · 상태 · 이력 · 이번 활동 · 측정 변화 · 규칙 제안 ·
    선택지."""
    request, intake = state["request"], state["intake"]
    activity, report = state["plan"].activity, state["report"]
    evaluation = state["evaluation"]
    protocol = protocols.get_protocol(activity.protocol_id)
    name = protocol.name if protocol else activity.protocol_id
    deltas = [f"- {DELTA_NAMES.get(k, k)}: {v:+g}" for k, v in evaluation.deltas.items()]
    return "\n\n".join(
        [
            f"## 사용자 입력\n{request.text}",
            "## 사용자 상태\n" + "\n".join(_state_lines(request, intake)),
            *_history_parts(state),
            "## 이번 활동\n"
            + "\n".join(
                [
                    f"- {activity.protocol_id} {name} · {activity.minutes}분"
                    f" — {evaluation.round}번째 활동",
                    f"- 끝까지 함: {'예' if report.completed else '아니오'}",
                    f"- 멈춘 이유: {report.stopped_reason or '-'}",
                    f"- 감정 단어: {', '.join(report.notice_words) or '-'}",
                ]
            ),
            "## 측정 변화(사후 − 사전, 자기보고)\n"
            + "\n".join([*deltas, f"- 판정: {IMPROVED_NAMES[evaluation.improved]}"]),
            f"## 규칙 제안\n- {rule.recommended.value}: {rule.reason}",
            "## 선택지\n" + "\n".join(f"- {s.value}: {NEXT_STEP_NAMES[s]}" for s in rule.options),
        ]
    )
