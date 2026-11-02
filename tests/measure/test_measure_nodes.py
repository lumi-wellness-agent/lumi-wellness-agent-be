"""measure_pre · measure_post · measure_final — interrupt payload와 재개 값 채점을 노드 단위로."""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agent.nodes.measure import measure_final, measure_post, measure_pre
from app.agent.state import CareState, IntakeResult
from app.schemas.care import CareGoal, CareRequest
from app.schemas.measure import InstrumentId, Timepoint

CONFIG = {"configurable": {"thread_id": "m"}}
SAM = {"INS-SAM": {"valence": 2, "arousal": 8}}
MFVAS = {"INS-MFVAS": {"fatigue": 80, "concentration": 70, "energy": 20}}


def single_node_graph(node):
    builder = StateGraph(CareState)
    builder.add_node("node", node)
    builder.add_edge(START, "node")
    builder.add_edge("node", END)
    return builder.compile(checkpointer=InMemorySaver())


def instrument_ids(out) -> list[str]:
    return [i["instrument_id"] for i in out["__interrupt__"][0].value["instruments"]]


def start(goal: CareGoal, lang="ko", **extra) -> dict:
    """노드 하나에 넣을 상태 — 문항 언어는 세션 요청(request.lang)을 따른다."""
    request = CareRequest(text="지금 상태", lang=lang)
    return {"request": request, "intake": IntakeResult(goal=goal), **extra}


def test_measure_pre_asks_anchor_plus_goal_scale_and_scores_both():
    graph = single_node_graph(measure_pre)
    out = graph.invoke(start(CareGoal.S1), CONFIG)
    payload = out["__interrupt__"][0].value
    assert payload["measure"] == "PRE" and payload["round"] == 0
    assert instrument_ids(out) == ["INS-SAM", "INS-BSRI"]
    answers = {**SAM, "INS-BSRI": {f"item{i}": 50 for i in range(1, 9)}}
    out = graph.invoke(Command(resume=answers), CONFIG)
    assert [m.instrument_id for m in out["pre"]] == [InstrumentId.SAM, InstrumentId.BSRI]
    assert out["pre"][1].scores == {"mean": 50.0}
    # BSRI는 사전·종료에만 재므로 라운드 사전값은 SAM뿐이다
    assert out["round_pre"] == [out["pre"][0]] and out["round_pre"][0].timepoint is Timepoint.PRE


def test_measure_pre_for_s3_keeps_fatigue_vas_in_round_pre():
    graph = single_node_graph(measure_pre)
    out = graph.invoke(start(CareGoal.S3), CONFIG)
    assert instrument_ids(out) == ["INS-SAM", "INS-MFVAS"]
    out = graph.invoke(Command(resume={**SAM, **MFVAS}), CONFIG)
    assert out["pre"][1].scores == {"fatigue": 80.0, "concentration": 70.0, "energy": 20.0}
    assert out["round_pre"] == out["pre"]  # MFVAS는 라운드마다 다시 재는 척도라 같이 넘어간다


def test_measure_post_is_anchor_only_with_round_number():
    graph = single_node_graph(measure_post)
    out = graph.invoke(start(CareGoal.S2, rounds=[]), CONFIG)
    payload = out["__interrupt__"][0].value
    assert payload["measure"] == "POST" and payload["round"] == 1
    assert instrument_ids(out) == ["INS-SAM"]
    out = graph.invoke(Command(resume={"INS-SAM": {"valence": 6, "arousal": 3}}), CONFIG)
    assert [m.round for m in out["post"]] == [1]
    assert out["post"][0].scores == {"valence": 6.0, "arousal": 3.0}


def test_measure_post_for_s3_adds_fatigue_vas():
    graph = single_node_graph(measure_post)
    out = graph.invoke(start(CareGoal.S3, rounds=[]), CONFIG)
    assert instrument_ids(out) == ["INS-SAM", "INS-MFVAS"]
    out = graph.invoke(Command(resume={**SAM, **MFVAS}), CONFIG)
    assert [m.instrument_id for m in out["post"]] == [InstrumentId.SAM, InstrumentId.MFVAS]
    assert all(m.timepoint is Timepoint.POST and m.round == 1 for m in out["post"])


def test_measure_final_skips_scale_measured_every_round():
    graph = single_node_graph(measure_final)
    out = graph.invoke(start(CareGoal.S3, rounds=[]), CONFIG)
    assert "__interrupt__" not in out and "final" not in out


def test_missing_instrument_answer_fails_loudly():
    graph = single_node_graph(measure_pre)
    graph.invoke(start(CareGoal.S2), CONFIG)
    with pytest.raises(ValueError, match="INS-STAI6 응답이 없다"):
        graph.invoke(Command(resume={"INS-SAM": {"valence": 2, "arousal": 8}}), CONFIG)


def test_english_session_shows_english_items_and_scores_the_same():
    """영어 세션은 문항 문구만 영어다 — 문항 id · 척도 · 채점은 한국어 세션과 같다."""
    answers = {
        **SAM,
        "INS-STAI6": {"calm": 1, "tense": 4, "upset": 3, "relaxed": 2, "content": 1, "worried": 4},
    }
    scores = {}
    for lang in ("ko", "en"):
        graph = single_node_graph(measure_pre)
        config = {"configurable": {"thread_id": f"m-{lang}"}}
        out = graph.invoke(start(CareGoal.S2, lang), config)
        stai6 = out["__interrupt__"][0].value["instruments"][1]
        if lang == "en":
            assert stai6["name"].endswith("(STAI-6)")
            assert stai6["items"][0] == {
                "id": "calm",
                "text": "I feel calm.",
                "low_label": "Not at all",
                "high_label": "Very much so",
            }
        else:
            assert stai6["items"][0]["text"] == "지금 나는 차분하다."
        out = graph.invoke(Command(resume=answers), config)
        scores[lang] = [m.scores for m in out["pre"]]
    assert scores["ko"] == scores["en"]
