"""data/protocols/PR-*.json 21개 — 본문 계약(CareProtocol)과 후보 조회. 활동 노드 담당의 합격 기준.

근거 연결 부분(evidence · grades)은 tests/schemas/test_protocol_contract.py가 본다.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import protocols
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder
from app.schemas.protocol import CareProtocol, Composition, StepType

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "data" / "protocols").glob("PR-*.json"))
LOADED = protocols.load_protocols()
EXPECTED_IDS = [f"PR-{n:02d}" for n in range(1, 23) if n != 13]


def test_twenty_one_protocols_match_the_workbook():
    assert [p.stem for p in FILES] == EXPECTED_IDS
    assert list(LOADED) == EXPECTED_IDS


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_protocol_file_parses_as_care_protocol(path):
    protocol = CareProtocol.model_validate_json(path.read_text(encoding="utf-8"))
    assert protocol.protocol_id == path.stem
    assert protocol.steps and protocol.duration_minutes.default > 0
    for step in protocol.steps:
        assert set(step.slots) <= protocol.content_spec.slot_types
    for relation in protocol.compatible_with + protocol.exclusive_with:
        assert relation.protocol_id in LOADED, f"{path.stem}: {relation.protocol_id} 없음"


def test_compositions_follow_the_sheet():
    by_composition = {}
    for p in LOADED.values():
        by_composition.setdefault(p.composition, []).append(p.protocol_id)
    assert by_composition[Composition.VALIDATED_MULTIMODAL] == ["PR-02", "PR-10", "PR-12"]
    assert by_composition[Composition.EXPERIMENTAL] == ["PR-15", "PR-21", "PR-22"]
    assert by_composition[Composition.STAGE_MODULE] == ["PR-16"]
    assert len(by_composition[Composition.SINGLE]) == 14


def test_stage_module_is_notice_only_and_never_a_candidate():
    module = LOADED["PR-16"]
    assert module.record_type == "stage_module" and module.media == []
    assert [s.type for s in module.steps] == [StepType.NOTICE]
    for goal in CareGoal:
        assert "PR-16" not in {p.protocol_id for p in protocols.candidates(goal)}


def test_every_goal_has_candidates_with_primary_goal_first():
    assert [p.protocol_id for p in protocols.candidates(CareGoal.S1)][:3] == [
        "PR-09",
        "PR-15",
        "PR-21",
    ]
    s2 = [p.protocol_id for p in protocols.candidates(CareGoal.S2)]
    assert s2[:2] == ["PR-01", "PR-02"] and s2[-2:] == ["PR-10", "PR-11"]  # 부 목표가 뒤
    assert [p.protocol_id for p in protocols.candidates(CareGoal.S3)] == [
        "PR-10",
        "PR-11",
        "PR-01",
        "PR-04",
    ]


def test_candidates_filter_by_order_and_minutes():
    pre_stress = protocols.candidates(CareGoal.S2, order=InterventionOrder.PRE_STRESS)
    ids = {p.protocol_id for p in pre_stress}
    assert "PR-03" in ids and "PR-06" in ids  # PRE_STRESS 전용 · 무관(일상)
    assert "PR-01" not in ids and "PR-02" not in ids  # POST_* 전용
    short = {p.protocol_id for p in protocols.candidates(CareGoal.S2, minutes=5)}
    assert "PR-06" in short and "PR-07" not in short  # 20분 PMR은 5분에 안 맞는다


def test_compact_is_what_the_planner_reads():
    compact = protocols.compact(LOADED["PR-06"])
    assert compact["protocol_id"] == "PR-06" and compact["goal"] == "S2"
    assert compact["duration_minutes"] == {"default": 5.0, "min": 5.0, "max": 20.0}
    assert compact["grades"]["SUBJECTIVE_STRESS"] == "A"
    assert "evidence" not in compact and "content_spec" not in compact


def test_body_invariants():
    doc = json.loads((ROOT / "data" / "protocols" / "PR-06.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="content_spec.slots에 없다"):
        CareProtocol.model_validate({**doc, "content_spec": {"slots": []}})
    with pytest.raises(ValidationError, match="steps.order"):
        CareProtocol.model_validate({**doc, "steps": [{**doc["steps"][0], "order": 2}]})
    with pytest.raises(ValidationError, match="record_type"):
        CareProtocol.model_validate({**doc, "composition": "STAGE_MODULE"})
    with pytest.raises(ValidationError, match="default가 max"):
        CareProtocol.model_validate(
            {**doc, "duration_minutes": {"default": 30, "min": 5, "max": 20}}
        )


def test_drafts_are_marked_for_review():
    for p in LOADED.values():
        assert p.review.status == "DRAFT"
        assert p.guide.intro_text or p.guide.intro_text_ref, p.protocol_id
