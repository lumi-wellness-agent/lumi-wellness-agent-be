"""콘텐츠 Factor — 활동 노드 A(자연 자극 감상)의 콘텐츠를 무엇으로 채울지 고르는 값.

plan의 LLM 호출 2(추천 이유)가 고른 활동에 맞춰 고르고(app/agent/nodes/planner.py), prepare가 공급자
검색어로 바꾼다(app/content/resolver.py). LLM은 검색어를 쓰지 않는다 — 아래 Enum 값만 고르고, 값마다
검수된 검색어 · 근거는 data/content_factors.json에 있다(app/content/factors.py가 읽는다).

Enum 값은 논문이 실제 자극으로 쓴 범주만 둔다(카탈로그 basis = STIMULUS). 근거 없이 사용자
선호용으로만 두는 풍경(꽃 · 하늘)은 FE 칩(profile.preferred_scenery)으로만 받고 LLM에는 열지
않는다. Factor는 연구 자극 범위 안에서 콘텐츠를 바꾸는 개인화이고, 효과를 키운다는 근거는 아니다.

우선순위: 사용자 칩(profile) > LLM Factor > 프로토콜 기본 검색어(content_spec.selection.query_tags).
Enum 값과 카탈로그 키가 같은지는 tests/content/test_factors.py가 본다.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Scenery(StrEnum):
    """자연 사진 · 영상의 풍경. 값은 resolver.SCENERY_QUERIES 키(FE 칩 값)와 같다."""

    FOREST = "forest"  # 숲 — P28 · P24(나무·풀·식물) · P33(woodland) · P25(Nature Forest 영상)
    LAKE_RIVER = "lake river"  # 호수·강 — P03(lakes, rivers) · N17(강 영상) · P25(Nature Stream)
    SEA = "sea"  # 바다 — P03(sea)
    MOUNTAIN = "mountain"  # 산·언덕 — P03(hills)
    MEADOW = "meadow"  # 풀밭·들판 — P28(trees, grass, fields) · P33(heathland)


class Soundscape(StrEnum):
    """자연음 범주. 사람 목소리 · 음악 · 교통 · 천둥은 어느 값에서도 뺀다(프로토콜 exclude)."""

    # 새소리 — N14(분수+새소리) · N15 · N16 · N18. 까마귀·까치 같은 거친 소리는 뺀다(N15)
    BIRDSONG = "birdsong"
    STREAM = "stream"  # 시냇물·강물 — N17(강 소리) · N18 · N14(분수) · N15(물 24%)
    RAIN = "rain"  # 잔잔한 비 — N16(rainfall)
    OCEAN_WAVES = "ocean_waves"  # 잔잔한 파도 — N16(ocean waves)
    FOREST_WIND = "forest_wind"  # 숲 바람 — N16(wind) · N18(가상 숲)


class ArtSubject(StrEnum):
    """미술 작품 주제(PR-20).

    E06은 풍경·물 풍경 작품이 스트레스 완화에 유용하다고 보고 수련 연못을 골랐다.
    """

    WATERSCAPE = "waterscape"  # 물 풍경 — E06 · E07(모네 수련 연못)
    LANDSCAPE = "landscape"  # 풍경 — E06(landscape artworks)


class MusicTrack(StrEnum):
    """이완 음악(PR-05) 검수 3곡. LLM은 추천만 하고 고르는 것은 사용자다.

    E08은 연구자가 만든 목록에서 사용자가 고른 음악의 효과를 g = 0.38(연구 1편)로 보고했다 — 대신
    정하지 않고 추천곡을 목록 맨 앞에 둔다(resolver). 곡마다 라벨(형태 · 가사 · 종교곡 · 성격)은
    data/content_factors.json에 있다.
    """

    MISERERE = "miserere"  # 알레그리 무반주 라틴어 합창 — E09가 쓴 자극
    MOZART_K448 = "mozart_k448"  # 두 대의 피아노 소나타, 기악 — E08 표(Scheufele 2000)
    VIVALDI_SPRING = "vivaldi_spring"  # 사계 봄, 현악 — E08 표(Chafin 2004)


class FactorSource(StrEnum):
    """자산의 Factor를 누가 정했나 — FE 문구가 다르다(사용자 칩이면 "좋아하는 ○○")."""

    USER = "USER"  # profile.preferred_scenery(FE 칩)
    LLM = "LLM"  # plan이 고른 content_factors
    DEFAULT = "DEFAULT"  # 프로토콜 기본 검색어


class ContentFactors(BaseModel):
    """LLM 호출 2의 구조화 출력 일부. 필드 설명이 LLM에 보인다.

    모든 칸이 필수다(구조화 출력 strict 호환). 고른 활동에 그 칸이 없으면 [] · null로 둔다.
    몇 개까지 쓰는지(풍경 최대 4 · 사진 1장이면 1)는 코드가 자른다(factors.sanitize).
    """

    scenery: list[Scenery] = Field(
        description="자연 사진·영상 풍경을 쓰일 순서대로 0~4개. 사진 여러 장이면 연구 자극"
        "(P03 호수·강·바다·언덕)처럼 여러 종을 섞고, 1장이거나 영상이면 1개. 고른 활동에 풍경 칸이 "
        "없으면 []"
    )
    soundscape: Soundscape | None = Field(
        description="자연음 범주 하나. 고른 활동에 자연음 칸이 없으면 null"
    )
    art_subject: ArtSubject | None = Field(
        description="미술 작품 주제 하나. 고른 활동에 작품 칸이 없으면 null"
    )
    music: MusicTrack | None = Field(
        description="이완 음악 추천곡 하나 — 사용자가 3곡 중 직접 고르고, 추천곡은 맨 앞에 보인다. "
        "고른 활동에 음악 칸이 없으면 null"
    )
    reason: str = Field(
        description="이렇게 고른 이유 한 문장(한국어) — 사용자 표현 · 기피 · 지난 라운드와 다르게. "
        "효과가 더 크다고 쓰지 않는다. 칸이 없으면 빈 문자열"
    )

    @classmethod
    def none(cls) -> "ContentFactors":
        """고를 칸이 없거나 LLM이 고르지 않았을 때."""
        return cls(scenery=[], soundscape=None, art_subject=None, music=None, reason="")

    def is_empty(self) -> bool:
        return (
            not self.scenery
            and self.soundscape is None
            and self.art_subject is None
            and self.music is None
        )
