"""validation.validate를 실제 Postgres로 확인한다. TEST_DATABASE_URL이 없으면 건너뛴다(CI에는
DB가 없다).

fixture DB(tests/evidence_db.py)에서 읽은 레코드와 상충 후보로 내린 판정이, fixture 전체를 후보로
쓰는 가짜 검증(tests/agent/fakes.py)과 같아야 한다 — DB에서 먼저 거른 상충 후보가 judge()의 규칙에
걸릴 레코드를 빠뜨리지 않는다는 뜻이다.
"""

import asyncio

import pytest

from app.agent import validation
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder, Medium, Outcome
from app.schemas.retrieval import Check, Flag
from tests.agent import fakes
from tests.evidence_db import fixture_records, requires_db

pytestmark = requires_db

S1_SCRIPT = {  # 플래너 흐름 테스트의 대본 — AI 생성 이미지, 없는 ID 포함
    "evidence_record_ids": [102, 112, 999],
    "goal": CareGoal.S1,
    "media": [Medium.AI_IMAGE],
    "user_order": InterventionOrder.POST_STRESS,
}


def run_validate(**kwargs):
    async def run():
        async with fixture_records() as pool:
            return await validation.validate(pool, **kwargs)

    return asyncio.run(run())


CASES = [
    S1_SCRIPT,
    {"evidence_record_ids": [102], "goal": CareGoal.S2, "media": [Medium.STATIC_IMAGE]},
    {"evidence_record_ids": [102], "goal": CareGoal.S2, "media": [Medium.AI_IMAGE]},
    # 시점은 프로토콜(PR-01: POST_STRESS · POST_FATIGUE)에서 읽는다(P05-E2는 FULL_TEXT)
    {
        "evidence_record_ids": [122, 132, 142, 162, 172],
        "goal": CareGoal.S3,
        "media": [Medium.STATIC_IMAGE],
        "protocol_id": "PR-01",
    },
    {
        "evidence_record_ids": [152, 151],
        "goal": CareGoal.S2,
        "media": [Medium.VIDEO],
        "user_order": InterventionOrder.POST_FATIGUE,
    },
    # 같은 ID를 두 번 주면 판정도 두 번. 연구 매체와 일부만 겹친다(PARTIAL)
    {
        "evidence_record_ids": [101, 101],
        "goal": CareGoal.S1,
        "media": [Medium.STATIC_IMAGE, Medium.MINDFULNESS],
    },
    {"evidence_record_ids": [], "goal": CareGoal.S1, "media": [Medium.STATIC_IMAGE]},
]


@pytest.mark.parametrize("kwargs", CASES)
def test_same_verdicts_as_fake_validate(kwargs):
    verdicts = run_validate(**kwargs)
    expected = asyncio.run(fakes.fake_validate(None, **kwargs))
    assert [v.model_dump() for v in verdicts] == [v.model_dump() for v in expected]


def test_conflicts_and_missing_ids_come_from_db():
    p24, p33, missing = run_validate(**S1_SCRIPT)
    assert p24.usable and p24.supported_outcomes == [Outcome.RUMINATION]
    assert set(p24.flags) == {Flag.CONFLICTING_EVIDENCE, Flag.ADAPTED_STIMULUS}
    conflict = next(c for c in p24.checks if c.check is Check.CONFLICT_SCAN)
    assert conflict.related_record_ids == [111]  # P33-E1의 첫 chunk
    assert p33.usable and Flag.NOT_SUPPORTING in p33.flags
    assert missing.evidence_record_id == 999 and not missing.usable


def test_ai_image_is_flagged_not_blocked():
    (static,) = run_validate(
        evidence_record_ids=[102], goal=CareGoal.S2, media=[Medium.STATIC_IMAGE]
    )
    (generated,) = run_validate(
        evidence_record_ids=[102], goal=CareGoal.S2, media=[Medium.AI_IMAGE]
    )
    assert static.usable and static.supported_outcomes == [Outcome.AFFECT]
    assert Flag.ADAPTED_STIMULUS not in static.flags
    assert generated.usable and Flag.ADAPTED_STIMULUS in generated.flags
