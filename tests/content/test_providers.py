import httpx
import pytest

from app.content.providers import (
    AIHordeProvider,
    ArtInstituteProvider,
    FreesoundProvider,
    PixabayProvider,
)


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_pixabay_selects_hd_video_and_keeps_attribution():
    def handler(request):
        assert request.url.params["key"] == "secret"
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "id": 7,
                        "user": "maker",
                        "pageURL": "https://pixabay.com/videos/id-7/",
                        "duration": 45,
                        "videos": {
                            "small": {"url": "https://cdn/s.mp4", "width": 640, "height": 360},
                            "large": {"url": "https://cdn/l.mp4", "width": 1920, "height": 1080},
                        },
                    }
                ]
            },
        )

    asset = PixabayProvider("secret", client_for(handler)).search_video("forest")
    assert asset.url == "https://cdn/l.mp4"
    assert asset.creator == "maker"
    assert asset.license == "Pixabay Content License"
    assert asset.verification == "PENDING_CONTENT_REVIEW"
    assert asset.metadata["loop_required"] is True


def test_pixabay_returns_requested_horizontal_nature_images():
    def handler(request):
        assert request.url.path == "/api/"
        assert request.url.params["image_type"] == "photo"
        assert request.url.params["orientation"] == "horizontal"
        return httpx.Response(
            200,
            json={
                "hits": [
                    {
                        "id": image_id,
                        "user": "maker",
                        "pageURL": f"https://pixabay.com/photos/{image_id}/",
                        "largeImageURL": f"https://cdn/{image_id}.jpg",
                        "imageWidth": 1920,
                        "imageHeight": 1080,
                        "tags": "forest, water",
                    }
                    for image_id in range(1, 4)
                ]
            },
        )

    assets = PixabayProvider("secret", client_for(handler)).search_images(
        "forest-images", count=3
    )
    assert len(assets) == 3
    assert all(asset.slot == "VISUAL_STILL" for asset in assets)
    assert all(asset.verification == "PENDING_CONTENT_REVIEW" for asset in assets)


def test_freesound_rejects_non_allowlisted_license():
    def handler(request):
        assert request.headers["Authorization"] == "Token secret"
        assert request.url.path == "/apiv2/search/"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 1,
                        "name": "not allowed",
                        "username": "a",
                        "license": "https://creativecommons.org/licenses/by-nc/4.0/",
                        "url": "https://freesound.org/s/1/",
                        "previews": {"preview-hq-mp3": "https://cdn/1.mp3"},
                    },
                    {
                        "id": 2,
                        "name": "forest",
                        "username": "b",
                        "license": "https://creativecommons.org/licenses/by/4.0/",
                        "url": "https://freesound.org/s/2/",
                        "previews": {"preview-hq-mp3": "https://cdn/2.mp3"},
                        "tags": ["forest"],
                    },
                ]
            },
        )

    asset = FreesoundProvider("secret", client_for(handler)).search_sound("forest")
    assert asset.url == "https://cdn/2.mp3"
    assert asset.license == "CC BY"


def test_freesound_applies_duration_and_excluded_term_filters():
    def handler(request):
        assert request.url.params["filter"] == "duration:[240 TO *]"
        assert request.url.params["sort"] == "rating_desc"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 1,
                        "name": "forest with traffic",
                        "username": "a",
                        "license": "https://creativecommons.org/licenses/by/4.0/",
                        "url": "https://freesound.org/s/1/",
                        "previews": {"preview-hq-mp3": "https://cdn/1.mp3"},
                        "duration": 500,
                        "tags": ["forest", "car"],
                    },
                    {
                        "id": 2,
                        "name": "quiet forest",
                        "username": "b",
                        "license": "https://creativecommons.org/licenses/by/4.0/",
                        "url": "https://freesound.org/s/2/",
                        "previews": {"preview-hq-mp3": "https://cdn/2.mp3"},
                        "duration": 600,
                        "tags": ["forest", "birds"],
                    },
                ]
            },
        )

    asset = FreesoundProvider("secret", client_for(handler)).search_sound(
        "quiet-forest-filtered", min_duration_seconds=240, exclude_terms=("traffic", "car")
    )
    assert asset.metadata["id"] == 2
    assert asset.metadata["duration_seconds"] == 600
    assert asset.verification == "PENDING_CONTENT_REVIEW"


def test_art_institute_only_returns_public_domain_work():
    def handler(request):
        assert request.url.params["query[term][is_public_domain]"] == "true"
        assert request.headers["AIC-User-Agent"] == "lumi-wellness-agent-be/0.1"
        return httpx.Response(
            200,
            json={
                "config": {"iiif_url": "https://iiif.example"},
                "data": [
                    {"id": 1, "image_id": "private", "is_public_domain": False},
                    {
                        "id": 2,
                        "title": "Water",
                        "artist_display": "Artist",
                        "image_id": "public",
                        "is_public_domain": True,
                    },
                ],
            },
        )

    asset = ArtInstituteProvider(client_for(handler)).search_artwork("water")
    assert asset.url == "https://iiif.example/public/full/843,/0/default.jpg"
    assert asset.license == "PUBLIC_DOMAIN"
    assert asset.verification == "PENDING_CONTENT_REVIEW"


def test_ai_horde_marks_generation_unverified(monkeypatch):
    responses = iter(
        [
            {"id": "job"},
            {"done": False},
            {"done": True},
            {"generations": [{"img": "https://cdn/generated.webp", "model": "m", "seed": "1"}]},
        ]
    )

    def handler(request):
        assert request.headers["apikey"] == "secret"
        return httpx.Response(200, json=next(responses))

    monkeypatch.setattr("app.content.providers.time.sleep", lambda _: None)
    asset = AIHordeProvider(
        "secret", client_for(handler), poll_timeout_seconds=1, poll_interval_seconds=0
    ).generate_image("nature")
    assert asset.generated is True
    assert asset.verification == "UNVERIFIED"
    assert asset.metadata["request_id"] == "job"


def test_ai_horde_splits_batches_at_twenty(monkeypatch):
    submitted_counts = []
    submitted_params = []
    jobs = iter(["job-1", "job-2"])

    def handler(request):
        if request.method == "POST":
            import json

            payload = json.loads(request.content)
            submitted_counts.append(payload["params"]["n"])
            submitted_params.append(payload)
            return httpx.Response(200, json={"id": next(jobs)})
        if "/check/" in request.url.path:
            return httpx.Response(200, json={"done": True})
        job_id = request.url.path.rsplit("/", 1)[-1]
        count = 20 if job_id == "job-1" else 5
        return httpx.Response(
            200,
            json={"generations": [{"img": f"https://cdn/{job_id}-{i}.webp"} for i in range(count)]},
        )

    monkeypatch.setattr("app.content.providers.time.sleep", lambda _: None)
    assets = AIHordeProvider(
        "secret", client_for(handler), poll_timeout_seconds=1, poll_interval_seconds=0
    ).generate_images("nature", count=25)
    assert submitted_counts == [20, 5]
    assert all(payload["params"]["width"] == 768 for payload in submitted_params)
    assert all(payload["params"]["height"] == 512 for payload in submitted_params)
    assert all(payload["trusted_workers"] is False for payload in submitted_params)
    assert all(payload["allow_downgrade"] is True for payload in submitted_params)
    assert len(assets) == 25


@pytest.mark.parametrize("status", [401, 429, 500])
def test_provider_http_errors_do_not_expose_key(status):
    client = client_for(lambda request: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(RuntimeError) as exc:
        PixabayProvider("top-secret", client).search_video(f"forest-{status}")
    assert "top-secret" not in str(exc.value)
