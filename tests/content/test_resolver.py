import httpx

from app.config import Settings
from app.content.providers import ContentProviderError
from app.content.resolver import resolve_activity_assets
from app.protocols import get_protocol
from app.schemas.care import CareRequest, ContentType, UserProfile


def test_missing_key_becomes_safe_warning_without_network_call():
    protocol = get_protocol("PR-04")
    assert protocol is not None
    result = resolve_activity_assets(
        protocol,
        CareRequest(text="자연음을 듣고 싶어요"),
        settings=Settings(freesound_api_key=""),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )
    assert result.assets == []
    assert result.warnings == ["SOUNDSCAPE: FREESOUND_API_KEY가 설정되지 않음"]


def test_ai_image_profile_uses_horde_for_pr01(monkeypatch):
    protocol = get_protocol("PR-01")
    assert protocol is not None
    captured = {}

    def fake_generate(self, prompt, *, count=1):
        from app.agent.state import ActivityAsset

        captured["prompt"] = prompt
        return [
            ActivityAsset(
                slot="VISUAL_STILL",
                url=f"https://cdn/image-{i}.webp",
                provider="AI_HORDE",
                generated=True,
                verification="UNVERIFIED",
            )
            for i in range(count)
        ]

    monkeypatch.setattr("app.content.resolver.AIHordeProvider.generate_images", fake_generate)
    request = CareRequest(
        text="사진을 보고 싶어요",
        profile=UserProfile(content_type=ContentType.AI_IMAGE, preferred_scenery=["숲"]),
    )
    result = resolve_activity_assets(
        protocol,
        request,
        settings=Settings(ai_horde_api_key="secret"),
        client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    assert result.assets[0].provider == "AI_HORDE"
    assert len(result.assets) == 25
    assert "숲" in captured["prompt"]
    assert result.warnings == []


def test_default_photo_profile_uses_pixabay_for_pr01(monkeypatch):
    protocol = get_protocol("PR-01")
    assert protocol is not None
    captured = {}

    def fake_search(self, query, *, count=1):
        from app.agent.state import ActivityAsset

        captured["query"] = query
        captured["count"] = count
        return [
            ActivityAsset(
                slot="VISUAL_STILL",
                url=f"https://cdn/photo-{i}.jpg",
                provider="PIXABAY",
                verification="PENDING_CONTENT_REVIEW",
            )
            for i in range(count)
        ]

    monkeypatch.setattr("app.content.resolver.PixabayProvider.search_images", fake_search)
    result = resolve_activity_assets(
        protocol,
        CareRequest(text="자연 사진을 보고 싶어요"),
        settings=Settings(pixabay_api_key="secret", ai_horde_api_key="secret"),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )
    assert captured == {"query": "calm forest water", "count": 25}
    assert len(result.assets) == 25
    assert all(asset.provider == "PIXABAY" for asset in result.assets)


def test_ai_generation_failure_falls_back_to_pixabay(monkeypatch):
    protocol = get_protocol("PR-03")
    assert protocol is not None

    def fail_generation(self, prompt, *, count=1):
        raise ContentProviderError("generation unavailable")

    def fake_search(self, query, *, count=1):
        from app.agent.state import ActivityAsset

        return [
            ActivityAsset(
                slot="VISUAL_STILL",
                url="https://cdn/fallback.jpg",
                provider="PIXABAY",
                verification="PENDING_CONTENT_REVIEW",
            )
        ]

    monkeypatch.setattr("app.content.resolver.AIHordeProvider.generate_images", fail_generation)
    monkeypatch.setattr("app.content.resolver.PixabayProvider.search_images", fake_search)
    result = resolve_activity_assets(
        protocol,
        CareRequest(
            text="AI 자연 이미지를 보고 싶어요",
            profile=UserProfile(content_type=ContentType.AI_IMAGE),
        ),
        settings=Settings(ai_horde_api_key="secret", pixabay_api_key="secret"),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )
    assert result.assets[0].provider == "PIXABAY"
    assert "AI 생성 실패로 Pixabay 사진을 사용함" in result.warnings[0]


def test_pr21_uses_registered_fixed_meditation_audio_without_api_key():
    protocol = get_protocol("PR-21")
    assert protocol is not None
    result = resolve_activity_assets(
        protocol,
        CareRequest(text="명상하고 싶어요"),
        settings=Settings(ai_horde_api_key="", pixabay_api_key=""),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )
    guide = next(asset for asset in result.assets if asset.slot == "GUIDE_AUDIO")
    assert guide.url == "/assets/audio/pr21/korean-complete.mp3"
    assert guide.metadata["duration_seconds"] == 952
    assert guide.license == "USER_PROVIDED_NONCOMMERCIAL"
    assert guide.verification == "PENDING_CONTENT_REVIEW"
    assert any("내용 검수" in warning for warning in result.warnings)


def test_pr05_returns_three_verified_fixed_music_choices_without_api_key():
    protocol = get_protocol("PR-05")
    assert protocol is not None
    result = resolve_activity_assets(
        protocol,
        CareRequest(text="긴장되기 전에 음악을 듣고 싶어요"),
        settings=Settings(freesound_api_key=""),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )
    music = [asset for asset in result.assets if asset.slot == "MUSIC"]
    assert len(music) == 3
    assert sum(asset.metadata["is_default"] for asset in music) == 1
    assert all(asset.license == "CC BY-SA 4.0" for asset in music)
    assert all(asset.verification == "VERIFIED" for asset in music)
    assert all(asset.metadata["selection_mode"] == "USER_CHOICE" for asset in music)
    assert all(asset.metadata["playback_duration_seconds"] == 600 for asset in music)
    assert all(asset.metadata["evidence_relation"] == "ADAPTED_STIMULUS" for asset in music)
    assert result.warnings == []


def test_pr14_offers_registered_mandala_svgs_without_api_key():
    protocol = get_protocol("PR-14")
    assert protocol is not None
    result = resolve_activity_assets(
        protocol,
        CareRequest(text="색칠하면서 머리를 비우고 싶어요"),
        settings=Settings(pixabay_api_key=""),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )
    templates = [asset for asset in result.assets if asset.slot == "COLORING_TEMPLATE"]
    assert [asset.url for asset in templates] == [
        f"/assets/coloring/pr14/mandala-{n}.svg" for n in range(1, 7)
    ]
    assert [asset.metadata["is_default"] for asset in templates] == [True] + [False] * 5
    assert all(asset.metadata["selection_mode"] == "FIRST_THEN_RANDOM" for asset in templates)
    assert len(result.warnings) == 6 and all("내용 검수" in w for w in result.warnings)


def _fake_pixabay(captured):
    def fake_search(self, query, *, count=1):
        from app.agent.state import ActivityAsset

        captured.append((query, count))
        return [
            ActivityAsset(
                slot="VISUAL_STILL",
                url=f"https://cdn/{query.replace(' ', '-')}-{i}.jpg",
                provider="PIXABAY",
                verification="PENDING_CONTENT_REVIEW",
            )
            for i in range(count)
        ]

    return fake_search


def _resolve_pr01(profile):
    protocol = get_protocol("PR-01")
    assert protocol is not None
    return resolve_activity_assets(
        protocol,
        CareRequest(text="자연 사진을 보고 싶어요", profile=profile),
        settings=Settings(pixabay_api_key="secret"),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )


def test_preferred_scenery_uses_reviewed_queries_and_alternates(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "app.content.resolver.PixabayProvider.search_images", _fake_pixabay(captured)
    )
    result = _resolve_pr01(UserProfile(preferred_scenery=["sea", "숲"]))
    # 풍경마다 나눠 받는다(25 → 13 + 13) — 명세 기본 검색어는 쓰지 않는다
    assert captured == [("calm sea horizon", 13), ("calm forest", 13)]
    assert len(result.assets) == 25
    picked = [a.metadata["preferred_scenery"] for a in result.assets[:4]]
    assert picked == ["sea", "forest", "sea", "forest"]
    assert result.assets[0].metadata["query"] == "calm sea horizon"


def test_unknown_scenery_falls_back_to_protocol_query(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "app.content.resolver.PixabayProvider.search_images", _fake_pixabay(captured)
    )
    result = _resolve_pr01(UserProfile(preferred_scenery=["desert"]))
    assert captured == [("calm forest water", 25)]
    assert all("preferred_scenery" not in a.metadata for a in result.assets)


def test_scenery_without_enough_photos_is_topped_up_from_protocol_query(monkeypatch):
    calls = []

    def search(self, query, *, count=1):
        from app.agent.state import ActivityAsset

        calls.append(query)
        if query == "calm sea horizon":
            raise ContentProviderError("조건에 맞는 Pixabay 자연 이미지가 부족함")
        return [
            ActivityAsset(
                slot="VISUAL_STILL",
                url=f"https://cdn/{query.replace(' ', '-')}-{i}.jpg",
                provider="PIXABAY",
                verification="PENDING_CONTENT_REVIEW",
            )
            for i in range(count)
        ]

    monkeypatch.setattr("app.content.resolver.PixabayProvider.search_images", search)
    result = _resolve_pr01(UserProfile(preferred_scenery=["sea"]))
    assert calls == ["calm sea horizon", "calm forest water"]
    assert len(result.assets) == 25
    assert any("좋아하는 풍경 sea" in w for w in result.warnings)


def test_single_photo_slot_asks_only_the_first_scenery(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "app.content.resolver.PixabayProvider.search_images", _fake_pixabay(captured)
    )
    protocol = get_protocol("PR-03")  # VISUAL_STILL 1장
    assert protocol is not None
    result = resolve_activity_assets(
        protocol,
        CareRequest(text="-", profile=UserProfile(preferred_scenery=["sea", "forest", "sky"])),
        settings=Settings(pixabay_api_key="secret"),
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(AssertionError())
            )
        ),
    )
    assert captured == [("calm sea horizon", 1)]
    assert len(result.assets) == 1


# ── 콘텐츠 Factor(plan이 고른 content_factors) ───────────────────────────────


def _no_network():
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError()))
    )


class LastChoice:
    """rng 대역 — 늘 마지막 검색어를 고른다(다양화 경로)."""

    def choice(self, items):
        return items[-1]


def _factors(scenery=(), soundscape=None, art_subject=None, music=None):
    from app.schemas.content import ContentFactors

    return ContentFactors(
        scenery=list(scenery),
        soundscape=soundscape,
        art_subject=art_subject,
        music=music,
        reason="-",
    )


def test_llm_scenery_drives_photos_when_the_user_picked_none(monkeypatch):
    from app.schemas.content import Scenery

    captured = []
    monkeypatch.setattr(
        "app.content.resolver.PixabayProvider.search_images", _fake_pixabay(captured)
    )
    protocol = get_protocol("PR-01")
    result = resolve_activity_assets(
        protocol,
        CareRequest(text="바다가 보고 싶어요"),
        factors=_factors([Scenery.SEA, Scenery.MOUNTAIN]),
        settings=Settings(pixabay_api_key="secret"),
        client=_no_network(),
    )
    assert captured == [("calm sea horizon", 13), ("green mountain valley", 13)]
    first = result.assets[0].metadata
    # LLM이 고른 풍경은 "좋아하는 ○○"가 아니다 — preferred_scenery를 남기지 않는다
    assert "preferred_scenery" not in first
    assert first["content_factor"]["source"] == "LLM"
    assert first["content_factor"]["value"] == "sea"
    assert first["content_factor"]["evidence"] == ["P03"]
    assert first["content_factor"]["reason"] == "-"


def test_user_chips_win_over_llm_scenery(monkeypatch):
    from app.schemas.content import Scenery

    captured = []
    monkeypatch.setattr(
        "app.content.resolver.PixabayProvider.search_images", _fake_pixabay(captured)
    )
    result = resolve_activity_assets(
        get_protocol("PR-03"),
        CareRequest(text="-", profile=UserProfile(preferred_scenery=["숲"])),
        factors=_factors([Scenery.SEA]),
        settings=Settings(pixabay_api_key="secret"),
        client=_no_network(),
    )
    assert captured == [("calm forest", 1)]
    assert result.assets[0].metadata["preferred_scenery"] == "forest"
    assert result.assets[0].metadata["content_factor"]["source"] == "USER"


def test_rng_picks_other_reviewed_queries_for_variety(monkeypatch):
    from app.schemas.content import Scenery

    captured = []
    monkeypatch.setattr(
        "app.content.resolver.PixabayProvider.search_images", _fake_pixabay(captured)
    )
    resolve_activity_assets(
        get_protocol("PR-03"),
        CareRequest(text="-"),
        factors=_factors([Scenery.FOREST]),
        rng=LastChoice(),
        settings=Settings(pixabay_api_key="secret"),
        client=_no_network(),
    )
    assert captured == [("sunlit woodland", 1)]


def test_soundscape_factor_sets_freesound_query_and_extra_excludes(monkeypatch):
    from app.agent.state import ActivityAsset
    from app.schemas.content import Soundscape

    calls = []

    def fake_sound(self, query, *, min_duration_seconds=0, exclude_terms=()):
        calls.append((query, exclude_terms))
        return ActivityAsset(slot="SOUNDSCAPE", url="https://cdn/s.mp3", provider="FREESOUND")

    monkeypatch.setattr("app.content.resolver.FreesoundProvider.search_sound", fake_sound)
    result = resolve_activity_assets(
        get_protocol("PR-04"),
        CareRequest(text="-"),
        factors=_factors(soundscape=Soundscape.BIRDSONG),
        settings=Settings(freesound_api_key="secret"),
        client=_no_network(),
    )
    query, excludes = calls[0]
    assert query == "birds quiet"
    # 프로토콜 exclude(사람 목소리 · 음악 · 천둥 …)에 거친 새소리(N15)를 더한다
    assert "voice" in excludes and "thunder" in excludes and "crow" in excludes
    assert result.assets[0].metadata["content_factor"]["label_ko"] == "새소리"


def test_video_and_sound_follow_the_user_scene_for_pr02(monkeypatch):
    from app.agent.state import ActivityAsset
    from app.schemas.content import Soundscape

    calls = {}

    def fake_video(self, query, **kwargs):
        calls["video"] = query
        return ActivityAsset(slot="VISUAL_VIDEO", url="https://cdn/v.mp4", provider="PIXABAY")

    def fake_sound(self, query, *, min_duration_seconds=0, exclude_terms=()):
        calls["sound"] = query
        return ActivityAsset(slot="SOUNDSCAPE", url="https://cdn/s.mp3", provider="FREESOUND")

    monkeypatch.setattr("app.content.resolver.PixabayProvider.search_video", fake_video)
    monkeypatch.setattr("app.content.resolver.FreesoundProvider.search_sound", fake_sound)
    result = resolve_activity_assets(
        get_protocol("PR-02"),
        # 사용자가 호수·강을 골랐다 — LLM이 고른 새소리 대신 시냇물 영상에 맞는 소리
        CareRequest(text="-", profile=UserProfile(preferred_scenery=["호수·강"])),
        factors=_factors(soundscape=Soundscape.BIRDSONG),
        settings=Settings(pixabay_api_key="secret", freesound_api_key="secret"),
        client=_no_network(),
    )
    assert calls == {"video": "stream", "sound": "gentle stream"}
    video, sound = result.assets
    assert video.metadata["content_factor"]["source"] == "USER"
    assert sound.metadata["content_factor"]["value"] == "stream"


def test_pr02_without_scenery_keeps_the_matching_default_pair(monkeypatch):
    from app.agent.state import ActivityAsset
    from app.schemas.content import Soundscape

    calls = {}

    def fake_video(self, query, **kwargs):
        calls["video"] = query
        return ActivityAsset(slot="VISUAL_VIDEO", url="https://cdn/v.mp4", provider="PIXABAY")

    def fake_sound(self, query, *, min_duration_seconds=0, exclude_terms=()):
        calls["sound"] = query
        return ActivityAsset(slot="SOUNDSCAPE", url="https://cdn/s.mp3", provider="FREESOUND")

    monkeypatch.setattr("app.content.resolver.PixabayProvider.search_video", fake_video)
    monkeypatch.setattr("app.content.resolver.FreesoundProvider.search_sound", fake_sound)
    resolve_activity_assets(
        get_protocol("PR-02"),
        CareRequest(text="-"),
        factors=_factors(soundscape=Soundscape.OCEAN_WAVES),
        settings=Settings(pixabay_api_key="secret", freesound_api_key="secret"),
        client=_no_network(),
    )
    assert calls == {"video": "forest", "sound": "forest ambience"}


def test_art_subject_sets_the_museum_query(monkeypatch):
    from app.agent.state import ActivityAsset
    from app.schemas.content import ArtSubject

    calls = []

    def fake_art(self, query):
        calls.append(query)
        return ActivityAsset(slot="ARTWORK", url="https://cdn/a.jpg", provider="AIC")

    monkeypatch.setattr("app.content.resolver.ArtInstituteProvider.search_artwork", fake_art)
    result = resolve_activity_assets(
        get_protocol("PR-20"),
        CareRequest(text="-"),
        factors=_factors(art_subject=ArtSubject.WATERSCAPE),
        settings=Settings(),
        client=_no_network(),
    )
    assert calls == ["water lilies"]
    assert result.assets[0].metadata["content_factor"]["evidence"] == ["E06", "E07"]


def test_ai_image_prompt_uses_reviewed_scenery_words_from_the_llm(monkeypatch):
    from app.agent.state import ActivityAsset
    from app.schemas.content import Scenery

    captured = {}

    def fake_generate(self, prompt, *, count=1):
        captured["prompt"] = prompt
        return [
            ActivityAsset(slot="VISUAL_STILL", url="https://cdn/g.webp", provider="AI_HORDE")
        ] * count

    monkeypatch.setattr("app.content.resolver.AIHordeProvider.generate_images", fake_generate)
    resolve_activity_assets(
        get_protocol("PR-21"),
        CareRequest(text="-", profile=UserProfile(content_type=ContentType.AI_IMAGE)),
        factors=_factors([Scenery.LAKE_RIVER]),
        settings=Settings(ai_horde_api_key="secret"),
        client=_no_network(),
    )
    assert "calm lake and gentle river surrounded by green nature" in captured["prompt"]


def test_recommended_music_goes_first_but_all_three_stay_for_the_user():
    from app.schemas.content import MusicTrack

    result = resolve_activity_assets(
        get_protocol("PR-05"),
        CareRequest(text="가사 없는 음악이 좋아요"),
        factors=_factors(music=MusicTrack.VIVALDI_SPRING),
        settings=Settings(),
        client=_no_network(),
    )
    music = [a for a in result.assets if a.slot == "MUSIC"]
    assert [a.metadata["asset_ref"] for a in music] == [
        "audio/pr05_vivaldi_spring_cc_by_sa_v1",
        "audio/pr05_miserere_cc_by_sa_v1",
        "audio/pr05_mozart_k448_cc_by_sa_v1",
    ]
    assert [a.metadata["is_default"] for a in music] == [True, False, False]
    assert music[0].metadata["recommended"] is True
    assert music[0].metadata["content_factor"]["source"] == "LLM"
    assert music[0].metadata["content_factor"]["evidence"] == ["E08"]
    assert music[0].metadata["content_factor"]["reason"] == "-"  # FE가 추천 이유로 보인다
    assert all(a.metadata["selection_mode"] == "USER_CHOICE" for a in music)
    assert "recommended" not in music[1].metadata


def test_music_without_recommendation_keeps_the_protocol_default():
    result = resolve_activity_assets(
        get_protocol("PR-05"),
        CareRequest(text="-"),
        settings=Settings(),
        client=_no_network(),
    )
    music = [a for a in result.assets if a.slot == "MUSIC"]
    assert music[0].metadata["asset_ref"] == "audio/pr05_miserere_cc_by_sa_v1"
    assert music[0].metadata["is_default"] is True
    assert not any("recommended" in a.metadata for a in music)


def test_four_sceneries_are_mixed_seven_photos_each(monkeypatch):
    from app.schemas.content import Scenery

    captured = []
    monkeypatch.setattr(
        "app.content.resolver.PixabayProvider.search_images", _fake_pixabay(captured)
    )
    result = resolve_activity_assets(
        get_protocol("PR-01"),
        CareRequest(text="-"),
        factors=_factors(
            [Scenery.LAKE_RIVER, Scenery.SEA, Scenery.MOUNTAIN, Scenery.FOREST]
        ),
        settings=Settings(pixabay_api_key="secret"),
        client=_no_network(),
    )
    # P03(호수·강 · 바다 · 언덕)처럼 4종 — 25장을 7장씩 받아 번갈아 섞고 25장에서 자른다
    assert [count for _, count in captured] == [7, 7, 7, 7]
    assert len(result.assets) == 25
    order = [a.metadata["content_factor"]["value"] for a in result.assets[:4]]
    assert order == ["lake river", "sea", "mountain", "forest"]


def test_artwork_url_is_rewritten_to_the_be_proxy(monkeypatch):
    from app.agent.state import ActivityAsset

    iiif = "https://www.artic.edu/iiif/2/2d484387-2509-5e8e-2c43-22f9981972eb/full/843,/0/default.jpg"

    def fake_art(self, query):
        return ActivityAsset(slot="ARTWORK", url=iiif, provider="ART_INSTITUTE_OF_CHICAGO")

    monkeypatch.setattr("app.content.resolver.ArtInstituteProvider.search_artwork", fake_art)
    result = resolve_activity_assets(
        get_protocol("PR-20"), CareRequest(text="-"), settings=Settings(), client=_no_network()
    )
    art = result.assets[0]
    assert art.url == "/media/artic/2d484387-2509-5e8e-2c43-22f9981972eb/843.jpg"
    assert art.metadata["origin_url"] == iiif
