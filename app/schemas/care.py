"""사용자 입력 — 자유 텍스트 + 프로필. 세션 상태(IntakeResult)는 intake 노드가 여기서 만든다.

에이전트 아키텍처 v2: 스트레스 0~10 수치 입력은 받지 않는다. 상태는 intake가 자유 텍스트에서
분류하고(목표·순서 조건·슬롯), 점수는 measure_pre가 척도로 잰다. 선호(색·풍경·접근성)는 프로필이다.

- goal · minutes_available · place는 FE가 칩으로 받아 보내면 intake의 분류보다 우선한다.
  되묻기 루프(ask_user interrupt)는 발표 범위에서 뺐다 — 빈 슬롯은 기본값으로 메운다.
- 색상 값의 형식(색 이름·HEX)은 정하지 않았다.
- 출생국가·성별·정확한 나이는 초기에 수집하지 않으므로 필드를 두지 않는다.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class CareGoal(StrEnum):
    """이번 세션에서 원하는 변화 = 결과 지표 라벨. 매체 묶음이 아니다(매체는 활동 노드가 정한다)."""

    S1 = "S1"  # 반복적으로 떠오르는 생각에서 잠시 벗어나기 → 반추 완화
    S2 = "S2"  # 스트레스와 긴장감 완화 → 긴장·스트레스 완화
    S3 = "S3"  # 인지 피로 이후 주의력 회복 → 주의·피로 회복


class ContentType(StrEnum):
    CURATED_PHOTO = "CURATED_PHOTO"  # 기존 자연 사진 (우선 제공)
    AI_IMAGE = "AI_IMAGE"  # AI 생성 자연 이미지 (효과 검증 필요)


class Place(StrEnum):
    INDOOR = "INDOOR"
    OUTDOOR = "OUTDOOR"


class UserProfile(BaseModel):
    """세션마다 다시 묻지 않는 선호. 콘텐츠 개인화(content_spec.personalization)에 쓴다."""

    preferred_colors: list[str] = Field(default=[], max_length=3)  # 선호 색상 2~3개 → 팔레트
    avoided_colors: list[str] = []  # 불편하거나 싫어하는 색상 → 생성 제약 조건
    preferred_scenery: list[str] = []  # 선호 풍경 (예: 숲, 호수·강, 꽃) → 이미지 개인화
    accessibility_note: str | None = None  # 색각·시각 민감성 → 색 구별과 자극 강도 조절
    native_language: str | None = None  # 모국어 → 색채·감정 연상 참고
    cultural_background: str | None = None  # 주된 문화적 배경 → 색채·감정 연상 참고
    content_type: ContentType = ContentType.CURATED_PHOTO


class CareRequest(BaseModel):
    """세션 시작 요청. text만 필수다."""

    text: str = Field(min_length=1, description="사용자 자연어 입력. 예: 발표 앞두고 긴장돼요")
    goal: CareGoal | None = None  # FE 칩. 있으면 intake 분류보다 우선
    minutes_available: int | None = Field(default=None, gt=0)  # FE 칩
    place: Place | None = None  # FE 칩
    profile: UserProfile = UserProfile()
