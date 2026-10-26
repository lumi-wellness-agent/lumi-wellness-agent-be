"""하이브리드 검색 — 메타데이터로 먼저 거른 뒤 벡터·키워드 검색 순위를 RRF로 합친다.

- 메타데이터: 전부 `@>`(jsonb_path_ops는 containment만). 결과 조건은 같은 객체 안에 둔다
  `metadata @> '{"media": ["STATIC_IMAGE"]}'`
  `metadata @> '{"results": [{"outcome": "RUMINATION", "effect": "POSITIVE"}]}'`
  outcomes OR: 지표마다 `{"results": [{"outcome": "AFFECT"}]}`를 만들어 `metadata @> any(…)`
  순서: 사용자 상태에 인용할 수 있는 연구 조건(retrieval.ORDER_COMPATIBILITY)마다
  `{"intervention_orders": ["POST_STRESS"]}`를 만들어 `@> any(…)`, 또는 조건 미상(빈 목록).
  미상은 검증도 통과시킨다(validation._order_check)
  종류: `@> '{"record_type": "ARM"}'`. 그리고 `embedding_model = 설정의 embedding_model`
- 벡터: 필터된 집합(수백 chunk)에 `embedding <=> 질의` exact 코사인. HNSW는 WHERE 뒤에
  적용돼 결과가 K보다 적을 수 있어 순위 기준으로 의존하지 않는다. 커지면 iterative scan 검토
- 키워드: 질의를 낱말로 나눠(기능어 제외) 낱말마다 pg_trgm `strict_word_similarity`가 0.5 이상인
  chunk를 고르고 그 합으로 정렬한다. ART·SRT·HRV 같은 정확한 용어를 잡는다 — 낱말 단위로 비교해
  ART가 SART에 걸리지 않고, 'SART로'처럼 조사가 붙어도 잡힌다. 질의 전체와의 `similarity`는 긴
  content에서 늘 낮게 나와 쓰지 않는다. 벡터처럼 필터된 집합에 직접 계산하고, 코퍼스가 커지면
  `<<%` 연산자로 바꿔 GIN(gin_trgm_ops) 인덱스를 쓴다.
  기획서의 BM25 비교는 따로 실험한다 — Postgres 전문 검색의 ts_rank·ts_rank_cd는
  BM25가 아니다.
- RRF: 레코드마다 Σ 1 / (k + 순위)로 합친다. 필터는 자동으로 완화하지 않고 로그에 남긴다.
- Top-K는 5로 시작해 3·10과 비교하고, Dense 단독 결과를 기준선으로 함께 기록한다(기획서 8.1).
- 질의 임베딩(retrieval/embedding.py)을 못 만들면(키 없음 · API 오류) 경고를 남기고 키워드로만
  찾는다. OpenAI 장애로 플래너가 Anthropic으로 넘어가도 근거 검색은 돈다. 풀이 없으면 빈 목록이다.

테이블·인덱스는 supabase/migrations에 있다. 결과 모양은 schemas/retrieval.py의 EvidenceHit.
Planner는 search_evidence Tool(app/agent/tools/evidence.py)로 이 모듈을 부른다.
"""

import logging
import re
from collections.abc import Sequence
from typing import Any

from pgvector import Vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.config import get_settings
from app.retrieval import embedding
from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType
from app.schemas.retrieval import ORDER_COMPATIBILITY, EvidenceHit, PaperRef

log = logging.getLogger(__name__)

TOP_K = 5
CANDIDATES = 50  # 벡터·키워드 검색이 각각 RRF에 넘기는 후보 수
RRF_K = 60  # RRF 상수(Cormack et al. 2009). 클수록 한쪽 검색의 상위 순위가 덜 독주한다
KEYWORD_MIN_SIMILARITY = 0.5  # pg_trgm strict_word_similarity_threshold의 기본값
# 키워드에서 빼는 낱말. 거의 모든 chunk에 걸려 순위를 흐린다('자연 vs 도시')
STOPWORDS = frozenset("a an and or the of in on at to for from with by vs versus".split())
# record_type을 주지 않으면 효과 근거가 될 수 있는 종류만.
# 이론 정의(DEFINITION)·참조표(LOOKUP)는 제외
DEFAULT_RECORD_TYPES: tuple[RecordType, ...] = (
    RecordType.ARM,
    RecordType.SYNTHESIS,
    RecordType.OBSERVATION,
)

# 후보는 벡터·키워드 순위의 합집합(최대 2 × CANDIDATES행)이고, RRF는 fuse()가 계산한다.
# 걸지 않는 필터는 인자가 null이다
SEARCH_SQL = """
with filtered as materialized (
  select e.id, e.content, e.embedding <=> %(embedding)s as distance
  from public.evidence_records e
  where e.embedding_model = %(embedding_model)s
    and e.metadata @> any(%(record_types)s::jsonb[])
    and (%(media)s::jsonb is null or e.metadata @> %(media)s::jsonb)
    and (%(orders)s::jsonb[] is null or e.metadata @> any(%(orders)s::jsonb[])
         or e.metadata -> 'intervention_orders' = '[]'::jsonb)
    and (%(outcomes)s::jsonb[] is null or e.metadata @> any(%(outcomes)s::jsonb[]))
),
dense as (
  select id, row_number() over (order by distance, id) as rank
  from filtered
  where distance is not null
  order by distance, id
  limit %(candidates)s
),
keyword as (
  select f.id, row_number() over (order by sum(t.sim) desc, f.id) as rank
  from filtered f
  cross join lateral (
    select strict_word_similarity(term, f.content) as sim
    from unnest(%(terms)s::text[]) as term
  ) t
  where t.sim >= %(min_similarity)s
  group by f.id
  order by rank
  limit %(candidates)s
)
select e.id, e.paper_id, e.experiment_id, e.section, e.chunk_idx, e.content, e.metadata,
       p.title, p.authors, p.year, p.doi, p.url, p.fulltext_status,
       d.rank as dense_rank, k.rank as keyword_rank
from dense d
full join keyword k on k.id = d.id
join public.evidence_records e on e.id = coalesce(d.id, k.id)
join public.papers p on p.id = e.paper_id
"""


def filter_params(
    *,
    medium: Medium | None,
    intervention_order: InterventionOrder | None,
    outcomes: Sequence[Outcome],
    record_type: RecordType | None,
) -> dict[str, Any]:
    """SEARCH_SQL의 필터 인자 — `metadata @>` 오른쪽에 놓을 jsonb 값. 걸지 않는 필터는 None."""
    types = [record_type] if record_type else DEFAULT_RECORD_TYPES
    orders = sorted(ORDER_COMPATIBILITY[intervention_order]) if intervention_order else []
    return {
        "record_types": [{"record_type": t.value} for t in types],
        "media": {"media": [medium.value]} if medium else None,
        "orders": [{"intervention_orders": [o.value]} for o in orders] or None,
        "outcomes": [{"results": [{"outcome": o.value}]} for o in dict.fromkeys(outcomes)] or None,
    }


def _jsonb(value: Any) -> Any:
    """dict → jsonb, dict 목록 → jsonb[]. None(필터 없음)은 그대로."""
    if value is None:
        return None
    return [Jsonb(v) for v in value] if isinstance(value, list) else Jsonb(value)


def keyword_terms(query: str) -> list[str]:
    """키워드 검색에 쓸 낱말. 소문자로 중복 없이, 한 글자와 기능어(STOPWORDS)는 뺀다."""
    words = re.findall(r"\w+", query.lower())
    return list(dict.fromkeys(w for w in words if len(w) > 1 and w not in STOPWORDS))


def rrf_score(*ranks: int | None) -> float:
    """Reciprocal Rank Fusion. 그 검색에 나오지 않은 쪽(None)은 더하지 않는다."""
    return sum(1 / (RRF_K + rank) for rank in ranks if rank is not None)


def to_hit(row: dict[str, Any], score: float) -> EvidenceHit:
    """SEARCH_SQL 한 행 → EvidenceHit. metadata(jsonb)는 EvidenceMetadata로 다시 검증된다."""
    return EvidenceHit(
        evidence_record_id=row["id"],
        paper=PaperRef(
            paper_id=row["paper_id"],
            title=row["title"],
            authors=row["authors"],
            year=row["year"],
            doi=row["doi"],
            url=row["url"],
            fulltext_status=row["fulltext_status"],
        ),
        experiment_id=row["experiment_id"],
        section=row["section"],
        chunk_idx=row["chunk_idx"],
        excerpt=row["content"],
        metadata=row["metadata"],
        score=score,
        dense_rank=row["dense_rank"],
        keyword_rank=row["keyword_rank"],
    )


def fuse(rows: Sequence[dict[str, Any]], limit: int) -> list[EvidenceHit]:
    """벡터·키워드 후보 행을 RRF 점수 순으로 limit건. 동점이면 id 순."""
    scored = sorted(
        ((rrf_score(row["dense_rank"], row["keyword_rank"]), row) for row in rows),
        key=lambda pair: (-pair[0], pair[1]["id"]),
    )
    return [to_hit(row, score) for score, row in scored[:limit]]


async def search(
    pool: AsyncConnectionPool | None,
    *,
    query: str,
    medium: Medium | None = None,
    intervention_order: InterventionOrder | None = None,
    outcomes: Sequence[Outcome] = (),
    record_type: RecordType | None = None,
    limit: int = TOP_K,
) -> list[EvidenceHit]:
    """프리필터 → 벡터·키워드 → RRF 상위 limit건. medium은 연구 매체(study_medium 적용 후).

    intervention_order(사용자 상태)를 주면 그 상태에 인용할 수 있는 연구 조건(ORDER_COMPATIBILITY —
    그 값 또는 NO_INDUCTION, NO_INDUCTION이면 전부)이거나 조건 미상인 레코드가, outcomes를 주면
    하나라도 측정한 레코드가 나온다. 0건이면 0건을 돌려준다(필터 자동 완화 없음).
    """
    wanted = (
        f"medium={medium} order={intervention_order} "
        f"outcomes={[o.value for o in outcomes]} type={record_type}"
    )
    if pool is None:
        log.warning("DB 풀이 없어(DATABASE_URL 미설정) 근거를 찾지 않는다: %r %s", query, wanted)
        return []
    try:
        vector = await embedding.embed_query(query)
    except Exception:
        log.warning("질의 임베딩 실패 — 키워드로만 찾는다: %r", query, exc_info=True)
        vector = None
    else:
        if vector is None:
            log.warning("OPENAI_API_KEY가 없어 키워드로만 찾는다: %r", query)
    filters = filter_params(
        medium=medium,
        intervention_order=intervention_order,
        outcomes=outcomes,
        record_type=record_type,
    )
    params = {
        **{name: _jsonb(value) for name, value in filters.items()},
        "embedding": None if vector is None else Vector(vector),
        "embedding_model": get_settings().embedding_model,
        "terms": keyword_terms(query),
        "min_similarity": KEYWORD_MIN_SIMILARITY,
        "candidates": CANDIDATES,
    }
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(SEARCH_SQL, params)
        rows = await cur.fetchall()
    hits = fuse(rows, limit)
    found = [hit.evidence_record_id for hit in hits] or "0건(필터를 완화하지 않는다)"
    log.info("근거 검색 %r %s → %s", query, wanted, found)
    return hits
