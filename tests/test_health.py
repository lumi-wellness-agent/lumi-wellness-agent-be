from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_ok():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_db_without_pool_returns_503():
    # lifespan을 돌리지 않아 DB 풀이 없다 — DATABASE_URL이 비어 있을 때와 같다
    response = TestClient(app).get("/health/db")

    assert response.status_code == 503


def test_pr21_audio_asset_is_served():
    response = TestClient(app).head("/assets/audio/pr21/korean-complete.mp3")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.headers["content-length"] == "22877110"


def test_pr05_audio_assets_are_served():
    assets = {
        "allegri-miserere-cantores-carmeli-linz-2006.mp3": "10303657",
        "mozart-k448-first-10min.mp3": "9600812",
        "vivaldi-spring-first-10min.mp3": "9600812",
    }

    for filename, expected_bytes in assets.items():
        response = TestClient(app).head(f"/assets/audio/pr05/{filename}")

        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["content-length"] == expected_bytes
