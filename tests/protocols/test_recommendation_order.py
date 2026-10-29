"""후보 추천 순서 — 등급 가중 추첨(protocols.recommendation_order).

맨 앞에 설 확률은 가중치 / 합이고, 같은 세션(시드)은 늘 같은 순서이며, 후보를 하나 빼도(활동 변경)
나머지 순서가 그대로다. 시드는 고정된 문자열이라 테스트도 늘 같은 결과다.
"""

from collections import Counter

import pytest

from app import protocols
from app.agent.state import default_environment
from app.schemas.care import CareGoal
from app.schemas.evidence import InterventionOrder

# 발표 시나리오 — S2 · 발표 앞둠 · 기본 10분 · 장소 모름
S2_PRE = protocols.plan_candidates(
    CareGoal.S2,
    order=InterventionOrder.PRE_STRESS,
    minutes=10,
    environment=default_environment(),
)


def ids(order: protocols.RecommendationOrder) -> list[str]:
    return [p.protocol_id for p in order.protocols]


def weighted(seed: str, search=S2_PRE, **kwargs) -> protocols.RecommendationOrder:
    return protocols.recommendation_order(search, CareGoal.S2, seed=seed, **kwargs)


def test_without_a_seed_the_order_stays_ranked():
    order = protocols.recommendation_order(S2_PRE, CareGoal.S2)
    assert (order.method, order.weights) == ("RANKED", {})
    assert ids(order) == [p.protocol_id for p in S2_PRE.protocols]
    assert order.probability("PR-06") is None


def test_weights_follow_the_best_goal_grade():
    # S2 1차 지표(스트레스 · 정서) 최고 등급: PR-06 · PR-07 A, PR-19 B, PR-20 · PR-17 C,
    # PR-05 D, PR-03 · PR-22 —
    assert weighted("s").weights == {
        "PR-06": 4.0,
        "PR-07": 4.0,
        "PR-19": 3.0,
        "PR-20": 2.0,
        "PR-17": 2.0,
        "PR-05": 1.0,
        "PR-03": 1.0,
        "PR-22": 1.0,
    }
    assert weighted("s").probability("PR-06") == pytest.approx(4 / 18)
    assert min(protocols.GRADE_WEIGHT.values()) > 0  # 어느 후보든 맨 앞에 설 수 있다


def test_a_session_always_gets_the_same_order_and_sessions_differ():
    first = weighted("session-1")
    assert first.method == "WEIGHTED" and ids(first) == ids(weighted("session-1"))
    assert sorted(ids(first)) == sorted(p.protocol_id for p in S2_PRE.protocols)
    assert len({tuple(ids(weighted(f"s{i}"))) for i in range(50)}) > 10


def test_the_first_activity_follows_the_weights():
    n = 3000
    first = Counter(ids(weighted(f"s{i}"))[0] for i in range(n))
    order = weighted("s")
    for protocol_id in order.weights:
        assert first[protocol_id] / n == pytest.approx(order.probability(protocol_id), abs=0.03)
    assert len(first) == len(order.weights)  # 등급 순이면 늘 PR-06이었다


def test_removing_a_candidate_keeps_the_rest_in_order():
    # 활동 변경 — 바꾸기로 한 활동을 빼도 같은 세션의 나머지는 같은 순서다
    full = ids(weighted("session-1"))
    without = protocols.plan_candidates(
        CareGoal.S2,
        order=InterventionOrder.PRE_STRESS,
        minutes=10,
        environment=default_environment(),
        exclude=[full[0]],
    )
    assert ids(weighted("session-1", without)) == full[1:]


def test_recent_activities_are_weighted_down_not_removed():
    order = weighted("s", recent=["PR-06"])
    assert order.weights["PR-06"] == pytest.approx(4.0 * protocols.RECENT_FACTOR)
    assert "PR-06" in ids(order)


def test_relaxed_minutes_keep_the_shortest_first():
    # S3 · 3분 칩 — 3분 안의 S3 활동이 없어 시간을 풀었다. 가용 시간을 덜 넘는 활동이 먼저다
    short = protocols.plan_candidates(CareGoal.S3, order=InterventionOrder.POST_FATIGUE, minutes=3)
    assert "minutes" in short.relaxed
    order = protocols.recommendation_order(short, CareGoal.S3, seed="s")
    assert order.method == "SHORTEST"
    assert ids(order) == [p.protocol_id for p in short.protocols]
