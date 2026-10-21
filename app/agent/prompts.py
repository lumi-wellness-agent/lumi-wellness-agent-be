"""Planner 프롬프트 초안. 문구는 Planner를 구현하며 다듬는다."""

from app.agent.state import CareState

PLANNER_SYSTEM_PROMPT = """\
너는 LUMI의 케어 세션 플래너다. 사용자의 상태·목표·선호에 맞는 회복 세션을 계획한다.

- 활동은 get_care_protocols가 돌려준 검수된 프로토콜에서만 고른다.
- 근거는 search_evidence로 찾고 validate_evidence를 통과한 것만 쓴다.
- 기본은 단일 세션(SINGLE)이다. 동시 복합(CONCURRENT)·순차 복합(SEQUENTIAL)은 프로토콜끼리
  호환될 때만 2~3개로 구성한다. 결합한 세션의 통합 효과는 검증되지 않았다.
- 지난 라운드에서 활동을 바꾸기로 했다면 같은 프로토콜을 다시 고르지 않는다.
- 치료 효과를 단정하지 않는다.

마지막에 CarePlan을 제출한다."""


def build_planner_input(state: CareState, problems: list[str]) -> str:
    """Planner에 보내는 사용자 메시지. 지난 라운드와 직전 계획의 거부 사유를 함께 싣는다."""
    parts = [f"## 사용자 입력\n{state['request'].model_dump_json()}"]
    if rounds := state.get("rounds"):
        lines = [
            f"- {r.plan.mode}: {[a.protocol_id for a in r.plan.activities]}"
            f" → 스트레스 변화 {r.evaluation.stress_change:+d},"
            f" 끝까지 함: {r.report.completed}, 사용자 선택: {r.next_step}"
            for r in rounds
        ]
        parts.append("## 지난 라운드\n" + "\n".join(lines))
    if problems:
        parts.append("## 직전 계획의 거부 사유\n" + "\n".join(f"- {p}" for p in problems))
    return "\n\n".join(parts)
