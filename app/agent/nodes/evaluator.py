"""③ Outcome Evaluator — 목표별 개선 정도를 잰다. LLM을 쓰지 않는다.

지금은 스트레스 수준(0~10)의 사전·사후 차이만 본다. 목표별 척도(S1 상태 반추와 부정 정서,
S2 긴장·불편, S3 피로·주의력)는 측정 방식을 정하면 SessionReport와 함께 넓힌다.
반추와 부정 정서는 따로 잰다 — 컬러링처럼 부정 정서가 줄어도 상태 반추는 그대로일 수 있다
(Drew 2018). 결과는 Session Outcome DB에 저장한다(테이블 미정).
"""

from app.agent.state import CareState, OutcomeEvaluation


def evaluate(state: CareState) -> dict:
    change = state["report"].post_stress_level - state["request"].stress_level
    evaluation = OutcomeEvaluation(
        goal=state["request"].goal, stress_change=change, improved=change < 0
    )
    return {"evaluation": evaluation}
