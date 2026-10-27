"""validation의 DB 없는 부분 — judge() 규칙(fixture 레코드로 검사 6개와 플래그), 시점·상충 후보
조건, 풀이 없을 때. DB 조회는 tests/agent/test_validation_db.py가 본다.
"""

import asyncio
import logging

import pytest

from app.agent.validation import (
    conflict_params,
    judge,
    media_match,
    missing_verdict,
    resolve_orders,
    validate,
)
from app.schemas.care import CareGoal
from app.schemas.evidence import ClinicalStatus, InterventionOrder, Medium, Outcome, Verification
from app.schemas.retrieval import Check, Flag, MediaMatch
from tests.agent import fakes

HITS = fakes.load_hits()
BY_ID = {hit.evidence_record_id: hit for hit in HITS}
P24_RESULTS, P33_RESULTS, P03_RESULTS, P05_RESULTS, N24_RESULTS = 102, 112, 122, 132, 152
POST_STRESS = [InterventionOrder.POST_STRESS]


def check(verdict, name: Check):
    return next(c for c in verdict.checks if c.check is name)


def test_direct_support_with_conflict_flag():
    verdict = judge(
        BY_ID[P24_RESULTS],
        goal=CareGoal.S1,
        media=[Medium.STATIC_IMAGE],
        orders=POST_STRESS,
        candidates=HITS,
    )
    assert verdict.usable and verdict.supports_goal
    assert verdict.supported_outcomes == [Outcome.RUMINATION]
    assert verdict.media_match is MediaMatch.EXACT
    assert verdict.flags == [Flag.CONFLICTING_EVIDENCE]
    conflict = check(verdict, Check.CONFLICT_SCAN)
    assert conflict.related_record_ids == [111]  # P33-E1은 chunk가 둘이어도 arm 하나로 센다
    assert "P33-E1" in conflict.reason
    assert [r.outcome for r in verdict.goal_results] == [
        Outcome.AFFECT,
        Outcome.AFFECT,
        Outcome.RUMINATION,
    ]


def test_ai_image_is_flagged_not_blocked():
    verdict = judge(
        BY_ID[P24_RESULTS], goal=CareGoal.S1, media=[Medium.AI_IMAGE], orders=POST_STRESS
    )
    assert verdict.usable and verdict.supports_goal
    assert verdict.media_match is MediaMatch.EXACT  # AI_IMAGE는 STATIC_IMAGE로 비교
    assert verdict.flags == [Flag.ADAPTED_STIMULUS]
    assert check(verdict, Check.STIMULUS_FIDELITY).passed is False
    assert "AI_IMAGE" in check(verdict, Check.STIMULUS_FIDELITY).reason


def test_secondary_source_is_blocked():
    # P05-E2는 2026-10-27 쪽 이미지 대조로 FULL_TEXT가 됐다 — 같은 레코드를 2차 자료로 바꿔 본다
    hit = BY_ID[P05_RESULTS]
    metadata = hit.metadata.model_copy(update={"verification": Verification.SECONDARY})
    secondary = hit.model_copy(update={"metadata": metadata})
    verdict = judge(secondary, goal=CareGoal.S3, media=[Medium.STATIC_IMAGE])
    assert check(verdict, Check.SOURCE_EXISTS).passed is False
    assert not verdict.usable
    assert verdict.supports_goal  # 지지는 하지만 인용할 수 없다
    assert Flag.NOT_SUPPORTING not in verdict.flags


def test_incompatible_order_is_blocked():
    verdict = judge(
        BY_ID[P24_RESULTS],
        goal=CareGoal.S1,
        media=[Medium.STATIC_IMAGE],
        orders=[InterventionOrder.PRE_STRESS],
    )
    assert check(verdict, Check.INTERVENTION_MATCH).passed is False
    assert not verdict.usable


def test_unspecified_order_checks_media_only():
    verdict = judge(BY_ID[P24_RESULTS], goal=CareGoal.S1, media=[Medium.STATIC_IMAGE])
    assert verdict.usable
    assert "순서 미지정" in check(verdict, Check.INTERVENTION_MATCH).reason


def test_no_induction_study_counts_for_stressed_user():
    hit = BY_ID[P24_RESULTS]
    meta = hit.metadata.model_copy(update={"intervention_orders": [InterventionOrder.NO_INDUCTION]})
    verdict = judge(
        hit.model_copy(update={"metadata": meta}),
        goal=CareGoal.S1,
        media=[Medium.STATIC_IMAGE],
        orders=POST_STRESS,
    )
    assert check(verdict, Check.INTERVENTION_MATCH).passed is True


def test_partial_media_match_is_usable_but_adapted():
    verdict = judge(BY_ID[N24_RESULTS], goal=CareGoal.S2, media=[Medium.STATIC_IMAGE])
    assert verdict.media_match is MediaMatch.PARTIAL
    assert verdict.usable and verdict.supported_outcomes == [Outcome.SUBJECTIVE_STRESS]
    assert verdict.flags == [Flag.ADAPTED_STIMULUS]


def test_disjoint_media_is_blocked():
    verdict = judge(BY_ID[P24_RESULTS], goal=CareGoal.S1, media=[Medium.BREATHING])
    assert verdict.media_match is MediaMatch.NONE
    assert check(verdict, Check.INTERVENTION_MATCH).passed is False
    assert not verdict.usable


def test_not_significant_result_is_usable_but_not_supporting():
    verdict = judge(
        BY_ID[P33_RESULTS],
        goal=CareGoal.S1,
        media=[Medium.STATIC_IMAGE],
        orders=POST_STRESS,
        candidates=HITS,
    )
    assert verdict.usable and not verdict.supports_goal
    assert verdict.flags == [Flag.NOT_SUPPORTING]
    assert check(verdict, Check.CONFLICT_SCAN).passed is True  # P24의 POSITIVE는 상충이 아니다


def test_unmeasured_goal_outcome_is_blocked():
    verdict = judge(BY_ID[P03_RESULTS], goal=CareGoal.S1, media=[Medium.STATIC_IMAGE])
    assert check(verdict, Check.OUTCOME_MEASURED).passed is False
    assert not verdict.usable
    assert "ATTENTION" in check(verdict, Check.OUTCOME_MEASURED).reason


def test_clinical_sample_is_flagged_not_blocked():
    hit = BY_ID[P24_RESULTS]
    meta = hit.metadata.model_copy(update={"clinical_status": ClinicalStatus.CLINICAL})
    verdict = judge(
        hit.model_copy(update={"metadata": meta}), goal=CareGoal.S1, media=[Medium.STATIC_IMAGE]
    )
    assert verdict.usable
    assert Flag.POPULATION_MISMATCH in verdict.flags


def test_missing_record_verdict():
    verdict = missing_verdict(999)
    assert not verdict.usable and not verdict.supports_goal
    assert [c.passed for c in verdict.checks] == [False, None, None, None, None, None]


@pytest.mark.parametrize(
    ("delivered", "studied", "expected"),
    [
        ([Medium.AI_IMAGE], [Medium.STATIC_IMAGE], MediaMatch.EXACT),
        ([Medium.NATURE_SOUND], [Medium.VR, Medium.NATURE_SOUND], MediaMatch.PARTIAL),
        ([Medium.BREATHING], [Medium.STATIC_IMAGE], MediaMatch.NONE),
        ([Medium.STATIC_IMAGE], [], MediaMatch.NONE),
    ],
)
def test_media_match(delivered, studied, expected):
    assert media_match(delivered, studied) is expected


def test_orders_come_from_user_then_protocol():
    assert resolve_orders(InterventionOrder.POST_STRESS, "PR-06") == POST_STRESS
    assert resolve_orders(None, "PR-01") == [
        InterventionOrder.POST_STRESS,
        InterventionOrder.POST_FATIGUE,
    ]
    assert resolve_orders(None, "PR-99") == []  # 검수 목록에 없는 ID
    assert resolve_orders(None, None) == []


def test_conflict_params_mirror_judge_rule():
    params = conflict_params(CareGoal.S1, [Medium.AI_IMAGE, Medium.STATIC_IMAGE])
    assert params == {
        "types": [{"record_type": "ARM"}, {"record_type": "SYNTHESIS"}],
        "media": [{"media": ["STATIC_IMAGE"]}],  # AI_IMAGE는 연구 매체로 바꿔 찾는다
        "results": [
            {"results": [{"outcome": "RUMINATION", "effect": "NEGATIVE"}]},
            {"results": [{"outcome": "RUMINATION", "effect": "NOT_SIGNIFICANT"}]},
        ],
    }


def test_validate_without_pool_cannot_confirm_sources(caplog):
    with caplog.at_level(logging.WARNING, logger="app.agent.validation"):
        verdicts = asyncio.run(
            validate(
                None,
                evidence_record_ids=[P24_RESULTS, 999],
                goal=CareGoal.S1,
                media=[Medium.STATIC_IMAGE],
            )
        )
    assert [v.evidence_record_id for v in verdicts] == [P24_RESULTS, 999]
    assert not any(v.usable for v in verdicts)
    assert "DATABASE_URL" in caplog.text


def test_validate_without_ids_skips_db():
    # 풀 대신 아무 객체를 넣는다 — DB를 쓰려 하면 AttributeError로 실패한다
    verdicts = asyncio.run(
        validate(object(), evidence_record_ids=[], goal=CareGoal.S1, media=[Medium.STATIC_IMAGE])
    )
    assert verdicts == []
