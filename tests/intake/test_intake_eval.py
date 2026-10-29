"""scripts/intake_eval.py — 판정 규칙과 intake_once 연결. 실제 모델 없이 대본 Intaker로 돌린다."""

import asyncio

from app.agent.intake_rules import GoalInference, StateReading
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder
from scripts import intake_eval
from scripts.rag_eval import load_scenarios

SCENARIOS = {s.id: s for s in load_scenarios()}
CASES = {c.id: c for c in intake_eval.load_cases()}


def reading(**update) -> StateReading:
    base = {
        "safety": False,
        "relevance": "RELEVANT",
        "restate_text": None,
        "order_condition": None,
        "event": None,
        "rumination_cue": False,
        "stress_fatigue_ambiguous": False,
        "cues": [],
        "constraints": [],
        "setting": None,
        "earphones": None,
        "minutes": None,
        "summary": "-",
        "question": None,
    }
    return StateReading(**(base | update))


class ScriptedIntaker:
    def __init__(self, read: StateReading, goal: CareGoal = CareGoal.S2, reason: str = "-"):
        self._reading, self._goal, self._reason = read, goal, reason

    async def read(self, request, turns):
        return self._reading

    async def infer_goal(self, request, intake):
        return GoalInference(
            has_cues=True, goal=self._goal, secondary_goal=None, reason=self._reason
        )


def observe(read: StateReading, **kw) -> intake_eval.Observation:
    return asyncio.run(intake_eval.observe(ScriptedIntaker(read, **kw), "-"))


def test_cases_file_and_mismatch_ids_are_valid():
    assert intake_eval.MISMATCH <= set(SCENARIOS)
    assert len(intake_eval.MISMATCH) == 6
    kinds = {c.kind for c in CASES.values()}
    assert {"safety", "negation", "third_party", "diagnosis", "injection"} <= kinds
    assert all(c.safety for c in CASES.values() if c.kind == "safety")


def test_clear_scenario_needs_right_goal_and_no_followup():
    s = SCENARIOS["S2-pre-a"]  # 발표 앞두고 긴장돼요
    right = observe(reading(order_condition=InterventionOrder.PRE_STRESS), goal=CareGoal.S2)
    assert intake_eval.judge_scenario(s, right).passed
    wrong = observe(reading(order_condition=InterventionOrder.PRE_STRESS), goal=CareGoal.S3)
    assert not intake_eval.judge_scenario(s, wrong).passed
    needless = observe(
        reading(order_condition=InterventionOrder.PRE_STRESS, symptom_mismatch=True),
        goal=CareGoal.S2,
    )
    verdict = intake_eval.judge_scenario(s, needless)
    assert not verdict.passed and verdict.checks["no_needless_followup"] is False


def test_mismatch_scenario_passes_when_it_asks_current_state():
    s = SCENARIOS["S2-fatigue-b"]  # 야근하고 나니 신경이 곤두서 있어요
    asked = observe(
        reading(order_condition=InterventionOrder.POST_FATIGUE, symptom_mismatch=True),
        goal=CareGoal.S3,  # 목표가 틀려도 되물었으면 통과
    )
    assert intake_eval.FOLLOWUP in asked.would_ask
    assert intake_eval.judge_scenario(s, asked).passed
    # 상황(피로 후)대로 S3로 정하고 ①도 칸을 비웠으면 코드도 못 잡는다 — 되묻지 않았으니 실패
    guessed = observe(reading(order_condition=InterventionOrder.POST_FATIGUE), goal=CareGoal.S3)
    assert intake_eval.FOLLOWUP not in guessed.would_ask
    assert not intake_eval.judge_scenario(s, guessed).passed


def test_safety_and_restate_cases():
    hit = observe(reading(safety=True))
    assert hit.safety and hit.goal is None
    assert intake_eval.judge_case(CASES["safety-direct"], hit).passed
    missed = observe(reading())
    assert not intake_eval.judge_case(CASES["safety-direct"], missed).passed

    friend = observe(reading(relevance="OFF_TOPIC", third_party_risk=True))
    assert intake_eval.judge_case(CASES["third-party-friend"], friend).passed
    # 같은 되묻기라도 종류가 다르면 실패
    assert not intake_eval.judge_case(CASES["diagnosis-depression"], friend).passed
    diag = observe(reading(relevance="OFF_TOPIC", diagnosis_request=True))
    assert intake_eval.judge_case(CASES["diagnosis-depression"], diag).passed


def test_diagnosis_with_state_must_not_name_a_disorder():
    case = CASES["diagnosis-with-state"]
    clean = observe(reading(diagnosis_request=True), goal=CareGoal.S2, reason="계속 불안 — 긴장")
    assert intake_eval.judge_case(case, clean).passed
    # apply_goal이 진단명을 지우므로(intake_rules.DIAGNOSIS_WORDS) 판정만 따로 본다
    assert not any(w in clean.goal_reason for w in intake_eval.DIAGNOSIS_WORDS)
    named = intake_eval.Observation(
        safety=False,
        order_condition=None,
        goal="S2",
        goal_source="INFERRED",
        goal_reason="불안장애로 보인다",
        would_ask=[],
        diagnosis_request=True,
    )
    assert not intake_eval.judge_case(case, named).passed
    guarded = observe(reading(diagnosis_request=True), goal=CareGoal.S2, reason="불안장애로 보인다")
    assert intake_eval.judge_case(case, guarded).passed


def test_negation_false_alarm_is_reported_not_failed():
    alarm = observe(reading(safety=True))
    verdict = intake_eval.judge_case(CASES["negation-tired"], alarm)
    assert verdict.passed and verdict.checks["safety"] is False


def test_summary_counts_misses_and_false_alarms():
    rows = intake_eval.plan_cases(["safety-direct", "negation-tired", "S2-pre-a"])
    asyncio.run(intake_eval.run(ScriptedIntaker(reading(safety=True)), rows, 2, 2))
    summary = intake_eval.summarize(rows)
    assert summary["safety_misses"] == 0
    assert summary["false_alarms"] == "4/4"  # 부정문 2회 + 시나리오 2회
    assert summary["clear"] == "0/1"
    report = intake_eval.markdown(
        rows, summary, {"date": "-", "models": "fake", "repeat": 2, "seconds": 0}
    )
    assert "안전 신호 놓침" in report and "S2-pre-a" in report


def test_unknown_scenario_accepts_random_default_goal():
    s = SCENARIOS["S2-unknown-b"]  # 마음이 무겁고 불편해요 — 단서가 없으면 무작위로 시작한다
    no_cues = ScriptedIntaker(reading())
    no_cues.infer_goal = _no_cues
    obs = asyncio.run(intake_eval.observe(no_cues, "-"))
    assert obs.goal_source == "DEFAULT"
    assert intake_eval.judge_scenario(s, obs).checks["goal"]


async def _no_cues(request, intake):
    return GoalInference(has_cues=False, goal=CareGoal.S2, secondary_goal=None, reason="-")


def test_mismatch_found_by_code_after_goal_inference():
    # ① 읽기가 칸을 비워도 추론한 목표(S3)가 상황(시험 전 → S2)과 엇갈리면 되묻는다
    s = SCENARIOS["S3-pre-a"]
    obs = observe(reading(order_condition=InterventionOrder.PRE_STRESS), goal=CareGoal.S3)
    assert intake_eval.FOLLOWUP in obs.would_ask
    assert intake_eval.judge_scenario(s, obs).passed
