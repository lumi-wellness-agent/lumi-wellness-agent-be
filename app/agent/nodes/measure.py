"""measure_pre · measure_post · measure_final — 코드 노드 + interrupt. LLM은 어느 단계에서도 없다.

- measure_pre: SAM(앵커) + 목표 척도(S2 STAI-6 · S1 BSRI · S3 MFVAS). 세션 시작에 1회.
- measure_post: 라운드마다 SAM, 라운드 척도(schemas/measure.ROUND_SCALES — S3의 MFVAS)가 있으면
  그것도. 다음 라운드의 사전값은 직전 사후값(confirm이 round_pre로 넘긴다).
- measure_final: END 경로에서 사전·종료에만 재는 목표 척도(STAI-6 · BSRI) 1회 → 세션 Δ.
  라운드마다 잰 척도(S3)는 마지막 사후값이 종료값이라 건너뛴다(replanner.route_after_confirm).

interrupt payload: {"measure": "PRE", "round": 0, "instruments": [instruments.present(...)]}
resume value:      {"INS-SAM": {"valence": 3, "arousal": 7}, "INS-STAI6": {"calm": 2, ...}}
도구 계약(문항 · 응답 척도 · 채점)은 app/instruments.py와 data/instruments/*.json.
"""

from langgraph.types import interrupt

from app import instruments
from app.agent.state import CareState
from app.schemas.measure import (
    GOAL_SCALE,
    InstrumentId,
    Measurement,
    Timepoint,
    final_scale,
    round_scale,
)


def _administer(ids: list[InstrumentId], timepoint: Timepoint, round_no: int) -> list[Measurement]:
    loaded = [instruments.get_instrument(i) for i in ids]
    answers = interrupt(
        {
            "measure": timepoint.value,
            "round": round_no,
            "instruments": [instruments.present(i) for i in loaded],
        }
    )
    if not isinstance(answers, dict):
        raise ValueError("측정 응답은 {instrument_id: {item_id: value}} 모양이어야 한다")
    out = []
    for instrument in loaded:
        key = instrument.instrument_id.value
        if key not in answers:
            raise ValueError(f"{key} 응답이 없다")
        out.append(instruments.measure(instrument, answers[key], timepoint, round_no))
    return out


def _round_ids(state: CareState) -> list[InstrumentId]:
    """라운드마다 재는 도구 — 앵커(SAM) + 라운드 척도(있으면)."""
    ids = [InstrumentId.SAM]
    if scale := round_scale(state["intake"].goal):
        ids.append(scale)
    return ids


def measure_pre(state: CareState) -> dict:
    ids = [InstrumentId.SAM]
    if scale := GOAL_SCALE[state["intake"].goal]:
        ids.append(scale)
    measurements = _administer(ids, Timepoint.PRE, 0)
    round_ids = _round_ids(state)
    return {
        "pre": measurements,
        "round_pre": [m for m in measurements if m.instrument_id in round_ids],
    }


def measure_post(state: CareState) -> dict:
    round_no = len(state.get("rounds", [])) + 1
    return {"post": _administer(_round_ids(state), Timepoint.POST, round_no)}


def measure_final(state: CareState) -> dict:
    scale = final_scale(state["intake"].goal)
    if scale is None:  # route_after_confirm이 건너뛰지만 방어
        return {}
    round_no = len(state.get("rounds", []))
    return {"final": _administer([scale], Timepoint.FINAL, round_no)[0]}
