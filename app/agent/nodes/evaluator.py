"""③ Outcome Evaluator — 라운드의 사전·사후 변화를 잰다. LLM을 쓰지 않는다.

Δ = 사후 − 사전(round_pre: 첫 라운드는 measure_pre 값, 다음 라운드는 직전 사후값). 도구별 scores
키를 하나로 합친다 — 현재 MVP는 SAM valence · arousal만 기록한다.
improved는 실험적 방향 지표다 — S1은 쾌(valence)↑, S2는 각성(arousal)↓를 참고한다.
두 방향 모두 목표 구성개념을 직접 측정한 값은 아니다. S3는 SAM으로 피로를 직접 측정할 수 없어
판정하지 않는다. 종료 시점의 SAM 변화는 API 응답에 함께 제공한다.
결과는 Session Outcome DB에 저장한다(테이블 미정 — 사용자 데이터 스키마가 다음 계약).
"""

from app.agent.state import CareState, OutcomeEvaluation
from app.schemas.care import CareGoal
from app.schemas.measure import Measurement

# (scores 키, 바람직한 방향). None = 세션 내 판정 기준 없음
IMPROVEMENT_RULE: dict[CareGoal, tuple[str, int] | None] = {
    CareGoal.S1: ("valence", +1),
    CareGoal.S2: ("arousal", -1),
    CareGoal.S3: None,  # SAM에는 피로 점수가 없어 개선 판정 불가
}


def deltas_between(pre: list[Measurement], post: list[Measurement]) -> dict[str, float]:
    """같은 도구끼리 사후 − 사전. 사전에 없는 도구·키는 뺀다."""
    before = {m.instrument_id: m.scores for m in pre}
    out: dict[str, float] = {}
    for m in post:
        base = before.get(m.instrument_id, {})
        out.update({k: v - base[k] for k, v in m.scores.items() if k in base})
    return out


def evaluate(state: CareState) -> dict:
    pre, post = state["round_pre"], state["post"]
    deltas = deltas_between(pre, post)
    goal = state["intake"].goal
    rule = IMPROVEMENT_RULE[goal]
    improved = None if rule is None else deltas.get(rule[0], 0.0) * rule[1] > 0
    evaluation = OutcomeEvaluation(
        goal=goal,
        round=post[0].round,
        deltas=deltas,
        improved=improved,
        completed=state["report"].completed,
    )
    return {"evaluation": evaluation}
