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


# 목표별 척도. S3는 검증된 순간 피로·주의 자가보고가 13편 안에 없어 앵커만 재고 목표 지표는 '—'
GOAL_SCALE: dict[CareGoal, InstrumentId | None] = {
    CareGoal.S1: InstrumentId.BSRI,
    CareGoal.S2: InstrumentId.STAI6,
    CareGoal.S3: None,
}


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
