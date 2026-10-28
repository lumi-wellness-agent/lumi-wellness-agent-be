"""BE 이미지 프록시의 주소 규칙 — 미술관 IIIF · Pixabay 사진만, 형식이 맞을 때 /media/…로."""

from app.agent.state import ActivityAsset
from app.content.proxy import artic_upstream, pixabay_upstream, proxied_url

ARTIC = "2d484387-2509-5e8e-2c43-22f9981972eb"
PIXABAY = "g2ed2d15d69aadfd5d9b6a4ac007557d44a078e5_1280.jpg"


def asset(provider: str, url: str, slot: str = "VISUAL_STILL") -> ActivityAsset:
    return ActivityAsset(slot=slot, url=url, provider=provider)


def test_artwork_and_photos_go_through_the_proxy():
    art = asset(
        "ART_INSTITUTE_OF_CHICAGO",
        f"https://www.artic.edu/iiif/2/{ARTIC}/full/843,/0/default.jpg",
        slot="ARTWORK",
    )
    assert proxied_url(art) == f"/media/artic/{ARTIC}/843.jpg"
    photo = asset("PIXABAY", f"https://pixabay.com/get/{PIXABAY}")
    assert proxied_url(photo) == f"/media/pixabay/{PIXABAY}"


def test_videos_generated_and_curated_assets_stay_direct():
    # Pixabay 정책상 영상은 직접 임베드할 수 있다
    video = asset("PIXABAY", f"https://pixabay.com/get/{PIXABAY}", slot="VISUAL_VIDEO")
    assert proxied_url(video) is None
    assert proxied_url(asset("AI_HORDE", "https://cdn.example/x.webp")) is None
    assert proxied_url(asset("CURATED_ASSET_LIBRARY", "/assets/audio/a.mp3", "MUSIC")) is None


def test_upstream_is_a_fixed_template_not_an_open_proxy():
    assert artic_upstream(ARTIC, 843) == (
        f"https://www.artic.edu/iiif/2/{ARTIC}/full/843,/0/default.jpg"
    )
    assert artic_upstream("../../etc", 843) is None
    assert artic_upstream(ARTIC, 4000) is None  # 캐시 없는 크기는 받지 않는다
    assert pixabay_upstream(PIXABAY) == f"https://pixabay.com/get/{PIXABAY}"
    assert pixabay_upstream("evil.com/x.jpg") is None
    assert pixabay_upstream("g1234_1280.jpg") is None  # 너무 짧다
    other = asset("PIXABAY", "https://evil.example/get/" + PIXABAY)
    assert proxied_url(other) is None
