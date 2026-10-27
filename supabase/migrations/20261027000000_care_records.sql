-- 세션 기록 — 동의(user_consents) · 세션(care_sessions) · 라운드(care_rounds) · 원문(care_texts).
-- 적용: Supabase 대시보드 SQL Editor에서 실행하거나 psql "$DATABASE_URL" -f <이 파일>
-- 파일 전체가 트랜잭션 하나라 중간에 실패하면 아무것도 바뀌지 않는다.
--
-- 행은 세션 API가 요청마다 그래프 상태에서 다시 쓴다(app/records.py). 분석 테이블(care_sessions ·
-- care_rounds)에는 분류 값 · 칩 답 · 측정값만 두고, 사용자가 쓴 글과 그 글에서 나온 글(요약 · 추천
-- 이유 등)은 원문 동의를 한 사용자만 care_texts에 보관 기한을 두고 남긴다.
-- 모든 행이 auth.users를 따라 지워진다(on delete cascade) — Supabase Auth에서 사용자를 지우면 기록 ·
-- 원문 · 동의가 함께 사라진다.

begin;

-- 동의. 사용자당 한 행. 행이 있으면 version의 필수 동의(세션 기록)를 한 것이다
create table public.user_consents (
  user_id uuid primary key references auth.users (id) on delete cascade,
  version text not null,  -- 동의 문구 버전(app/records.CONSENT_VERSION)
  records_agreed_at timestamptz not null,  -- 필수: 세션 기록(상황 분류 · 답 · 측정값 · 활동 · 결과)
  raw_text boolean not null default false,  -- 선택: 원문 보관
  updated_at timestamptz not null default now()
);

-- 세션 하나 = LangGraph thread 하나(id = thread_id). 끝나면 체크포인트를 지우고 end_summary로 답한다
create table public.care_sessions (
  id uuid primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- 마지막 요청이 멈춘 자리(세션 API의 step · node)
  step text check (step in ('ASK', 'MEASURE', 'RUN', 'CONFIRM', 'END', 'STALLED')),
  node text,
  goal text check (goal in ('S1', 'S2', 'S3')),
  request jsonb not null default '{}',  -- 환경 칩(setting · earphones · minutes_available …). 글 없음
  intake jsonb,  -- IntakeResult의 분류 값(app/records.INTAKE_FIELDS). 글은 care_texts로
  turns jsonb not null default '[]',  -- 질문 slot과 칩 답. 질문 문장과 글 답은 care_texts로
  pre jsonb,  -- 세션 시작 측정(Measurement 목록)
  final jsonb,  -- 종료 측정(Measurement) — S1 BSRI · S2 STAI-6
  end_reason text check (end_reason in ('DONE', 'SAFETY')),
  end_summary jsonb,  -- 끝난 세션의 END payload. 체크포인트를 지운 뒤 GET이 이것으로 답한다
  ended_at timestamptz,
  -- 끝나지 않은 채 쉬어 정리 작업이 체크포인트를 지운 시각. 이 세션은 더 이어 갈 수 없다
  expired_at timestamptz,
  -- SAFETY로 끝난 세션은 끝난 사실만 남긴다
  constraint care_sessions_safety_check check (
    end_reason is distinct from 'SAFETY' or (intake is null and turns = '[]' and request = '{}')
  )
);

create index care_sessions_user_idx on public.care_sessions (user_id, created_at desc);
-- 정리 작업이 쉬는 미완료 세션을 찾는다
create index care_sessions_open_idx on public.care_sessions (updated_at)
  where ended_at is null and expired_at is null;

-- 끝난 라운드 하나(plan → … → confirm). 결정을 받은 뒤에 생긴다
create table public.care_rounds (
  session_id uuid not null references public.care_sessions (id) on delete cascade,
  round int not null check (round >= 1),
  protocol_id text not null,
  minutes int not null check (minutes > 0),
  evidence_record_ids bigint[] not null default '{}',
  relaxed text[] not null default '{}',  -- 후보가 모자라 푼 조건(order · environment)
  environment_gaps text[] not null default '{}',
  completed boolean not null,
  pre jsonb not null,  -- 이 라운드가 비교한 측정(Measurement 목록)
  post jsonb not null,
  deltas jsonb not null,  -- 사후 − 사전
  improved boolean,  -- null = 세션 내 판정 기준 없음
  recommended text check (recommended in ('END', 'CHANGE_ACTIVITY', 'ADDITIONAL_SESSION')),
  options text[] not null default '{}',  -- 고를 수 있던 다음 단계
  next_step text not null check (next_step in ('END', 'CHANGE_ACTIVITY', 'ADDITIONAL_SESSION')),
  created_at timestamptz not null default now(),
  primary key (session_id, round)
);

-- 원문. 원문 동의를 한 사용자의 세션만, expires_at까지. SAFETY로 끝난 세션은 남기지 않는다
create table public.care_texts (
  session_id uuid primary key references public.care_sessions (id) on delete cascade,
  user_id uuid not null references auth.users (id) on delete cascade,  -- 동의 철회 때 한 번에 지운다
  request_text text not null,  -- 처음 입력(CareRequest.text)
  answers jsonb not null default '[]',  -- 질문 문장과 직접 쓴 답
  intake jsonb not null default '{}',  -- 문장에서 읽은 글(event · cues · summary · goal_reason …)
  rounds jsonb not null default '[]',  -- 라운드의 글(추천 이유 · 멈춘 이유 · 한 단어 응답)
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  expires_at timestamptz not null
);

create index care_texts_user_idx on public.care_texts (user_id);
create index care_texts_expires_idx on public.care_texts (expires_at);

-- public 스키마는 Data API로 노출된다. 근거 테이블처럼 RLS를 켜고 정책을 두지 않아 publishable 키로는
-- 읽고 쓸 수 없다. 테이블 소유자인 postgres 역할로 접속하는 백엔드는 RLS의 영향을 받지 않는다.
alter table public.user_consents enable row level security;
alter table public.care_sessions enable row level security;
alter table public.care_rounds enable row level security;
alter table public.care_texts enable row level security;

commit;
