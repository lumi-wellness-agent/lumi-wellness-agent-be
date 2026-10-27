"""measure_pre · measure_post · measure_final — 코드 노드 + interrupt. LLM은 어느 단계에서도 없다.

현재 MVP는 세 측정 시점(PRE · POST · FINAL) 모두 SAM만 제시하고 응답을 받는다.
STAI-6 · BSRI는 data에서 TODO로 유지하며 FE payload에는 보내지 않는다.
intake가 정한 goal은 plan·근거 선택에 사용한다. 척도 선택표(GOAL_SCALE)는 문항·권한 확인 뒤
다시 연결한다. SAM 점수는 현재 기분의 쾌·각성 변화이며 목표별 임상 효과를 뜻하지 않는다.

interrupt payload:
{
  "measure": "PRE",
  "round": 0,
  "instruments": [{"instrument_id": "INS-SAM", "status": "READY", ...}],
  "required_instrument_ids": ["INS-SAM"]
}
resume value: {"INS-SAM": {"valence": 3, "arousal": 7}}
"""

from langgraph.types import interrupt

from app import instruments
from app.agent.state import CareState
from app.schemas.measure import InstrumentId, Measurement, Timepoint

ACTIVE_INSTRUMENTS = [InstrumentId.SAM]


def _administer(ids: list[InstrumentId], timepoint: Timepoint, round_no: int) -> list[Measurement]:
    """제시 목록과 필수 응답 목록을 분리한다. 현재 MVP에서는 둘 다 SAM 하나다."""
    loaded = [instruments.get_instrument(i) for i in ids]
    answers = interrupt(
        {
            "measure": timepoint.value,
            "round": round_no,
            "instruments": [instruments.present(i) for i in loaded],
            "required_instrument_ids": [i.instrument_id.value for i in loaded],
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


def measure_pre(state: CareState) -> dict:
    measurements = _administer(ACTIVE_INSTRUMENTS, Timepoint.PRE, 0)
    return {"pre": measurements, "round_pre": measurements.copy()}


def measure_post(state: CareState) -> dict:
    round_no = len(state.get("rounds", [])) + 1
    return {"post": _administer(ACTIVE_INSTRUMENTS, Timepoint.POST, round_no)}


def measure_final(state: CareState) -> dict:
    round_no = len(state.get("rounds", []))
    return {"final": _administer(ACTIVE_INSTRUMENTS, Timepoint.FINAL, round_no)[0]}
