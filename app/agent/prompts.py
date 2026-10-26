"""intake · Planner 프롬프트 초안. 문구는 각 담당이 다듬는다(intake 담당 · plan 담당).

LLM이 읽는 사용 설명서는 Tool docstring과 Field(description)에도 있다
(app/agent/tools/*, app/agent/state.py의 IntakeResult · CarePlan).
"""

from app.agent.state import CareState, IntakeResult, NextStep
from app.schemas.care import CareRequest, ContentType
from app.schemas.retrieval import GOAL_OUTCOMES

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


# gpt-4o-mini는 절차를 번호로 적지 않으면 search_evidence를 건너뛰고 빈 ID로 validate_evidence만
# 부르고, 목록 순서·이름에 끌려 등급이 낮은 활동을 고른다(2026-10-26 S1·S2 실측). 단계와 인자를
# 그대로 적는다. 목표 · 사용자 상태 · 시간 · 이미지 선호는 Tool이 세션에서 읽는다(PlannerContext)
PLANNER_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 사용자 상태에 맞는 활동 노드 하나를 고르고, 그 활동의 연구 근거를
찾아 검증한 뒤 CarePlan을 제출한다. 아래 순서를 지키고 단계를 건너뛰지 않는다.

1. get_care_protocols — 이번 세션의 목표·사용자 상태·시간에 맞는 검수된 프로토콜 목록이 온다.
   활동은 이 목록에서만 고른다.
2. 활동 하나를 고른다. 라운드당 1개이고 프로토콜을 임의로 조합하지 않는다(조합은
   VALIDATED_MULTIMODAL 프로토콜 자체로만 제공한다).
   - 목록은 주 목표가 맞는 것부터, 목표 1차 지표의 등급(A > B > C > 미확인 > D > —)이 높은 순이다.
     맨 앞의 것을 고른다. 이름이나 요약이 사용자 상황과 비슷해 보여도 등급이 낮은 것을 앞세우지
     않는다.
   - 맨 앞의 것을 건너뛰는 경우는 둘뿐이다: 사용자 제약·장소에 걸릴 때, 지난 라운드에서 활동
     변경(CHANGE_ACTIVITY)을 골라 같은 프로토콜을 다시 고를 수 없을 때. 그러면 다음 것을 고른다.
   - 활동 시간(minutes)은 duration_minutes의 min~max 안, 사용자가 쓸 수 있는 시간 이내.
     보통 default를 쓴다.
3. search_evidence — 고른 활동의 근거를 찾는다. query는 활동과 목표 지표를 논문 용어(영어)로
   쓴다(예: "slow breathing anxiety stress", "mindfulness rumination"). medium은 활동의 매체 하나,
   intervention_order는 사용자 상태(미상이면 비운다), outcomes는 목표 1차 지표. 0건이면 outcomes,
   intervention_order 순서로 필터를 빼고 다시 찾는다.
4. validate_evidence — 3에서 받은 evidence_record_id를 한 번에 모두, protocol_id는 고른 활동을
   넣는다. 목표·사용자 상태·실제로 제공할 매체(AI 생성 이미지 포함)는 도구가 세션에서 읽는다.
5. CarePlan을 제출한다.
   - evidence_record_ids에는 usable=true인 ID만 넣는다. supports_goal=true인 것을 앞에 둔다.
   - rationale은 2~3문장: 사용자 상태 → 이 활동을 고른 이유 → 근거(저자·연도와
     supported_outcomes의 지표). ADAPTED_STIMULUS면 연구 자극과 다르다는 점을, EXPERIMENTAL이거나
     등급이 D뿐이면 '실험적'임을 적는다. usable 근거가 없으면 evidence_record_ids를 비우고 연구
     근거를 찾지 못했다고 적는다.

원칙
- rationale은 사용자에게 보이는 한국어 문장이다. enum 이름(POST_FATIGUE · RUMINATION …)을 그대로
  쓰지 않는다. evidence_record_ids에 넣은 근거만 언급한다.
- supported_outcomes에 있는 지표만 '개선'으로 말한다. 정서(AFFECT) 결과를 스트레스 감소로 바꿔
  말하지 않는다. supports_goal=false인 근거는 상충·참고로만 말한다.
- 통계값(p · g · SMD)은 옮겨 적지 않는다. 치료 효과를 단정하지 않고, 건강한 성인 대상 결과를 임상
  효과로 넓히지 않는다."""

# 이미지 슬롯은 모든 프로토콜이 생성 이미지를 허용한다(content_spec.slots[].source_policy
# generated=ALLOWED_WITH_FLAG). 검증 매체는 validate_evidence가 바꾸고(protocols.delivered_media),
# 여기서는 rationale에 차이를 밝히도록 알린다
IMAGE_DELIVERY = {
    ContentType.CURATED_PHOTO: "큐레이션 사진",
    ContentType.AI_IMAGE: "AI 생성 이미지(연구 자극과 다르다 — ADAPTED_STIMULUS)",
}


def _state_lines(request: CareRequest, intake: IntakeResult) -> list[str]:
    """사용자 상태. search_evidence 필터에 넣을 값은 인자 이름을 함께 적는다."""
    outcomes = GOAL_OUTCOMES[intake.goal].primary if intake.goal else []
    order = intake.order_condition.value if intake.order_condition else "미상"
    return [
        f"- 목표: {intake.goal.value if intake.goal else '미정'}",
        f"- 목표 1차 지표: {', '.join(o.value for o in outcomes) or '없음'} (outcomes)",
        f"- 사용자 상태: {order} (intervention_order)",
        f"- 쓸 수 있는 시간: {intake.minutes or '미상'}분",
        f"- 장소: {intake.place.value if intake.place else '미상'}",
        f"- 제약: {', '.join(intake.constraints) or '없음'}",
        f"- 이미지 제공: {IMAGE_DELIVERY[request.profile.content_type]}",
        f"- 요약: {intake.summary or '-'}",
    ]


def build_planner_input(state: CareState, problems: list[str]) -> str:
    """Planner에 보내는 사용자 메시지. 입력·상태·사전 측정·지난 라운드·거부 사유."""
    request, intake = state["request"], state["intake"]
    parts = [
        f"## 사용자 입력\n{request.text}",
        "## 사용자 상태\n" + "\n".join(_state_lines(request, intake)),
    ]
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
                f"- 사용자가 활동 변경을 골랐다. {last.plan.activity.protocol_id}는 목록에서 뺐다 —"
                " 다른 프로토콜을 고른다"
            )
        parts.append("## 지난 라운드\n" + "\n".join(lines))
    if problems:
        parts.append("## 직전 계획의 거부 사유\n" + "\n".join(f"- {p}" for p in problems))
    return "\n\n".join(parts)
