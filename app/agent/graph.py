"""오케스트레이터 — 노드 배선과 라우팅. LLM을 부르지 않는 층이다.

에이전트 아키텍처를 그대로 옮겼다(`make graph`로 Mermaid 출력).

    START → plan → prepare → run_session ⏸ → evaluate → replan → confirm ⏸ → END
    confirm에서 사용자가 활동 변경이나 추가 세션을 고르면 plan으로 돌아간다.

① plan         Adaptive Care Planning Agent (nodes/planner.py)
② prepare      Care Session Executor — 콘텐츠 준비 (nodes/executor.py)
   run_session  Care Session Executor — FE 실행을 기다린다 ⏸
③ evaluate     Outcome Evaluator (nodes/evaluator.py)
④ replan       Replanning — 제안 (nodes/replanner.py)
   confirm      Replanning — 사용자 선택·동의를 기다린다 ⏸

⏸는 interrupt다. 그래프가 멈추고 상태를 체크포인터에 남긴다. 같은 thread_id로
`Command(resume=값)`을 보내면 그 자리부터 이어서 돈다. 세션 하나가 thread 하나다.
라우팅은 순수 함수라 LLM 없이 테스트한다.
"""

from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.context import AgentContext
from app.agent.nodes.evaluator import evaluate
from app.agent.nodes.executor import prepare_session, run_session
from app.agent.nodes.planner import Planner, make_plan_node
from app.agent.nodes.replanner import confirm, replan
from app.agent.state import CarePlan, CareState, NextStep


def route_after_confirm(state: CareState) -> Literal["plan", "__end__"]:
    """종료면 끝내고, 활동 변경·추가 세션이면 Planner로 돌아간다."""
    return END if state["next_step"] is NextStep.END else "plan"


def build_graph(
    planner: Planner, checkpointer: BaseCheckpointSaver | None = None
) -> CompiledStateGraph:
    """세션 그래프. interrupt가 있어 실행하려면 checkpointer가 필요하다(checkpoint.py)."""
    builder = StateGraph(CareState, context_schema=AgentContext)
    builder.add_node("plan", make_plan_node(planner))
    builder.add_node("prepare", prepare_session)
    builder.add_node("run_session", run_session)
    builder.add_node("evaluate", evaluate)
    builder.add_node("replan", replan)
    builder.add_node("confirm", confirm)
    builder.add_edge(START, "plan")
    builder.add_edge("plan", "prepare")
    builder.add_edge("prepare", "run_session")
    builder.add_edge("run_session", "evaluate")
    builder.add_edge("evaluate", "replan")
    builder.add_edge("replan", "confirm")
    builder.add_conditional_edges("confirm", route_after_confirm, ["plan", END])
    return builder.compile(checkpointer=checkpointer)


async def _unconfigured_planner(
    state: CareState, context: AgentContext, problems: list[str]
) -> CarePlan:
    raise RuntimeError("Planner의 LLM을 아직 정하지 않았습니다")


if __name__ == "__main__":
    # make graph: 문서·발표용 Mermaid. 그리기만 하므로 Planner를 부르지 않는다
    print(build_graph(_unconfigured_planner).get_graph().draw_mermaid())
