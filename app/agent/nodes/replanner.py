"""④ Replanning — 종료 · 활동 변경 · 추가 세션 중 하나를 제안하고 사용자의 선택을 받는다.

- replan: 제안과 고를 수 있는 선택지(options)를 만든다. 규칙(임시): 끝까지 못 함 → 활동 변경,
  라운드 상한 → 종료, 개선 → 종료, 판정 기준 없음 → 종료, 그 밖 → 활동 변경. 기준은 팀이 정한다.
  선택지는 next_step_options — 라운드 상한이면 종료만, 바꿀 활동이 없으면 활동 변경을 뺀다. 권할
  단계가 선택지에 없으면 종료를 권한다.
- confirm: interrupt로 평가 · 제안 · 선택지를 보여 주고 ReplanDecision을 기다린다. 동의한 경우에만
  다음 라운드(plan)로 간다. 다음 라운드의 사전값은 직전 사후값(round_pre = post). 결정은 세션 API가
  선택지로 미리 거른다 — 여기서 거부하면 그 재개 값에 세션이 묶인다(app/api/sessions.py).
- route_after_confirm: END면 사전·종료에만 재는 목표 척도(STAI-6 · BSRI)가 있을 때 measure_final을
  거친다. 라운드마다 잰 S3(MFVAS)는 마지막 사후값이 종료값이라 바로 END.
"""

from typing import Literal

from langgraph.graph import END
from langgraph.types import interrupt

from app.agent.nodes.planner import planner_context, search_candidates
from app.agent.state import CareState, NextStep, ReplanDecision, ReplanProposal, Round
from app.schemas.measure import final_scale

MAX_ROUNDS = 3  # 라운드 2–3회 제한(등급 규칙 시트 '라운드 규칙')


def next_step_options(state: CareState, rounds_done: int) -> list[NextStep]:
    """고를 수 있는 다음 단계. 라운드 상한이면 종료만, 바꿀 활동이 없으면 활동 변경을 뺀다.

    바꿀 활동은 plan이 다음 라운드에 볼 후보(이번 활동을 뺀 planner.search_candidates)로 정한다 —
    지금 계획보다 조건을 더 풀어야만 나오면(예: 환경을 풀어야만) 바꿀 활동이 없다고 본다.
    """
    if rounds_done >= MAX_ROUNDS:
        return [NextStep.END]
    plan = state["plan"]
    session = planner_context(None, state)
    alternatives = search_candidates(session, exclude=[plan.activity.protocol_id])
    can_change = bool(alternatives.protocols) and set(alternatives.relaxed) <= set(plan.relaxed)
    return [step for step in NextStep if step is not NextStep.CHANGE_ACTIVITY or can_change]


def replan(state: CareState) -> dict:
    report, evaluation = state["report"], state["evaluation"]
    rounds_done = len(state.get("rounds", [])) + 1
    options = next_step_options(state, rounds_done)
    if not report.completed:
        recommended, reason = NextStep.CHANGE_ACTIVITY, "활동을 끝까지 하지 못했다"
    elif rounds_done >= MAX_ROUNDS:
        recommended, reason = NextStep.END, f"라운드 {MAX_ROUNDS}회를 채웠다"
    elif evaluation.improved:
        recommended, reason = NextStep.END, "사전보다 나아졌다"
    elif evaluation.improved is None:
        recommended, reason = NextStep.END, "이 목표는 세션 내 판정 기준이 없다(목표 지표 —)"
    else:
        recommended, reason = NextStep.CHANGE_ACTIVITY, "사전보다 나아지지 않았다"
    if recommended not in options:
        limit = (
            f"라운드 {MAX_ROUNDS}회를 채웠다"
            if rounds_done >= MAX_ROUNDS
            else "지금 여건에서 바꿀 활동이 없다"
        )
        recommended, reason = NextStep.END, f"{reason} — {limit}"
    return {"proposal": ReplanProposal(recommended=recommended, reason=reason, options=options)}


def confirm(state: CareState) -> dict:
    value = interrupt(
        {
            "evaluation": state["evaluation"].model_dump(mode="json"),
            "proposal": state["proposal"].model_dump(mode="json"),
            "options": [step.value for step in state["proposal"].options],
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
        proposal=state["proposal"],
    )
    update: dict = {"next_step": decision.next_step, "rounds": [finished]}
    if decision.next_step is not NextStep.END:
        update["round_pre"] = state["post"]  # 다음 라운드의 사전값 = 이번 사후값
    return update


def route_after_confirm(state: CareState) -> Literal["plan", "measure_final", "__end__"]:
    """종료면 STAI-6 · BSRI를 한 번 더 재고 끝낸다(라운드마다 잰 S3는 바로 끝).

    활동 변경·추가 세션이면 Planner로.
    """
    if state["next_step"] is not NextStep.END:
        return "plan"
    return "measure_final" if final_scale(state["intake"].goal) else END
