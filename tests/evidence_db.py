"""근거 DB 테스트 공용 — evidence_records를 fixture 16건(tests/fixtures/evidence_hits.json)으로
바꾼 트랜잭션.

TEST_DATABASE_URL에는 supabase/migrations를 적용한 DB를 쓴다. 검색·검증은 이 트랜잭션 안에서 돌고
끝에 되돌리므로 적재해 둔 근거는 그대로 남는다. 임베딩은 가짜다 — fixture의 score 순서대로 질의
벡터(각도 0)와의 각도를 벌려 벡터 순위를 가짜 검색(tests/agent/fakes.py)의 정렬과 맞춘다.
"""

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
from app.retrieval.embedding import EMBEDDING_DIMS
from tests.agent import fakes

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

requires_db = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL이 없다")

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


class OneConnection:
    """pool.connection()만 흉내 낸다. 검색·검증이 테스트의 트랜잭션 안에서 돌게 한다."""

    def __init__(self, conn: AsyncConnection):
        self.conn = conn

    @asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncIterator[AsyncConnection]:
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
