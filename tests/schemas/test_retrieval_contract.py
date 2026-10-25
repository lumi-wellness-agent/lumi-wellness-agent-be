"""계약 3 — 도구 출력 모델(EvidenceHit · EvidenceVerdict)의 fixture 파싱과 일관성 규칙."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.care import CareGoal
from app.schemas.evidence import GRADED_OUTCOMES, InterventionOrder, Outcome
from app.schemas.retrieval import (
    BLOCKING_CHECKS,
    GOAL_OUTCOMES,
    ORDER_COMPATIBILITY,
    Check,
    CheckResult,
    EvidenceHit,
    EvidenceVerdict,
    Flag,
    MediaMatch,
)

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "evidence_hits.json"


def test_evidence_hits_fixture_parses():
    hits = [EvidenceHit.model_validate(h) for h in json.loads(FIXTURE.read_text(encoding="utf-8"))]
    ids = [h.evidence_record_id for h in hits]
    assert len(hits) == 16 and len(set(ids)) == 16
    assert all(h.excerpt.startswith("[fixture]") for h in hits)  # 원문 적재 전 자리표시자
    assert {h.paper.paper_id for h in hits} == {
        "P24",
        "P33",
        "P03",
        "P05",
        "N19",
        "N24",
        "P01",
        "P04",
    }


def test_check_order_and_blocking_set():
    assert [c.value for c in Check] == [
        "SOURCE_EXISTS",
        "INTERVENTION_MATCH",
        "POPULATION_FIT",
        "OUTCOME_MEASURED",
        "CONFLICT_SCAN",
        "STIMULUS_FIDELITY",
    ]
    assert BLOCKING_CHECKS == {
        Check.SOURCE_EXISTS,
        Check.INTERVENTION_MATCH,
        Check.OUTCOME_MEASURED,
    }


def test_goal_outcomes_cover_goals_with_graded_outcomes():
    assert set(GOAL_OUTCOMES) == set(CareGoal)
    for spec in GOAL_OUTCOMES.values():
        assert spec.primary and set(spec.primary + spec.secondary) <= set(GRADED_OUTCOMES)


def test_order_compatibility_policy():
    assert set(ORDER_COMPATIBILITY) == set(InterventionOrder)
    for allowed in ORDER_COMPATIBILITY.values():
        assert InterventionOrder.NO_INDUCTION in allowed
    assert InterventionOrder.PRE_STRESS not in ORDER_COMPATIBILITY[InterventionOrder.POST_STRESS]


def _checks(passed: dict[Check, bool | None] | None = None) -> list[CheckResult]:
    passed = passed or {}
    return [CheckResult(check=c, passed=passed.get(c, True), reason="-") for c in Check]


def test_verdict_consistency_ok():
    EvidenceVerdict(
        evidence_record_id=1,
        usable=True,
        supports_goal=True,
        supported_outcomes=[Outcome.AFFECT],
        media_match=MediaMatch.EXACT,
        checks=_checks({Check.STIMULUS_FIDELITY: False}),
        flags=[Flag.ADAPTED_STIMULUS],
    )
    EvidenceVerdict(
        evidence_record_id=999,
        usable=False,
        supports_goal=False,
        media_match=MediaMatch.NONE,
        checks=_checks({c: (False if c is Check.SOURCE_EXISTS else None) for c in Check}),
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            dict(
                usable=True,
                supports_goal=False,
                checks=_checks({Check.OUTCOME_MEASURED: False}),
                flags=[Flag.NOT_SUPPORTING],
            ),
            "usable은 차단 검사",
        ),
        (dict(usable=True, supports_goal=True, checks=_checks()), "supported_outcomes"),
        (dict(usable=True, supports_goal=False, checks=_checks()), "NOT_SUPPORTING"),
        (
            dict(
                usable=True,
                supports_goal=True,
                supported_outcomes=[Outcome.AFFECT],
                checks=list(reversed(_checks())),
            ),
            "Check 순서대로",
        ),
        (
            dict(
                usable=True,
                supports_goal=True,
                supported_outcomes=[Outcome.AFFECT],
                checks=[_checks()[0]] * 6,
            ),
            "Check 순서대로",
        ),
    ],
)
def test_verdict_inconsistencies_rejected(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        EvidenceVerdict(evidence_record_id=1, media_match=MediaMatch.EXACT, **kwargs)
