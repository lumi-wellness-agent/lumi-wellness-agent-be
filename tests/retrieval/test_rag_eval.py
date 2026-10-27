"""scripts/rag_eval.py의 DB 없는 부분 — 지표, chunk → 레코드 접기, 매체별 순위 합치기, 정답,
케이스 파일. 실제 검색은 DB와 OPENAI_API_KEY가 필요해 `make rag-eval ARGS="run"`으로 따로 돌린다.
"""

import math

from app import protocols
from app.schemas.care import CareGoal
from app.schemas.evidence import Medium, Outcome
from app.schemas.retrieval import GOAL_OUTCOMES
from scripts import rag_eval

A, B, C = ("P16", "P16-E1"), ("P15", "P15-E1"), ("E08", None)


def test_record_ranks_take_the_best_chunk_of_each_record():
    key_of = {1: A, 2: A, 3: C, 4: B}
    assert rag_eval.record_ranks([2, 3, 1, 4], key_of) == {A: 1, C: 2, B: 4}


def test_metrics_count_records_not_chunks():
    ranks = {A: 1, B: 4}
    m = rag_eval.metrics(ranks, [A, B, C])
    assert m["hit@3"] == 1.0 and m["hit@5"] == 1.0
    assert m["recall@3"] == 1 / 3 and m["recall@5"] == 2 / 3 and m["recall@10"] == 2 / 3
    assert m["mrr"] == 1.0
    none = rag_eval.metrics(ranks, [C])
    assert none["hit@10"] == 0.0 and none["recall@10"] == 0.0 and none["mrr"] == 0.0
    assert math.isnan(rag_eval.recall_at(ranks, [], 5))


def test_merge_rankings_keeps_best_score_and_breaks_ties_by_id():
    first = [(10, 0.5), (11, 0.4)]
    second = [(11, 0.6), (12, 0.6), (13, 0.1)]
    assert rag_eval.merge_rankings([first, second], limit=3) == [11, 12, 10]


def test_rank_rows_by_mode():
    rows = [
        {"id": 1, "dense_rank": 2, "keyword_rank": None},
        {"id": 2, "dense_rank": 1, "keyword_rank": 2},
        {"id": 3, "dense_rank": None, "keyword_rank": 1},
    ]
    assert [cid for cid, _ in rag_eval.rank_rows(rows, "dense", 10)] == [2, 1]
    assert [cid for cid, _ in rag_eval.rank_rows(rows, "keyword", 10)] == [3, 2]


def test_gold_is_the_evidence_that_measured_the_goal_outcomes():
    loaded = protocols.load_protocols()
    s2 = rag_eval.gold(loaded["PR-06"], CareGoal.S2)
    assert s2 and len({r.key for r in s2}) == len(s2)
    primary = set(GOAL_OUTCOMES[CareGoal.S2].primary)
    by_key = {(e.paper_id, e.experiment_id): e for e in loaded["PR-06"].evidence}
    assert all(primary & set(by_key[r.key].outcomes) for r in s2)
    assert rag_eval.gold(loaded["PR-15"], CareGoal.S1) == []  # P34는 RUMINATION을 측정하지 않았다


def test_template_query_uses_research_terms():
    query = rag_eval.template_query([Medium.STATIC_IMAGE, Medium.MINDFULNESS], CareGoal.S1)
    assert query == "nature images mindfulness meditation rumination"
    assert rag_eval.research_media(protocols.load_protocols()["PR-01"]) == [Medium.STATIC_IMAGE]


def test_build_cases_is_deterministic_and_covers_three_groups():
    cases, skipped = rag_eval.build_cases()
    again, _ = rag_eval.build_cases()
    assert [c.model_dump() for c in cases] == [c.model_dump() for c in again]
    assert len({c.id for c in cases}) == len(cases)
    assert {c.group for c in cases} == {"planner", "template", "open"}
    assert all(c.relevant for c in cases)
    assert all(c.media and c.outcomes for c in cases if c.group != "open")
    assert all(not c.media and not c.outcomes for c in cases if c.group == "open")
    assert any("PR-21" in line for line in skipped)  # 83편 밖 근거뿐이라 정답이 없다


def test_case_file_matches_the_schema_and_the_protocols():
    cases = rag_eval.load_cases()
    loaded = protocols.load_protocols()
    assert cases and len({c.id for c in cases}) == len(cases)
    for case in cases:
        protocol = loaded[case.protocol_id]
        assert case.goal in {protocol.goal, *protocol.secondary_goals}
        if case.outcomes:
            assert set(case.outcomes) <= set(Outcome)
