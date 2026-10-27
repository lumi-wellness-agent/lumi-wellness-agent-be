"""measure_pre · measure_post · measure_final — payload와 활성 응답 채점을 검증한다."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.nodes.measure import measure_final, measure_post, measure_pre
from app.agent.state import CareState, IntakeResult
from app.schemas.care import CareGoal
from app.schemas.measure import InstrumentId, Timepoint

CONFIG = {"configurable": {"thread_id": "m"}}
SAM = {"INS-SAM": {"valence": 2, "arousal": 8}}


def single_node_graph(node):
    builder = StateGraph(CareState)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    return builder.compile(checkpointer=InMemorySaver())


def payload(out) -> dict:
    return out["__interrupt__"][0].value


@pytest.mark.parametrize(
    ("node", "timepoint", "round_no"),
    [
        (measure_pre, "PRE", 0),
        (measure_post, "POST", 1),
        (measure_final, "FINAL", 0),
    ],
)
@pytest.mark.parametrize("goal", [CareGoal.S1, CareGoal.S2, CareGoal.S3])
def test_all_measure_nodes_send_and_require_only_sam(node, timepoint, round_no, goal):
    graph = single_node_graph(node)
    out = graph.invoke(
        {"intake": IntakeResult(goal=goal), "rounds": []},
        CONFIG,
    )
    data = payload(out)

    assert data["measure"] == timepoint
    assert data["round"] == round_no
    assert data["required_instrument_ids"] == ["INS-SAM"]
    assert [i["instrument_id"] for i in data["instruments"]] == ["INS-SAM"]
    assert data["instruments"][0]["status"] == "READY"

    out = graph.invoke(Command(resume=SAM), CONFIG)
    if node is measure_final:
        assert out["final"].instrument_id is InstrumentId.SAM
        assert out["final"].timepoint is Timepoint.FINAL
    else:
        key = "pre" if node is measure_pre else "post"
        measurements = out[key]
        assert [m.instrument_id for m in measurements] == [InstrumentId.SAM]
        assert measurements[0].scores == {"valence": 2.0, "arousal": 8.0}
        assert measurements[0].timepoint.value == timepoint
        if node is measure_pre:
            assert [m.instrument_id for m in out["round_pre"]] == [InstrumentId.SAM]


@pytest.mark.parametrize("node", [measure_pre, measure_post, measure_final])
def test_missing_sam_answer_fails_loudly(node):
    graph = single_node_graph(node)
    graph.invoke(
        {"intake": IntakeResult(goal=CareGoal.S2), "rounds": []},
        CONFIG,
    )
    with pytest.raises(ValueError, match="INS-SAM 응답이 없다"):
        graph.invoke(Command(resume={"NOT-INS-SAM": {}}), CONFIG)
