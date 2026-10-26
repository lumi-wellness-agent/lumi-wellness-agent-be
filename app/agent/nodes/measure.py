"""measure_pre · measure_post · measure_final — 코드 노드 + interrupt. LLM은 어느 단계에서도 없다.

- measure_pre: SAM(앵커) + 목표 척도(S2 STAI-6 · S1 BSRI · S3 없음). 세션 시작에 1회.
- measure_post: 라운드마다 SAM만. 다음 라운드의 사전값은 직전 사후값(confirm이 round_pre로 넘긴다).
- measure_final: END 경로에서 목표 척도 1회 → 세션 Δ. S3는 건너뛴다(replanner.route_after_confirm).

interrupt payload: {"measure": "PRE", "round": 0, "instruments": [instruments.present(...)]}
resume value:      {"INS-SAM": {"valence": 3, "arousal": 7}, "INS-STAI6": {"calm": 2, ...}}
도구 계약(문항 · 응답 척도 · 채점)은 app/instruments.py와 data/instruments/*.json.
"""

from langgraph.types import interrupt

from app import instruments
from app.agent.state import CareState
from app.schemas.measure import GOAL_SCALE, InstrumentId, Measurement, Timepoint


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


def _goal_scale(state: CareState) -> InstrumentId | None:
    return GOAL_SCALE[state["intake"].goal]


def measure_pre(state: CareState) -> dict:
    ids = [InstrumentId.SAM]
    if scale := _goal_scale(state):
        ids.append(scale)
    measurements = _administer(ids, Timepoint.PRE, 0)
    return {"pre": measurements, "round_pre": measurements[0]}


def measure_post(state: CareState) -> dict:
    round_no = len(state.get("rounds", [])) + 1
    return {"post": _administer([InstrumentId.SAM], Timepoint.POST, round_no)[0]}


def measure_final(state: CareState) -> dict:
    scale = _goal_scale(state)
    if scale is None:  # route_after_confirm이 건너뛰지만 방어
        return {}
    round_no = len(state.get("rounds", []))
    return {"final": _administer([scale], Timepoint.FINAL, round_no)[0]}
