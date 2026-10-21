"""사용자 입력. 'MVP에서 추천하는 사용자 입력' 표를 옮긴 초안이다.

- 현재 감정은 입력 방식을 정하지 않아 스트레스 수준(0~10)만 둔다.
- 색상 값의 형식(색 이름·HEX)은 정하지 않았다.
- 출생국가·성별·정확한 나이는 초기에 수집하지 않으므로 필드를 두지 않는다.
- 콘텐츠 유형은 '사용자 화면 예시'에서 가져왔다.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class CareGoal(StrEnum):
    """이번 세션에서 원하는 변화. 값 하나가 회복 세션 하나에 대응한다."""

    S1 = "S1"  # 반복적으로 떠오르는 생각에서 잠시 벗어나기 → 반복적 생각 완화
    S2 = "S2"  # 스트레스와 긴장감 완화 → 스트레스·긴장 완화
    S3 = "S3"  # 인지 피로 이후 주의력 회복 → 주의력·인지 피로 회복


class ContentType(StrEnum):
    CURATED_PHOTO = "CURATED_PHOTO"  # 기존 자연 사진 (우선 제공)
    AI_IMAGE = "AI_IMAGE"  # AI 생성 자연 이미지 (효과 검증 필요)


class CareRequest(BaseModel):
    """세션 추천 요청. 필수 두 항목 외에는 비워 둘 수 있다."""

    # 필수
    stress_level: int = Field(ge=0, le=10)  # 0 매우 낮음 ~ 10 매우 높음 → 추천 기능 결정
    goal: CareGoal  # 원하는 변화 → 세 모드 중 선택
    # 권장
    preferred_colors: list[str] = Field(default=[], max_length=3)  # 선호 색상 2~3개 → 팔레트
    avoided_colors: list[str] = []  # 불편하거나 싫어하는 색상 → 생성 제약 조건
    preferred_scenery: list[str] = []  # 선호 풍경 (예: 숲, 호수·강, 꽃) → 이미지 개인화
    # 선택
    accessibility_note: str | None = None  # 색각·시각 민감성 → 색 구별과 자극 강도 조절
    native_language: str | None = None  # 모국어 → 색채·감정 연상 참고
    cultural_background: str | None = None  # 주된 문화적 배경 → 색채·감정 연상 참고
    content_type: ContentType = ContentType.CURATED_PHOTO
