"""하이브리드 검색 — 메타데이터로 먼저 거른 뒤 벡터·키워드 검색 순위를 RRF로 합친다.

- 메타데이터: `metadata @> '{"medium": "STATIC_IMAGE"}'` (GIN `jsonb_path_ops` 인덱스)
- 벡터: `embedding <=> 질의 임베딩` (코사인 거리, HNSW `vector_cosine_ops` 인덱스)
- 키워드: pg_trgm `similarity(content, 질의)` (GIN `gin_trgm_ops` 인덱스).
  ART·SRT·HRV 같은 정확한 용어를 잡는다. 기획서의 BM25 비교는 따로 실험한다 —
  Postgres 내장 전문 검색의 순위 함수(ts_rank, ts_rank_cd)는 BM25가 아니다.
- RRF: 레코드마다 Σ 1 / (k + 순위)로 합친다.
- Top-K는 5로 시작해 3·10과 비교하고, Dense 단독 결과를 기준선으로 함께 기록한다(기획서 8.1).

테이블과 인덱스는 supabase/migrations의 첫 마이그레이션에 있다.
Planner는 search_evidence Tool(app/agent/tools/evidence.py)로 이 모듈을 부른다.
"""
