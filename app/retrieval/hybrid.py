"""하이브리드 검색 — 메타데이터로 먼저 거른 뒤 벡터·키워드 검색 순위를 RRF로 합친다.

- 메타데이터: 전부 `@>`(jsonb_path_ops는 containment만). 결과 조건은 같은 객체 안에 둔다
  `metadata @> '{"media": ["STATIC_IMAGE"]}'`
  `metadata @> '{"results": [{"outcome": "RUMINATION", "effect": "POSITIVE"}]}'`
  outcomes OR: 지표마다 `@> '{"results": [{"outcome": "AFFECT"}]}'`를 or로 잇는다
  순서: `@> '{"intervention_orders": ["POST_STRESS"]}' or … ["NO_INDUCTION"]`
  종류: `@> '{"record_type": "ARM"}'`. 그리고 `embedding_model = 설정의 embedding_model`
- 벡터: 필터된 집합(수백 chunk)에 `embedding <=> 질의` exact 코사인. HNSW는 WHERE 뒤에
  적용돼 결과가 K보다 적을 수 있어 순위 기준으로 의존하지 않는다. 커지면 iterative scan 검토
- 키워드: pg_trgm `content % 질의`·ILIKE로 후보를 고르고 `similarity`로 정렬한다
  (GIN gin_trgm_ops). ART·SRT·HRV 같은 정확한 용어를 잡는다.
  기획서의 BM25 비교는 따로 실험한다 — Postgres 전문 검색의 ts_rank·ts_rank_cd는
  BM25가 아니다.
- RRF: 레코드마다 Σ 1 / (k + 순위)로 합친다. 필터는 자동으로 완화하지 않고 로그에 남긴다.
- Top-K는 5로 시작해 3·10과 비교하고, Dense 단독 결과를 기준선으로 함께 기록한다(기획서 8.1).

테이블·인덱스는 supabase/migrations에 있다. 결과 모양은 schemas/retrieval.py의 EvidenceHit.
Planner는 search_evidence Tool(app/agent/tools/evidence.py)로 이 모듈을 부른다.
"""

from collections.abc import Sequence

from psycopg_pool import AsyncConnectionPool

from app.schemas.evidence import InterventionOrder, Medium, Outcome, RecordType
from app.schemas.retrieval import EvidenceHit

TOP_K = 5
# record_type을 주지 않으면 효과 근거가 될 수 있는 종류만.
# 이론 정의(DEFINITION)·참조표(LOOKUP)는 제외
DEFAULT_RECORD_TYPES: tuple[RecordType, ...] = (
    RecordType.ARM,
    RecordType.SYNTHESIS,
    RecordType.OBSERVATION,
)


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

    intervention_order를 주면 그 값 또는 NO_INDUCTION인 레코드가, outcomes를 주면 하나라도 측정한
    레코드가 나온다. 0건이면 0건을 돌려준다(필터 자동 완화 없음).
    """
    raise NotImplementedError("PR-C: 질의 임베딩 + SQL을 붙인다")
