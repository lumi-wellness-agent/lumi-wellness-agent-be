"""measure_pre · measure_post · measure_final — interrupt 계약과 활성 응답 채점을 검증한다."""

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
PLACEHOLDER_ANSWERS = {
    "INS-STAI6": {f"item{i}": 3 for i in range(1, 7)},
    "INS-BSRI": {f"item{i}": 50 for i in range(1, 9)},
}
TODO_IDS = ["INS-STAI6", "INS-BSRI"]


def single_node_graph(node):
    builder = StateGraph(CareState)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    return builder.compile(checkpointer=InMemorySaver())


def payload(out) -> dict:
    return out["__interrupt__"][0].value


def instruments_by_id(out) -> dict[str, dict]:
    return {item["instrument_id"]: item for item in payload(out)["instruments"]}


@pytest.mark.parametrize("goal", [CareGoal.S1, CareGoal.S2, CareGoal.S3])
def test_measure_pre_sends_sam_and_disabled_todo_placeholders(goal):
    graph = single_node_graph(measure_pre)
    out = graph.invoke({"intake": IntakeResult(goal=goal)}, CONFIG)
    data = payload(out)

    assert data["measure"] == "PRE" and data["round"] == 0
    assert data["required_instrument_ids"] == ["INS-SAM"]
    sent = instruments_by_id(out)
    assert list(sent) == ["INS-SAM", *TODO_IDS]
    assert sent["INS-SAM"]["enabled"] is True
    assert sent["INS-SAM"]["status"] == "READY"
    for instrument_id in TODO_IDS:
        assert sent[instrument_id]["enabled"] is False
        assert sent[instrument_id]["status"] == "TODO"

    # 비활성 척도 답이 함께 와도 SAM만 채점·저장한다.
    out = graph.invoke(Command(resume={**SAM, **PLACEHOLDER_ANSWERS}), CONFIG)
    assert [m.instrument_id for m in out["pre"]] == [InstrumentId.SAM]
    assert [m.instrument_id for m in out["round_pre"]] == [InstrumentId.SAM]
    assert out["pre"][0].scores == {"valence": 2.0, "arousal": 8.0}
    assert out["round_pre"][0].timepoint is Timepoint.PRE


def test_measure_post_requires_and_stores_only_sam():
    graph = single_node_graph(measure_post)
    out = graph.invoke({"intake": IntakeResult(goal=CareGoal.S2), "rounds": []}, CONFIG)
    data = payload(out)
    assert data["measure"] == "POST" and data["round"] == 1
    assert data["required_instrument_ids"] == ["INS-SAM"]
    sent = instruments_by_id(out)
    assert list(sent) == ["INS-SAM"]
    assert sent["INS-SAM"]["enabled"] is True

    out = graph.invoke(Command(resume=SAM), CONFIG)
    assert [m.instrument_id for m in out["post"]] == [InstrumentId.SAM]
    assert out["post"][0].round == 1
    assert out["post"][0].timepoint is Timepoint.POST


@pytest.mark.parametrize("goal", [CareGoal.S1, CareGoal.S2, CareGoal.S3])
def test_measure_final_sends_disabled_todo_and_stores_only_sam(goal):
    graph = single_node_graph(measure_final)
    out = graph.invoke({"intake": IntakeResult(goal=goal), "rounds": []}, CONFIG)
    data = payload(out)
    assert data["measure"] == "FINAL" and data["round"] == 0
    assert data["required_instrument_ids"] == ["INS-SAM"]
    sent = instruments_by_id(out)
    assert list(sent) == ["INS-SAM", *TODO_IDS]
    assert all(sent[i]["enabled"] is False and sent[i]["status"] == "TODO" for i in TODO_IDS)

    out = graph.invoke(Command(resume={**SAM, **PLACEHOLDER_ANSWERS}), CONFIG)
    assert out["final"].instrument_id is InstrumentId.SAM
    assert out["final"].timepoint is Timepoint.FINAL


@pytest.mark.parametrize("node", [measure_pre, measure_post, measure_final])
def test_missing_sam_answer_fails_loudly(node):
    graph = single_node_graph(node)
    graph.invoke(
        {"intake": IntakeResult(goal=CareGoal.S2), "rounds": []},
        CONFIG,
    )
    with pytest.raises(ValueError, match="INS-SAM 응답이 없다"):
        graph.invoke(Command(resume={}), CONFIG)
