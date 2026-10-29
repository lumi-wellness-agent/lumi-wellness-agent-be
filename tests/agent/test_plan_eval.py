"""scripts/plan_eval.py — 시나리오 → plan 노드 입력, 기대 후보, 규칙 판정. 실제 모델 · DB 없이
대본 Planner와 fixture 검색 · 검증(tests/agent/fakes.py)으로 돌린다. 실제 모델로는
`make plan-eval`로 따로 돌린다.
"""

import asyncio

import pytest

from app.agent.nodes.planner import ActivityChoice, PlanRationale
from app.schemas.care import CareGoal, Place, Setting, environment_for
from app.schemas.retrieval import GOAL_OUTCOMES
from scripts import plan_eval
from scripts.rag_eval import load_scenarios
from tests.agent import fakes

SCENARIOS = {s.id: s for s in load_scenarios()}


class ScriptedPlanner:
    """정해 둔 활동을 차례로 고른다."""

    def __init__(self, *protocol_ids: str):
        self.protocol_ids = list(protocol_ids)

    async def choose(self, state, candidates, problems):
        return ActivityChoice(
            protocol_id=self.protocol_ids.pop(0), search_query="nature images attention", reason="-"
        )

    async def explain(self, state, choice, protocol, evidence, problems=()):
        return PlanRationale.text("추천 이유")


@pytest.fixture
def fake_backends(monkeypatch):
    monkeypatch.setattr("app.retrieval.hybrid.search", fakes.fake_search)
    monkeypatch.setattr("app.agent.validation.validate", fakes.fake_validate)


def plan(planner, scenario_id: str, previous: str | None = None) -> plan_eval.PlanRun:
    scenario = SCENARIOS[scenario_id]
    return asyncio.run(plan_eval.plan_once(planner, scenario, previous=previous))


def test_scenario_state_turns_chips_into_the_intake():
    state = plan_eval.scenario_state(SCENARIOS["S2-pre-a"])  # 사무실 · 이어폰 없음 · 5분
    intake = state["intake"]
    assert intake.environment == environment_for(Setting.OFFICE_CAFE, False)
    assert intake.environment.audio is False
    assert intake.place is Place.INDOOR and intake.minutes == 5
    assert state["request"].minutes_available == 5


def test_expected_candidates_follow_the_chips():
    s3 = plan_eval.expected(SCENARIOS["S3-pre-b"])  # 사건 전 S3는 10분이면 상태 조건이 풀린다
    assert [p.protocol_id for p in s3.protocols] == ["PR-01"] and s3.relaxed == ("order",)
    s1 = plan_eval.expected(SCENARIOS["S1-pre-b"])  # 사무실 · 이어폰 없음 · 5분이면 컬러링뿐
    assert [p.protocol_id for p in s1.protocols] == ["PR-15"]
    changed = plan_eval.expected(SCENARIOS["S3-fatigue-a"], exclude=["PR-11"])
    assert "PR-11" not in [p.protocol_id for p in changed.protocols]


def test_a_valid_first_choice_passes_every_check(fake_backends):
    run = plan(ScriptedPlanner("PR-01"), "S3-unknown-a")
    assert run.protocol_id == "PR-01" and run.calls == 1
    assert all(run.checks.values()), run.checks
    assert "changed" not in run.checks  # 첫 라운드에는 없다
    assert run.cited and run.borrowed == []  # fixture 근거는 PR-01의 evidence[]다


def test_the_rationale_and_what_the_explainer_saw_are_saved(fake_backends):
    run = plan(ScriptedPlanner("PR-01"), "S3-unknown-a")
    assert run.rationale == "추천 이유"
    assert run.explain_input and "## 검증된 근거" in run.explain_input
    assert {e["key"] for e in run.evidence} == set(run.cited)  # 카드에 실린 근거를 설명했다
    assert all({"authors", "year", "outcomes", "flags"} <= e.keys() for e in run.evidence)
    assert run.composition and set(run.grades) == {
        o.value for o in GOAL_OUTCOMES[CareGoal.S3].primary
    }
    assert run.factors is None  # 대역은 Factor를 고르지 않는다


def test_a_rejected_first_choice_fails_first_try_only(fake_backends):
    run = plan(ScriptedPlanner("PR-06", "PR-01"), "S3-unknown-a")  # PR-06은 S3 후보가 아니다
    assert run.calls == 2 and run.protocol_id == "PR-01"
    assert not run.checks["first_try"] and run.checks["constraints"]


def test_change_activity_round_excludes_the_previous_activity(fake_backends):
    run = plan(ScriptedPlanner("PR-10"), "S3-fatigue-a", previous="PR-11")
    assert run.round == 1 and "PR-11" not in run.candidates
    assert run.checks["changed"] and run.checks["constraints"]


def test_summary_counts_each_check_over_the_runs_it_applies_to(fake_backends):
    runs = [
        plan(ScriptedPlanner("PR-01"), "S3-unknown-a"),
        plan(ScriptedPlanner("PR-10"), "S3-fatigue-a", previous="PR-11"),
    ]
    summary = plan_eval.summarize(runs)
    assert summary["checks"]["planned"] == [2, 2]
    assert summary["checks"]["changed"] == [1, 1]  # 활동 변경 라운드만
    assert summary["with_evidence"] == [2, 2]
    # fixture에 PR-10 자기 근거가 없어 같은 매체(영상) 리뷰 P01 · P04를 빌려 온다
    assert runs[1].borrowed == ["P01", "P04"]
    assert summary["borrowed"] == ["S3-fatigue-a PR-10"] and summary["empty_cards"] == []
    assert summary["chosen"] == {"S3 PR-01": 1}
    # 세션 ID가 없으면 등급 순 — S3 · 미상의 맨 앞은 PR-10인데 대본이 PR-01(2번째)을 골랐고,
    # 변경 라운드(PR-11을 뺐다)의 PR-10은 맨 앞이다
    assert summary["first_in_order"] == [1, 2]
    assert "첫 계획에서 고른 활동 1종: S3 PR-01 ×1" in plan_eval.format_summary(summary)


def test_a_session_id_orders_the_candidates_like_production(fake_backends):
    run = asyncio.run(
        plan_eval.plan_once(
            ScriptedPlanner("PR-04"), SCENARIOS["S3-unknown-a"], session_id="plan-eval:S3-unknown-a"
        )
    )
    assert run.protocol_id == "PR-04" and run.position is not None
    assert all(run.checks.values()), run.checks
