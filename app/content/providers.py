"""A 콘텐츠 외부 공급자 클라이언트.

각 클라이언트는 공급자 응답을 ActivityAsset으로 정규화한다. API 키, 원문 사용자 입력과
공급자 응답 전체는 로그나 상태에 남기지 않는다.
"""

import time
from typing import Any

import httpx

from app.agent.state import ActivityAsset

_PROVIDER_CACHE_SECONDS = 24 * 60 * 60
_search_cache: dict[tuple[str, str], tuple[float, ActivityAsset]] = {}
_image_search_cache: dict[tuple[str, str], tuple[float, list[ActivityAsset]]] = {}


def _cached(provider: str, query: str) -> ActivityAsset | None:
    cached = _search_cache.get((provider, query))
    if cached is None or cached[0] <= time.monotonic():
        return None
    return cached[1].model_copy(deep=True)


def _cache(provider: str, query: str, asset: ActivityAsset, ttl_seconds: int) -> None:
    _search_cache[(provider, query)] = (
        time.monotonic() + ttl_seconds,
        asset.model_copy(deep=True),
    )


def _cached_images(provider: str, query: str) -> list[ActivityAsset] | None:
    cached = _image_search_cache.get((provider, query))
    if cached is None or cached[0] <= time.monotonic():
        return None
    return [asset.model_copy(deep=True) for asset in cached[1]]


def _cache_images(
    provider: str, query: str, assets: list[ActivityAsset], ttl_seconds: int
) -> None:
    _image_search_cache[(provider, query)] = (
        time.monotonic() + ttl_seconds,
        [asset.model_copy(deep=True) for asset in assets],
    )


class ContentProviderError(RuntimeError):
    """공급자 호출 또는 응답 검증 실패."""


def _request_json(client: httpx.Client, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = client.request(method, url, **kwargs)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ContentProviderError(f"공급자 요청 실패: {type(exc).__name__}") from exc
    if not isinstance(body, dict):
        raise ContentProviderError("공급자 응답 형식이 올바르지 않음")
    return body


# 자연 사진 명세(PR-01 · 03 · 21 content_spec.selection.exclude_tags)의
# "사람 · 문자·로고 · 인공 구조물 중심"을 Pixabay 태그로 거른다. 태그에 이 단어가 있으면 뺀다
# (예: "lake bled, church" → church).
EXCLUDED_IMAGE_TAGS = (
    "people",
    "person",
    "man",
    "woman",
    "girl",
    "boy",
    "child",
    "human",
    "crowd",
    "building",
    "church",
    "architecture",
    "castle",
    "tower",
    "city",
    "town",
    "village",
    "house",
    "temple",
    "monument",
    "bridge",
    "road",
    "street",
    "car",
    "text",
    "logo",
    "sign",
)


def _excluded(tags: list[str]) -> bool:
    words = {w for tag in tags for w in tag.lower().replace("-", " ").split()}
    return any(word in words for word in EXCLUDED_IMAGE_TAGS)


class PixabayProvider:
    image_base_url = "https://pixabay.com/api/"
    video_base_url = "https://pixabay.com/api/videos/"

    def __init__(self, api_key: str, client: httpx.Client):
        self.api_key = api_key
        self.client = client

    def search_images(self, query: str, *, count: int = 1) -> list[ActivityAsset]:
        if count < 1 or count > 200:
            raise ValueError("count는 1 이상 200 이하여야 함")
        cache_key = f"image:{query}:{count}"
        if cached := _cached_images("PIXABAY", cache_key):
            return cached
        body = _request_json(
            self.client,
            "GET",
            self.image_base_url,
            params={
                "key": self.api_key,
                "q": query,
                "category": "nature",
                "image_type": "photo",
                "orientation": "horizontal",
                "safesearch": "true",
                "min_width": 1280,
                "min_height": 720,
                "order": "popular",
                "per_page": max(20, min(count * 3, 200)),  # 태그로 거르는 몫을 남긴다
            },
        )
        assets: list[ActivityAsset] = []
        for hit in body.get("hits", []):
            url = hit.get("largeImageURL") or hit.get("webformatURL")
            if not url or hit.get("imageWidth", 0) < 1280 or hit.get("imageHeight", 0) < 720:
                continue
            tags = [tag.strip() for tag in str(hit.get("tags", "")).split(",") if tag.strip()]
            if _excluded(tags):
                continue
            assets.append(
                ActivityAsset(
                    slot="VISUAL_STILL",
                    url=url,
                    provider="PIXABAY",
                    title=f"Pixabay nature image {hit.get('id')}",
                    creator=hit.get("user"),
                    source_url=hit.get("pageURL"),
                    license="Pixabay Content License",
                    verification="PENDING_CONTENT_REVIEW",
                    metadata={
                        "id": hit.get("id"),
                        "width": hit.get("imageWidth"),
                        "height": hit.get("imageHeight"),
                        "tags": tags,
                        "automated_screening": {
                            "category": "nature",
                            "image_type": "photo",
                            "orientation": "horizontal",
                            "safesearch": True,
                            "min_width": 1280,
                            "min_height": 720,
                        },
                    },
                )
            )
            if len(assets) == count:
                break
        if len(assets) < count:
            raise ContentProviderError(
                f"조건에 맞는 Pixabay 자연 이미지가 부족함: 요청 {count}, 확보 {len(assets)}"
            )
        _cache_images("PIXABAY", cache_key, assets, _PROVIDER_CACHE_SECONDS)
        return assets

    def search_video(
        self,
        query: str,
        *,
        min_source_duration_seconds: int = 30,
        session_duration_seconds: int = 600,
    ) -> ActivityAsset:
        cache_key = f"video:{query}:{min_source_duration_seconds}:{session_duration_seconds}"
        if cached := _cached("PIXABAY", cache_key):
            return cached
        body = _request_json(
            self.client,
            "GET",
            self.video_base_url,
            params={
                "key": self.api_key,
                "q": query,
                "category": "nature",
                "video_type": "film",
                "safesearch": "true",
                "per_page": 100,
            },
        )
        hits = sorted(
            body.get("hits", []), key=lambda hit: hit.get("duration", 0), reverse=True
        )
        for hit in hits:
            source_duration = int(hit.get("duration") or 0)
            if source_duration < min_source_duration_seconds:
                continue
            videos = hit.get("videos", {})
            candidates = sorted(
                (v for v in videos.values() if isinstance(v, dict)),
                key=lambda v: (v.get("width", 0) * v.get("height", 0)),
                reverse=True,
            )
            video = next(
                (
                    v
                    for v in candidates
                    if v.get("url") and v.get("width", 0) >= 1280 and v.get("height", 0) >= 720
                ),
                None,
            )
            if video:
                asset = ActivityAsset(
                    slot="VISUAL_VIDEO",
                    url=video["url"],
                    provider="PIXABAY",
                    title=f"Pixabay nature video {hit.get('id')}",
                    creator=hit.get("user"),
                    source_url=hit.get("pageURL"),
                    license="Pixabay Content License",
                    verification="PENDING_CONTENT_REVIEW",
                    metadata={
                        "id": hit.get("id"),
                        "width": video.get("width"),
                        "height": video.get("height"),
                        "source_duration_seconds": source_duration,
                        "session_duration_seconds": session_duration_seconds,
                        "loop_required": source_duration < session_duration_seconds,
                        "automated_screening": {
                            "category": "nature",
                            "video_type": "film",
                            "safesearch": True,
                            "min_width": 1280,
                            "min_height": 720,
                            "min_source_duration_seconds": min_source_duration_seconds,
                        },
                    },
                )
                # Pixabay 정책상 같은 검색 요청은 24시간 캐시한다.
                _cache("PIXABAY", cache_key, asset, _PROVIDER_CACHE_SECONDS)
                return asset
        raise ContentProviderError("조건에 맞는 Pixabay 자연 영상을 찾지 못함")


def _allowed_freesound_license(value: str) -> str | None:
    lowered = value.lower()
    if "publicdomain/zero" in lowered or "creativecommons.org/publicdomain" in lowered:
        return "CC0"
    if "creativecommons.org/licenses/by/" in lowered:
        return "CC BY"
    return None


class FreesoundProvider:
    base_url = "https://freesound.org/apiv2/search/"

    def __init__(self, api_key: str, client: httpx.Client):
        self.api_key = api_key
        self.client = client

    def search_sound(
        self,
        query: str,
        *,
        min_duration_seconds: int = 0,
        exclude_terms: tuple[str, ...] = (),
    ) -> ActivityAsset:
        normalized_excludes = tuple(term.casefold() for term in exclude_terms)
        cache_key = f"{query}:{min_duration_seconds}:{','.join(normalized_excludes)}"
        if cached := _cached("FREESOUND", cache_key):
            return cached
        params = {
            "query": query,
            "fields": (
                "id,name,username,license,url,previews,duration,tags,avg_rating,num_downloads"
            ),
            "sort": "rating_desc",
            "page_size": 100,
        }
        if min_duration_seconds > 0:
            params["filter"] = f"duration:[{min_duration_seconds} TO *]"
        body = _request_json(
            self.client,
            "GET",
            self.base_url,
            params=params,
            headers={"Authorization": f"Token {self.api_key}"},
        )
        for sound in body.get("results", []):
            license_name = _allowed_freesound_license(str(sound.get("license", "")))
            preview = sound.get("previews", {}).get("preview-hq-mp3")
            duration = float(sound.get("duration") or 0)
            searchable = " ".join(
                [str(sound.get("name", "")), *[str(tag) for tag in sound.get("tags", [])]]
            ).casefold()
            if (
                not (license_name and preview)
                or duration < min_duration_seconds
                or any(term in searchable for term in normalized_excludes)
            ):
                continue
            asset = ActivityAsset(
                slot="SOUNDSCAPE",
                url=preview,
                provider="FREESOUND",
                title=sound.get("name"),
                creator=sound.get("username"),
                source_url=sound.get("url"),
                license=license_name,
                verification="PENDING_CONTENT_REVIEW",
                metadata={
                    "id": sound.get("id"),
                    "duration_seconds": duration,
                    "tags": sound.get("tags", []),
                    "avg_rating": sound.get("avg_rating"),
                    "num_downloads": sound.get("num_downloads"),
                    "automated_screening": {
                        "min_duration_seconds": min_duration_seconds,
                        "exclude_terms": list(normalized_excludes),
                        "license_allowlist": ["CC0", "CC BY"],
                    },
                },
            )
            _cache("FREESOUND", cache_key, asset, _PROVIDER_CACHE_SECONDS)
            return asset
        raise ContentProviderError("허용 라이선스의 Freesound 자연음을 찾지 못함")


class ArtInstituteProvider:
    base_url = "https://api.artic.edu/api/v1/artworks/search"

    def __init__(self, client: httpx.Client):
        self.client = client

    def search_artwork(self, query: str) -> ActivityAsset:
        fields = ",".join(
            [
                "id",
                "title",
                "artist_display",
                "date_display",
                "image_id",
                "is_public_domain",
                "copyright_notice",
            ]
        )
        body = _request_json(
            self.client,
            "GET",
            self.base_url,
            params={
                "q": query,
                "query[term][is_public_domain]": "true",
                "limit": 20,
                "fields": fields,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; LUMI/0.1)",
                "AIC-User-Agent": "lumi-wellness-agent-be/0.1",
                "Accept": "application/json",
            },
        )
        iiif_url = body.get("config", {}).get("iiif_url", "https://www.artic.edu/iiif/2")
        for artwork in body.get("data", []):
            if not artwork.get("is_public_domain") or not artwork.get("image_id"):
                continue
            artwork_id = artwork.get("id")
            return ActivityAsset(
                slot="ARTWORK",
                url=f"{iiif_url}/{artwork['image_id']}/full/843,/0/default.jpg",
                provider="ART_INSTITUTE_OF_CHICAGO",
                title=artwork.get("title"),
                creator=artwork.get("artist_display"),
                source_url=f"https://www.artic.edu/artworks/{artwork_id}",
                license="PUBLIC_DOMAIN",
                verification="PENDING_CONTENT_REVIEW",
                metadata={
                    "id": artwork_id,
                    "date_display": artwork.get("date_display"),
                    "copyright_notice": artwork.get("copyright_notice"),
                    "automated_screening": {
                        "is_public_domain": True,
                        "has_image_id": True,
                    },
                },
            )
        raise ContentProviderError("공개 도메인 미술 작품을 찾지 못함")


class AIHordeProvider:
    base_url = "https://stablehorde.net/api/v2"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client,
        *,
        poll_timeout_seconds: float = 60.0,
        poll_interval_seconds: float = 2.0,
    ):
        self.api_key = api_key
        self.client = client
        self.poll_timeout_seconds = poll_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds

    @property
    def headers(self) -> dict[str, str]:
        return {"apikey": self.api_key, "Client-Agent": "lumi-wellness-agent-be:0.1"}

    def _submit(self, prompt: str, count: int) -> str:
        submitted = _request_json(
            self.client,
            "POST",
            f"{self.base_url}/generate/async",
            headers=self.headers,
            json={
                "prompt": prompt,
                "params": {"n": count, "width": 768, "height": 512, "steps": 20},
                "nsfw": False,
                "trusted_workers": False,
                "slow_workers": True,
                "censor_nsfw": True,
                "allow_downgrade": True,
            },
        )
        request_id = submitted.get("id")
        if not request_id:
            raise ContentProviderError("AI Horde가 생성 요청 ID를 반환하지 않음")
        return str(request_id)

    def generate_images(self, prompt: str, *, count: int = 1) -> list[ActivityAsset]:
        """최대 20장인 Horde 배치 제한에 맞춰 요청하고 하나의 제한 시간 안에서 회수한다."""
        if count < 1:
            raise ValueError("count는 1 이상이어야 함")
        request_ids: list[str] = []
        remaining = count
        while remaining:
            batch_size = min(remaining, 20)
            request_ids.append(self._submit(prompt, batch_size))
            remaining -= batch_size

        deadline = time.monotonic() + self.poll_timeout_seconds
        pending = set(request_ids)
        assets: list[ActivityAsset] = []
        while pending and time.monotonic() < deadline:
            for request_id in tuple(pending):
                check = _request_json(
                    self.client,
                    "GET",
                    f"{self.base_url}/generate/check/{request_id}",
                    headers=self.headers,
                )
                if check.get("faulted"):
                    raise ContentProviderError("AI Horde 생성 작업 실패")
                if not check.get("done"):
                    continue
                status = _request_json(
                    self.client,
                    "GET",
                    f"{self.base_url}/generate/status/{request_id}",
                    headers=self.headers,
                )
                for generation in status.get("generations", []):
                    if not generation.get("img"):
                        continue
                    assets.append(
                        ActivityAsset(
                            slot="VISUAL_STILL",
                            url=generation["img"],
                            provider="AI_HORDE",
                            title="AI-generated restorative nature image",
                            generated=True,
                            verification="UNVERIFIED",
                            metadata={
                                "request_id": request_id,
                                "model": generation.get("model"),
                                "seed": generation.get("seed"),
                            },
                        )
                    )
                pending.remove(request_id)
            if pending:
                time.sleep(self.poll_interval_seconds)
        if pending:
            raise ContentProviderError("AI Horde 이미지 생성 대기 시간 초과")
        if len(assets) < count:
            raise ContentProviderError("AI Horde가 요청한 수량의 이미지 URL을 반환하지 않음")
        return assets[:count]

    def generate_image(self, prompt: str) -> ActivityAsset:
        return self.generate_images(prompt, count=1)[0]
