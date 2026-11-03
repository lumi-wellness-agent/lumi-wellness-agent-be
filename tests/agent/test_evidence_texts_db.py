"""적재한 근거 전체(evidence_records)의 카드에 보이는 한국어 메타데이터가 문구 사전과 맞는지 —
TEST_DATABASE_URL(lumi-pg, Supabase와 같은 적재)이 있을 때만. 근거를 다시 적재해 값이 바뀌면 여기서
빠진 문구와 남은 문구가 보인다 — data/evidence_texts/en.json을 함께 고친다."""

import asyncio
import re

from psycopg import AsyncConnection

from app.agent.card_texts import evidence_glossary
from tests.evidence_db import DATABASE_URL, requires_db

pytestmark = requires_db
HANGUL = re.compile(r"[가-힣]")


async def shown_korean() -> set[str]:
    conn = await AsyncConnection.connect(DATABASE_URL, prepare_threshold=None)
    try:
        cur = await conn.execute("select metadata from public.evidence_records")
        rows = await cur.fetchall()
    finally:
        await conn.close()
    values: set[str] = set()
    for (meta,) in rows:
        values.add(meta.get("population_detail") or "")
        for r in meta.get("results") or []:
            values |= {r.get(k) or "" for k in ("measure", "comparison", "stat")}
    return {v for v in values if HANGUL.search(v)}


def test_the_glossary_matches_the_loaded_evidence():
    korean = asyncio.run(shown_korean())
    glossary = set(evidence_glossary("en"))
    assert sorted(korean - glossary) == []  # 옮기지 않은 문구
    assert sorted(glossary - korean) == []  # 적재에서 사라진 문구
