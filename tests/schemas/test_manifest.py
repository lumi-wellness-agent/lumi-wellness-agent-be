"""계약 1 — data/manifest.csv(82편)와 JSON·fixture의 논문 ID가 실재하는지."""

import csv
import json
import re
from collections import Counter
from pathlib import Path

from app.schemas.evidence import FulltextStatus, StudyDesign

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "manifest.csv"
FIXTURES = ROOT / "tests" / "fixtures"
PAPER_ID = re.compile(r"^[PEN][0-9]{2}$")

ROWS = list(csv.DictReader(MANIFEST.open(encoding="utf-8")))
IDS = {row["rc_id"] for row in ROWS}


def test_82_unique_rc_ids():
    assert len(ROWS) == 82 and len(IDS) == 82
    assert all(PAPER_ID.match(pid) for pid in IDS)


def test_design_counts_match_master_sheet():
    counts = Counter(row["study_design"] for row in ROWS)
    assert counts == {
        StudyDesign.THEORY_REVIEW: 5,
        StudyDesign.SYSTEMATIC_REVIEW_META: 17,
        StudyDesign.SCALE_DEVELOPMENT: 13,
        StudyDesign.RANDOMIZED_EXPERIMENT: 35,
        StudyDesign.QUASI_EXPERIMENT: 7,
        StudyDesign.OBSERVATIONAL: 5,
    }


def test_fulltext_status():
    by_status: dict[str, set[str]] = {}
    for row in ROWS:
        FulltextStatus(row["fulltext_status"])
        by_status.setdefault(row["fulltext_status"], set()).add(row["rc_id"])
        assert bool(row["pdf_path"]) == (row["fulltext_status"] != "MISSING")
    assert by_status["MISSING"] == {"E02", "E13", "N06", "N10", "N23", "P30"}
    assert by_status["IMAGE_PDF"] == {"N02", "N03", "N09", "P05", "P26", "P27"}


def test_every_paper_has_a_locator():
    assert all(row["doi"] or row["url"] for row in ROWS)


def test_legacy_crosswalk():
    legacy = {row["rc_id"]: row["legacy_36_id"] for row in ROWS if row["legacy_36_id"]}
    assert len(legacy) == 36 and len(set(legacy.values())) == 36
    assert legacy["P28"] == "P08"  # 옛 P08(Brown 2013)은 R/C P28
    assert legacy["P09"] == "P28"
    assert legacy["P08"] == "P36"  # R/C P08(이승훈·현명호 2004)은 옛 P36 — 같은 번호가 다른 논문


def test_protocol_fixture_papers_exist():
    for path in (FIXTURES / "protocols").glob("PR-*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        missing = {e["paper_id"] for e in doc["evidence"]} - IDS
        assert not missing, f"{path.name}: {missing}"


def test_evidence_hits_fixture_papers_exist():
    hits = json.loads((FIXTURES / "evidence_hits.json").read_text(encoding="utf-8"))
    by_id = {row["rc_id"]: row for row in ROWS}
    for hit in hits:
        paper = hit["paper"]
        row = by_id[paper["paper_id"]]
        assert paper["title"] == row["title"]
        assert paper["fulltext_status"] == row["fulltext_status"]
