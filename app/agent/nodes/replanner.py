"""④ Replanning — 종료 · 활동 변경 · 추가 세션 중 하나를 제안하고 사용자의 선택을 받는다.

- replan: 제안을 만든다. 규칙(임시): 끝까지 못 함 → 활동 변경, 라운드 상한 → 종료, 개선 → 종료,
  판정 기준 없음(S3) → 종료, 그 밖 → 활동 변경. 기준은 팀이 정한다.
- confirm: interrupt로 평가와 제안을 보여 주고 ReplanDecision을 기다린다. 동의한 경우에만 다음
  라운드(plan)로 간다. 다음 라운드의 사전값은 직전 사후값(round_pre = post).
- route_after_confirm: END면 목표 척도가 있을 때 measure_final을 거친다(S3는 바로 END).
"""

from typing import Literal

from langgraph.graph import END
from langgraph.types import interrupt

from app.agent.state import CareState, NextStep, ReplanDecision, ReplanProposal, Round
from app.schemas.measure import GOAL_SCALE

MAX_ROUNDS = 3  # 라운드 2–3회 제한(등급 규칙 시트 '라운드 규칙')


def replan(state: CareState) -> dict:
    report, evaluation = state["report"], state["evaluation"]
    rounds_done = len(state.get("rounds", [])) + 1
    if not report.completed:
        proposal = ReplanProposal(
            recommended=NextStep.CHANGE_ACTIVITY, reason="활동을 끝까지 하지 못했다"
        )
    elif rounds_done >= MAX_ROUNDS:
        proposal = ReplanProposal(
            recommended=NextStep.END, reason=f"라운드 {MAX_ROUNDS}회를 채웠다"
        )
    elif evaluation.improved:
        proposal = ReplanProposal(recommended=NextStep.END, reason="사전보다 나아졌다")
    elif evaluation.improved is None:
        proposal = ReplanProposal(
            recommended=NextStep.END, reason="이 목표는 세션 내 판정 기준이 없다(목표 지표 —)"
        )
    else:
        proposal = ReplanProposal(
            recommended=NextStep.CHANGE_ACTIVITY, reason="사전보다 나아지지 않았다"
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
        pre=state["round_pre"],
        post=state["post"],
        evaluation=state["evaluation"],
        next_step=decision.next_step,
    )
    update: dict = {"next_step": decision.next_step, "rounds": [finished]}
    if decision.next_step is not NextStep.END:
        update["round_pre"] = state["post"]  # 다음 라운드의 사전값 = 이번 사후값
    return update


def route_after_confirm(state: CareState) -> Literal["plan", "measure_final", "__end__"]:
    """종료면 목표 척도를 한 번 더 재고 끝낸다(S3는 바로 끝). 활동 변경·추가 세션이면 Planner로."""
    if state["next_step"] is not NextStep.END:
        return "plan"
    return "measure_final" if GOAL_SCALE[state["intake"].goal] else END
