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
