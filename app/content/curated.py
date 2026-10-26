"""저장소에 포함된 고정 콘텐츠 자산 매니페스트."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "static" / "audio" / "manifest.json"


@lru_cache
def curated_assets() -> dict[str, dict[str, Any]]:
    if not MANIFEST_PATH.exists():
        return {}
    with MANIFEST_PATH.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError("고정 콘텐츠 자산 매니페스트는 객체여야 한다")
    return data


def curated_asset(ref: str) -> dict[str, Any] | None:
    value = curated_assets().get(ref)
    return value.copy() if value else None
