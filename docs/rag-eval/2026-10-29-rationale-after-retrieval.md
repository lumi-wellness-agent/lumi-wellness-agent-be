# 추천 이유 평가 — 검색 개선 후 (레코드 상위 5 · 라벨 수정)

- 생성: `gpt-5.4-mini` · 후보 순서 시드 `plan-eval` · 근거 DB `localhost:54329/postgres` · 계획 36개(시나리오 30 + 활동 변경 6) · 2026-10-29T15:32:03
- 심판: `gpt-5.5`(추론 medium) · 토큰 92,020/15,469 · 2026-10-29T15:33:05
- 규칙은 이 기록을 만든 코드의 규칙(`app/agent/rationale_rules.py`)으로 다시 쟀다

## 규칙 (코드)

| 규칙 | 통과 | 판정 |
| --- | --- | --- |
| sentences | 36 | 36 |
| script | 36 | 36 |
| codes | 36 | 36 |
| stats | 36 | 36 |
| factors | 36 | 36 |
| experimental | 36 | 36 |
| years | 36 | 36 |
| no_evidence | 2 | 2 |

## Judge 항목

| 항목 | PASS | FAIL | NA |
| --- | --- | --- | --- |
| faithful | 35 | 1 | 0 |
| outcomes | 35 | 1 | 0 |
| hedged | 35 | 1 | 0 |
| disclosure | 28 | 0 | 8 |
| personal | 36 | 0 | 0 |

## 전부 통과

규칙과 Judge 항목을 모두 통과한 추천 이유 33/36

## 어긴 것 — 3건

| 계획 | 활동 | 항목 | 문제 구절 | 이유 |
| --- | --- | --- | --- | --- |
| a4/S2-daily-b | PR-06 | hedged | 짧은 호흡으로 바로 몸의 긴장을 낮추는 활동이 맞습니다. | 효과를 완화 표현 없이 단정하고 ‘바로’ 긴장이 낮아질 것처럼 즉시 효과를 약속합니다. |
| a4/S2-unknown-b+change | PR-14 | outcomes | Holt, Furbert & Sweetingham(2019)은 불안과 마음챙김 점수의 개선을 확인했습니다. | Holt, Furbert & Sweetingham(2019)의 개선 지표는 불안·주관적 스트레스이며, 마음챙김은 해당 근거의 개선 지표에 없습니다. |
| a4/S3-post-a | PR-04 | faithful | 다만 이 근거는 건강한 성인을 대상으로 한 결과입니다. | 근거 발췌에는 성인 참가자 정보는 있지만 ‘건강한’ 성인이라는 조건은 제시되어 있지 않습니다. |

## Judge 실패 — 0건

없음.
