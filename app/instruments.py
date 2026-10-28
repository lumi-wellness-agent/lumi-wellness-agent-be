"""측정 도구 — 문항·응답 척도·채점. data/instruments/INS-*.json이 원천이다.

measure_pre · measure_post · measure_final(app/agent/nodes/measure.py)이 present()로 interrupt에
문항을 싣고, FE 응답을 score()로 채점해 Measurement(app/schemas/measure.py)로 남긴다.
PROVISIONAL은 출처 문항의 한국어 번역 초안 또는 팀 작성 임시 문항을 내부 시험에 제시하는 상태다.
화면에는 '한국어 번역본 · 내부 시험용'으로 표시하고, 검증된 한국어판처럼 해석하지 않는다.
READY는 확인된 문항을 제시하는 상태, TODO는 제시 문구가 아직 없는 상태다.
"""

from collections.abc import Mapping
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.schemas.measure import InstrumentId, Measurement, Timepoint

INSTRUMENT_DIR = Path(__file__).resolve().parents[1] / "data" / "instruments"


class ScoringKind(StrEnum):
    PER_ITEM = "PER_ITEM"  # 문항마다 점수 하나 — SAM(valence · arousal)
    SUM_PRORATED = "SUM_PRORATED"  # 역채점 후 합 × prorate_items / 문항 수 — STAI-6(20–80)
    MEAN = "MEAN"  # 문항 평균 — BSRI(0–100)


class Item(BaseModel):
    id: str
    text: str | None = None  # None = 담당자가 채운다(저작권·번역 확인 뒤)
    reverse: bool = False  # 역채점 문항
    low_label: str | None = None
    high_label: str | None = None


class Scale(BaseModel):
    min: float
    max: float
    step: float = 1
    kind: Literal["LIKERT", "PICTORIAL", "SLIDER"] = "LIKERT"


class Scoring(BaseModel):
    kind: ScoringKind
    key: str = "total"  # SUM_PRORATED · MEAN 결과의 scores 키
    prorate_items: int | None = None  # SUM_PRORATED: 이 문항 수의 전체판 점수로 환산(STAI-6 → 20)
    range: tuple[float, float] | None = None  # 채점 결과 범위(문서용)


class Instrument(BaseModel):
    instrument_id: InstrumentId
    name: str
    paper_id: str
    concept: str  # 구성개념(쾌·각성, 상태 불안, 상태 반추)
    timeframe: str
    instruction: str | None = None
    items: list[Item] = Field(min_length=1)
    scale: Scale
    scoring: Scoring
    license_note: str | None = None
    # READY = 확인된 문항을 제시한다.
    # PROVISIONAL = 번역 초안 또는 임시 문항을 내부 시험에 제시한다.
    # TODO = 제시 문구가 아직 없다.
    status: Literal["READY", "PROVISIONAL", "TODO"] = "TODO"
    note: str | None = None

    @model_validator(mode="after")
    def _rules(self) -> Self:
        ids = [item.id for item in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("문항 id가 겹친다")
        if self.status != "TODO" and any(item.text is None for item in self.items):
            raise ValueError(f"{self.status} 도구는 모든 문항에 text가 있어야 한다")
        if self.scoring.kind is ScoringKind.SUM_PRORATED and not self.scoring.prorate_items:
            raise ValueError("SUM_PRORATED는 prorate_items가 필요하다")
        return self


@lru_cache
def load_instruments() -> dict[InstrumentId, Instrument]:
    out: dict[InstrumentId, Instrument] = {}
    for path in sorted(INSTRUMENT_DIR.glob("INS-*.json")):
        instrument = Instrument.model_validate_json(path.read_text(encoding="utf-8"))
        if instrument.instrument_id.value != path.stem:
            raise ValueError(
                f"{path.name}: instrument_id {instrument.instrument_id}와 파일명이 다르다"
            )
        out[instrument.instrument_id] = instrument
    return out


def get_instrument(instrument_id: InstrumentId) -> Instrument:
    return load_instruments()[instrument_id]


def present(instrument: Instrument) -> dict:
    """interrupt에 싣는 제시용 payload. FE가 그대로 렌더링한다."""
    return {
        "instrument_id": instrument.instrument_id.value,
        "name": instrument.name,
        "instruction": instrument.instruction,
        "scale": instrument.scale.model_dump(),
        "items": [
            {"id": i.id, "text": i.text, "low_label": i.low_label, "high_label": i.high_label}
            for i in instrument.items
        ],
    }


def score(instrument: Instrument, answers: Mapping[str, float]) -> dict[str, float]:
    """문항별 원응답 → 채점. 빠진 문항·범위 밖 값은 ValueError."""
    scale = instrument.scale
    values: dict[str, float] = {}
    for item in instrument.items:
        if item.id not in answers:
            raise ValueError(f"{instrument.instrument_id}: 문항 {item.id} 응답이 없다")
        value = float(answers[item.id])
        if not scale.min <= value <= scale.max:
            raise ValueError(
                f"{instrument.instrument_id}: {item.id}={value}는 {scale.min}–{scale.max} 밖이다"
            )
        values[item.id] = scale.min + scale.max - value if item.reverse else value
    kind = instrument.scoring.kind
    if kind is ScoringKind.PER_ITEM:
        return values
    total = sum(values.values())
    if kind is ScoringKind.SUM_PRORATED:
        total = total * instrument.scoring.prorate_items / len(values)
        return {instrument.scoring.key: round(total, 2)}
    return {instrument.scoring.key: round(total / len(values), 2)}


def measure(
    instrument: Instrument, answers: Mapping[str, float], timepoint: Timepoint, round_no: int
) -> Measurement:
    """FE 응답 하나를 채점해 Measurement로. 문항에 없는 키는 버린다."""
    ids = {item.id for item in instrument.items}
    return Measurement(
        instrument_id=instrument.instrument_id,
        timepoint=timepoint,
        round=round_no,
        answers={k: float(v) for k, v in answers.items() if k in ids},
        scores=score(instrument, answers),
    )
