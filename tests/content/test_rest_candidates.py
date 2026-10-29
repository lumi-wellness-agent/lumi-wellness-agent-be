import random

import httpx

from app.config import Settings
from app.content import providers
from app.content.providers import ContentProviderError, PixabayProvider
from app.content.resolver import resolve_activity_assets
from app.protocols import get_protocol
from app.schemas.care import CareRequest, UserProfile


def test_candidate_lookup_uses_id_hd_and_cached_copy():
    providers._search_cache.clear()
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"hits": [{
            "id": 165178, "duration": 32, "user": "author",
            "pageURL": "https://pixabay.com/videos/id-165178/",
            "videos": {
                "large": {"url": "https://cdn/4k.mp4", "width": 3840, "height": 2160},
                "medium": {"url": "https://cdn/hd.mp4", "width": 1280, "height": 720},
            },
        }]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        provider = PixabayProvider("test", client)
        asset = provider.video_by_id(165178)
        assert asset.url == "https://cdn/hd.mp4"
        assert asset.metadata["loop_required"] is True
        assert asset.verification == "PENDING_CONTENT_REVIEW"
        asset.metadata["id"] = 0
        assert provider.video_by_id(165178).metadata["id"] == 165178
    assert len(calls) == 1
    assert calls[0].url.params["id"] == "165178"
    providers._search_cache.clear()


def test_pr10_keeps_fixed_video_across_seeds_and_other_scenery(monkeypatch):
    from app.agent.state import ActivityAsset

    calls = []

    def lookup(self, video_id, **kwargs):
        calls.append(video_id)
        return ActivityAsset(
            slot="VISUAL_VIDEO", url=f"https://cdn/{video_id}.mp4", provider="PIXABAY"
        )

    monkeypatch.setattr(PixabayProvider, "video_by_id", lookup)
    selected = set()
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError("unexpected network"))
    )) as client:
        for seed in range(12):
            result = resolve_activity_assets(
                get_protocol("PR-10"),
                CareRequest(text="-", profile=UserProfile(preferred_scenery=["sea"])),
                rng=random.Random(seed), settings=Settings(pixabay_api_key="test"),
                client=client,
            )
            assert len(result.assets) == 1
            selected.add(result.assets[0].url)
            assert result.assets[0].metadata["selection_mode"] == "FIXED"
    assert selected == {"https://cdn/165178.mp4"}
    assert set(calls) == {165178}


def test_unavailable_fixed_video_warns_without_switching(monkeypatch):
    calls = []

    def fail(self, video_id, **kwargs):
        calls.append(video_id)
        raise ContentProviderError("unavailable")

    monkeypatch.setattr(PixabayProvider, "video_by_id", fail)
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError("unexpected network"))
    )) as client:
        result = resolve_activity_assets(
            get_protocol("PR-10"), CareRequest(text="-"),
            settings=Settings(pixabay_api_key="test"), client=client,
        )
    assert calls == [165178]
    assert result.assets == []
    assert result.warnings == ["VISUAL_VIDEO: 숲 영상 후보를 불러오지 못함"]
