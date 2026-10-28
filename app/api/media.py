"""외부 공급자 이미지를 BE가 대신 받아 전달한다 — 미술관(IIIF) · Pixabay 사진.

- 미술관(Art Institute of Chicago): IIIF 이미지 서버가 Cloudflare 뒤에 있어 브라우저가 직접 부르면
  이미지 대신 보안 확인 페이지가 온다(2026-10-28 실측) — FE <img>가 깨진다.
- Pixabay: API 문서가 검색 결과 이미지의 영구 핫링크를 허용하지 않고 서버로 받아 쓰라고 한다.

오픈 프록시가 되지 않게 상위 주소 규칙은 app/content/proxy.py에 고정 템플릿으로 둔다. 받은
응답이 이미지가 아니면(보안 확인 HTML 등) 502를 돌려준다. 받은 이미지는 프로세스 메모리에 조금
캐시하고 브라우저 · CDN 캐시 헤더를 단다. 자산 주소를 /media/…로 바꾸는 곳은 app/content/resolver.py
(proxied_url)다.
"""

import time
from collections import OrderedDict

import httpx
from fastapi import APIRouter, HTTPException, Response

from app.config import get_settings
from app.content.proxy import artic_upstream, pixabay_upstream

router = APIRouter(prefix="/media", tags=["media"])

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; LUMI/0.1)",
    "AIC-User-Agent": "lumi-wellness-agent-be/0.1",
    "Accept": "image/avif,image/webp,image/jpeg,image/png,image/*;q=0.8",
}
CACHE_SECONDS = 24 * 60 * 60
_MAX_CACHED = 40
_MAX_BYTES = 8 * 1024 * 1024
_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()


class UpstreamError(RuntimeError):
    """상위 서버가 이미지를 주지 않았다(차단 · 만료 · 형식)."""


def fetch_image(url: str) -> tuple[bytes, str]:
    """상위 이미지를 받는다. 테스트는 이 함수를 바꿔 끼운다."""
    cached = _cache.get(url)
    if cached and cached[0] > time.monotonic():
        _cache.move_to_end(url)
        return cached[1], cached[2]
    timeout = get_settings().content_request_timeout_seconds
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=HEADERS)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"요청 실패: {type(exc).__name__}") from exc
    content_type = response.headers.get("content-type", "").split(";")[0].strip()
    if response.status_code != 200 or not content_type.startswith("image/"):
        raise UpstreamError(f"이미지가 아님: {response.status_code} {content_type or '-'}")
    if len(response.content) > _MAX_BYTES:
        raise UpstreamError("이미지가 너무 큼")
    _cache[url] = (time.monotonic() + CACHE_SECONDS, response.content, content_type)
    while len(_cache) > _MAX_CACHED:
        _cache.popitem(last=False)
    return response.content, content_type


def _send(url: str) -> Response:
    try:
        body, content_type = fetch_image(url)
    except UpstreamError as exc:
        detail = f"공급자 이미지를 받지 못했습니다({exc})"
        raise HTTPException(status_code=502, detail=detail) from exc
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}, immutable"},
    )


@router.get("/artic/{image_id}/{width}.jpg")
def artic_image(image_id: str, width: int) -> Response:
    if not (url := artic_upstream(image_id, width)):
        raise HTTPException(status_code=404, detail="알 수 없는 이미지")
    return _send(url)


@router.get("/pixabay/{name}")
def pixabay_image(name: str) -> Response:
    if not (url := pixabay_upstream(name)):
        raise HTTPException(status_code=404, detail="알 수 없는 이미지")
    return _send(url)
