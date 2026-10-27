"""측정 계약 — 어떤 도구를 언제 쓰고 결과를 어떤 모양으로 남기는지. 문항·채점은 app/instruments.py.

측정은 RAG가 아니다. 도구는 구조화 표(data/instruments)에 있고 LLM은 문항을 만들거나 바꾸거나
점수를 계산하지 않는다. 세션 안에서는 '지금' 시간틀 도구만 쓴다(측정도구 시트).
"""

from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.care import CareGoal


class InstrumentId(StrEnum):
    SAM = "INS-SAM"  # 공통 앵커. 쾌·각성 그림 척도 2탭. 모든 MEASURE 노드
    STAI6 = "INS-STAI6"  # S2 목표 척도. 6문항 4점 → 20–80 환산. 사전·종료만
    BSRI = "INS-BSRI"  # S1 목표 척도. 8문항 0–100 슬라이더. 사전·종료만
    MFVAS = "INS-MFVAS"  # S3 목표 척도(임시·미검증). 정신 피로 VAS 3문항 슬라이더. 사전·라운드마다


# 목표별 척도. S3는 13편 안에 검증된 순간 피로·주의 자가보고가 없어 임시 도구(INS-MFVAS, 팀 작성·
# 미검증)로 주관적 피로만 잰다. 근거 쪽 S3 1차 지표는 그대로 ATTENTION(과제)이다
# (retrieval.GOAL_OUTCOMES) — 측정 지표와 근거 지표가 다르다는 것을 Reference 카드에 적는다
GOAL_SCALE: dict[CareGoal, InstrumentId | None] = {
    CareGoal.S1: InstrumentId.BSRI,
    CareGoal.S2: InstrumentId.STAI6,
    CareGoal.S3: InstrumentId.MFVAS,
}

# 짧아서 라운드마다 SAM과 함께 다시 재는 목표 척도. 라운드 판정에 직접 쓰고 종료(measure_final)
# 에서는 다시 묻지 않는다 — 마지막 라운드의 사후값이 종료값이다.
# 나머지 목표 척도(STAI-6 · BSRI)는 사전·종료에만 잰다
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
