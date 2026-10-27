"""세션 API 인증 — Supabase Auth가 발급한 액세스 토큰을 검증한다.

FE가 Supabase Auth(Google · Kakao)로 로그인해 받은 액세스 토큰을 `Authorization: Bearer`로 보낸다.
프로젝트는 비대칭 서명 키(ES256)를 쓰므로 공개 키 목록(JWKS,
`{SUPABASE_URL}/auth/v1/.well-known/jwks.json`)으로 서명을 검증하고 만료 · aud(authenticated) ·
iss를 본다. sub가 사용자 ID(auth.users.id)다. 익명 로그인 토큰(is_anonymous)과 대칭 키(HS256) 토큰은
받지 않는다.

서명이 맞아도 로그인 세션이 살아 있어야 한다. 토큰의 session_id를 Supabase의 auth.sessions에서
확인한다(AuthSessions). 로그아웃 · 다른 기기 전체 로그아웃 · 탈퇴하면 행이 지워지므로, 액세스
토큰이 만료(기본 1시간)되기 전에도 바로 거절한다. auth 스키마는 Supabase가 관리하므로 id와
not_after만 본다. DB 없이 띄운 서버(DATABASE_URL 없음)는 세션을 확인하지 않는다.

JWKS는 JWKS_TTL_SECONDS 동안 캐시한다. 모르는 kid가 오면(키 교체) 한 번 다시 받는다.
SUPABASE_URL이 비어 있으면 검증기를 만들지 않고 세션 API가 503(AUTH_UNAVAILABLE)을 돌려준다 —
인증 없이 열리지 않는다. 테스트는 current_user를 dependency_overrides로 바꾼다.
"""

import time
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import httpx
import jwt
import psycopg
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg_pool import AsyncConnectionPool

JWKS_TTL_SECONDS = 600  # Supabase도 JWKS를 10분 캐시한다
ALGORITHMS = ["ES256", "RS256"]  # Supabase 비대칭 서명 키
AUDIENCE = "authenticated"


@dataclass(frozen=True)
class User:
    id: UUID  # auth.users.id = 토큰의 sub
    session_id: UUID | None = None  # auth.sessions.id = 토큰의 session_id. 토큰을 갱신해도 같다
    provider: str | None = None  # app_metadata.provider — google · kakao


class SupabaseVerifier:
    """액세스 토큰 → User. 서명 · 만료 · aud · iss가 맞지 않으면 jwt.InvalidTokenError."""

    def __init__(self, supabase_url: str, transport: httpx.AsyncBaseTransport | None = None):
        self.issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self.jwks_url = f"{self.issuer}/.well-known/jwks.json"
        self._transport = transport  # 테스트가 JWKS 응답을 바꾼다
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at: float | None = None

    async def _fetch(self) -> None:
        async with httpx.AsyncClient(timeout=5, transport=self._transport) as client:
            res = await client.get(self.jwks_url)
            res.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(res.json()).keys
        self._keys = {key.key_id: key for key in keys if key.key_id}
        self._fetched_at = time.monotonic()

    async def _key(self, kid: str | None) -> jwt.PyJWK:
        stale = self._fetched_at is None or time.monotonic() - self._fetched_at > JWKS_TTL_SECONDS
        if stale or kid not in self._keys:
            await self._fetch()
        if kid not in self._keys:
            raise jwt.InvalidTokenError("서명 키(kid)를 JWKS에서 찾지 못했다")
        return self._keys[kid]

    async def verify(self, token: str) -> User:
        header = jwt.get_unverified_header(token)
        if header.get("alg") not in ALGORITHMS:
            raise jwt.InvalidAlgorithmError(f"{header.get('alg')} 토큰은 받지 않는다")
        key = await self._key(header.get("kid"))
        claims = jwt.decode(
            token,
            key.key,
            algorithms=ALGORITHMS,
            audience=AUDIENCE,
            issuer=self.issuer,
            options={"require": ["exp", "sub", "aud", "iss", "session_id"]},
        )
        if claims.get("is_anonymous"):
            raise jwt.InvalidTokenError("익명 로그인은 받지 않는다")
        try:
            user_id, session_id = UUID(claims["sub"]), UUID(claims["session_id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise jwt.InvalidTokenError("sub · session_id가 ID가 아니다") from exc
        provider = (claims.get("app_metadata") or {}).get("provider")
        return User(id=user_id, session_id=session_id, provider=provider)


ALIVE_SQL = """
select exists (
  select 1 from auth.sessions where id = %s and (not_after is null or not_after > now())
)
"""


class AuthSessions:
    """Supabase의 로그인 세션(auth.sessions). 로그아웃하면 그 행이, 탈퇴하면 사용자의 행이 모두
    지워진다. app/db.py의 풀로 읽는다."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def alive(self, session_id: UUID) -> bool:
        async with self.pool.connection(timeout=5) as conn:
            cur = await conn.execute(ALIVE_SQL, (session_id,))
            (alive,) = await cur.fetchone()
        return alive


_bearer = HTTPBearer(auto_error=False)


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        401, {"code": code, "message": message}, headers={"WWW-Authenticate": "Bearer"}
    )


def _unavailable(message: str) -> HTTPException:
    return HTTPException(503, {"code": "AUTH_UNAVAILABLE", "message": message})


async def current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    """요청의 사용자. 토큰이 없거나 틀리거나 로그인 세션이 끝났으면 401, 확인할 수 없으면 503."""
    verifier: SupabaseVerifier | None = getattr(request.app.state, "auth", None)
    if verifier is None:
        raise _unavailable("인증을 설정하지 않았습니다. SUPABASE_URL을 확인하세요.")
    if credentials is None:
        raise _unauthorized("UNAUTHENTICATED", "로그인이 필요합니다.")
    try:
        user = await verifier.verify(credentials.credentials)
    except jwt.InvalidTokenError as exc:
        raise _unauthorized("INVALID_TOKEN", "로그인 정보가 맞지 않거나 만료됐습니다.") from exc
    except (httpx.HTTPError, jwt.PyJWKSetError) as exc:
        raise _unavailable("로그인 서버의 공개 키를 받지 못했습니다.") from exc
    sessions: AuthSessions | None = getattr(request.app.state, "auth_sessions", None)
    if sessions is not None:
        try:
            alive = await sessions.alive(user.session_id)
        except psycopg.Error as exc:  # 풀 대기 시간 초과(PoolTimeout)도 여기로 온다
            raise _unavailable("로그인 세션을 확인하지 못했습니다.") from exc
        if not alive:
            message = "로그아웃했거나 탈퇴한 계정의 로그인입니다. 다시 로그인하세요."
            raise _unauthorized("SESSION_REVOKED", message)
    return user


CurrentUser = Annotated[User, Depends(current_user)]
