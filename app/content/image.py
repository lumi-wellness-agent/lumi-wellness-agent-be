"""AI 이미지 프롬프트 — 선호 색상·풍경을 안전한 자연 이미지 요청으로 바꾼다.

기존 자연 사진 추천이 우선이고, AI 생성 이미지는 보조 콘텐츠다(효과 검증 필요).
생성 결과는 연구 자극과 동등하다고 간주하지 않고 UNVERIFIED로 표시한다.
② Care Session Executor의 prepare_session(app/agent/nodes/executor.py)이 간접 호출한다.
"""

from app.schemas.care import UserProfile


def nature_prompt(
    profile: UserProfile, *, meditation: bool = False, scenery: list[str] | None = None
) -> str:
    """AI Horde에 보낼 자연 이미지 프롬프트. 자유 사용자 문장은 넣지 않는다.

    scenery는 plan이 고른 풍경의 검수된 영어 묘사(data/content_factors.json ai_prompt)다. 사용자
    칩(profile.preferred_scenery)이 있으면 resolver가 넘기지 않아 칩이 우선한다.
    """
    scenery_text = (
        ", ".join(scenery or profile.preferred_scenery) or "calm forest and gentle water"
    )
    colors = ", ".join(profile.preferred_colors)
    parts = [
        "realistic restorative nature photograph",
        scenery_text,
        "soft natural light",
        "stable horizon",
    ]
    if colors:
        parts.append(f"subtle palette of {colors}")
    if meditation:
        parts.append("simple quiet composition suitable for a fifteen minute meditation")
    # 빼고 싶은 것은 Horde 부정 프롬프트(" ### " 뒤)로 보낸다. 긍정 문장에 "no people"을 쓰면 오히려
    # 사람 · 글자가 그려지고 NSFW 검열 오탐이 잦아진다
    negative = [*NEGATIVE_TERMS, *profile.avoided_colors]
    return ", ".join(parts) + " ### " + ", ".join(negative)


NEGATIVE_TERMS = [
    "people",
    "person",
    "face",
    "body",
    "nude",
    "nsfw",
    "text",
    "watermark",
    "logo",
    "buildings",
    "threatening animals",
]
