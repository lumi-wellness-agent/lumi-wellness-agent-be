"""측정 계약 — 어떤 도구를 언제 쓰고 결과를 어떤 모양으로 남기는지. 문항·채점은 app/instruments.py.

측정은 RAG가 아니다. 도구는 구조화 표(data/instruments)에 있고 LLM은 문항을 만들거나 바꾸거나
점수를 계산하지 않는다. 세션 안에서는 '지금' 시간틀 도구만 쓴다(측정도구 시트).
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.care import CareGoal


class InstrumentId(StrEnum):
    SAM = "INS-SAM"  # 현재 MVP의 공통 측정. 쾌·각성 그림 척도 2탭. 모든 MEASURE 노드
    STAI6 = "INS-STAI6"  # 후속 후보(S2): TODO. 6문항 4점 → 20–80 환산 설계
    BSRI = "INS-BSRI"  # 후속 후보(S1): TODO. 8문항 0–100 슬라이더 설계
    MFVAS = "INS-MFVAS"  # 후속 후보(S3): PROVISIONAL·미검증. 현재 MVP payload에는 미포함


# 후속 버전의 목표별 척도 후보. 현재 SAM-only MVP의 measure 노드에서는 이 매핑을 사용하지 않는다.
# S3 후보 MFVAS는 팀 작성·미검증이며, 근거 쪽 1차 지표 ATTENTION(과제)과도 다르다.
GOAL_SCALE: dict[CareGoal, InstrumentId | None] = {
    CareGoal.S1: InstrumentId.BSRI,
    CareGoal.S2: InstrumentId.STAI6,
    CareGoal.S3: InstrumentId.MFVAS,
}

# 미래에 goal-specific 척도를 활성화할 때 사용할 측정 빈도 표. 현재 MVP 노드는 사용하지 않는다.
ROUND_SCALES: frozenset[InstrumentId] = frozenset({InstrumentId.MFVAS})


def round_scale(goal: CareGoal) -> InstrumentId | None:
    """라운드마다 앵커와 함께 재는 목표 척도. 없으면 None(라운드 사이는 SAM만)."""
    scale = GOAL_SCALE[goal]
    return scale if scale in ROUND_SCALES else None


def final_scale(goal: CareGoal) -> InstrumentId | None:
    """종료 경로에서 한 번 더 재는 목표 척도. 라운드마다 잰 척도는 다시 묻지 않는다."""
    scale = GOAL_SCALE[goal]
    return None if scale is None or scale in ROUND_SCALES else scale


class Timepoint(StrEnum):
    PRE = "PRE"  # 세션 시작 — measure_pre
    POST = "POST"  # 라운드 끝 — measure_post
    FINAL = "FINAL"  # 세션 끝 — measure_final


class Measurement(BaseModel):
    """측정 한 번 = 도구 하나 × 시점 하나. answers는 문항별 원응답, scores는 채점 결과."""

    instrument_id: InstrumentId
    timepoint: Timepoint
    round: int = Field(ge=0)  # PRE는 0, POST·FINAL은 라운드 번호(1부터)
    answers: dict[str, float]
    scores: dict[str, float]  # SAM: valence · arousal / STAI-6: total / BSRI: mean
