"""계약 2 — 활동 노드 JSON의 근거 연결 부분. data/protocols/PR-*.json 전부를 파싱한다.

data/protocols/는 계약을 통과하는 운영 파일(수정 사유는 JSON note에)이고,
tests/fixtures/protocols_raw/는 활동 노드 담당의 원본이다.
원본의 알려진 불일치가 고쳐지면 test_raw_pr01_still_has_known_grade_mismatch가 실패하므로 그때
raw 사본을 지우거나 갱신한다.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.evidence import InterventionOrder
from app.schemas.protocol import (
    EvidenceRole,
    Grade,
    ProtocolEvidenceRef,
    ProtocolEvidenceSection,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PROTOCOL_DIR = FIXTURES.parent.parent / "data" / "protocols"
PROTOCOLS = sorted(PROTOCOL_DIR.glob("PR-*.json"))


@pytest.mark.parametrize("path", PROTOCOLS, ids=[p.stem for p in PROTOCOLS])
def test_protocol_fixture_parses(path):
    section = ProtocolEvidenceSection.model_validate_json(path.read_text(encoding="utf-8"))
    assert section.protocol_id == path.stem
    assert section.paper_id_scheme == "RC_MASTER"


def test_raw_pr01_still_has_known_grade_mismatch():
    raw = (FIXTURES / "protocols_raw" / "PR-01.json").read_text(encoding="utf-8")
    with pytest.raises(ValidationError, match="SUBJECTIVE_STRESS: P24-E1"):
        ProtocolEvidenceSection.model_validate_json(raw)


def pr01() -> dict:
    return json.loads((PROTOCOL_DIR / "PR-01.json").read_text(encoding="utf-8"))


def test_pr01_labels_are_normalized():
    section = ProtocolEvidenceSection.model_validate(pr01())
    by_key = {ref.key: ref for ref in section.evidence}
    assert by_key["P24-E1"].orders == [InterventionOrder.POST_STRESS]
    assert by_key["N24"].role is EvidenceRole.META and by_key["N24"].orders == []
    assert section.intervention_order == [
        InterventionOrder.POST_STRESS,
        InterventionOrder.POST_FATIGUE,
    ]
    assert section.grades["PHYSIO"].grade is Grade.D


def test_duplicate_evidence_key_rejected():
    doc = pr01()
    doc["evidence"].append(doc["evidence"][0])
    with pytest.raises(ValidationError, match="두 번"):
        ProtocolEvidenceSection.model_validate(doc)


def test_pending_grade_label():
    doc = pr01()
    doc["grades"]["PHYSIO"] = {"grade": "원문 확인", "support": [], "conflicts": ["P24-E1"]}
    assert ProtocolEvidenceSection.model_validate(doc).grades["PHYSIO"].grade is Grade.PENDING


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"grade": "D", "support": [], "conflicts": []}, "등급 D인데 conflicts"),
        ({"grade": "B", "support": [], "conflicts": ["P24-E1"]}, "등급 B인데 support"),
        ({"grade": "—", "support": ["P24-E1"], "conflicts": []}, "등급 —인데 근거"),
        ({"grade": "B", "support": ["P99-E1"], "conflicts": []}, "evidence\\[\\]에 없다"),
        ({"grade": "B", "support": ["P03-E1"], "conflicts": []}, "측정하지 않았다"),
    ],
)
def test_grade_invariants(entry, message):
    doc = pr01()
    doc["grades"]["PHYSIO"] = entry
    with pytest.raises(ValidationError, match=message):
        ProtocolEvidenceSection.model_validate(doc)


def test_primary_must_be_experimental_arm():
    ref = pr01()["evidence"][0]
    with pytest.raises(ValidationError, match="PRIMARY는 무작위·준실험만"):
        ProtocolEvidenceRef.model_validate({**ref, "design": "관찰·설문·질적 연구"})
    with pytest.raises(ValidationError, match="experiment_id가 있는 arm"):
        ProtocolEvidenceRef.model_validate({**ref, "experiment_id": None})


def test_meta_has_no_experiment_id():
    ref = next(e for e in pr01()["evidence"] if e["role"] == "META")
    with pytest.raises(ValidationError, match="META는 experiment_id"):
        ProtocolEvidenceRef.model_validate({**ref, "experiment_id": f"{ref['paper_id']}-E1"})


def test_composite_order_label_in_evidence_entry():
    ref = ProtocolEvidenceRef.model_validate(
        {**pr01()["evidence"][0], "order": "POST_FATIGUE / 무관"}
    )
    assert ref.orders == [InterventionOrder.POST_FATIGUE, InterventionOrder.NO_INDUCTION]


def test_stage_module_and_out_of_corpus_node_allowed():
    doc = pr01()
    doc.update(protocol_id="PR-21", media=[], evidence=[])
    doc["grades"] = {k: {"grade": "—", "support": [], "conflicts": []} for k in doc["grades"]}
    section = ProtocolEvidenceSection.model_validate(doc)
    assert section.media == [] and section.evidence == []


def test_grade_keys_must_be_the_five_graded_outcomes():
    doc = pr01()
    del doc["grades"]["PHYSIO"]
    with pytest.raises(ValidationError, match="grades 키는"):
        ProtocolEvidenceSection.model_validate(doc)


def test_scheme_must_be_rc_master():
    doc = pr01()
    doc["paper_id_scheme"] = "SHEET_36"
    with pytest.raises(ValidationError, match="RC_MASTER"):
        ProtocolEvidenceSection.model_validate(doc)
