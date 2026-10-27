"""근거 arm 시트 → 적재 레코드(scripts/seed_evidence.py). 워크북이 없으면 건너뛴다(CI에는 없다).

워크북은 이 저장소와 나란히 있는 Research_paper 폴더에 있다. DB·임베딩 없이 build_records()만
돌려, 시트 65행이 전부 레코드 계약(EvidenceRecordIn)을 통과해 Supabase에 적재한 건수와 같은
레코드가 되는지, 가짜 검색·검증이 쓰는 fixture가 판정에 쓰는 값이 적재 결과와 같은지 본다.
시트가 바뀌면 건수를 갱신한다.
"""

import csv
from collections import Counter
from pathlib import Path

import pytest

from app.ingestion.extract import MAX_CHARS
from app.ingestion.parse import RESEARCH_DIR, research_root
from app.schemas.evidence import RecordType, Section, Verification
from scripts.scaffold_protocols import read_sheet
from scripts.seed_evidence import (
    EXCERPTS,
    MANIFEST,
    OVERRIDES,
    ROOT,
    build_records,
    load_excerpts_file,
    load_overrides,
)
from tests.agent import fakes


def _workbook() -> Path:
    """형제 폴더 Research_paper의 워크북. 워크트리에서도 상위로 올라가며 찾는다."""
    try:
        return research_root(ROOT) / RESEARCH_DIR / "LUMI_측정도구_프로토콜_2026-10-23.xlsx"
    except FileNotFoundError:
        return ROOT.parent / "Research_paper" / "없음.xlsx"


XLSX = _workbook()
# 검색 필터와 검증 규칙(judge)이 읽는 필드. arm_label · n_detail 같은 표기는 fixture와 다를 수 있다
DECISION_FIELDS = ("record_type", "verification", "media", "intervention_orders", "clinical_status")

pytestmark = pytest.mark.skipif(not XLSX.exists(), reason="워크북이 없다(저장소 밖 Research_paper)")


@pytest.fixture(scope="module")
def seeded():
    rows = read_sheet(XLSX, "근거 arm", "protocol_id")
    papers = {row["rc_id"]: row for row in csv.DictReader(MANIFEST.open(encoding="utf-8"))}
    records, _ = build_records(rows, papers, load_overrides(OVERRIDES))
    return rows, records


def test_sheet_rows_become_records(seeded):
    rows, records = seeded
    chunks = [chunk for record in records for chunk in record.chunks]
    assert len(rows) == 65
    assert len(records) == 61  # 여러 프로토콜이 인용한 같은 arm 4행을 합친다
    assert len(chunks) == 122  # 레코드마다 METHODS · RESULTS
    assert len({chunk.paper_id for chunk in chunks}) == 57
    keys = [(c.paper_id, c.experiment_id, c.section, c.chunk_idx) for c in chunks]
    assert len(set(keys)) == len(keys)  # evidence_records_chunk_key
    assert Counter(r.metadata.record_type for r in records) == {
        RecordType.ARM: 44,
        RecordType.SYNTHESIS: 16,
        RecordType.OBSERVATION: 1,
    }
    assert Counter(r.metadata.verification for r in records) == {
        Verification.FULL_TEXT: 51,
        Verification.SECONDARY: 7,
        Verification.ABSTRACT: 3,
    }


def test_fixture_matches_seeded_decision_values(seeded):
    _, records = seeded
    chunks = {
        (c.paper_id, c.experiment_id, c.section, c.chunk_idx): c
        for record in records
        for c in record.chunks
    }
    for hit in fakes.load_hits():
        seeded_meta = chunks[(hit.paper.paper_id, hit.experiment_id, hit.section, hit.chunk_idx)]
        seeded_meta = seeded_meta.metadata
        for field in DECISION_FIELDS:
            assert getattr(seeded_meta, field) == getattr(hit.metadata, field), (
                hit.evidence_record_id,
                field,
            )
        assert [(r.outcome, r.effect) for r in seeded_meta.results] == [
            (r.outcome, r.effect) for r in hit.metadata.results
        ], hit.evidence_record_id


def test_excerpts_keep_record_keys_and_ids(seeded):
    """원문 발췌(data/evidence_excerpts.json)를 적용해도 chunk 키(paper_id, experiment_id,
    section, chunk_idx)가 같다 — upsert가 같은 행을 갱신해 id(evidence_record_ids)가 유지된다."""
    rows, records = seeded
    excerpts = load_excerpts_file(EXCERPTS)
    papers = {row["rc_id"]: row for row in csv.DictReader(MANIFEST.open(encoding="utf-8"))}
    with_excerpts, _ = build_records(rows, papers, load_overrides(OVERRIDES), excerpts)

    def keys(rs):
        return [(c.paper_id, c.experiment_id, c.section, c.chunk_idx) for r in rs for c in r.chunks]

    assert keys(with_excerpts) == keys(records)
    assert set(excerpts) <= {r.key for r in records}  # 시트에 없는 키가 없다
    by_key = {r.key: r for r in with_excerpts}
    for key, sections in excerpts.items():
        record = by_key[key]
        assert record.excerpted == [s for s in (Section.METHODS, Section.RESULTS) if s in sections]
        for chunk in record.chunks:
            if chunk.section in sections:
                assert chunk.content == sections[chunk.section]
                assert len(chunk.content) <= MAX_CHARS
