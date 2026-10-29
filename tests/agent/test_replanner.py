"""④ Replanning — 선택지 · 규칙 제안(코드)과 다음 단계 추천(LLM 대역)."""

import asyncio

from langgraph.runtime import Runtime

from app.agent import llm
from app.agent.context import AgentContext
from app.agent.nodes.replanner import (
    LlmReplanner,
    NextStepChoice,
    make_recommender,
    make_replan_node,
    rule_proposal,
)
from app.agent.prompts import RECOMMEND_SYSTEM_PROMPT
from app.agent.state import NextStep, OutcomeEvaluation, Round, SessionReport
from app.schemas.care import CareGoal, CareRequest
from app.schemas.measure import Timepoint
from tests.agent import fakes
from tests.agent.test_graph import BREATHING, S2_INTAKE, FakeReplanner, anchor
from tests.agent.test_planner_llm import ClientError


def after(report: SessionReport, deltas: dict, improved: bool | None, rounds=()) -> dict:
    """라운드를 마친 S2 세션(PR-06) — 사후 측정 · 평가까지 끝났다."""
    return {
        "request": CareRequest(text="발표 앞두고 긴장돼요"),
        "intake": S2_INTAKE,
        "plan": BREATHING,
        "report": report,
        "evaluation": OutcomeEvaluation(
            goal=CareGoal.S2,
            round=len(rounds) + 1,
            deltas=deltas,
            improved=improved,
            completed=report.completed,
        ),
        "rounds": list(rounds),
    }


IMPROVED = after(SessionReport(completed=True, notice_words=["아직 떨림"]), {"arousal": -2.0}, True)
STOPPED = after(
    SessionReport(completed=False, stopped_reason="어지러워요"), {"arousal": 1.0}, False
)


def run_replan(replanner, state: dict):
    node = make_replan_node(replanner)
    return asyncio.run(node(state, Runtime(context=AgentContext())))["proposal"]


def test_the_llm_recommends_within_the_options_and_the_rule_is_kept():
    choice = NextStepChoice(
        recommended=NextStep.ADDITIONAL_SESSION,
        reason="긴장이 조금 내려갔어요. 아직 떨림이 남았다면 같은 호흡을 한 번 더 해 볼까요?",
    )
    replanner = FakeReplanner(choice)
    proposal = run_replan(replanner, IMPROVED)
    assert (proposal.recommended, proposal.reason, proposal.by_llm) == (
        NextStep.ADDITIONAL_SESSION,
        choice.reason,
        True,
    )
    assert proposal.rule_recommended is NextStep.END  # 규칙: 나아졌다 → 종료
    assert proposal.options == list(NextStep)
    assert replanner.rules == [rule_proposal(IMPROVED)]  # LLM은 규칙 제안을 기본값으로 받는다


def test_the_rule_stands_when_the_llm_goes_outside_the_options_or_fails():
    rule = rule_proposal(STOPPED)
    assert (rule.recommended, rule.options) == (
        NextStep.CHANGE_ACTIVITY,
        [NextStep.END, NextStep.CHANGE_ACTIVITY],  # 멈춘 활동은 한 번 더가 없다
    )
    outside = NextStepChoice(recommended=NextStep.ADDITIONAL_SESSION, reason="…")
    assert run_replan(FakeReplanner(outside), STOPPED) == rule
    assert run_replan(FakeReplanner(RuntimeError("모델 장애")), STOPPED) == rule
    assert not rule.by_llm


def test_a_single_option_or_no_replanner_keeps_the_rule():
    done = Round(
        plan=BREATHING,
        report=SessionReport(completed=True),
        pre=anchor(Timepoint.PRE, 0),
        post=anchor(Timepoint.POST, 1),
        evaluation=OutcomeEvaluation(
            goal=CareGoal.S2, round=1, deltas={}, improved=False, completed=True
        ),
        next_step=NextStep.ADDITIONAL_SESSION,
    )
    third = after(SessionReport(completed=True), {"arousal": 0.0}, False, rounds=[done, done])
    replanner = FakeReplanner()
    proposal = run_replan(replanner, third)
    assert proposal.options == [NextStep.END] and replanner.rules == []  # 라운드 상한
    assert run_replan(None, IMPROVED) == rule_proposal(IMPROVED)


def test_recommender_sends_the_round_the_rule_and_the_options():
    model = fakes.FakeStructuredModel(NextStepChoice(recommended=NextStep.END, reason="…"))
    asyncio.run(make_recommender(model)(STOPPED, rule_proposal(STOPPED)))
    assert model.schemas == [(NextStepChoice, "json_schema")]
    system, human = model.inputs[0]
    assert system.content == RECOMMEND_SYSTEM_PROMPT
    text = human.content
    assert "## 사용자 입력\n발표 앞두고 긴장돼요" in text
    assert "- PR-06 주기적 한숨 호흡 5분 · 5분 — 1번째 활동" in text
    assert "- 끝까지 함: 아니오\n- 멈춘 이유: 어지러워요\n- 감정 단어: -" in text
    assert "- 긴장(각성) — 낮을수록 차분하다: +1\n- 판정: 큰 변화 없음" in text
    assert "## 규칙 제안\n- CHANGE_ACTIVITY: 활동을 끝까지 하지 못했다" in text
    assert text.endswith(
        "## 선택지\n- END: 여기서 마친다\n- CHANGE_ACTIVITY: 같은 목표로 다른 활동을 한다"
    )


def test_llm_replanner_falls_back_to_the_next_provider(monkeypatch):
    monkeypatch.setattr(llm, "BACKOFF_SECONDS", 0)
    choice = NextStepChoice(recommended=NextStep.END, reason="…")
    broken = fakes.FakeStructuredModel(ClientError())
    working = fakes.FakeStructuredModel(choice)
    replanner = LlmReplanner([("openai", broken), ("anthropic", working)])
    assert asyncio.run(replanner.recommend(IMPROVED, rule_proposal(IMPROVED))) is choice
