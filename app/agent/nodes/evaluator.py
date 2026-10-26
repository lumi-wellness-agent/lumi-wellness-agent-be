"""③ Outcome Evaluator — 라운드의 사전·사후 SAM 변화를 잰다. LLM을 쓰지 않는다.

Δ = 사후 − 사전(round_pre: 첫 라운드는 measure_pre의 SAM, 다음 라운드는 직전 사후값).
improved는 목표별 임시 규칙 — S1은 쾌(valence)↑, S2는 각성(arousal)↓, S3는 기준 없음(None, '—').
목표 척도(STAI-6 · BSRI)의 세션 Δ는 measure_final 뒤 API 응답에서 pre와 final로 계산한다.
결과는 Session Outcome DB에 저장한다(테이블 미정 — 사용자 데이터 스키마가 다음 계약).
"""

from app.agent.state import CareState, OutcomeEvaluation
from app.schemas.care import CareGoal

# (scores 키, 바람직한 방향). None = 세션 내 판정 기준 없음
IMPROVEMENT_RULE: dict[CareGoal, tuple[str, int] | None] = {
    CareGoal.S1: ("valence", +1),
    CareGoal.S2: ("arousal", -1),
    CareGoal.S3: None,
}


def evaluate(state: CareState) -> dict:
    pre, post = state["round_pre"], state["post"]
    deltas = {k: post.scores[k] - pre.scores[k] for k in post.scores if k in pre.scores}
    goal = state["intake"].goal
    rule = IMPROVEMENT_RULE[goal]
    improved = None if rule is None else deltas.get(rule[0], 0.0) * rule[1] > 0
    evaluation = OutcomeEvaluation(
        goal=goal,
        round=post.round,
        deltas=deltas,
        improved=improved,
        completed=state["report"].completed,
    )
    return {"evaluation": evaluation}
