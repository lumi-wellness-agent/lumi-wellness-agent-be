"""measure_pre · measure_post · measure_final — interrupt payload와 재개 값 채점을 노드 단위로."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.nodes.measure import measure_final, measure_post, measure_pre
from app.agent.state import CareState, IntakeResult
from app.schemas.care import CareGoal
from app.schemas.measure import InstrumentId, Timepoint

CONFIG = {"configurable": {"thread_id": "m"}}


def single_node_graph(node):
    builder = StateGraph(CareState)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_measure_pre_asks_anchor_plus_goal_scale_and_scores_both():
    graph = single_node_graph(measure_pre)
    out = graph.invoke({"intake": IntakeResult(goal=CareGoal.S1)}, CONFIG)
    payload = out["__interrupt__"][0].value
    assert payload["measure"] == "PRE" and payload["round"] == 0
    assert [i["instrument_id"] for i in payload["instruments"]] == ["INS-SAM", "INS-BSRI"]
    answers = {
        "INS-SAM": {"valence": 2, "arousal": 8},
        "INS-BSRI": {f"item{i}": 50 for i in range(1, 9)},
    }
    out = graph.invoke(Command(resume=answers), CONFIG)
    assert [m.instrument_id for m in out["pre"]] == [InstrumentId.SAM, InstrumentId.BSRI]
    assert out["pre"][1].scores == {"mean": 50.0}
    assert out["round_pre"] == out["pre"][0] and out["round_pre"].timepoint is Timepoint.PRE


def test_measure_post_is_anchor_only_with_round_number():
    graph = single_node_graph(measure_post)
    out = graph.invoke({"intake": IntakeResult(goal=CareGoal.S2), "rounds": []}, CONFIG)
    payload = out["__interrupt__"][0].value
    assert payload["measure"] == "POST" and payload["round"] == 1
    assert [i["instrument_id"] for i in payload["instruments"]] == ["INS-SAM"]
    out = graph.invoke(Command(resume={"INS-SAM": {"valence": 6, "arousal": 3}}), CONFIG)
    assert out["post"].round == 1 and out["post"].scores == {"valence": 6.0, "arousal": 3.0}


def test_measure_final_skips_without_goal_scale():
    graph = single_node_graph(measure_final)
    out = graph.invoke({"intake": IntakeResult(goal=CareGoal.S3), "rounds": []}, CONFIG)
    assert "__interrupt__" not in out and "final" not in out


def test_missing_instrument_answer_fails_loudly():
    graph = single_node_graph(measure_pre)
    graph.invoke({"intake": IntakeResult(goal=CareGoal.S2)}, CONFIG)
    with pytest.raises(ValueError, match="INS-STAI6 응답이 없다"):
        graph.invoke(Command(resume={"INS-SAM": {"valence": 2, "arousal": 8}}), CONFIG)
