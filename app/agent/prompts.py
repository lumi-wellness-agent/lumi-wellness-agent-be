"""intake · Planner 프롬프트와 입력 메시지. 문구는 각 담당이 다듬는다(intake 담당 · plan 담당).

LLM이 읽는 사용 설명서는 구조화 출력 모델의 Field(description)에도 있다
(app/agent/state.py의 IntakeResult, app/agent/nodes/planner.py의 ActivityChoice · PlanRationale).
"""

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.agent.state import CareState, IntakeResult, NextStep
from app.schemas.care import CareGoal, CareRequest, ContentType
from app.schemas.evidence import OUTCOME_LABELS, InterventionOrder, Outcome
from app.schemas.protocol import CareProtocol
from app.schemas.retrieval import GOAL_OUTCOMES

if TYPE_CHECKING:
    from app.agent.nodes.planner import ActivityChoice, CitedEvidence

INTAKE_SYSTEM_PROMPT = """\
너는 LUMI의 인테이크 분류기다. 사용자가 한 말을 읽고 IntakeResult를 채운다. 대화하지 않는다.

- goal: 원하는 변화. 반복되는 생각에서 벗어나고 싶으면 S1, 긴장·스트레스를 풀고 싶으면 S2,
  오래 집중해 머리가 피로하면 S3. 둘 이상이 섞이거나 알 수 없으면 null.
- order_condition: 스트레스 상황이 앞에 있으면 PRE_STRESS(발표·면접 직전), 방금 겪었으면
  POST_STRESS, 오래 집중한 뒤면 POST_FATIGUE, 특별한 계기가 없으면 NO_INDUCTION. 모르면 null.
- minutes · place: 말한 것만 적는다. 말하지 않았으면 null — 추측하지 않는다.
- constraints: 활동에 영향을 주는 제약(이명, 멀미, 눈 피로, 소리를 틀 수 없는 상황 …).
- safety: 자해·자살 생각, 위기, 학대처럼 세션 대신 안전 안내가 필요하면 true.
- 점수나 척도 응답을 추정하거나 만들지 않는다. 측정은 다음 단계가 척도로 한다.
- summary: 사용자 상태를 한 줄로.
"""


def build_intake_input(request: CareRequest) -> str:
    """intake에 보내는 사용자 메시지. FE 칩으로 받은 값은 그대로 쓰라고 함께 싣는다."""
    parts = [f"## 사용자 입력\n{request.text}"]
    chips = {
        "goal": request.goal.value if request.goal else None,
        "minutes_available": request.minutes_available,
        "place": request.place.value if request.place else None,
    }
    given = {k: v for k, v in chips.items() if v is not None}
    if given:
        lines = "\n".join(f"- {k}: {v}" for k, v in given.items())
        parts.append(f"## 사용자가 직접 고른 값(그대로 쓴다)\n{lines}")
    return "\n\n".join(parts)


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

EXPLAIN_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 고른 활동과 검증된 연구 근거로, 사용자에게 보여 줄 추천 이유를
한국어 2~3문장으로 쓴다. 순서는 사용자 상태 → 이 활동을 고른 이유 → 근거(저자 · 연도와 개선이
확인된 지표).

- 근거 목록에 있는 연구만 언급한다. '개선 지표'에 있는 지표만 개선으로 말한다 — 정서 결과를
  스트레스 감소로 바꿔 말하지 않는다. 목표 지지가 '아니오'인 근거는 참고로만 말한다.
- 플래그가 있으면 한 구절로 밝힌다: ADAPTED_STIMULUS는 제공 콘텐츠가 연구 자극과 다르다,
  CONFLICTING_EVIDENCE는 반대 결과도 있다, POPULATION_MISMATCH는 임상 표본 결과다.
- 활동이 EXPERIMENTAL이거나 목표 지표 등급이 D뿐이면 '실험적'이라고 적는다.
- 근거가 없으면 연구 근거를 찾지 못했다고 솔직하게 적는다.
- enum 이름(POST_FATIGUE · RUMINATION …)과 통계값(p · g · SMD)을 그대로 쓰지 않는다. 치료 효과를
  단정하지 않고, 건강한 성인 대상 결과를 임상 효과로 넓히지 않는다."""

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
        ]
    )
