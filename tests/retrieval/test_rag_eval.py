"""scripts/rag_eval.py의 DB 없는 부분 — 지표(다른 활동 근거 비율 · fallback 근거 수 포함), chunk →
레코드 접기, 매체별 순위 합치기, 정답과 사용자 상태로 빼는 정답, 시나리오 · 질의 · 케이스 파일,
결과 비교. 실제 검색은 DB와 OPENAI_API_KEY가, 질의 수집은 LLM이 필요해
`make rag-eval ARGS="run"` · `ARGS="collect"`로 따로 돌린다.
"""

import collections
import json
import math

from app import protocols
from app.schemas.care import CareGoal, ContentType
from app.schemas.evidence import InterventionOrder, Medium
from app.schemas.intake import MINUTES_CHOICES
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


def test_foreign_counts_top_records_outside_the_activity_evidence():
    ranks = {A: 1, B: 3, C: 6}
    assert rag_eval.foreign(ranks, {A}) == [B]  # C는 6위라 plan이 검증에 넘기는 상위 5 밖
    assert rag_eval.foreign_at(ranks, {A}) == 0.5
    assert rag_eval.foreign_at(ranks, {A, B}) == 0.0
    assert rag_eval.foreign_at({}, {A}) == 0.0  # 아무것도 못 찾았으면 섞인 것도 없다


def test_fallback_counts_borrowed_records_instead_of_recall():
    case = rag_eval.EvalCase(
        id="S1-pre-a-PR-15", group="fallback", query="q", protocol_id="PR-15", goal=CareGoal.S1
    )

    def result(ranks):
        return rag_eval.CaseResult(
            case=case,
            view="fallback",
            mode="hybrid",
            ranked=[],
            ranks=ranks,
            gold=[],
            unresolved=[],
            excluded=[],
            own=frozenset(),
        )

    assert result({A: 1, B: 4, C: 7}).metrics == {"found@5": 2.0, "empty@5": 0.0}
    assert result({}).metrics == {"found@5": 0.0, "empty@5": 1.0}  # 카드에 근거가 없다
    assert result({}).scored  # 정답이 없어도 평균에 넣는다


def test_policy_excluded_drops_gold_the_order_filter_refuses():
    orders = {A: {"POST_STRESS"}, B: {"NO_INDUCTION"}, C: set()}
    keys = [A, B, C]
    # 사건 전 사용자에게 스트레스 뒤 회복 연구는 인용하지 않는다. 조건 미상(C)은 거르지 않는다
    assert rag_eval.policy_excluded(keys, orders, InterventionOrder.PRE_STRESS) == [A]
    assert rag_eval.policy_excluded(keys, orders, InterventionOrder.POST_STRESS) == []
    assert rag_eval.policy_excluded(keys, orders, None) == []


def test_compare_shows_a_dash_for_metrics_older_results_lack(tmp_path):
    def saved(foreign: float | None) -> str:
        metrics = {m: 0.5 for m in rag_eval.METRICS if m != "foreign@5"}
        if foreign is not None:
            metrics["foreign@5"] = foreign
        result = {
            "case": "S2-pre-a-PR-06",
            "view": "planner",
            "mode": "hybrid",
            "gold": ["P16/P16-E1"],
        }
        return json.dumps(
            {
                "meta": {"db": "lumi-pg", "at": "-"},
                "summary": {
                    "planner/hybrid": {"n": 1, **metrics},
                    "template/hybrid": {"n": 1, **metrics},  # 옛 평가셋의 묶음
                },
                "results": [{**result, "metrics": metrics}],
            }
        )

    before, after = tmp_path / "before.json", tmp_path / "after.json"
    before.write_text(saved(None), encoding="utf-8")
    after.write_text(saved(0.0), encoding="utf-8")
    lines = rag_eval.compare(before, after).splitlines()
    assert lines[2].startswith("planner/hybrid") and lines[2].split()[-1] == "—"
    assert lines[3].startswith("template/hybrid")  # 모르는 묶음은 뒤로 보낸다
    assert lines[-1] == "hybrid recall@5 · foreign@5가 바뀐 케이스 없음"


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


def test_search_media_follow_the_delivered_media():
    loaded = protocols.load_protocols()
    # PR-01은 정지 이미지와 AI 이미지가 대안이라 하나만 제공하지만 연구 매체는 같다
    for content_type in ContentType:
        assert rag_eval.search_media(loaded["PR-01"], content_type) == [Medium.STATIC_IMAGE]
    pr10 = rag_eval.search_media(loaded["PR-10"], ContentType.CURATED_PHOTO)
    assert pr10 == [Medium.MINDFULNESS, Medium.VIDEO]


def test_scenarios_cover_every_goal_and_state_twice():
    scenarios = rag_eval.load_scenarios()
    assert len({s.id for s in scenarios}) == len(scenarios) == 30
    contexts = collections.defaultdict(list)
    for s in scenarios:
        contexts[(s.goal, s.order_condition)].append(s)
        assert s.minutes in MINUTES_CHOICES
    assert len(contexts) == len(CareGoal) * (len(InterventionOrder) + 1)  # + 잘 모르겠어요
    for pair in contexts.values():
        # 문맥마다 직접 · 돌려 말한 문장 하나씩, 칩은 넉넉 · 빠듯 하나씩
        assert sorted(s.phrasing for s in pair) == ["direct", "indirect"]
        assert sorted(s.tier for s in pair) == ["roomy", "tight"]


def test_every_situation_has_a_collected_query():
    """활동 JSON이 바뀌어 후보가 늘거나 줄면 깨진다 — make rag-eval ARGS="collect"로 채운다."""
    queried = collections.defaultdict(set)
    for q in rag_eval.load_queries():
        queried[q.scenario].add(q.protocol_id)
    for scenario in rag_eval.load_scenarios():
        candidates = {p.protocol_id for p in rag_eval.situation_candidates(scenario)}
        assert queried[scenario.id] == candidates, scenario.id


def test_case_file_is_the_build_output():
    """evidence[]나 시나리오가 바뀌면 깨진다 — make rag-eval ARGS="build"로 다시 만든다."""
    cases, skipped = rag_eval.build_cases(rag_eval.load_scenarios(), rag_eval.load_queries())
    assert skipped == []
    assert [c.model_dump() for c in rag_eval.load_cases()] == [c.model_dump() for c in cases]


def test_fallback_cases_are_the_activities_without_goal_evidence():
    cases, _ = rag_eval.build_cases(rag_eval.load_scenarios(), rag_eval.load_queries())
    assert len({c.id for c in cases}) == len(cases)
    assert all(c.relevant for c in cases if c.group == "planner")
    fallback = {(c.protocol_id, c.goal) for c in cases if c.group == "fallback"}
    assert fallback == {("PR-15", CareGoal.S1), ("PR-21", CareGoal.S1), ("PR-03", CareGoal.S2)}
    assert all(c.media and c.outcomes == list(GOAL_OUTCOMES[c.goal].primary) for c in cases)


def test_build_merges_the_same_query_and_skips_stale_ones():
    scenarios = rag_eval.load_scenarios()
    context = (CareGoal.S2, InterventionOrder.PRE_STRESS)
    pre = [s for s in scenarios if (s.goal, s.order_condition) == context]
    common = {
        "goal": CareGoal.S2,
        "order_condition": InterventionOrder.PRE_STRESS,
        "protocol_id": "PR-06",
        "step": 0,
        "model": "m",
        "collected": "d",
    }

    def query(scenario_id: str, text: str, **change) -> rag_eval.CollectedQuery:
        return rag_eval.CollectedQuery(scenario=scenario_id, query=text, **(common | change))

    a, b = pre
    rows = [
        query(a.id, "slow breathing stress"),
        query(b.id, "slow breathing stress"),  # 같은 상황 · 같은 질의 → 하나로
        query(b.id, "box breathing anxiety"),
        query(a.id, "slow breathing stress", goal=CareGoal.S1),  # 목표가 바뀐 옛 질의
    ]
    cases, skipped = rag_eval.build_cases(scenarios, rows)
    assert [c.query for c in cases] == ["slow breathing stress", "box breathing anxiety"]
    assert cases[0].source.endswith(f"{a.id} {b.id}")
    assert len(skipped) == 1 and skipped[0].startswith(f"{a.id} PR-06")
