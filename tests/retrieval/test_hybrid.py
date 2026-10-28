"""hybrid.search의 DB 없는 부분 — 필터 인자, 키워드 낱말, RRF, 행 → EvidenceHit, 풀·키가 없을 때.

SQL은 tests/retrieval/test_hybrid_db.py가 실제 Postgres로 확인한다.
"""

import asyncio
import logging
import re

from app.config import Settings
from app.retrieval import embedding, hybrid
from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType
from app.schemas.retrieval import EvidenceHit
from tests.agent import fakes

BY_ID = {hit.evidence_record_id: hit for hit in fakes.load_hits()}
SCORES = {"score", "dense_rank", "keyword_rank"}
PAPER_COLUMNS = ("paper_id", "title", "authors", "year", "doi", "url", "fulltext_status")


def row(hit: EvidenceHit, *, dense: int | None, keyword: int | None) -> dict:
    """SEARCH_SQL이 돌려주는 한 행."""
    paper = hit.paper.model_dump(mode="json")
    return {
        "id": hit.evidence_record_id,
        **{name: paper[name] for name in PAPER_COLUMNS},
        "experiment_id": hit.experiment_id,
        "section": hit.section.value,
        "chunk_idx": hit.chunk_idx,
        "content": hit.excerpt,
        "metadata": hit.metadata.model_dump(mode="json"),
        "dense_rank": dense,
        "keyword_rank": keyword,
    }


def test_filter_params_are_containment_values():
    params = hybrid.filter_params(
        medium=Medium.STATIC_IMAGE,
        intervention_order=InterventionOrder.POST_STRESS,
        outcomes=[Outcome.RUMINATION, Outcome.AFFECT, Outcome.RUMINATION],
        record_type=None,
    )
    assert params == {
        "record_types": [
            {"record_type": "ARM"},
            {"record_type": "SYNTHESIS"},
            {"record_type": "OBSERVATION"},
        ],
        "media": {"media": ["STATIC_IMAGE"]},
        "orders": [
            {"intervention_orders": ["NO_INDUCTION"]},
            {"intervention_orders": ["POST_STRESS"]},
        ],
        "outcomes": [
            {"results": [{"outcome": "RUMINATION"}]},
            {"results": [{"outcome": "AFFECT"}]},
        ],
    }


def test_unset_filters_are_null():
    params = hybrid.filter_params(
        medium=None, intervention_order=None, outcomes=(), record_type=RecordType.SYNTHESIS
    )
    assert params == {
        "record_types": [{"record_type": "SYNTHESIS"}],
        "media": None,
        "orders": None,
        "outcomes": None,
    }


def test_no_induction_user_can_cite_every_study_condition():
    params = hybrid.filter_params(
        medium=None,
        intervention_order=InterventionOrder.NO_INDUCTION,
        outcomes=(),
        record_type=None,
    )
    cited = {order["intervention_orders"][0] for order in params["orders"]}
    assert cited == {order.value for order in InterventionOrder}


def test_search_params_fill_every_sql_parameter():
    params = hybrid.search_params(
        query="nature images rumination",
        vector=[1.0, 0.0],
        medium=Medium.STATIC_IMAGE,
        intervention_order=None,
        outcomes=(),
        record_type=None,
        evidence_keys=["P24-E1", "N24", "P24-E1"],
    )
    # 평가(scripts/rag_eval.py)도 이 함수로 SQL 인자를 만든다 — 빠진 자리표시자가 없어야 한다
    assert set(params) == set(re.findall(r"%\((\w+)\)s", hybrid.SEARCH_SQL))
    assert params["evidence_keys"] == ["P24-E1", "N24"]
    assert params["terms"] == ["nature", "images", "rumination"]


def test_search_params_without_scope_or_vector_are_null():
    params = hybrid.search_params(
        query="SART",
        vector=None,
        medium=None,
        intervention_order=None,
        outcomes=(),
        record_type=None,
    )
    assert params["evidence_keys"] is None and params["embedding"] is None


def test_keyword_terms_drop_function_words():
    terms = hybrid.keyword_terms("Nature vs urban images: SART & HF-HRV of the 반추, nature")
    assert terms == ["nature", "urban", "images", "sart", "hf", "hrv", "반추"]


def test_rrf_adds_the_ranks_that_exist():
    assert hybrid.rrf_score(1, None) == 1 / 61
    assert hybrid.rrf_score(3, 2) == 1 / 63 + 1 / 62
    assert hybrid.rrf_score(None, None) == 0


def test_fuse_orders_by_rrf_and_rebuilds_hits():
    rows = [
        row(BY_ID[102], dense=1, keyword=None),
        row(BY_ID[121], dense=8, keyword=1),
        row(BY_ID[122], dense=3, keyword=2),
        row(BY_ID[141], dense=None, keyword=3),
    ]
    hits = hybrid.fuse(rows, limit=3)
    # 두 검색에 다 나온 chunk가 벡터 1위보다 앞선다
    assert [hit.evidence_record_id for hit in hits] == [122, 121, 102]
    assert [(hit.dense_rank, hit.keyword_rank) for hit in hits] == [(3, 2), (8, 1), (1, None)]
    assert [hit.score for hit in hits] == [
        hybrid.rrf_score(3, 2),
        hybrid.rrf_score(8, 1),
        hybrid.rrf_score(1),
    ]
    for hit in hits:
        expected = BY_ID[hit.evidence_record_id]
        assert hit.model_dump(exclude=SCORES) == expected.model_dump(exclude=SCORES)


def test_fuse_breaks_ties_by_id():
    rows = [row(BY_ID[141], dense=None, keyword=1), row(BY_ID[102], dense=1, keyword=None)]
    assert [hit.evidence_record_id for hit in hybrid.fuse(rows, limit=5)] == [102, 141]


def test_without_pool_returns_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger=hybrid.__name__):
        hits = asyncio.run(
            hybrid.search(None, query="nature images rumination", medium=Medium.STATIC_IMAGE)
        )
    assert hits == []
    assert "DATABASE_URL" in caplog.text


def test_query_is_not_embedded_without_key(monkeypatch):
    settings = Settings(_env_file=None, openai_api_key="")
    monkeypatch.setattr(embedding, "get_settings", lambda: settings)
    assert asyncio.run(embedding.embed_query("nature images rumination")) is None
