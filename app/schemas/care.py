"""사용자 입력 — 자유 텍스트 + 프로필. 세션 상태(IntakeResult)는 intake 노드가 여기서 만든다.

에이전트 아키텍처 v2: 스트레스 0~10 수치 입력은 받지 않는다. 상태는 intake가 자유 텍스트에서
분류하고(목표·순서 조건·슬롯), 점수는 measure_pre가 척도로 잰다. 선호(색·풍경·접근성)는 프로필이다.

- FE 칩은 환경 조건(setting · earphones · minutes_available)만 받는다. 칩 값은 intake가 문장에서
  읽은 값보다 우선한다. 목표(Goal)와 상태(State)는 intake가 추론하고, 부족하면 질문한다
  (app/agent/nodes/intake.py · app/schemas/intake.py).
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
    """실내 · 실외. intake가 setting에서 정하고(place_for) 플래너 입력에 쓴다."""

    INDOOR = "INDOOR"
    OUTDOOR = "OUTDOOR"


class Setting(StrEnum):
    """지금 있는 곳(환경 칩). 사용자는 소리·화면 같은 조건이 아니라 장소로 답한다.

    Place(INDOOR · OUTDOOR)는 그대로 두고, 코드가 setting에서 Place(place_for)와 환경 조건
    (environment_for)을 정한다.
    """

    HOME = "HOME"  # 집이나 혼자 있는 방
    OFFICE_CAFE = "OFFICE_CAFE"  # 사무실·카페(사람이 있음)
    TRANSIT = "TRANSIT"  # 지하철·버스
    OUTDOOR = "OUTDOOR"  # 밖에 나갈 수 있음


class Environment(BaseModel):
    """활동 노드의 eligibility(needs · quiet_required · place)와 맞대는 환경 조건. None = 모름."""

    audio: bool | None = None  # 소리를 들을 수 있다 — needs AUDIO
    quiet: bool | None = None  # 조용한 곳이다 — quiet_required
    screen: bool | None = None  # 화면을 볼 수 있다 — needs SCREEN
    outdoor: bool | None = None  # 밖에 나갈 수 있다 — needs OUTDOORS · place OUTDOOR


def place_for(setting: Setting | None) -> Place | None:
    """setting → Place. 밖이면 OUTDOOR, 그 밖의 장소는 INDOOR, 모르면 None."""
    if setting is None:
        return None
    return Place.OUTDOOR if setting is Setting.OUTDOOR else Place.INDOOR


def environment_for(setting: Setting | None, earphones: bool | None) -> Environment:
    """setting + 이어폰 → 환경 조건. FE와 BE가 어긋나지 않게 이 표 하나만 둔다.

    사무실·카페에서 이어폰이 있으면 소리 활동을 할 수 있다고 본다(소리가 남에게 새지 않고 주변
    소음을 막는다). 지하철·버스는 이어폰이 있어도 조용한 곳으로 보지 않는다(안내 방송 · 이동).
    장소를 모르면 전부 None이다 — intake_finish가 기본값을 정한다.
    """
    if setting is None:
        return Environment()
    if setting is Setting.HOME:
        return Environment(audio=True, quiet=True, screen=True, outdoor=False)
    if setting is Setting.OFFICE_CAFE:
        return Environment(audio=bool(earphones), quiet=bool(earphones), screen=True, outdoor=False)
    if setting is Setting.TRANSIT:
        return Environment(audio=bool(earphones), quiet=False, screen=True, outdoor=False)
    return Environment(audio=True, quiet=True, screen=True, outdoor=True)  # OUTDOOR


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
    """세션 시작 요청. text만 필수다.

    FE 칩은 환경 조건만이다 — setting · 이어폰 · 시간. 목표와 상태는 intake가 문장과 대화로
    추론한다. goal · place는 리허설(scripts/plan_trial.py)과 옛 FE 호환용으로 남겼다.
    """

    text: str = Field(min_length=1, description="사용자 자연어 입력. 예: 발표 앞두고 긴장돼요")
    setting: Setting | None = None  # FE 환경 칩. 코드가 Place로 바꾼다(place_for)
    earphones: bool | None = None  # FE 환경 칩(토글)
    minutes_available: int | None = Field(default=None, gt=0)  # FE 환경 칩
    goal: CareGoal | None = None  # 리허설용. 있으면 목표 추론을 건너뛴다(goal_source = CHIP)
    place: Place | None = None  # 옛 칩. setting이 있으면 setting이 우선
    profile: UserProfile = UserProfile()
