"""BE 이미지 프록시 라우트 — 상위 응답이 이미지일 때만 그대로 전달하고, 아니면 502."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import media

ARTIC = "2d484387-2509-5e8e-2c43-22f9981972eb"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(media.router)
    return TestClient(app)


def test_artwork_is_fetched_from_the_fixed_iiif_address(client, monkeypatch):
    asked = []

    def fake_fetch(url):
        asked.append(url)
        return b"\xff\xd8jpeg", "image/jpeg"

    monkeypatch.setattr(media, "fetch_image", fake_fetch)
    res = client.get(f"/media/artic/{ARTIC}/843.jpg")
    assert res.status_code == 200 and res.content == b"\xff\xd8jpeg"
    assert res.headers["content-type"] == "image/jpeg"
    assert "max-age=86400" in res.headers["cache-control"]
    assert asked == [f"https://www.artic.edu/iiif/2/{ARTIC}/full/843,/0/default.jpg"]


def test_a_blocked_upstream_becomes_502(client, monkeypatch):
    def blocked(url):
        raise media.UpstreamError("이미지가 아님: 403 text/html")  # Cloudflare 보안 확인 페이지

    monkeypatch.setattr(media, "fetch_image", blocked)
    res = client.get(f"/media/artic/{ARTIC}/843.jpg")
    assert res.status_code == 502


def test_unknown_paths_are_not_fetched(client, monkeypatch):
    monkeypatch.setattr(media, "fetch_image", lambda url: pytest.fail(f"불렀다: {url}"))
    assert client.get("/media/artic/not-an-id/843.jpg").status_code == 404
    assert client.get(f"/media/artic/{ARTIC}/4000.jpg").status_code == 404
    assert client.get("/media/pixabay/evil.jpg").status_code == 404
