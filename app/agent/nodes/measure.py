"""measure_pre · measure_post · measure_final — 코드 노드 + interrupt. LLM은 어느 단계에서도 없다.

현재 MVP에서는 SAM만 실제 응답을 요구하고 Measurement로 저장한다.
사전·종료 payload에는 STAI-6·BSRI를 TODO 상태의 비활성 자리표시자로 함께 전달한다.
라운드 사이는 SAM만 보낸다. 척도 선택표(GOAL_SCALE)는 근거·권한 확인 뒤 다시 연결한다.

interrupt payload:
{
  "measure": "PRE",
  "round": 0,
  "instruments": [{"instrument_id": "INS-SAM", "enabled": true, ...},
                  {"instrument_id": "INS-STAI6", "enabled": false, "status": "TODO", ...}],
  "required_instrument_ids": ["INS-SAM"]
}
resume value: {"INS-SAM": {"valence": 3, "arousal": 7}}
FE가 실수로 비활성 도구 응답을 보내더라도 무시한다.
"""

from langgraph.types import interrupt

from app import instruments
from app.agent.state import CareState
from app.schemas.measure import InstrumentId, Measurement, Timepoint

ACTIVE_INSTRUMENTS = [InstrumentId.SAM]
PRE_FINAL_PLACEHOLDERS = [InstrumentId.STAI6, InstrumentId.BSRI]


def _administer(
    ids: list[InstrumentId],
    timepoint: Timepoint,
    round_no: int,
    *,
    placeholders: list[InstrumentId] | None = None,
) -> list[Measurement]:
    """제시 목록과 필수 응답 목록을 분리한다. 자리표시자는 채점·저장하지 않는다."""
    required = [instruments.get_instrument(i) for i in ids]
    optional = [instruments.get_instrument(i) for i in (placeholders or [])]
    answers = interrupt(
        {
            "measure": timepoint.value,
            "round": round_no,
            "instruments": [
                *(instruments.present(i, enabled=True) for i in required),
                *(instruments.present(i, enabled=False) for i in optional),
            ],
            "required_instrument_ids": [i.instrument_id.value for i in required],
        }
    )
    if not isinstance(answers, dict):
        raise ValueError("측정 응답은 {instrument_id: {item_id: value}} 모양이어야 한다")
    out = []
    for instrument in required:
        key = instrument.instrument_id.value
        if key not in answers:
            raise ValueError(f"{key} 응답이 없다")
        out.append(instruments.measure(instrument, answers[key], timepoint, round_no))
    return out


def measure_pre(state: CareState) -> dict:
    measurements = _administer(
        ACTIVE_INSTRUMENTS,
        Timepoint.PRE,
        0,
        placeholders=PRE_FINAL_PLACEHOLDERS,
    )
    return {"pre": measurements, "round_pre": measurements.copy()}


def measure_post(state: CareState) -> dict:
    round_no = len(state.get("rounds", [])) + 1
    return {"post": _administer(ACTIVE_INSTRUMENTS, Timepoint.POST, round_no)}


def measure_final(state: CareState) -> dict:
    round_no = len(state.get("rounds", []))
    return {
        "final": _administer(
            ACTIVE_INSTRUMENTS,
            Timepoint.FINAL,
            round_no,
            placeholders=PRE_FINAL_PLACEHOLDERS,
        )[0]
    }
