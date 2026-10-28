"""Care Protocol의 content_spec을 실제 실행 자산으로 해석한다."""

from dataclasses import dataclass

import httpx

from app.agent.state import ActivityAsset
from app.config import Settings, get_settings
from app.content.curated import curated_asset
from app.content.image import nature_prompt
from app.content.providers import (
    AIHordeProvider,
    ArtInstituteProvider,
    ContentProviderError,
    FreesoundProvider,
    PixabayProvider,
)
from app.schemas.care import CareRequest, ContentType
from app.schemas.protocol import CareProtocol, SlotType


@dataclass
class ContentResolution:
    assets: list[ActivityAsset]
    warnings: list[str]


def _query(request: CareRequest, fallback: str) -> str:
    return " ".join(request.profile.preferred_scenery) or fallback


# 좋아하는 풍경(FE 시작 화면 칩 → profile.preferred_scenery) → 자연 사진 검색어.
# 사진 명세의 include_tags(물 · 식생 · 열린 조망 · 자연광 — P03 · P24 · P33)를 벗어나지 않게
# 검수한 검색어만 쓴다. 한국어 값도 받는다.
SCENERY_QUERIES: dict[str, str] = {
    "forest": "calm forest",
    "lake river": "calm lake water",
    "sea": "calm sea horizon",
    "mountain": "green mountain valley",
    "flowers": "meadow flowers nature",
    "sky": "open sky landscape",
}
SCENERY_ALIASES = {
    "숲": "forest",
    "호수·강": "lake river",
    "바다": "sea",
    "산": "mountain",
    "꽃": "flowers",
    "하늘": "sky",
}
MAX_SCENERY = 3


def preferred_sceneries(request: CareRequest) -> list[str]:
    """검색어가 있는 좋아하는 풍경만, 고른 순서대로 최대 MAX_SCENERY개."""
    out: list[str] = []
    for value in request.profile.preferred_scenery:
        key = SCENERY_ALIASES.get(value.strip(), value.strip().lower())
        if key in SCENERY_QUERIES and key not in out:
            out.append(key)
    return out[:MAX_SCENERY]


def personalized_photos(
    pixabay: PixabayProvider, sceneries: list[str], fallback_query: str, count: int
) -> tuple[list[ActivityAsset], list[str]]:
    """좋아하는 풍경마다 나눠 받아 번갈아 섞는다. 모자라면 명세 기본 검색어로 채운다.

    자산 metadata에 preferred_scenery(그 사진을 고른 풍경)와 query를 남긴다 — FE가 "좋아하는 ○○
    풍경으로 골랐어요"를 보이고, Reference 카드가 연구 자극과 다르다(ADAPTED_STIMULUS)는 근거로
    쓴다.
    """
    each = -(-count // len(sceneries))  # 올림
    groups: list[list[ActivityAsset]] = []
    warnings: list[str] = []
    for scenery in sceneries:
        query = SCENERY_QUERIES[scenery]
        try:
            found = pixabay.search_images(query, count=each)
        except ContentProviderError as exc:
            warnings.append(f"VISUAL_STILL: 좋아하는 풍경 {scenery} 사진을 받지 못함 ({exc})")
            continue
        for asset in found:
            asset.metadata = {**asset.metadata, "preferred_scenery": scenery, "query": query}
        groups.append(found)
    mixed: list[ActivityAsset] = []
    for i in range(each):
        mixed += [group[i] for group in groups if i < len(group)]
    seen: set[str] = set()
    photos = [a for a in mixed if not (a.url in seen or seen.add(a.url))][:count]
    if len(photos) < count:
        for asset in pixabay.search_images(fallback_query, count=count):
            if asset.url not in seen and len(photos) < count:
                seen.add(asset.url)
                photos.append(asset)
    return photos, warnings


def _configured_query(selection: dict, request: CareRequest, fallback: str) -> str:
    preferred = _query(request, "")
    tags = selection.get("query_tags", [])
    # 공급자 검색어는 검수된 영문 태그를 우선하고, 없을 때만 사용자 선호를 쓴다.
    return (tags[0] if tags else preferred) or fallback


def resolve_activity_assets(
    protocol: CareProtocol,
    request: CareRequest,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> ContentResolution:
    """콘텐츠 슬롯을 자산으로 채운다 — 외부 공급자(A 담당)와 검수 자산(음원 · 컬러링 도안).
    공급자 실패는 세션 상태에 안전한 경고로 남긴다."""
    settings = settings or get_settings()
    own_client = client is None
    client = client or httpx.Client(timeout=settings.content_request_timeout_seconds)
    assets: list[ActivityAsset] = []
    warnings: list[str] = []

    try:
        for slot in protocol.content_spec.slots:
            spec = slot.model_extra or {}
            selection = spec.get("selection", {})
            provider = selection.get("provider")
            try:
                slot_assets: list[ActivityAsset] = []
                generation = spec.get("generation", {})
                ai_image_requested = (
                    slot.slot is SlotType.VISUAL_STILL
                    and request.profile.content_type is ContentType.AI_IMAGE
                    and generation.get("provider") == "AI_HORDE"
                )
                if ai_image_requested:
                    if not settings.ai_horde_api_key:
                        raise ContentProviderError("AI_HORDE_API_KEY가 설정되지 않음")
                    count = int(spec.get("count", 1))
                    try:
                        slot_assets = AIHordeProvider(
                            settings.ai_horde_api_key,
                            client,
                            poll_timeout_seconds=settings.ai_horde_poll_timeout_seconds,
                        ).generate_images(
                            nature_prompt(
                                request.profile,
                                meditation=protocol.protocol_id == "PR-21",
                            ),
                            count=count,
                        )
                    except ContentProviderError as exc:
                        if not settings.pixabay_api_key:
                            raise
                        warnings.append(
                            f"{slot.slot.value}: AI 생성 실패로 Pixabay 사진을 사용함 ({exc})"
                        )
                        slot_assets = PixabayProvider(
                            settings.pixabay_api_key, client
                        ).search_images(
                            _configured_query(selection, request, "calm forest water"),
                            count=count,
                        )
                elif provider == "PIXABAY":
                    if not settings.pixabay_api_key:
                        raise ContentProviderError("PIXABAY_API_KEY가 설정되지 않음")
                    pixabay = PixabayProvider(settings.pixabay_api_key, client)
                    if slot.slot is SlotType.VISUAL_STILL:
                        default_query = _configured_query(selection, request, "calm forest water")
                        count = int(spec.get("count", 1))
                        if sceneries := preferred_sceneries(request):
                            slot_assets, scenery_warnings = personalized_photos(
                                pixabay, sceneries, default_query, count
                            )
                            warnings += scenery_warnings
                        else:
                            slot_assets = pixabay.search_images(default_query, count=count)
                    else:
                        render = spec.get("render", {})
                        slot_assets = [
                            pixabay.search_video(
                                _configured_query(selection, request, "calm nature"),
                                min_source_duration_seconds=int(
                                    selection.get("min_source_duration_seconds", 30)
                                ),
                                session_duration_seconds=int(
                                    render.get("duration_seconds", 600)
                                ),
                            )
                        ]
                elif provider == "FREESOUND":
                    if not settings.freesound_api_key:
                        raise ContentProviderError("FREESOUND_API_KEY가 설정되지 않음")
                    render = spec.get("render", {})
                    slot_assets = [
                        FreesoundProvider(settings.freesound_api_key, client).search_sound(
                            _configured_query(selection, request, "gentle forest stream ambience"),
                            min_duration_seconds=int(render.get("duration_seconds", 0)),
                            exclude_terms=tuple(selection.get("exclude_query_terms", [])),
                        )
                    ]
                elif provider == "ART_INSTITUTE_OF_CHICAGO":
                    slot_assets = [
                        ArtInstituteProvider(client).search_artwork(
                            _query(request, "calm nature landscape")
                        )
                    ]
                elif provider == "CURATED_ASSET_LIBRARY":
                    # 음원(PR-05 · 21)은 audio_asset_refs, 컬러링 도안(PR-14 · 15)은 asset_refs
                    refs = selection.get("asset_refs", selection.get("audio_asset_refs"))
                    if refs is None:
                        refs = [selection.get("audio_asset_ref") or selection.get("collection")]
                    if not isinstance(refs, list) or not refs or not all(refs):
                        raise ContentProviderError("검수 자산 참조 목록이 비어 있음")

                    default_ref = (
                        selection.get("default_asset_ref")
                        or selection.get("default_audio_asset_ref")
                        or refs[0]
                    )
                    if default_ref not in refs:
                        raise ContentProviderError("기본 검수 자산이 후보 목록에 없음")

                    for ref in refs:
                        record = curated_asset(str(ref))
                        if not record:
                            raise ContentProviderError(f"검수 자산이 아직 등록되지 않음: {ref}")
                        metadata = {
                            **record.get("metadata", {}),
                            "asset_ref": ref,
                            "selection_mode": selection.get("mode", "FIXED"),
                            "is_default": ref == default_ref,
                        }
                        slot_assets.append(
                            ActivityAsset(
                                slot=slot.slot.value,
                                url=record["url"],
                                provider=record.get("provider", provider),
                                title=record.get("title"),
                                creator=record.get("creator"),
                                source_url=record.get("source_url"),
                                license=record.get("license"),
                                verification=record.get(
                                    "verification", "PENDING_CONTENT_REVIEW"
                                ),
                                metadata=metadata,
                            )
                        )
                        if slot.required and record.get("verification") != "VERIFIED":
                            warnings.append(
                                f"{slot.slot.value}: 고정 자산의 내용 검수가 필요함 ({ref})"
                            )
                elif slot.slot is SlotType.VISUAL_STILL:
                    raise ContentProviderError("이미지 공급자가 프로토콜에 설정되지 않음")

                for asset in slot_assets:
                    asset.slot = slot.slot.value
                    assets.append(asset)
            except ContentProviderError as exc:
                if slot.required:
                    warnings.append(f"{slot.slot.value}: {exc}")
    finally:
        if own_client:
            client.close()
    return ContentResolution(assets=assets, warnings=warnings)
