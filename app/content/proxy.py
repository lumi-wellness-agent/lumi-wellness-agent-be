"""BE가 대신 받아 전달하는 공급자 이미지의 주소 규칙 — 라우트는 app/api/media.py.

오픈 프록시가 되지 않게 상위 주소는 공급자마다 고정 템플릿이고, 경로 조각은 형식을 검사한다.
"""

import re

from app.agent.state import ActivityAsset
from app.schemas.protocol import SlotType

# 미술관 image_id는 UUID, Pixabay /get/ 파일명은 g + 16진수 + _크기.확장자
ARTIC_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PIXABAY_FILE = re.compile(r"^g[0-9a-f]{16,256}_(?:640|960|1280|1920)\.(?:jpg|jpeg|png)$")
ARTIC_WIDTHS = (843,)  # IIIF 캐시가 있는 크기(미술관 권장)

_ARTIC_URL = re.compile(r"/iiif/2/([^/]+)/full/(\d+),/0/default\.jpg$")
_PIXABAY_URL = re.compile(r"^https://pixabay\.com/get/([^/?#]+)$")


def artic_upstream(image_id: str, width: int) -> str | None:
    if not ARTIC_ID.match(image_id) or width not in ARTIC_WIDTHS:
        return None
    return f"https://www.artic.edu/iiif/2/{image_id}/full/{width},/0/default.jpg"


def pixabay_upstream(name: str) -> str | None:
    return f"https://pixabay.com/get/{name}" if PIXABAY_FILE.match(name) else None


def proxied_url(asset: ActivityAsset) -> str | None:
    """BE 프록시 주소(/media/…). 미술관 IIIF는 Cloudflare가 브라우저 직접 호출을 막고, Pixabay는
    검색 결과 이미지의 영구 핫링크를 허용하지 않는다. 영상(Pixabay 정책상 직접 임베드 허용) ·
    AI 생성 · 검수 자산은 그대로 둔다."""
    if asset.provider == "ART_INSTITUTE_OF_CHICAGO":
        found = _ARTIC_URL.search(asset.url)
        if found and artic_upstream(found[1], int(found[2])):
            return f"/media/artic/{found[1]}/{found[2]}.jpg"
    if asset.provider == "PIXABAY" and asset.slot == SlotType.VISUAL_STILL.value:
        found = _PIXABAY_URL.match(asset.url)
        if found and pixabay_upstream(found[1]):
            return f"/media/pixabay/{found[1]}"
    return None
