"""AI 이미지 프롬프트 — 선호 색상·풍경을 안전한 자연 이미지 요청으로 바꾼다.

기존 자연 사진 추천이 우선이고, AI 생성 이미지는 보조 콘텐츠다(효과 검증 필요).
생성 결과는 연구 자극과 동등하다고 간주하지 않고 UNVERIFIED로 표시한다.
② Care Session Executor의 prepare_session(app/agent/nodes/executor.py)이 간접 호출한다.
"""

from app.schemas.care import UserProfile


def nature_prompt(profile: UserProfile, *, meditation: bool = False) -> str:
    """AI Horde에 보낼 자연 이미지 프롬프트. 자유 사용자 문장은 넣지 않는다."""
    scenery = ", ".join(profile.preferred_scenery) or "calm forest and gentle water"
    colors = ", ".join(profile.preferred_colors)
    parts = [
        "realistic restorative nature photograph",
        scenery,
        "soft natural light",
        "stable horizon",
        "no people, no text, no logo, no buildings, no threatening animals",
    ]
    if colors:
        parts.append(f"subtle palette of {colors}")
    if profile.avoided_colors:
        parts.append(f"avoid these colors: {', '.join(profile.avoided_colors)}")
    if meditation:
        parts.append("simple quiet composition suitable for a fifteen minute meditation")
    return ", ".join(parts)
