"""④ Replanning — 종료 · 활동 변경 · 추가 세션 중 하나를 제안하고 사용자의 선택을 받는다.

- replan: 고를 수 있는 선택지(options)와 규칙 제안을 코드가 만들고, LLM(구조화 출력 1회)이 선택지
  안에서 권할 단계와 이유를 고른다.
  - 규칙(임시, rule_proposal): 끝까지 못 함 → 활동 변경, 라운드 상한 → 종료, 개선 → 종료, 판정
    기준 없음 → 종료, 그 밖 → 활동 변경. 권할 단계가 선택지에 없으면 종료. 기준은 팀이 정한다.
  - 선택지(next_step_options): 라운드 상한이면 종료만, 바꿀 활동이 없으면 활동 변경을, 중간에 멈춘
    활동이면 같은 활동을 한 번 더 하는 추가 세션을 뺀다.
  - LLM(Replanner)은 규칙 제안을 기본값으로 받고, 멈춘 이유 · 감정 단어 · 처음 입력 · Δ에 다른
    신호가 있을 때만 바꾼다. 선택지 밖을 권하거나 호출이 실패하면 규칙 제안을 그대로 쓴다 — 추천이
    세션을 막지 않는다. 선택지가 하나면(라운드 상한) 부르지 않는다. 규칙 제안은
    rule_recommended로 함께 남는다(기록에서 비교).
- confirm: interrupt로 평가 · 제안 · 선택지를 보여 주고 ReplanDecision을 기다린다. 동의한 경우에만
  다음 라운드(plan)로 간다. 다음 라운드의 사전값은 직전 사후값(round_pre = post). 결정은 세션 API가
  선택지로 미리 거른다 — 여기서 거부하면 그 재개 값에 세션이 묶인다(app/api/sessions.py).
- route_after_confirm: END면 사전·종료에만 재는 목표 척도(STAI-6 · BSRI)가 있을 때 measure_final을
  거친다. 라운드마다 잰 S3(MFVAS)는 마지막 사후값이 종료값이라 바로 END.
"""

import logging
from collections.abc import Awaitable, Sequence
from typing import Literal, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from app.agent.context import AgentContext
from app.agent.llm import with_fallback
from app.agent.nodes.planner import planner_context, search_candidates
from app.agent.prompts import RECOMMEND_SYSTEM_PROMPT, build_recommend_input
from app.agent.state import CareState, NextStep, ReplanDecision, ReplanProposal, Round
from app.schemas.measure import final_scale

log = logging.getLogger(__name__)

MAX_ROUNDS = 3  # 라운드 2–3회 제한(등급 규칙 시트 '라운드 규칙')


def next_step_options(state: CareState, rounds_done: int) -> list[NextStep]:
    """고를 수 있는 다음 단계. 라운드 상한이면 종료만, 바꿀 활동이 없으면 활동 변경을, 중간에 멈춘
    활동이면 추가 세션(같은 활동 한 번 더)을 뺀다.

    바꿀 활동은 plan이 다음 라운드에 볼 후보(이번 활동과 이번 세션에서 바꾸기로 한 활동을 뺀
    planner.search_candidates)로 정한다 — 지금 계획보다 조건을 더 풀어야만 나오면(예: 환경을
    풀어야만) 바꿀 활동이 없다고 본다. 멈춘 이유(불편 · 어지러움 · 시간 없음 · 그만)가 무엇이든
    같은 활동을 다시 권하지 않는다.
    """
    if rounds_done >= MAX_ROUNDS:
        return [NextStep.END]
    plan = state["plan"]
    session = planner_context(None, state)
    exclude = [*session.excluded_protocol_ids, plan.activity.protocol_id]
    alternatives = search_candidates(session, exclude=exclude)
    can_change = bool(alternatives.protocols) and set(alternatives.relaxed) <= set(plan.relaxed)
    can_repeat = state["report"].completed
    return [
        step
        for step in NextStep
        if (step is not NextStep.CHANGE_ACTIVITY or can_change)
        and (step is not NextStep.ADDITIONAL_SESSION or can_repeat)
    ]


class NextStepChoice(BaseModel):
    """replan LLM의 구조화 출력 — 권할 다음 단계와 그 이유. 필드 설명이 LLM에 보인다."""

    recommended: NextStep = Field(description="선택지에 있는 다음 단계 하나")
    reason: str = Field(description="사용자에게 보여 줄 권하는 이유 1~2문장(한국어, '~해요' 체)")


class Replanner(Protocol):
    """replan의 LLM 판단. 운영은 LlmReplanner, 테스트는 대역을 넣는다."""

    def recommend(self, state: CareState, rule: ReplanProposal) -> Awaitable[NextStepChoice]: ...


def make_recommender(model: BaseChatModel):
    """구조화 출력(json_schema) 한 번으로 NextStepChoice를 받는다."""
    structured = model.with_structured_output(NextStepChoice, method="json_schema")

    async def recommend(state: CareState, rule: ReplanProposal) -> NextStepChoice:
        messages = [
            SystemMessage(RECOMMEND_SYSTEM_PROMPT),
            HumanMessage(build_recommend_input(state, rule)),
        ]
        return await structured.ainvoke(messages)

    return recommend


class LlmReplanner:
    """공급자별 모델로 만든 recommend에 재시도·폴백(app/agent/llm.py)을 두른 Replanner."""

    def __init__(self, models: Sequence[tuple[str, BaseChatModel]]):
        self._recommend = with_fallback([(name, make_recommender(m)) for name, m in models])

    async def recommend(self, state: CareState, rule: ReplanProposal) -> NextStepChoice:
        return await self._recommend(state, rule)


def rule_proposal(state: CareState) -> ReplanProposal:
    """규칙 제안과 선택지. LLM이 없거나 실패하면 이것이 그대로 제안이다."""
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
    return ReplanProposal(
        recommended=recommended, reason=reason, options=options, rule_recommended=recommended
    )


def make_replan_node(replanner: Replanner | None = None):
    """replan 노드. replanner가 없으면(그래프 테스트 · Mermaid) 규칙 제안만 낸다."""

    async def replan(state: CareState, runtime: Runtime[AgentContext]) -> dict:
        rule = rule_proposal(state)
        if replanner is None or len(rule.options) < 2:
            return {"proposal": rule}
        runtime.stream_writer({"node": "replan", "step": "recommend"})
        try:
            choice = await replanner.recommend(state, rule)
        except Exception:  # 추천은 안내다 — 실패해도 규칙 제안으로 세션을 잇는다
            log.warning("다음 단계 추천(LLM)이 실패해 규칙 제안을 쓴다", exc_info=True)
            return {"proposal": rule}
        if choice.recommended not in rule.options:
            log.warning("LLM이 선택지 밖 %s를 권해 규칙 제안을 쓴다", choice.recommended.value)
            return {"proposal": rule}
        log.info(
            "다음 단계 추천 %s (규칙 %s) — %s",
            choice.recommended.value,
            rule.recommended.value,
            choice.reason,
        )
        update = {"recommended": choice.recommended, "reason": choice.reason, "by_llm": True}
        return {"proposal": rule.model_copy(update=update)}

    return replan


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
