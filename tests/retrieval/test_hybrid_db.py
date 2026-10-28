"""hybrid.search를 실제 Postgres로 확인한다. TEST_DATABASE_URL이 없으면 건너뛴다(CI에는 DB가 없다).

fixture DB(tests/evidence_db.py)는 벡터 순위가 가짜 검색(tests/agent/fakes.py)의 정렬과 같다. 그래서
키워드가 걸리지 않는 질의에서는 실제 검색과 가짜 검색의 결과가 같아야 한다.
"""

import asyncio
import logging

import pytest

from app.retrieval import embedding, hybrid
from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType
from tests.agent import fakes
from tests.evidence_db import fixture_records, query_vector, requires_db

pytestmark = requires_db

QUERY = "nature images rumination"  # fixture content(한국어 요약)에 걸리는 낱말이 없다
BY_ID = {hit.evidence_record_id: hit for hit in fakes.load_hits()}
SCORES = {"score", "dense_rank", "keyword_rank"}


async def no_key(text: str) -> None:
    return None


async def api_down(text: str) -> list[float]:
    raise ConnectionError("OpenAI 장애")


def run_search(monkeypatch, *, embed=query_vector, setup: str | None = None, **kwargs):
    monkeypatch.setattr(embedding, "embed_query", embed)

    async def run():
        async with fixture_records() as pool:
            if setup:
                await pool.conn.execute(setup)
            return await hybrid.search(pool, **kwargs)

    return asyncio.run(run())


def ids(hits) -> list[int]:
    return [hit.evidence_record_id for hit in hits]


CASES = [
    ({}, [102, 112, 122, 101, 132]),  # 종류 기본값(ARM·SYNTHESIS·OBSERVATION), 상위 5건
    (
        {
            "medium": Medium.STATIC_IMAGE,
            "intervention_order": InterventionOrder.POST_STRESS,
            "outcomes": [Outcome.RUMINATION],
        },
        [102, 112, 101, 111],
    ),
    # 조건 미상(N24 메타분석)은 함께 나온다
    (
        {
            "intervention_order": InterventionOrder.POST_STRESS,
            "outcomes": [Outcome.SUBJECTIVE_STRESS],
        },
        [152, 151],
    ),
    # 유도 없는 사용자에게는 모든 연구 조건을 인용할 수 있다(ORDER_COMPATIBILITY)
    ({"intervention_order": InterventionOrder.NO_INDUCTION}, [102, 112, 122, 101, 132]),
    ({"medium": Medium.VR}, [152, 151]),
    ({"record_type": RecordType.SYNTHESIS}, [152, 162, 172, 151, 161]),
    ({"outcomes": [Outcome.SUBJECTIVE_STRESS, Outcome.ATTENTION]}, [122, 132, 142, 121, 152]),
    # 0건이면 0건 — 필터를 완화하지 않는다
    ({"outcomes": [Outcome.SUBJECTIVE_STRESS], "record_type": RecordType.ARM}, []),
    # 활동 근거 범위 — ARM은 experiment_id, 리뷰는 paper_id(experiment_id 없는 레코드)로 맞춘다
    ({"evidence_keys": ["P24-E1", "N24"]}, [102, 101, 152, 151]),
    ({"evidence_keys": ["P03-E1", "P01"], "outcomes": [Outcome.ATTENTION]}, [122, 121, 162, 161]),
    # 논문 키는 그 논문의 실험 레코드를 부르지 않는다 — evidence[] key와 같은 단위
    ({"evidence_keys": ["P24"]}, []),
]


@pytest.mark.parametrize(("filters", "expected"), CASES)
def test_filters_match_fake_search(monkeypatch, filters, expected):
    hits = run_search(monkeypatch, query=QUERY, **filters)
    fake = asyncio.run(fakes.fake_search(None, query=QUERY, **filters))
    assert ids(hits) == expected == ids(fake)
    assert all(hit.keyword_rank is None for hit in hits)


def test_hits_round_trip_fixture_records(monkeypatch):
    hits = run_search(monkeypatch, query=QUERY, limit=20)
    assert len(hits) == len(BY_ID)
    for rank, hit in enumerate(hits, start=1):
        expected = BY_ID[hit.evidence_record_id]
        assert hit.model_dump(exclude=SCORES) == expected.model_dump(exclude=SCORES)
        assert (hit.dense_rank, hit.keyword_rank) == (rank, None)
        assert hit.score == hybrid.rrf_score(rank)


def test_keyword_match_lifts_exact_terms(monkeypatch):
    # 'SART'는 P03-E1 두 chunk(121 · 122)와 N19-E1 METHODS(141)에만 있다
    hits = run_search(monkeypatch, query="SART")
    assert ids(hits) == [122, 121, 141, 102, 112]
    assert [hit.keyword_rank for hit in hits] == [2, 1, 3, None, None]
    assert all(hit.score == hybrid.rrf_score(hit.dense_rank, hit.keyword_rank) for hit in hits)


def test_keyword_compares_whole_words(monkeypatch):
    hits = run_search(monkeypatch, query="ART", limit=20)
    assert hits and all(hit.keyword_rank is None for hit in hits)  # SART에 걸리지 않는다


@pytest.mark.parametrize("embed", [no_key, api_down])
def test_without_query_vector_searches_keywords_only(monkeypatch, caplog, embed):
    with caplog.at_level(logging.WARNING, logger=hybrid.__name__):
        hits = run_search(monkeypatch, embed=embed, query="SART")
    assert ids(hits) == [121, 122, 141]
    assert all(hit.dense_rank is None for hit in hits)
    assert "키워드로만" in caplog.text


def test_rows_from_another_embedding_model_are_skipped(monkeypatch):
    hits = run_search(
        monkeypatch,
        setup="update public.evidence_records set embedding_model = 'other-model' where id = 102",
        query=QUERY,
    )
    assert ids(hits) == [112, 122, 101, 132, 111]
