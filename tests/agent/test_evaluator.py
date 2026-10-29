"""③ Outcome Evaluator — 목표별 임시 판정 규칙(evaluator.IMPROVEMENT_RULE)."""

import pytest

from app.agent.nodes.evaluator import evaluate
from app.agent.state import IntakeResult, SessionReport
from app.schemas.care import CareGoal
from app.schemas.measure import InstrumentId, Measurement, Timepoint


def measured(instrument_id: InstrumentId, timepoint: Timepoint, scores: dict) -> Measurement:
    round_no = 0 if timepoint is Timepoint.PRE else 1
    return Measurement(
        instrument_id=instrument_id,
        timepoint=timepoint,
        round=round_no,
        answers=scores,
        scores=scores,
    )


def improved(goal: CareGoal, instrument_id: InstrumentId, before: dict, after: dict) -> bool | None:
    state = {
        "intake": IntakeResult(goal=goal),
        "round_pre": [measured(instrument_id, Timepoint.PRE, before)],
        "post": [measured(instrument_id, Timepoint.POST, after)],
        "report": SessionReport(completed=True),
    }
    return evaluate(state)["evaluation"].improved


@pytest.mark.parametrize(("after", "expected"), [(70, True), (71, False), (80, False), (95, False)])
def test_s3_needs_fatigue_to_drop_at_least_ten_points(after, expected):
    """MFVAS는 숫자를 보여 주지 않는 0–100 슬라이더라 몇 점 차는 손 위치 오차로 본다."""
    before = {"fatigue": 80}
    assert improved(CareGoal.S3, InstrumentId.MFVAS, before, {"fatigue": after}) is expected


@pytest.mark.parametrize(
    ("goal", "after", "expected"),
    [
        (CareGoal.S1, {"valence": 4, "arousal": 7}, True),
        (CareGoal.S1, {"valence": 3, "arousal": 4}, False),
        (CareGoal.S2, {"valence": 3, "arousal": 6}, True),
        (CareGoal.S2, {"valence": 5, "arousal": 7}, False),
    ],
)
def test_sam_goals_count_a_single_step(goal, after, expected):
    """SAM(1–9)은 한 칸이 최소 변화다."""
    before = {"valence": 3, "arousal": 7}
    assert improved(goal, InstrumentId.SAM, before, after) is expected
