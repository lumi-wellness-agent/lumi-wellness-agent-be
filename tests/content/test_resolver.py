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
