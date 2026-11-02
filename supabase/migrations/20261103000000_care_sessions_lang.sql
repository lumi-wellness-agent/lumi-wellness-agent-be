-- 세션 언어(app/schemas/care.CareRequest.lang) — 그 세션의 고정 질문 · 측정 문항 · 안내를 보낸 언어.
-- 측정값은 언어별로 따로 본다: 한국어 STAI-6 · BSRI는 번역 초안이고 영어는 원판이라 같은 척도라고 가정할 근거가 없다.
-- 게스트 · SAFETY 세션에도 남긴다 — 내용이 아니라 언어별 안전 판정 비율을 보는 값이다(care_sessions_safety_check는
-- request · intake · turns만 비운다). 지금까지의 세션은 모두 한국어라 기본값으로 채운다. 기본값이 상수라 표를 다시 쓰지 않는다.
begin;

alter table public.care_sessions
  add column lang text not null default 'ko' check (lang in ('ko', 'en'));

commit;
