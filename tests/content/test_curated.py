import hashlib
from pathlib import Path

import pytest

from app.content.curated import curated_asset, curated_assets

STATIC_ROOT = Path(__file__).resolve().parents[2] / "app" / "static"


def test_pr05_curated_audio_choices_match_registered_files_and_hashes():
    refs = [
        "audio/pr05_miserere_cc_by_sa_v1",
        "audio/pr05_mozart_k448_cc_by_sa_v1",
        "audio/pr05_vivaldi_spring_cc_by_sa_v1",
    ]

    for ref in refs:
        record = curated_asset(ref)
        assert record is not None

        path = STATIC_ROOT / record["url"].removeprefix("/assets/")
        assert path.is_file()
        assert path.stat().st_size == record["metadata"]["file_bytes"]
        assert (
            hashlib.sha256(path.read_bytes()).hexdigest().upper()
            == record["metadata"]["sha256"]
        )
        assert record["metadata"]["playback_duration_seconds"] == 600
        assert record["metadata"]["original_duration_seconds"] > 600
        assert record["metadata"]["technical_review"] == "DECODE_OK"


@pytest.mark.parametrize("ref", sorted(curated_assets()))
def test_every_registered_asset_is_the_file_it_describes(ref):
    record = curated_asset(ref)
    path = STATIC_ROOT / record["url"].removeprefix("/assets/")
    assert path.is_file()
    metadata = record["metadata"]
    if "file_bytes" in metadata:
        assert path.stat().st_size == metadata["file_bytes"]
    assert hashlib.sha256(path.read_bytes()).hexdigest().upper() == metadata["sha256"]


def test_coloring_templates_are_svgs_with_regions_and_an_outline():
    refs = [ref for ref in curated_assets() if ref.startswith("coloring/")]
    assert len(refs) == 11  # PR-14 만다라 6 · PR-15 자연 5
    for ref in refs:
        record = curated_asset(ref)
        svg = (STATIC_ROOT / record["url"].removeprefix("/assets/")).read_text(encoding="utf-8")
        # FE가 SVG를 DOM에 그대로 넣어 칠한다 — 영역 id와 외곽선이 있고 스크립트는 없어야 한다
        assert svg.count("data-region-id=") == record["metadata"]["region_count"] > 0
        assert 'id="outline"' in svg
        assert "<script" not in svg.lower()
        assert record["verification"] == "PENDING_CONTENT_REVIEW"  # 원본 출처·라이선스 확인 전
