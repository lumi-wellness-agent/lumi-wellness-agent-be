"""hybrid.search를 실제 Postgres로 확인한다. TEST_DATABASE_URL이 없으면 건너뛴다(CI에는 DB가 없다).

evidence_records를 fixture 16건(tests/fixtures/evidence_hits.json)으로 바꾼 트랜잭션 안에서 검색하고
끝에 되돌린다 — 적재해 둔 근거는 그대로 남는다. 임베딩은 가짜다. fixture의 score 순서대로 질의
벡터와의 각도를 벌려 벡터 순위를 가짜 검색(tests/agent/fakes.py)의 정렬과 맞춘다. 그래서 키워드가
걸리지 않는 질의에서는 실제 검색과 가짜 검색의 결과가 같아야 한다.
"""

import asyncio
import logging
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from pgvector import Vector
from pgvector.psycopg import register_vector_async
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.retrieval import embedding, hybrid
from app.retrieval.embedding import EMBEDDING_DIMS
from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType
from tests.agent import fakes

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL이 없다")

QUERY = "nature images rumination"  # fixture content(한국어 요약)에 걸리는 낱말이 없다
BY_ID = {hit.evidence_record_id: hit for hit in fakes.load_hits()}
SCORES = {"score", "dense_rank", "keyword_rank"}

PAPER_SQL = """
insert into public.papers (id, study_design, fulltext_status, title, authors, year, doi, url)
values (%s, %s, %s, %s, %s, %s, %s, %s)
on conflict (id) do update set
  study_design = excluded.study_design, fulltext_status = excluded.fulltext_status,
  title = excluded.title, authors = excluded.authors, year = excluded.year,
  doi = excluded.doi, url = excluded.url
"""
RECORD_SQL = """
insert into public.evidence_records
  (id, paper_id, experiment_id, section, chunk_idx, content, metadata, embedding, embedding_model)
overriding system value
values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def vector_at(angle: float) -> list[float]:
    """질의 벡터(각도 0)와의 코사인 거리가 각도 순서를 따르는 벡터."""
    values = [0.0] * EMBEDDING_DIMS
    values[0], values[1] = math.cos(angle), math.sin(angle)
    return values


async def query_vector(text: str) -> list[float]:
    return vector_at(0.0)


async def no_key(text: str) -> None:
    return None


async def api_down(text: str) -> list[float]:
    raise ConnectionError("OpenAI 장애")


class OneConnection:
    """pool.connection()만 흉내 낸다. 검색이 테스트의 트랜잭션 안에서 돌게 한다."""

    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        yield self.conn


@asynccontextmanager
async def fixture_records() -> AsyncIterator[OneConnection]:
    hits = fakes.load_hits()
    conn = await AsyncConnection.connect(DATABASE_URL, prepare_threshold=None)
    try:
        await register_vector_async(conn)
        async with conn.cursor() as cur:
            await cur.execute("delete from public.evidence_records")
            for hit in {h.paper.paper_id: h for h in hits}.values():
                paper = hit.paper
                await cur.execute(
                    PAPER_SQL,
                    (
                        paper.paper_id,
                        hit.metadata.study_design.value,
                        paper.fulltext_status.value,
                        paper.title,
                        paper.authors,
                        paper.year,
                        paper.doi,
                        paper.url,
                    ),
                )
            by_score = sorted(hits, key=lambda h: h.score, reverse=True)
            for i, hit in enumerate(by_score, start=1):
                await cur.execute(
                    RECORD_SQL,
                    (
                        hit.evidence_record_id,
                        hit.paper.paper_id,
                        hit.experiment_id,
                        hit.section.value,
                        hit.chunk_idx,
                        hit.excerpt,
                        Jsonb(hit.metadata.model_dump(mode="json")),
                        Vector(vector_at(0.05 * i)),
                        get_settings().embedding_model,
                    ),
                )
        yield OneConnection(conn)
    finally:
        await conn.rollback()
        await conn.close()


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
