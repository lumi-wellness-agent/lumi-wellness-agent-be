"""intake · Planner 프롬프트 초안. 문구는 각 담당이 다듬는다(intake 담당 · plan 담당).

LLM이 읽는 사용 설명서는 Tool docstring과 Field(description)에도 있다
(app/agent/tools/*, app/agent/state.py의 IntakeResult · CarePlan).
"""

from app.agent.state import CareState
from app.schemas.care import CareRequest

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


PLANNER_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 사용자의 상태·목표·제약에 맞는 활동 노드 하나를 고른다.

- 활동은 get_care_protocols가 돌려준 검수된 프로토콜에서만 고른다. 라운드당 활동 1개.
  프로토콜을 임의로 조합하지 않는다 — 조합은 VALIDATED_MULTIMODAL 프로토콜 자체로만 제공한다.
- 사용자 상태(order_condition)가 프로토콜의 허용 상태(intervention_order)에 맞아야 한다.
  비어 있으면 제한이 없다.
- 활동 시간은 프로토콜의 duration_minutes 범위 안에서, 사용자가 쓸 수 있는 시간 이내로 정한다.
- 근거는 search_evidence로 찾고 validate_evidence를 통과한(usable) 것만 evidence_record_ids에
  넣는다. supports_goal=False인 근거는 상충·참고로만 쓴다. supported_outcomes에 있는 지표만
  '개선'으로 말한다.
- EXPERIMENTAL 프로토콜이나 등급 D뿐인 프로토콜을 고르면 rationale에 '실험적'임을 적는다.
- 지난 라운드에서 활동을 바꾸기로 했다면 같은 프로토콜을 다시 고르지 않는다.
- 치료 효과를 단정하지 않는다.

마지막에 CarePlan을 제출한다."""


def build_planner_input(state: CareState, problems: list[str]) -> str:
    """Planner에 보내는 사용자 메시지. 인테이크·사전 측정·지난 라운드·직전 계획의 거부 사유."""
    parts = [f"## 사용자 상태(인테이크)\n{state['intake'].model_dump_json()}"]
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
        parts.append("## 지난 라운드\n" + "\n".join(lines))
    if problems:
        parts.append("## 직전 계획의 거부 사유\n" + "\n".join(f"- {p}" for p in problems))
    return "\n\n".join(parts)
