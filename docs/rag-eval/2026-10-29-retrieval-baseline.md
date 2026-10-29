# 근거 검색 평가 — 기준선 (개선 전)

- 평가셋 `rag_eval.jsonl` · 케이스 180개(planner 163 · fallback 17)
- 근거 DB `localhost:54329/postgres` · 검색 구성 hybrid RRF k=60 · 후보 50×2(벡터 · 키워드) · 상위 5 · 키워드 유사도 0.5 · 임베딩 text-embedding-3-small
- 코드 `8849fbc+` · 2026-10-29T14:29:46

## 정답이 있는 상황

| view/mode | n | hit@3 | hit@5 | hit@10 | recall@3 | recall@5 | recall@10 | mrr | foreign@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| planner/hybrid | 163 | 1.000 | 1.000 | 1.000 | 0.840 | 0.920 | 0.949 | 1.000 | 0.000 |
| planner/dense | 163 | 1.000 | 1.000 | 1.000 | 0.820 | 0.901 | 0.947 | 1.000 | 0.000 |
| planner/keyword | 163 | 0.951 | 0.951 | 0.951 | 0.783 | 0.829 | 0.849 | 0.951 | 0.000 |
| open/hybrid | 163 | 0.620 | 0.675 | 0.810 | 0.426 | 0.515 | 0.685 | 0.606 | 0.604 |
| open/dense | 163 | 0.804 | 0.877 | 0.920 | 0.540 | 0.676 | 0.819 | 0.805 | 0.435 |
| open/keyword | 163 | 0.472 | 0.503 | 0.656 | 0.304 | 0.369 | 0.508 | 0.417 | 0.723 |

## fallback — 그 활동 evidence[]에 목표 지표 근거가 없는 상황

| view/mode | n | found@5 | empty@5 |
| --- | ---: | ---: | ---: |
| fallback/hybrid | 17 | 2.471 | 0.118 |
| fallback/dense | 17 | 2.471 | 0.118 |
| fallback/keyword | 17 | 2.471 | 0.118 |

## 상위 5 안에 못 든 정답 (planner · hybrid) — 34건

| 케이스 | 질의 | 놓친 정답@순위 |
| --- | --- | --- |
| S2-pre-a-PR-19 | gratitude journaling stress affect | E11/E11-E1@-, E12/E12-E1@- |
| S2-pre-a-PR-18 | positive writing stress affect | E15@- |
| S2-pre-b-PR-19 | gratitude journaling stress reduction | E11/E11-E1@-, E12/E12-E1@- |
| S2-post-a-PR-14 | mandala coloring stress reduction | E02/E02-E1@10, P31/P31-E1@8, P32/P32-E1@6, P30/P30-E1@- |
| S2-post-a-PR-18 | positive event writing stress affect | E15@- |
| S2-post-a-PR-19 | positive events journaling stress affect | E11/E11-E1@-, E12/E12-E1@- |
| S2-post-b-PR-14 | coloring subjective stress | E02/E02-E1@9, P30/P30-E1@-, E04@7 |
| S2-post-b-PR-19 | gratitude journaling stress affect | E11/E11-E1@-, E12/E12-E1@- |
| S2-post-b-PR-18 | expressive writing stress recovery | E15@- |
| S2-fatigue-a-PR-18 | positive writing stress affect | E15@- |
| S2-fatigue-a-PR-19 | gratitude journaling affect stress | E11/E11-E1@-, E12/E12-E1@- |
| S2-fatigue-b-PR-19 | positive events writing stress affect | E11/E11-E1@-, E12/E12-E1@- |
| S2-daily-a-PR-14 | coloring subjective stress | E02/E02-E1@9, P30/P30-E1@-, E04@7 |
| S2-daily-a-PR-18 | positive writing stress affect | E15@- |
| S2-daily-a-PR-19 | gratitude journaling stress affect | E11/E11-E1@-, E12/E12-E1@- |
| S2-daily-b-PR-14 | coloring stress reduction subjective stress | E02/E02-E1@10, P30/P30-E1@9, E04@7 |
| S2-unknown-a-PR-14 | coloring stress reduction | E02/E02-E1@-, P30/P30-E1@9, E04@7 |
| S2-unknown-a-PR-18 | positive writing stress affect | E15@- |
| S2-unknown-a-PR-19 | gratitude journaling stress affect | E11/E11-E1@-, E12/E12-E1@- |
| S2-unknown-b-PR-14 | coloring subjective stress | E02/E02-E1@9, P30/P30-E1@-, E04@7 |
| S2-unknown-b-PR-18 | positive expressive writing stress affect | E15@- |
| S3-post-a-PR-11 | outdoor walking attention restoration | P07/P07-E1@6 |
| S3-fatigue-a-PR-11 | outdoor walking attention fatigue recovery | P07/P07-E1@6 |
| S3-fatigue-a-PR-01 | nature photos attention fatigue | P01@7 |
| S3-fatigue-b-PR-11 | outdoor walking attention fatigue | P07/P07-E1@6 |
| S3-fatigue-b-PR-01 | nature images attention fatigue | P03/P03-E1@7 |
| S3-daily-a-PR-11 | outdoor walking attention restoration | P07/P07-E1@8 |
| S3-daily-a-PR-01 | nature images attention fatigue | P03/P03-E1@7 |
| S3-daily-b-PR-11 | outdoor walking attention fatigue | P07/P07-E1@6 |
| S3-daily-b-PR-01 | nature images attention | P03/P03-E1@8 |
| S3-unknown-a-PR-11 | outdoor walking attention fatigue | P07/P07-E1@6 |
| S3-unknown-a-PR-01 | natural images attention fatigue | P03/P03-E1@6 |
| S3-unknown-b-PR-11 | outdoor walk attention fatigue | P04@7 |
| S3-unknown-b-PR-01 | nature photographs attention fatigue | P01@7 |

- 사용자 상태로 일부러 거른 정답 19건(분모에서 뺌)

## 상위 5에 든 다른 활동의 근거 (planner · hybrid) — 0건

없음.

## fallback 상위 5 (hybrid) — 17건

| 케이스 | 질의 | 상위 5 레코드 |
| --- | --- | --- |
| S1-pre-a-PR-15 | nature coloring rumination | 없음 — 카드에 근거가 없다 |
| S1-pre-a-PR-21 | mindfulness rumination | N26/N26-E1, P13, P14 |
| S1-post-a-PR-15 | nature coloring rumination | P32/P32-E1 |
| S1-post-a-PR-21 | mindfulness rumination | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13 |
| S1-post-b-PR-21 | mindfulness rumination stress | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13 |
| S1-fatigue-a-PR-15 | nature coloring rumination | 없음 — 카드에 근거가 없다 |
| S1-fatigue-a-PR-21 | mindfulness rumination | N26/N26-E1, P13, P14 |
| S1-daily-a-PR-15 | nature coloring rumination | P32/P32-E1 |
| S1-daily-a-PR-21 | nature images mindfulness rumination | N26/N26-E1, P24/P24-E1, P33/P33-E1, P13 |
| S1-daily-b-PR-21 | mindfulness rumination | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13 |
| S1-unknown-a-PR-15 | nature coloring rumination | P32/P32-E1 |
| S1-unknown-a-PR-21 | mindfulness rumination | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13 |
| S2-pre-a-PR-03 | nature images stress anticipation | N24 |
| S2-daily-a-PR-03 | natural images stress recovery | P24/P24-E1, N24, P33/P33-E1 |
| S2-daily-b-PR-03 | nature images stress physiology | P24/P24-E1, N24, P33/P33-E1 |
| S2-unknown-a-PR-03 | nature images stress reduction | P24/P24-E1, N24, P33/P33-E1 |
| S2-unknown-b-PR-03 | nature scenes subjective stress | P24/P24-E1, N24, P33/P33-E1 |
