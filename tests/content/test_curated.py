import hashlib
from pathlib import Path

from app.content.curated import curated_asset

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
