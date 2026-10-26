-- 근거 계약 v2 — papers.id를 R/C 마스터 ID로 바꾸고 계약(app/schemas/evidence.py)의 열·제약을 더한다.
-- 적용: Supabase 대시보드 SQL Editor에서 실행하거나 psql "$DATABASE_URL" -f <이 파일>
-- 파일 전체가 트랜잭션 하나라 중간에 실패하면 아무것도 바뀌지 않는다.

begin;

-- papers.id의 뜻이 36편 시트 ID에서 R/C 마스터 ID로 바뀐다. 같은 번호가 다른 논문인 경우가 있어
-- (옛 P08 = R/C P28) 옛 ID로 넣은 행이 남으면 조인이 조용히 어긋난다. 그래서 빈 테이블에만 적용한다.
do $$
begin
  if exists (select 1 from public.papers) or exists (select 1 from public.evidence_records) then
    raise exception 'papers 또는 evidence_records에 행이 있다. 비운 뒤 다시 적용한다';
  end if;
end
$$;

-- 논문 한 편 = data/manifest.csv 한 행. id는 rc_id(P01…P39 · E01…E21 · N01…N26)
alter table public.papers
  -- 36편 근거 재분류 시트의 옛 ID. 조인 키가 아니다(36편만 값이 있다)
  add column legacy_36_id text unique,
  -- 논문 전체의 유형(StudyDesign). 레코드 metadata.study_design의 기본값
  add column study_design text not null check (study_design in (
    'THEORY_REVIEW', 'SYSTEMATIC_REVIEW_META', 'SCALE_DEVELOPMENT',
    'RANDOMIZED_EXPERIMENT', 'QUASI_EXPERIMENT', 'OBSERVATIONAL'
  )),
  -- 원문 확보 형태(FulltextStatus). 검증의 SOURCE_EXISTS가 본다
  add column fulltext_status text not null check (fulltext_status in (
    'TEXT', 'IMAGE_PDF', 'ABSTRACT_ONLY', 'MISSING'
  )),
  add constraint papers_id_check check (id ~ '^[PEN][0-9]{2}$');

alter table public.evidence_records
  -- 같은 논문·arm·section 안에서 chunk의 순서(0부터)
  add column chunk_idx int not null default 0 check (chunk_idx >= 0),
  -- embedding을 만든 모델. 검색은 설정의 모델로 만든 행만 본다
  add column embedding_model text,
  -- arm ID = paper_id + '-E' + 1부터 시작하는 번호(E0 · E01 금지)
  add constraint evidence_records_experiment_id_check check (
    experiment_id ~ '^[PEN][0-9]{2}-E[1-9][0-9]*$' and starts_with(experiment_id, paper_id || '-E')
  ),
  -- Section의 정규 값. 표기가 갈리면(Methods · METHODS) 아래 unique를 비켜 간다
  add constraint evidence_records_section_check check (section in (
    'ABSTRACT', 'INTRODUCTION', 'METHODS', 'RESULTS', 'DISCUSSION', 'TABLE', 'OTHER'
  )),
  -- 모델을 모르는 임베딩은 검색에서 조용히 빠지므로 둘은 함께 채운다
  add constraint evidence_records_embedding_model_check check (
    (embedding is null) = (embedding_model is null)
  ),
  -- chunk 하나에 한 행. arm이 없는 레코드(experiment_id null)끼리도 겹치지 않게 null을 같은 값으로 본다
  add constraint evidence_records_chunk_key
    unique nulls not distinct (paper_id, experiment_id, section, chunk_idx);

-- 위 unique 인덱스가 paper_id로 시작해 paper_id 조회를 대신한다
drop index public.evidence_records_paper_id_idx;

commit;
