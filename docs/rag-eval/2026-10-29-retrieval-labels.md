# 근거 검색 평가 — 레코드 상위 5 + 라벨 수정 (E12-E1 · E15 · E21 메타데이터, PR-19 E11-E1 태그)

- 평가셋 `rag_eval.jsonl` · 케이스 180개(planner 163 · fallback 17)
- 근거 DB `localhost:54329/postgres` · 검색 구성 hybrid RRF k=60 · 후보 50×2(벡터 · 키워드) · 레코드 상위 5(chunk 20개에서) · 키워드 유사도 0.5 · 임베딩 text-embedding-3-small
- 코드 `eff6e57+` · 2026-10-29T15:30:33

## 정답이 있는 상황

| view/mode | n | hit@3 | hit@5 | hit@10 | recall@3 | recall@5 | recall@10 | mrr | foreign@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| planner/hybrid | 163 | 1.000 | 1.000 | 1.000 | 0.937 | 0.989 | 1.000 | 1.000 | 0.000 |
| planner/dense | 163 | 1.000 | 1.000 | 1.000 | 0.937 | 0.989 | 1.000 | 1.000 | 0.000 |
| planner/keyword | 163 | 0.957 | 0.957 | 0.957 | 0.855 | 0.895 | 0.895 | 0.957 | 0.000 |
| open/hybrid | 163 | 0.644 | 0.712 | 0.865 | 0.456 | 0.566 | 0.756 | 0.630 | 0.639 |
| open/dense | 163 | 0.840 | 0.902 | 0.939 | 0.616 | 0.758 | 0.901 | 0.819 | 0.534 |
| open/keyword | 163 | 0.479 | 0.528 | 0.699 | 0.318 | 0.386 | 0.569 | 0.439 | 0.744 |

## fallback — 그 활동 evidence[]에 목표 지표 근거가 없는 상황

| view/mode | n | found@5 | empty@5 |
| --- | ---: | ---: | ---: |
| fallback/hybrid | 17 | 2.765 | 0.118 |
| fallback/dense | 17 | 2.765 | 0.118 |
| fallback/keyword | 17 | 2.765 | 0.118 |

## 상위 5 안에 못 든 정답 (planner · hybrid) — 6건

| 케이스 | 질의 | 놓친 정답@순위 |
| --- | --- | --- |
| S2-post-a-PR-14 | mandala coloring stress reduction | E02/E02-E1@6, P30/P30-E1@7 |
| S2-post-b-PR-14 | coloring subjective stress | E02/E02-E1@6, P30/P30-E1@7 |
| S2-daily-a-PR-14 | coloring subjective stress | E02/E02-E1@6, P30/P30-E1@7 |
| S2-daily-b-PR-14 | coloring stress reduction subjective stress | E02/E02-E1@7, P30/P30-E1@6 |
| S2-unknown-a-PR-14 | coloring stress reduction | E02/E02-E1@7, P30/P30-E1@6 |
| S2-unknown-b-PR-14 | coloring subjective stress | E02/E02-E1@6, P30/P30-E1@7 |

- 사용자 상태로 일부러 거른 정답 19건(분모에서 뺌)

## 상위 5에 든 다른 활동의 근거 (planner · hybrid) — 0건

없음.

## fallback 상위 5 (hybrid) — 17건

| 케이스 | 질의 | 상위 5 레코드 |
| --- | --- | --- |
| S1-pre-a-PR-15 | nature coloring rumination | 없음 — 카드에 근거가 없다 |
| S1-pre-a-PR-21 | mindfulness rumination | N26/N26-E1, P13, P14 |
| S1-post-a-PR-15 | nature coloring rumination | P32/P32-E1 |
| S1-post-a-PR-21 | mindfulness rumination | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13, P14 |
| S1-post-b-PR-21 | mindfulness rumination stress | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13, P14 |
| S1-fatigue-a-PR-15 | nature coloring rumination | 없음 — 카드에 근거가 없다 |
| S1-fatigue-a-PR-21 | mindfulness rumination | N26/N26-E1, P13, P14 |
| S1-daily-a-PR-15 | nature coloring rumination | P32/P32-E1 |
| S1-daily-a-PR-21 | nature images mindfulness rumination | N26/N26-E1, P24/P24-E1, P33/P33-E1, P13, P14 |
| S1-daily-b-PR-21 | mindfulness rumination | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13, P14 |
| S1-unknown-a-PR-15 | nature coloring rumination | P32/P32-E1 |
| S1-unknown-a-PR-21 | mindfulness rumination | N26/N26-E1, P33/P33-E1, P24/P24-E1, P13, P14 |
| S2-pre-a-PR-03 | nature images stress anticipation | N24 |
| S2-daily-a-PR-03 | natural images stress recovery | P24/P24-E1, N24, P33/P33-E1 |
| S2-daily-b-PR-03 | nature images stress physiology | P24/P24-E1, N24, P33/P33-E1 |
| S2-unknown-a-PR-03 | nature images stress reduction | P24/P24-E1, N24, P33/P33-E1 |
| S2-unknown-b-PR-03 | nature scenes subjective stress | P24/P24-E1, N24, P33/P33-E1 |
