# 추천 이유 평가 — 기준선 (수정 전)

- 생성: `gpt-5.4-mini` · 후보 순서 시드 `plan-eval` · 근거 DB `localhost:54329/postgres` · 계획 36개(시나리오 30 + 활동 변경 6) · 2026-10-29T11:48:24
- 심판: `gpt-5.5`(추론 medium) · 토큰 87,089/25,406 · 2026-10-29T11:49:53
- 규칙은 이 기록을 만든 코드의 규칙(`app/agent/rationale_rules.py`)으로 다시 쟀다

## 규칙 (코드)

| 규칙 | 통과 | 판정 |
| --- | --- | --- |
| sentences | 36 | 36 |
| script | 36 | 36 |
| codes | 36 | 36 |
| stats | 36 | 36 |
| factors | 34 | 36 |
| experimental | 36 | 36 |
| years | 36 | 36 |
| no_evidence | 2 | 2 |

## Judge 항목

| 항목 | PASS | FAIL | NA |
| --- | --- | --- | --- |
| faithful | 34 | 2 | 0 |
| outcomes | 31 | 5 | 0 |
| hedged | 36 | 0 | 0 |
| disclosure | 7 | 15 | 14 |
| personal | 36 | 0 | 0 |

## 전부 통과

규칙과 Judge 항목을 모두 통과한 추천 이유 19/36

## 어긴 것 — 24건

| 계획 | 활동 | 항목 | 문제 구절 | 이유 |
| --- | --- | --- | --- | --- |
| w1/S1-daily-a | PR-15 | disclosure | — | 검증된 근거가 NOT_SUPPORTING 플래그인데, 연구가 목표를 지지하지 않는다는 취지는 밝혔지만 플래그 의미로 요구되는 공개 대상에는 해당하지 않아 NA가 아니라, 검증된 지지 근거가 없다는 점을 명확히 밝혀야 합니다. |
| w1/S1-daily-b | PR-21 | disclosure | — | 사용한 근거에 CONFLICTING_EVIDENCE와 ADAPTED_STIMULUS 플래그가 있는데, 반대 결과도 있다는 점과 제공 콘텐츠가 연구 자극과 다르다는 점을 밝히지 않았습니다. |
| w1/S2-pre-a | PR-19 | outcomes | 검증된 근거에서는 삼킴(savoring) 개입이 부정 정서 증상과 긍정 심리 상태에서 개선이 확인됐습니다(Chen et al., 2026). | 해당 근거들은 모두 목표 지지가 '아니오'이고 개선 지표가 '없음'이므로, 부정 정서 증상과 긍정 심리 상태의 개선을 효과 근거처럼 말할 수 없습니다. |
| w1/S2-pre-b | PR-08 | outcomes | 전기피부활동도 감소했습니다 | 전기피부활동 감소는 발췌에는 있지만 해당 근거의 '개선 지표'에 없으므로 개선된 목표 지표처럼 말하면 안 됩니다. |
| w1/S2-post-b | PR-01 | factors | 숲, 바다 | 규칙 |
| w1/S2-post-b | PR-01 | faithful | 큐레이션 사진의 풍경은 숲·호수·바다·산을 섞어 지루함을 줄이되 연구 자극 범위 안에서 구성했고 | 입력 근거에는 큐레이션 사진이 숲·호수·바다·산으로 구성됐거나 지루함을 줄이도록 구성됐다는 내용, 연구 자극 범위 안이라는 내용이 없습니다. |
| w1/S2-post-b | PR-01 | outcomes | Chen et al. (2025)에서 자연 이미지 감상 후 정서·기분과 상태 반추가 좋아졌으며 | Chen et al. (2025)의 개선 지표에는 정서·기분만 있고 상태 반추는 포함되어 있지 않습니다. |
| w1/S2-post-b | PR-01 | disclosure | 연구 자극 범위 안에서 구성했고 | ADAPTED_STIMULUS 플래그의 ‘제공 콘텐츠가 연구 자극과 다르다’는 점과 CONFLICTING_EVIDENCE 플래그의 ‘반대 결과도 있다’는 점을 밝히지 않았습니다. |
| w1/S2-fatigue-a | PR-20 | factors | 물 풍경 | 규칙 |
| w1/S2-fatigue-a | PR-20 | outcomes | Trupp et al. (2023)에서는 온라인 미술 감상 뒤 불안과 부정적 기분이 줄었습니다. | 해당 근거는 목표 지지 ‘아니오’이고 개선 지표가 ‘없음’인데, 불안과 부정적 기분 개선의 근거처럼 말했습니다. |
| w1/S2-fatigue-a | PR-20 | disclosure | — | Trupp et al. (2023) 등 검증된 근거들이 NOT_SUPPORTING/목표 지지 ‘아니오’라는 점을 충분히 밝히지 않았습니다. |
| w1/S2-unknown-a | PR-01 | outcomes | 주관적 스트레스와 불안 같은 정서 지표가 함께 나아졌습니다(Chen et al., 2025; Fan & Baharum, 2024). | Chen et al. (2025)의 개선 지표는 정서·기분인데, 이를 주관적 스트레스와 불안 개선 근거처럼 함께 제시했습니다. |
| w1/S2-unknown-a | PR-01 | disclosure | — | 검증된 근거의 CONFLICTING_EVIDENCE와 ADAPTED_STIMULUS 플래그, 즉 반대 결과도 있고 제공 콘텐츠가 연구 자극과 다를 수 있다는 점을 밝히지 않았습니다. |
| w1/S2-unknown-b | PR-01 | disclosure | — | 검증된 근거의 CONFLICTING_EVIDENCE(반대 결과도 있음)와 ADAPTED_STIMULUS(제공 콘텐츠가 연구 자극과 다를 수 있음) 플래그를 밝히지 않았습니다. |
| w1/S3-pre-a | PR-11 | disclosure | — | Ohly et al. (2016)와 Bell et al. (2025)의 ADAPTED_STIMULUS 플래그, 즉 제공 콘텐츠가 연구 자극과 다르다는 점을 밝히지 않았습니다. |
| w1/S3-pre-b | PR-01 | disclosure | — | 검증된 근거의 플래그인 CONFLICTING_EVIDENCE(반대 결과도 있음)와 ADAPTED_STIMULUS(제공 콘텐츠가 연구 자극과 다름)를 밝히지 않았습니다. |
| w1/S3-post-b | PR-01 | disclosure | — | 검증된 근거의 CONFLICTING_EVIDENCE와 ADAPTED_STIMULUS 플래그, 즉 반대 결과도 있고 제공 콘텐츠가 연구 자극과 다를 수 있다는 점을 밝히지 않았습니다. |
| w1/S3-fatigue-a | PR-11 | disclosure | — | ADAPTED_STIMULUS 플래그가 있는 근거에 대해 제공 콘텐츠가 연구 자극과 다르다는 점을 밝히지 않았습니다. |
| w1/S3-fatigue-b | PR-01 | disclosure | — | 인용한 근거들에 있는 CONFLICTING_EVIDENCE와 ADAPTED_STIMULUS 플래그, 즉 반대 결과도 있고 제공 콘텐츠가 연구 자극과 다를 수 있다는 점을 밝히지 않았습니다. |
| w1/S3-daily-a | PR-01 | disclosure | — | 검증된 근거에 CONFLICTING_EVIDENCE와 ADAPTED_STIMULUS 플래그가 있는데, 반대 결과도 있다는 점과 제공 콘텐츠가 연구 자극과 다르다는 점을 밝히지 않았습니다. |
| w1/S3-daily-b | PR-10 | disclosure | — | 검증된 근거에 ADAPTED_STIMULUS 플래그가 있는데, 제공 활동/콘텐츠가 연구 자극과 다르다는 점을 밝히지 않았습니다. |
| w1/S3-unknown-a | PR-01 | disclosure | — | 검증된 근거에 CONFLICTING_EVIDENCE와 ADAPTED_STIMULUS 플래그가 있는데, 반대 결과도 있다는 점과 제공 콘텐츠가 연구 자극과 다를 수 있다는 점을 밝히지 않았습니다. |
| w1/S3-unknown-b | PR-01 | faithful | 건강한 성인 연구들에서 이런 경향이 확인됐습니다(Berman et al., 2008; Bell et al., 2025). | 제공된 근거에는 Berman et al. (2008)와 Bell et al. (2025)가 ‘건강한 성인’ 연구라는 정보가 없습니다. |
| w1/S3-unknown-b | PR-01 | disclosure | — | 검증된 근거에 ADAPTED_STIMULUS 플래그가 있는데, 제공 콘텐츠가 연구 자극과 다를 수 있다는 점을 밝히지 않았습니다. |

## Judge 실패 — 0건

없음.
