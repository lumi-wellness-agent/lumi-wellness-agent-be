-- 연구 근거 DB 초안 — 논문(papers)과 실험 단위 Evidence Record(evidence_records).
-- 적용: Supabase 대시보드 SQL Editor에서 실행하거나 psql "$DATABASE_URL" -f <이 파일>

create extension if not exists vector with schema extensions;
create extension if not exists pg_trgm with schema extensions;

-- 논문 한 편. id는 근거 재분류 시트의 ID(P01…, N01…)를 그대로 쓴다.
create table public.papers (
  id text primary key,
  doi text unique,  -- 학위논문처럼 DOI가 없으면 null이고 url로 원문을 가리킨다
  title text not null,
  authors text,
  year int,
  url text,
  created_at timestamptz not null default now()
);

-- 검색 단위. 논문을 섹션별로 나누고, 같은 실험의 방법·결과는 experiment_id로 묶는다.
-- 한 논문에 실험이 여럿이면(예: Berman 2008의 보행 실험과 사진 실험) experiment_id를 따로 둔다.
create table public.evidence_records (
  id bigint generated always as identity primary key,
  paper_id text not null references public.papers (id) on delete cascade,
  experiment_id text,  -- 예: P03-E1. 이론 서술처럼 실험에 속하지 않으면 null
  section text not null,  -- 예: Methods, Results
  content text not null,  -- 원문 구간. 요약하지 않고 그대로 넣어 출처를 검증할 수 있게 한다
  metadata jsonb not null default '{}',  -- app/schemas/evidence.py의 EvidenceMetadata
  embedding extensions.vector(1536),  -- text-embedding-3-small(기획서 8.1 초기 후보)의 차원
  created_at timestamptz not null default now()
);

create index evidence_records_paper_id_idx on public.evidence_records (paper_id);
create index evidence_records_metadata_idx on public.evidence_records
  using gin (metadata jsonb_path_ops);
create index evidence_records_embedding_idx on public.evidence_records
  using hnsw (embedding extensions.vector_cosine_ops);
create index evidence_records_content_trgm_idx on public.evidence_records
  using gin (content extensions.gin_trgm_ops);

-- public 스키마의 테이블은 Data API로 노출된다. RLS를 켜고 정책을 두지 않아
-- publishable 키로는 읽고 쓸 수 없게 한다. 테이블 소유자인 postgres 역할로 접속하는
-- 백엔드는 RLS의 영향을 받지 않는다.
alter table public.papers enable row level security;
alter table public.evidence_records enable row level security;
