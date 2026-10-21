"""④ Replanning — 종료 · 활동 변경 · 추가 세션 중 하나를 제안하고 사용자의 선택을 받는다.

- replan: 제안을 만든다. 지금은 임시 규칙이다(아래). 기준은 팀이 정한다.
- confirm: interrupt로 평가와 제안을 보여 주고 ReplanDecision을 기다린다. Planner로 돌아가는 것은
  사용자가 활동 변경이나 추가 세션을 고른(동의한) 경우뿐이다.
"""

from langgraph.types import interrupt

from app.agent.state import CareState, NextStep, ReplanDecision, ReplanProposal, Round


def replan(state: CareState) -> dict:
    report, evaluation = state["report"], state["evaluation"]
    if not report.completed:
        proposal = ReplanProposal(
            recommended=NextStep.CHANGE_ACTIVITY, reason="활동을 끝까지 하지 못했다"
        )
    elif evaluation.improved:
        proposal = ReplanProposal(recommended=NextStep.END, reason="스트레스 수준이 낮아졌다")
    else:
        proposal = ReplanProposal(
            recommended=NextStep.CHANGE_ACTIVITY, reason="스트레스 수준이 낮아지지 않았다"
        )
    return {"proposal": proposal}


def confirm(state: CareState) -> dict:
    value = interrupt(
        {
            "evaluation": state["evaluation"].model_dump(mode="json"),
            "proposal": state["proposal"].model_dump(mode="json"),
            "options": [step.value for step in NextStep],
        }
    )
    decision = ReplanDecision.model_validate(value)
    finished = Round(
        plan=state["plan"],
        report=state["report"],
        evaluation=state["evaluation"],
        next_step=decision.next_step,
    )
    # 다음 라운드는 지금 상태에서 시작한다. 새 입력이 없으면 이번 사후 값을 사전 값으로 쓴다
    request = decision.request or state["request"].model_copy(
        update={"stress_level": state["report"].post_stress_level}
    )
    return {"next_step": decision.next_step, "rounds": [finished], "request": request}
