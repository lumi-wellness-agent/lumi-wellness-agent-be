"""intake · Planner 프롬프트와 입력 메시지. 문구는 각 담당이 다듬는다(intake 담당 · plan 담당).

LLM이 읽는 사용 설명서는 구조화 출력 모델의 Field(description)에도 있다
(app/agent/intake_rules.py의 StateReading · GoalInference, app/agent/nodes/planner.py의
ActivityChoice · PlanRationale).
"""

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.agent.state import CareState, IntakeResult, NextStep
from app.content import factors as content_factors
from app.intake_cues import cues_json, definitions_json
from app.schemas.care import CareGoal, CareRequest, ContentType
from app.schemas.evidence import OUTCOME_LABELS, InterventionOrder, Outcome
from app.schemas.intake import IntakeTurn, QuestionSlot
from app.schemas.protocol import CareProtocol
from app.schemas.retrieval import GOAL_OUTCOMES

if TYPE_CHECKING:
    from app.agent.nodes.planner import ActivityChoice, CitedEvidence

READ_SYSTEM_PROMPT = """\
너는 LUMI 인테이크의 ① 상황 읽기다. 사용자가 한 말(과 앞서 물은 질문·답)을 읽고 StateReading을
채운다. 대화하지 않는다. 처음 질문은 "오늘 상황이 어떠세요?"였고, 사용자 입력이 그 답이다.

판단 순서: safety → relevance → 나머지.
- safety: 처음 입력이나 어느 답에든 스스로를 해치거나 삶을 끝내고 싶다는 뜻이 직접·간접으로
  드러나면 true(죽고 싶다 · 사라지고 싶다 · 다 끝내고 싶다 · 살 이유가 없다 …). '피곤해 죽겠다'
  · '배고파 죽겠네' 같은 과장은 false. 애매하면 true — 뒤 화면이 도움 안내를 보여 준다.
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

# 목록 순서·이름에 끌려 등급이 낮은 활동을 고르는 일이 있어(2026-10-26 gpt-4o-mini 실측) 고르는
# 규칙을 그대로 적는다. 후보 목록은 코드가 등급 순으로 정렬해 싣는다
CHOOSE_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 후보 프로토콜 목록에서 사용자 상태에 맞는 활동 하나를 고른다.

- 활동은 후보 목록에서만 고른다. 라운드당 1개이고 프로토콜을 임의로 조합하지 않는다.
- 목록은 주 목표가 맞는 것부터, 목표 1차 지표의 등급(A > B > C > 미확인 > D > —)이 높은 순이다.
  맨 앞의 것을 고른다. 이름이나 요약이 사용자 상황과 비슷해 보여도 등급이 낮은 것을 앞세우지 않는다.
- 맨 앞의 것을 건너뛰는 경우는 사용자 제약(이명 · 멀미 · 눈 피로 · 소리를 틀 수 없음 …)이나 장소에
  걸릴 때, 직전 계획의 거부 사유에 해당할 때뿐이다. 그러면 다음 것을 고른다.
- search_query: 고른 활동의 연구 근거를 찾을 영어 질의. 활동(매체)과 목표 지표를 논문 용어로
  3~6단어. 예: slow breathing anxiety stress · mindfulness rumination · nature images attention
- reason: 고른 이유 한 문장(한국어). 제약·장소 때문에 앞의 것을 건너뛰었으면 그 이유를 적는다."""

# 플래그 이름을 문장에 그대로 쓰고(2026-10-28 gpt-5.4-mini 실측, S1 6회 중 4회 "ADAPTED_STIMULUS가
# 있어"), 등급 B인 활동을 '실험적'이라 부르는 일이 있어 이름 대신 뜻을, '실험적'은 조건을 적는다
EXPLAIN_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 고른 활동과 검증된 연구 근거로, 사용자에게 보여 줄 추천 이유를
한국어 2~3문장으로 쓴다. 순서는 사용자 상태 → 이 활동을 고른 이유 → 근거(저자 · 연도와 개선이
확인된 지표).

- 근거 목록에 있는 연구만 언급한다. '개선 지표'에 있는 지표만 개선으로 말한다 — 정서 결과를
  스트레스 감소로 바꿔 말하지 않는다. 목표 지지가 '아니오'인 근거는 참고로만 말한다.
- 플래그가 있으면 이름 대신 뜻을 한 구절로 밝힌다: ADAPTED_STIMULUS는 제공 콘텐츠가 연구 자극과
  다르다, CONFLICTING_EVIDENCE는 반대 결과도 있다, POPULATION_MISMATCH는 임상 표본 결과다.
- '실험적'은 구성이 EXPERIMENTAL이거나 목표 지표 등급이 D뿐일 때만 쓴다.
- 근거가 없으면 연구 근거를 찾지 못했다고 솔직하게 적는다.
- enum · 플래그 이름(POST_FATIGUE · RUMINATION · ADAPTED_STIMULUS …)과 통계값(p · g · SMD)을 그대로
  쓰지 않는다. 치료 효과를 단정하지 않고, 건강한 성인 대상 결과를 임상 효과로 넓히지 않는다.

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


def _state_lines(request: CareRequest, intake: IntakeResult) -> list[str]:
    goal = f"{intake.goal.value} — {GOAL_NAMES[intake.goal]}" if intake.goal else "미정"
    order = intake.order_condition
    return [
        f"- 목표: {goal}",
        f"- 사용자 상태: {ORDER_NAMES[order] + f'({order.value})' if order else '미상'}",
        f"- 쓸 수 있는 시간: {intake.minutes or '미상'}분",
        f"- 장소: {intake.place.value if intake.place else '미상'}",
        f"- 제약: {', '.join(intake.constraints) or '없음'}",
        f"- 이미지 제공: {IMAGE_DELIVERY[request.profile.content_type]}",
        f"- 요약: {intake.summary or '-'}",
    ]


def _history_parts(state: CareState) -> list[str]:
    """사전 측정과 지난 라운드. 활동 변경을 골랐으면 그 활동을 후보에서 뺐다고 알린다."""
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
        last = rounds[-1]
        if last.next_step is NextStep.CHANGE_ACTIVITY:
            lines.append(
                f"- 사용자가 활동 변경을 골랐다. {last.plan.activity.protocol_id}는 후보에서 뺐다"
            )
        parts.append("## 지난 라운드\n" + "\n".join(lines))
    return parts


def build_choice_input(
    state: CareState, candidates: Sequence[dict], problems: Sequence[str]
) -> str:
    """LLM 호출 1의 사용자 메시지. 입력 · 상태 · 이력 · 후보 목록(등급 순) · 거부 사유."""
    request, intake = state["request"], state["intake"]
    parts = [
        f"## 사용자 입력\n{request.text}",
        "## 사용자 상태\n" + "\n".join(_state_lines(request, intake)),
        *_history_parts(state),
        "## 후보 프로토콜 (등급 순)\n" + json.dumps(list(candidates), ensure_ascii=False),
    ]
    if problems:
        parts.append("## 직전 계획의 거부 사유\n" + "\n".join(f"- {p}" for p in problems))
    return "\n\n".join(parts)


def build_rationale_input(
    state: CareState,
    choice: "ActivityChoice",
    protocol: CareProtocol,
    evidence: Sequence["CitedEvidence"],
) -> str:
    """LLM 호출 2의 사용자 메시지. 상태 · 고른 활동(구성 · 목표 지표 등급) · 검증된 근거 목록."""
    request, intake = state["request"], state["intake"]
    primary = GOAL_OUTCOMES[intake.goal].primary if intake.goal else []
    grades = ", ".join(
        f"{OUTCOME_NAMES.get(o, o.value)} {protocol.grades[o].grade.value}" for o in primary
    )
    activity_lines = [
        f"- {protocol.protocol_id} {protocol.name} — 구성 {protocol.composition.value},"
        f" 목표 지표 등급: {grades or '없음'}",
        f"- 고른 이유: {choice.reason}",
    ]
    if evidence:
        evidence_lines = []
        for cited in evidence:
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
        evidence_part = f"## 검증된 근거 ({len(evidence)}건)\n" + "\n".join(evidence_lines)
    else:
        evidence_part = "## 검증된 근거\n검증을 통과한 근거가 없다."
    return "\n\n".join(
        [
            f"## 사용자 입력\n{request.text}",
            "## 사용자 상태\n" + "\n".join(_state_lines(request, intake)),
            "## 고른 활동\n" + "\n".join(activity_lines),
            evidence_part,
            _factor_part(state, protocol),
        ]
    )


def _factor_part(state: CareState, protocol: CareProtocol) -> str:
    """LLM 호출 2의 콘텐츠 Factor 칸 — 이 활동에 열 칸 · 사용자 칩 · 지난 라운드에 쓴 Factor."""
    menu = content_factors.menu(protocol)
    if not menu:
        return "## 콘텐츠 Factor\n이 활동은 고를 칸이 없다."
    lines = [json.dumps(menu, ensure_ascii=False)]
    chips = state["request"].profile.preferred_scenery
    if chips:
        lines.append(f"- 사용자가 고른 좋아하는 풍경: {', '.join(chips)}")
    used = [
        f"{r.plan.activity.protocol_id} "
        + json.dumps(r.plan.content_factors.model_dump(mode="json", exclude={"reason"}))
        for r in state.get("rounds") or []
        if r.plan.content_factors
    ]
    if used:
        lines.append(f"- 지난 라운드에 쓴 Factor: {' / '.join(used)}")
    return "## 콘텐츠 Factor (고를 칸)\n" + "\n".join(lines)
