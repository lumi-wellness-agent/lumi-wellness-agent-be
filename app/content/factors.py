"""콘텐츠 Factor 카탈로그(data/content_factors.json) — 값마다 근거 · 검수 검색어.

- plan: 고른 활동에 열 칸(menu)을 LLM 입력에 싣고, LLM 출력을 활동에 맞게 다듬는다(sanitize).
- prepare(app/content/resolver.py): 값의 검색어 가운데 하나를 고른다(pick_query). rng를 주면
  세션마다 다른 검색어를 쓰고(다양화), 주지 않으면 첫 검색어를 쓴다(테스트 · 재현).

LLM은 basis가 STIMULUS인 값만 고른다. DESIGN 값(꽃 · 하늘)은 사용자 칩으로만 들어온다. Enum
(app/schemas/content.py)과 카탈로그 키가 같은지는 tests/content/test_factors.py가 본다.
"""

import json
import random
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.schemas.content import ContentFactors, FactorSource, Scenery, Soundscape
from app.schemas.protocol import CareProtocol, SlotType

CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "content_factors.json"


class Basis(StrEnum):
    STIMULUS = "STIMULUS"  # 논문 자극으로 쓰인 범주 — LLM이 고를 수 있다
    DESIGN = "DESIGN"  # 근거 없는 선호용 — 사용자 칩으로만


class FactorEvidence(BaseModel):
    paper_id: str
    page: int | None = None
    note: str


class FactorValue(BaseModel):
    label_ko: str
    basis: Basis
    evidence: list[FactorEvidence] = []
    # 풍경
    image_queries: list[str] = []
    video_queries: list[str] = []
    ai_prompt: str | None = None
    matching_soundscapes: list[Soundscape] = []
    # 자연음 · 작품
    queries: list[str] = []
    exclude_query_terms: list[str] = []


class FactorAxis(BaseModel):
    label_ko: str
    slots: list[SlotType]
    max_values: int
    values: dict[str, FactorValue]


class FactorCatalog(BaseModel):
    version: str
    axes: dict[str, FactorAxis]
    excluded: list[dict[str, Any]] = []


SCENERY = "scenery"
SOUNDSCAPE = "soundscape"
ART_SUBJECT = "art_subject"


@lru_cache
def load_catalog() -> FactorCatalog:
    return FactorCatalog.model_validate(json.loads(CATALOG_PATH.read_text(encoding="utf-8")))


def value(axis: str, key: str) -> FactorValue | None:
    found = load_catalog().axes.get(axis)
    return found.values.get(key) if found else None


def selectable(axis: str) -> list[str]:
    """LLM이 고를 수 있는 값 — 근거가 있는(STIMULUS) 것만."""
    return [k for k, v in load_catalog().axes[axis].values.items() if v.basis is Basis.STIMULUS]


def pick_query(queries: list[str], rng: random.Random | None) -> str:
    """값의 검색어 하나. rng가 없으면 첫 검색어(대표 검색어)."""
    if not queries:
        raise ValueError("검색어가 없는 Factor 값")
    return rng.choice(queries) if rng else queries[0]


def tag(axis: str, key: str, source: FactorSource, query: str | None = None) -> dict[str, Any]:
    """자산 metadata에 남길 Factor 정보. FE는 source가 USER일 때만 "좋아하는 ○○"라고 쓴다."""
    found = value(axis, key)
    out: dict[str, Any] = {
        "axis": axis,
        "value": key,
        "label_ko": found.label_ko if found else key,
        "source": source.value,
    }
    if found and found.evidence:
        out["evidence"] = [e.paper_id for e in found.evidence]
    if query:
        out["query"] = query
    return out


# ── 활동별로 열 칸 ─────────────────────────────────────────────────────────────


def _slot_specs(protocol: CareProtocol) -> dict[SlotType, dict]:
    """슬롯 → 명세(selection · generation · count …)."""
    return {slot.slot: slot.model_extra or {} for slot in protocol.content_spec.slots}


def _provider(spec: dict) -> str | None:
    return (spec.get("selection") or {}).get("provider")


def scenery_capacity(protocol: CareProtocol) -> int:
    """이 활동이 쓰는 풍경 수 — 사진 여러 장이면 최대 3, 사진 1장 · 영상이면 1, 칸이 없으면 0."""
    specs = _slot_specs(protocol)
    still = specs.get(SlotType.VISUAL_STILL)
    if still and (_provider(still) == "PIXABAY" or (still.get("generation") or {}).get("provider")):
        max_values = load_catalog().axes[SCENERY].max_values
        return max_values if int(still.get("count", 1)) > 1 else 1
    video = specs.get(SlotType.VISUAL_VIDEO)
    if video and _provider(video) == "PIXABAY":
        return 1
    return 0


def video_only(protocol: CareProtocol) -> bool:
    """풍경이 영상으로만 쓰인다(PR-02) — 영상 검색어가 있는 풍경만 고를 수 있다."""
    specs = _slot_specs(protocol)
    return SlotType.VISUAL_VIDEO in specs and SlotType.VISUAL_STILL not in specs


def axes_for(protocol: CareProtocol) -> list[str]:
    """이 활동에 열 Factor 칸. 검수 자산만 쓰는 활동(음악 · 안내 음성 · 도안)과 A 밖 활동은 없다."""
    specs = _slot_specs(protocol)
    axes = []
    if scenery_capacity(protocol):
        axes.append(SCENERY)
    sound = specs.get(SlotType.SOUNDSCAPE)
    if sound and _provider(sound) == "FREESOUND":
        axes.append(SOUNDSCAPE)
    if SlotType.ARTWORK in specs:
        axes.append(ART_SUBJECT)
    return axes


def scenery_choices(protocol: CareProtocol) -> list[str]:
    keys = selectable(SCENERY)
    if video_only(protocol):
        keys = [k for k in keys if load_catalog().axes[SCENERY].values[k].video_queries]
    return keys


def menu(protocol: CareProtocol) -> list[dict[str, Any]]:
    """LLM 입력에 실을 칸 — 칸마다 고를 수 있는 값 · 개수 · 근거 논문."""
    catalog = load_catalog()
    axes = axes_for(protocol)
    out = []
    for axis in axes:
        spec = catalog.axes[axis]
        keys = scenery_choices(protocol) if axis == SCENERY else selectable(axis)
        item: dict[str, Any] = {
            "field": axis,
            "label": spec.label_ko,
            "max": scenery_capacity(protocol) if axis == SCENERY else 1,
            "values": [
                {
                    "value": k,
                    "label": spec.values[k].label_ko,
                    "evidence": [e.paper_id for e in spec.values[k].evidence],
                }
                for k in keys
            ],
        }
        if axis == SOUNDSCAPE and SCENERY in axes and video_only(protocol):
            sceneries = catalog.axes[SCENERY].values
            pairs = [
                f"{k} → {', '.join(s.value for s in sceneries[k].matching_soundscapes)}"
                for k in scenery_choices(protocol)
            ]
            item["note"] = "영상 풍경과 맞는 소리만 고른다: " + " · ".join(pairs)
        out.append(item)
    return out


def sanitize(protocol: CareProtocol, factors: ContentFactors | None) -> ContentFactors:
    """LLM 출력을 활동에 맞게 다듬는다 — 없는 칸은 비우고, 근거 없는 값 · 중복을 빼고, 개수를
    자른다.

    영상과 자연음을 함께 쓰는 활동(PR-02)은 소리를 영상 풍경에 맞춘다(content_spec: 영상 장면과
    일치하는 음원). 풍경이 없으면 소리도 비워 프로토콜 기본값(서로 맞는 숲 영상 · 숲 소리)을 쓴다.
    """
    if factors is None:
        return ContentFactors.none()
    axes = axes_for(protocol)
    scenery: list[Scenery] = []
    if SCENERY in axes:
        allowed = set(scenery_choices(protocol))
        for item in factors.scenery:
            if item.value in allowed and item not in scenery:
                scenery.append(item)
        scenery = scenery[: scenery_capacity(protocol)]
    soundscape = factors.soundscape if SOUNDSCAPE in axes else None
    if soundscape is not None and soundscape.value not in selectable(SOUNDSCAPE):
        soundscape = None
    if soundscape is not None and SCENERY in axes and video_only(protocol):
        if not scenery:
            soundscape = None
        else:
            matching = value(SCENERY, scenery[0].value).matching_soundscapes
            if soundscape not in matching:
                soundscape = matching[0] if matching else None
    art = factors.art_subject if ART_SUBJECT in axes else None
    if art is not None and art.value not in selectable(ART_SUBJECT):
        art = None
    cleaned = ContentFactors(
        scenery=scenery, soundscape=soundscape, art_subject=art, reason=factors.reason
    )
    return ContentFactors.none() if cleaned.is_empty() else cleaned

