"""저장소에 포함된 고정 콘텐츠 자산 매니페스트 — app/static/*/manifest.json(음원 · 컬러링 도안)."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"


@lru_cache
def curated_assets() -> dict[str, dict[str, Any]]:
    assets: dict[str, dict[str, Any]] = {}
    for path in sorted(STATIC_ROOT.glob("*/manifest.json")):
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError(f"고정 콘텐츠 자산 매니페스트는 객체여야 한다: {path}")
        duplicated = assets.keys() & data.keys()
        if duplicated:
            raise ValueError(f"자산 키가 매니페스트 둘에 있다: {sorted(duplicated)}")
        assets.update(data)
    return assets


def curated_asset(ref: str) -> dict[str, Any] | None:
    value = curated_assets().get(ref)
    return value.copy() if value else None
