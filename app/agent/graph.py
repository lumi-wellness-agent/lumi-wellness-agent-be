"""오케스트레이터 — 노드 배선과 라우팅. LLM을 부르지 않는 층이다.

에이전트 아키텍처 v2를 그대로 옮겼다(`make graph`로 Mermaid 출력).

    START → intake → measure_pre ⏸ → plan → prepare → run_session ⏸ → measure_post ⏸
          → evaluate → replan → confirm ⏸ → (END 경로) measure_final ⏸ → END
    intake에서 안전 신호·목표 미정이면 세션을 열지 않고 END.
    confirm에서 활동 변경·추가 세션을 고르면 plan으로 돌아간다(라운드 2–3회).

    intake        LLM 호출 1회(구조화 출력), 에이전트 아님      nodes/intake.py
    measure_pre   코드 ⏸ SAM + 목표 척도                         nodes/measure.py
  ① plan          Adaptive Care Planning (고정 절차, LLM 2회)   nodes/planner.py
  ② prepare       콘텐츠 준비                                     nodes/executor.py
     run_session  FE 실행 대기 ⏸                                  nodes/executor.py
     measure_post 코드 ⏸ SAM                                     nodes/measure.py
  ③ evaluate      Outcome Evaluator                               nodes/evaluator.py
  ④ replan        Replanning 규칙                                 nodes/replanner.py
     confirm      사용자 선택·동의 대기 ⏸                         nodes/replanner.py
     measure_final 코드 ⏸ 목표 척도(S3 없음)                      nodes/measure.py

⏸는 interrupt다. 그래프가 멈추고 상태를 체크포인터에 남긴다. 같은 thread_id로
`Command(resume=값)`을 보내면 그 자리부터 이어서 돈다. 세션 하나가 thread 하나다.
라우팅은 순수 함수라 LLM 없이 테스트한다.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.context import AgentContext
from app.agent.nodes.evaluator import evaluate
from app.agent.nodes.executor import prepare_session, run_session
from app.agent.nodes.intake import Intaker, make_intake_node, route_after_intake
from app.agent.nodes.measure import measure_final, measure_post, measure_pre
from app.agent.nodes.planner import Planner, make_plan_node
from app.agent.nodes.replanner import confirm, replan, route_after_confirm
from app.agent.state import CareState, IntakeResult
from app.schemas.care import CareRequest


def build_graph(
    planner: Planner, intaker: Intaker, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """세션 그래프. interrupt가 있어 실행하려면 checkpointer가 필요하다(checkpoint.py)."""
    builder = StateGraph(CareState, context_schema=AgentContext)
    builder.add_node("intake", make_intake_node(intaker))
    builder.add_node("measure_pre", measure_pre)
    builder.add_node("plan", make_plan_node(planner))
    builder.add_node("prepare", prepare_session)
    builder.add_node("run_session", run_session)
    builder.add_node("measure_post", measure_post)
    builder.add_node("evaluate", evaluate)
    builder.add_node("replan", replan)
    builder.add_node("confirm", confirm)
    builder.add_node("measure_final", measure_final)
    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", route_after_intake, ["measure_pre", END])
    builder.add_edge("measure_pre", "plan")
    builder.add_edge("plan", "prepare")
    builder.add_edge("prepare", "run_session")
    builder.add_edge("run_session", "measure_post")
    builder.add_edge("measure_post", "evaluate")
    builder.add_edge("evaluate", "replan")
    builder.add_edge("replan", "confirm")
    builder.add_conditional_edges("confirm", route_after_confirm, ["plan", "measure_final", END])
    builder.add_edge("measure_final", END)
    return builder.compile(checkpointer=checkpointer)


class _UnconfiguredPlanner:
    async def choose(self, *args):
        raise RuntimeError("Planner의 LLM을 아직 조립하지 않았습니다(app/agent/assembly.py)")

    async def explain(self, *args):
        raise RuntimeError("Planner의 LLM을 아직 조립하지 않았습니다(app/agent/assembly.py)")


async def _unconfigured_intaker(request: CareRequest) -> IntakeResult:
    raise RuntimeError("intake의 LLM을 아직 조립하지 않았습니다(app/agent/assembly.py)")


if __name__ == "__main__":
    # make graph: 문서·발표용 Mermaid. 그리기만 하므로 LLM을 부르지 않는다
    print(build_graph(_UnconfiguredPlanner(), _unconfigured_intaker).get_graph().draw_mermaid())
