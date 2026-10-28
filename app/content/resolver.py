"""Care Protocol의 content_spec을 실제 실행 자산으로 해석한다.

콘텐츠 Factor(app/schemas/content.py)가 공급자 검색어를 정한다. 우선순위는 사용자 칩
(profile.preferred_scenery) > plan이 고른 content_factors(LLM) > 프로토콜 기본 검색어
(selection.query_tags). Factor 값마다 검수 검색어가 여럿이고(data/content_factors.json), rng를 주면
세션마다 하나를 골라 같은 풍경이라도 다른 사진·소리를 받는다. rng가 없으면 대표 검색어(첫 번째)를
쓴다.
"""

import random
from dataclasses import dataclass

import httpx

from app.agent.state import ActivityAsset
from app.config import Settings, get_settings
from app.content import factors as factor_catalog
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
from app.schemas.content import ContentFactors, FactorSource, Soundscape
from app.schemas.protocol import CareProtocol, SlotType


@dataclass
class ContentResolution:
    assets: list[ActivityAsset]
    warnings: list[str]


def _query(request: CareRequest, fallback: str) -> str:
    return " ".join(request.profile.preferred_scenery) or fallback


# 풍경(FE 시작 화면 칩 → profile.preferred_scenery, plan의 content_factors.scenery) → 대표 자연 사진
# 검색어. 사진 명세의 include_tags(물 · 식생 · 열린 조망 · 자연광 — P03 · P24 · P33)를 벗어나지 않게
# 검수한 검색어만 쓴다(data/content_factors.json의 image_queries 첫 번째). 한국어 값도 받는다.
SCENERY_QUERIES: dict[str, str] = {
    key: spec.image_queries[0]
    for key, spec in factor_catalog.load_catalog().axes[factor_catalog.SCENERY].values.items()
}
SCENERY_ALIASES = {
    "숲": "forest",
    "호수·강": "lake river",
    "바다": "sea",
    "산": "mountain",
    "풀밭": "meadow",
    "들판": "meadow",
    "꽃": "flowers",
    "하늘": "sky",
}
# 섞는 풍경 수 상한(카탈로그 scenery.max_values — P03의 호수·강·바다·언덕처럼 여러 종)
MAX_SCENERY = factor_catalog.load_catalog().axes[factor_catalog.SCENERY].max_values


def preferred_sceneries(request: CareRequest) -> list[str]:
    """검색어가 있는 좋아하는 풍경만, 고른 순서대로 최대 MAX_SCENERY개."""
    out: list[str] = []
    for value in request.profile.preferred_scenery:
        key = SCENERY_ALIASES.get(value.strip(), value.strip().lower())
        if key in SCENERY_QUERIES and key not in out:
            out.append(key)
    return out[:MAX_SCENERY]


def scenery_plan(
    request: CareRequest, factors: ContentFactors | None
) -> tuple[list[str], FactorSource]:
    """이번 세션의 풍경과 출처 — 사용자 칩 > plan이 고른 풍경 > 없음(기본 검색어)."""
    if chosen := preferred_sceneries(request):
        return chosen, FactorSource.USER
    picked = [s.value for s in factors.scenery if s.value in SCENERY_QUERIES] if factors else []
    if picked:
        return picked[:MAX_SCENERY], FactorSource.LLM
    return [], FactorSource.DEFAULT


def _scenery_query(scenery: str, rng: random.Random | None) -> str:
    spec = factor_catalog.value(factor_catalog.SCENERY, scenery)
    return factor_catalog.pick_query(spec.image_queries, rng) if spec else SCENERY_QUERIES[scenery]


def personalized_photos(
    pixabay: PixabayProvider,
    sceneries: list[str],
    fallback_query: str,
    count: int,
    *,
    source: FactorSource = FactorSource.USER,
    rng: random.Random | None = None,
) -> tuple[list[ActivityAsset], list[str]]:
    """풍경마다 나눠 받아 번갈아 섞는다. 모자라면 명세 기본 검색어로 채운다.

    자산 metadata에 content_factor(축 · 값 · 출처 · 근거 논문)와 query를 남긴다. 사용자 칩이면
    preferred_scenery도 남겨 FE가 "좋아하는 ○○ 풍경으로 골랐어요"를 보인다 — plan이 고른 풍경(LLM)은
    사용자가 좋아한다고 한 것이 아니라 남기지 않는다. 연구 자극과 다르다는 표시(ADAPTED_STIMULUS)를
    Reference 카드에 잇는 것은 아직이다(카드는 plan이 자산보다 먼저 만든다).

    사진이 풍경 수보다 적으면(PR-03 · 21은 1장) 앞의 풍경만 부른다. 기본 검색어로 채우다 실패해도
    받은 사진은 돌려준다.
    """
    sceneries = sceneries[:count]
    each = -(-count // len(sceneries))  # 올림
    groups: list[list[ActivityAsset]] = []
    warnings: list[str] = []
    who = "좋아하는 풍경" if source is FactorSource.USER else "고른 풍경"
    for scenery in sceneries:
        query = _scenery_query(scenery, rng)
        try:
            found = pixabay.search_images(query, count=each)
        except ContentProviderError as exc:
            warnings.append(f"VISUAL_STILL: {who} {scenery} 사진을 받지 못함 ({exc})")
            continue
        tag = factor_catalog.tag(factor_catalog.SCENERY, scenery, source)
        for asset in found:
            asset.metadata = {**asset.metadata, "content_factor": tag, "query": query}
            if source is FactorSource.USER:
                asset.metadata["preferred_scenery"] = scenery
        groups.append(found)
    mixed: list[ActivityAsset] = []
    for i in range(each):
        mixed += [group[i] for group in groups if i < len(group)]
    seen: set[str] = set()
    photos = [a for a in mixed if not (a.url in seen or seen.add(a.url))][:count]
    if len(photos) < count:
        try:
            extra = pixabay.search_images(fallback_query, count=count)
        except ContentProviderError as exc:
            if not photos:
                raise
            warnings.append(
                f"VISUAL_STILL: 기본 검색어로 채우지 못해 {len(photos)}장만 보임 ({exc})"
            )
            extra = []
        for asset in extra:
            if asset.url not in seen and len(photos) < count:
                seen.add(asset.url)
                photos.append(asset)
    return photos, warnings


def _configured_query(selection: dict, request: CareRequest, fallback: str) -> str:
    preferred = _query(request, "")
    tags = selection.get("query_tags", [])
    # 공급자 검색어는 검수된 영문 태그를 우선하고, 없을 때만 사용자 선호를 쓴다.
    return (tags[0] if tags else preferred) or fallback


def _video_scenery(sceneries: list[str]) -> str | None:
    """영상 검색어가 있는 첫 풍경(숲 · 호수·강 — P25 · N17). 없으면 명세 기본 영상."""
    for scenery in sceneries:
        spec = factor_catalog.value(factor_catalog.SCENERY, scenery)
        if spec and spec.video_queries:
            return scenery
    return None


def _soundscape_plan(
    protocol: CareProtocol,
    sceneries: list[str],
    scenery_source: FactorSource,
    factors: ContentFactors | None,
) -> tuple[Soundscape | None, FactorSource]:
    """자연음 범주와 출처. 영상과 소리를 함께 쓰는 활동(PR-02)은 소리를 영상 풍경에 맞춘다.

    plan의 sanitize가 LLM 출력을 이미 맞췄지만, 사용자 칩이 영상 풍경을 바꿨으면 여기서 다시 맞춘다.
    영상 풍경이 없으면 소리도 명세 기본값(서로 맞는 숲 영상 · 숲 소리)으로 둔다.
    """
    soundscape = factors.soundscape if factors else None
    if not factor_catalog.video_only(protocol):
        return soundscape, FactorSource.LLM
    scene = _video_scenery(sceneries)
    if scene is None:
        return None, FactorSource.DEFAULT
    matching = factor_catalog.value(factor_catalog.SCENERY, scene).matching_soundscapes
    if soundscape in matching:
        return soundscape, FactorSource.LLM
    return (matching[0] if matching else None), scenery_source


def _recommended_music(
    slot: SlotType, refs: list, factors: ContentFactors | None
) -> factor_catalog.FactorValue | None:
    """plan이 추천한 이완 음악 — 음악 슬롯이고 그 곡이 이 목록에 있을 때만."""
    if slot is not SlotType.MUSIC or not factors or factors.music is None:
        return None
    spec = factor_catalog.value(factor_catalog.MUSIC, factors.music.value)
    return spec if spec and spec.asset_ref in refs else None


def _ai_scenery(sceneries: list[str], source: FactorSource) -> list[str] | None:
    """AI 이미지 프롬프트에 넣을 풍경. 사용자 칩은 지금처럼 칩 값을 그대로(nature_prompt가 읽는다),
    plan이 고른 풍경은 카탈로그의 영어 묘사를 쓴다."""
    if source is not FactorSource.LLM:
        return None
    prompts = [
        spec.ai_prompt
        for s in sceneries
        if (spec := factor_catalog.value(factor_catalog.SCENERY, s)) and spec.ai_prompt
    ]
    return prompts or None


def resolve_activity_assets(
    protocol: CareProtocol,
    request: CareRequest,
    *,
    factors: ContentFactors | None = None,
    rng: random.Random | None = None,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> ContentResolution:
    """콘텐츠 슬롯을 자산으로 채운다 — 외부 공급자(A 담당)와 검수 자산(음원 · 컬러링 도안).
    공급자 실패는 세션 상태에 안전한 경고로 남긴다.

    factors는 plan이 고른 콘텐츠 Factor(CarePlan.content_factors), rng는 검색어 다양화용이다.
    """
    settings = settings or get_settings()
    own_client = client is None
    client = client or httpx.Client(timeout=settings.content_request_timeout_seconds)
    assets: list[ActivityAsset] = []
    warnings: list[str] = []
    sceneries, scenery_source = scenery_plan(request, factors)
    soundscape, sound_source = _soundscape_plan(protocol, sceneries, scenery_source, factors)
    art_subject = factors.art_subject if factors else None

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
                                scenery=_ai_scenery(sceneries, scenery_source),
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
                        if sceneries:
                            slot_assets, scenery_warnings = personalized_photos(
                                pixabay,
                                sceneries,
                                default_query,
                                count,
                                source=scenery_source,
                                rng=rng,
                            )
                            warnings += scenery_warnings
                        else:
                            slot_assets = pixabay.search_images(default_query, count=count)
                    else:
                        render = spec.get("render", {})
                        scene = _video_scenery(sceneries)
                        query = (
                            factor_catalog.pick_query(
                                factor_catalog.value(factor_catalog.SCENERY, scene).video_queries,
                                rng,
                            )
                            if scene
                            else _configured_query(selection, request, "calm nature")
                        )
                        video = pixabay.search_video(
                            query,
                            min_source_duration_seconds=int(
                                selection.get("min_source_duration_seconds", 30)
                            ),
                            session_duration_seconds=int(render.get("duration_seconds", 600)),
                        )
                        if scene:
                            video.metadata = {
                                **video.metadata,
                                "content_factor": factor_catalog.tag(
                                    factor_catalog.SCENERY, scene, scenery_source, query
                                ),
                            }
                        slot_assets = [video]
                elif provider == "FREESOUND":
                    if not settings.freesound_api_key:
                        raise ContentProviderError("FREESOUND_API_KEY가 설정되지 않음")
                    render = spec.get("render", {})
                    excludes = list(selection.get("exclude_query_terms", []))
                    sound_spec = (
                        factor_catalog.value(factor_catalog.SOUNDSCAPE, soundscape.value)
                        if soundscape
                        else None
                    )
                    if sound_spec:
                        query = factor_catalog.pick_query(sound_spec.queries, rng)
                        excludes += [t for t in sound_spec.exclude_query_terms if t not in excludes]
                    else:
                        query = _configured_query(
                            selection, request, "gentle forest stream ambience"
                        )
                    sound = FreesoundProvider(settings.freesound_api_key, client).search_sound(
                        query,
                        min_duration_seconds=int(render.get("duration_seconds", 0)),
                        exclude_terms=tuple(excludes),
                    )
                    if sound_spec:
                        sound.metadata = {
                            **sound.metadata,
                            "content_factor": factor_catalog.tag(
                                factor_catalog.SOUNDSCAPE, soundscape.value, sound_source, query
                            ),
                        }
                    slot_assets = [sound]
                elif provider == "ART_INSTITUTE_OF_CHICAGO":
                    art_spec = (
                        factor_catalog.value(factor_catalog.ART_SUBJECT, art_subject.value)
                        if art_subject
                        else None
                    )
                    if art_spec:
                        query = factor_catalog.pick_query(art_spec.queries, rng)
                    else:
                        query = _query(request, "calm nature landscape")
                    artwork = ArtInstituteProvider(client).search_artwork(query)
                    if art_spec:
                        artwork.metadata = {
                            **artwork.metadata,
                            "content_factor": factor_catalog.tag(
                                factor_catalog.ART_SUBJECT,
                                art_subject.value,
                                FactorSource.LLM,
                                query,
                            ),
                        }
                    slot_assets = [artwork]
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
                    # 이완 음악(PR-05): plan이 추천한 곡을 맨 앞 · 기본으로. 고르는 것은 사용자다
                    recommended = _recommended_music(slot.slot, refs, factors)
                    if recommended:
                        default_ref = recommended.asset_ref
                        refs = [default_ref] + [r for r in refs if r != default_ref]

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
                        if recommended and ref == default_ref:
                            metadata["recommended"] = True
                            metadata["content_factor"] = factor_catalog.tag(
                                factor_catalog.MUSIC, factors.music.value, FactorSource.LLM
                            )
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
    # LLM이 고른 Factor에는 고른 이유를 붙인다 — FE가 "LUMI가 고른 …" 아래에 보인다
    if factors and factors.reason:
        for asset in assets:
            tag = asset.metadata.get("content_factor")
            if tag and tag.get("source") == FactorSource.LLM.value:
                asset.metadata["content_factor"] = {**tag, "reason": factors.reason}
    return ContentResolution(assets=assets, warnings=warnings)
