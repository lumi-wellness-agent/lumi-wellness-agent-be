"""로그인 토큰 검증 — 가짜 JWKS(EC P-256)로 서명한 토큰을 검증기와 current_user에 넣는다."""

import asyncio
import time
import uuid

import httpx
import jwt
import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import ECAlgorithm

from app.auth import CurrentUser, SupabaseVerifier

SUPABASE_URL = "https://example.supabase.co"
ISSUER = f"{SUPABASE_URL}/auth/v1"
USER_ID = "8f9c2b1e-4d3a-4f6b-9a7c-1e2d3c4b5a69"
SESSION_ID = "3b2f6c4e-9d1a-4e7b-8c5f-0a1b2c3d4e5f"


class Keys:
    """JWKS를 내주는 가짜 Supabase. rotate()로 키를 바꾸고, 받아 간 횟수를 센다."""

    def __init__(self):
        self.fetches = 0
        self.rotate()

    def rotate(self) -> None:
        self.kid = str(uuid.uuid4())
        self.private = ec.generate_private_key(ec.SECP256R1())

    def jwks(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{ISSUER}/.well-known/jwks.json"
        self.fetches += 1
        jwk = ECAlgorithm.to_jwk(self.private.public_key(), as_dict=True)
        return httpx.Response(200, json={"keys": [{**jwk, "kid": self.kid, "alg": "ES256"}]})

    def token(self, **claims) -> str:
        now = int(time.time())
        body = {
            "sub": USER_ID,
            "session_id": SESSION_ID,
            "aud": "authenticated",
            "iss": ISSUER,
            "exp": now + 3600,
            "iat": now,
            "role": "authenticated",
            "is_anonymous": False,
            "app_metadata": {"provider": "kakao", "providers": ["kakao"]},
            **claims,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return jwt.encode(body, self.private, algorithm="ES256", headers={"kid": self.kid})


@pytest.fixture
def keys() -> Keys:
    return Keys()


def verifier_for(keys: Keys) -> SupabaseVerifier:
    return SupabaseVerifier(SUPABASE_URL + "/", transport=httpx.MockTransport(keys.jwks))


def verify(verifier: SupabaseVerifier, token: str):
    return asyncio.run(verifier.verify(token))


def test_a_supabase_token_gives_the_user(keys):
    user = verify(verifier_for(keys), keys.token())
    assert (str(user.id), str(user.session_id), user.provider) == (USER_ID, SESSION_ID, "kakao")


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 10},  # 만료
        {"aud": "anon"},  # 다른 대상
        {"iss": "https://other.supabase.co/auth/v1"},  # 다른 프로젝트
        {"is_anonymous": True},  # 익명 로그인
        {"sub": "service-role"},  # 사용자 ID가 아니다
        {"sub": None},  # sub 없음
        {"session_id": None},  # 로그인 세션이 없는 토큰
        {"session_id": "not-a-uuid"},
    ],
)
def test_tokens_that_are_not_a_signed_in_user_of_this_project_are_refused(keys, claims):
    with pytest.raises(jwt.InvalidTokenError):
        verify(verifier_for(keys), keys.token(**claims))


def test_a_token_signed_with_another_key_or_a_shared_secret_is_refused(keys):
    verifier = verifier_for(keys)
    stranger = Keys()
    stranger.kid = keys.kid  # 같은 kid를 내세워도 서명이 맞지 않는다
    with pytest.raises(jwt.InvalidTokenError):
        verify(verifier, stranger.token())
    hs256 = jwt.encode({"sub": USER_ID}, "legacy-secret-that-is-long-enough!", algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        verify(verifier, hs256)
    with pytest.raises(jwt.InvalidTokenError):
        verify(verifier, "not-a-jwt")


def test_jwks_is_cached_and_refetched_once_for_a_new_key(keys):
    verifier = verifier_for(keys)
    verify(verifier, keys.token())
    verify(verifier, keys.token())
    assert keys.fetches == 1
    keys.rotate()  # Supabase에서 서명 키를 바꿨다
    verify(verifier, keys.token())
    assert keys.fetches == 2


class Sessions:
    """auth.sessions 대역 — alive에 든 세션만 살아 있다. down이면 DB 오류를 낸다."""

    def __init__(self, *alive: str, down: bool = False):
        self.alive_ids = {uuid.UUID(sid) for sid in alive}
        self.down = down

    async def alive(self, session_id: uuid.UUID) -> bool:
        if self.down:
            raise psycopg.OperationalError("connection refused")
        return session_id in self.alive_ids


def client_with(verifier: SupabaseVerifier | None, sessions: Sessions | None = None) -> TestClient:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(user: CurrentUser) -> dict:
        return {"id": str(user.id)}

    app.state.auth = verifier
    app.state.auth_sessions = sessions
    return TestClient(app)


def test_current_user_answers_401_without_a_valid_token_and_503_without_a_verifier(keys):
    client = client_with(verifier_for(keys))
    ok = client.get("/whoami", headers={"Authorization": f"Bearer {keys.token()}"})
    assert ok.json() == {"id": USER_ID}

    missing = client.get("/whoami")
    assert (missing.status_code, missing.json()["detail"]["code"]) == (401, "UNAUTHENTICATED")
    assert missing.headers["www-authenticate"] == "Bearer"
    expired = keys.token(exp=int(time.time()) - 10)
    res = client.get("/whoami", headers={"Authorization": f"Bearer {expired}"})
    assert (res.status_code, res.json()["detail"]["code"]) == (401, "INVALID_TOKEN")

    res = client_with(None).get("/whoami", headers={"Authorization": f"Bearer {keys.token()}"})
    assert (res.status_code, res.json()["detail"]["code"]) == (503, "AUTH_UNAVAILABLE")


def test_an_unreachable_jwks_is_a_503(keys):
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502)

    verifier = SupabaseVerifier(SUPABASE_URL, transport=httpx.MockTransport(down))
    res = client_with(verifier).get("/whoami", headers={"Authorization": f"Bearer {keys.token()}"})
    assert (res.status_code, res.json()["detail"]["code"]) == (503, "AUTH_UNAVAILABLE")


def test_a_token_of_an_ended_login_session_is_refused_at_once(keys):
    # 로그아웃 · 다른 기기 전체 로그아웃 · 탈퇴하면 auth.sessions의 행이 지워진다
    bearer = {"Authorization": f"Bearer {keys.token()}"}
    assert client_with(verifier_for(keys), Sessions(SESSION_ID)).get(
        "/whoami", headers=bearer
    ).json() == {"id": USER_ID}
    res = client_with(verifier_for(keys), Sessions()).get("/whoami", headers=bearer)
    assert (res.status_code, res.json()["detail"]["code"]) == (401, "SESSION_REVOKED")
    # 세션을 확인할 수 없으면 통과시키지 않는다
    res = client_with(verifier_for(keys), Sessions(down=True)).get("/whoami", headers=bearer)
    assert (res.status_code, res.json()["detail"]["code"]) == (503, "AUTH_UNAVAILABLE")
