-- 라운드 기록에 활동을 고른 과정과 다음 단계 추천의 출처를 더한다.
-- 적용: Supabase 대시보드 SQL Editor에서 실행하거나 psql "$DATABASE_URL" -f <이 파일>
-- 컬럼 추가만 한다(nullable) — 이 컬럼을 모르는 이전 코드와 함께 돌아도 된다. 새 코드는 라운드를
-- 쓸 때 이 컬럼을 채우므로, 새 코드가 배포되기 전에 적용한다.
--
-- - selection: plan이 LLM에 보인 후보 순서와 고른 위치(app/agent/state.Selection). 순서는 등급 가중
--   추첨이라(app/protocols.recommendation_order) probability(그 활동이 맨 앞에 설 확률)로 분석에서
--   무작위성을 보정한다. 이전 라운드는 null.
-- - rule_recommended · recommended_by_llm: replan이 규칙으로 권한 단계와, 실제로 보인 추천
--   (recommended)을 LLM이 정했는지. LLM 추천이 규칙과 다를 때 사용자가 따랐는지 본다.

begin;

alter table public.care_rounds
  add column selection jsonb,  -- method · order · weights · probability · position
  add column rule_recommended text
    check (rule_recommended in ('END', 'CHANGE_ACTIVITY', 'ADDITIONAL_SESSION')),
  add column recommended_by_llm boolean;  -- false = 규칙 제안 그대로(선택지 하나 · LLM 실패)

commit;
