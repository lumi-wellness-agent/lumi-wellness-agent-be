# 추천 이유 평가 — 후검증 · 꼭 밝힐 것 (수정 후)

- 생성: `gpt-5.4-mini` · 후보 순서 시드 `plan-eval` · 근거 DB `localhost:54329/postgres` · 계획 36개(시나리오 30 + 활동 변경 6) · 2026-10-29T13:01:03
- 심판: `gpt-5.5`(추론 medium) · 토큰 77,896/16,449 · 2026-10-29T13:01:42
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
| faithful | 34 | 2 | 0 |
| outcomes | 34 | 2 | 0 |
| hedged | 36 | 0 | 0 |
| disclosure | 26 | 1 | 9 |
| personal | 36 | 0 | 0 |

## 전부 통과

규칙과 Judge 항목을 모두 통과한 추천 이유 33/36

## 어긴 것 — 5건

| 계획 | 활동 | 항목 | 문제 구절 | 이유 |
| --- | --- | --- | --- | --- |
| a2/S2-post-b | PR-01 | faithful | Chen et al. (2025)과 Fan & Baharum (2024)에서 불안·주관적 스트레스 지표의 변화가 확인됐습니다. | Chen et al. (2025)의 개선 지표는 정서·기분으로 제시되어 있어 불안·주관적 스트레스 지표 변화가 확인됐다고 말할 근거가 없습니다. |
| a2/S2-post-b | PR-01 | outcomes | Chen et al. (2025)과 Fan & Baharum (2024)에서 불안·주관적 스트레스 지표의 변화가 확인됐습니다. | Chen et al. (2025)의 개선 지표에는 불안·주관적 스트레스가 없는데 해당 지표가 개선된 근거처럼 말했습니다. |
| a2/S2-unknown-b | PR-01 | faithful | Chen et al. (2025), Fan & Baharum (2024)에서 불안·주관적 스트레스 지표가 개선되었습니다. | Chen et al. (2025)의 검증된 근거에는 개선 지표가 정서·기분으로만 제시되어 있어 불안·주관적 스트레스 지표가 개선됐다고 말할 근거가 없습니다. |
| a2/S2-unknown-b | PR-01 | outcomes | Chen et al. (2025), Fan & Baharum (2024)에서 불안·주관적 스트레스 지표가 개선되었습니다. | Chen et al. (2025)에서 개선됐다고 말한 불안·주관적 스트레스 지표가 해당 근거의 개선 지표에 없습니다. |
| a2/S3-post-b | PR-01 | disclosure | — | ADAPTED_STIMULUS 플래그에 해당하는, 연구에서 쓴 자극을 LUMI에 맞게 바꾼 콘텐츠라 같은 효과가 나는지는 확인되지 않았다는 설명이 빠졌습니다. |

## Judge 실패 — 0건

없음.
