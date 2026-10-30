# RAG 사용 vs LLM 단독 vs LLM + 웹 검색 — 2026-10-30 · P18 라벨 · 지표 표시 이름 수정 후

- A. LUMI RAG: 생성 `gpt-5.4-mini` · 추천 이유 36개 × 반복 1 · 생성 2026-10-30T15:18:39 · 생성 비용 $0.244 · Judge `gpt-5.5`(medium) 토큰 64,801/31,616 $1.272 · 2026-10-30T15:20:55 · 토큰 · 비용은 plan 노드 한 번(선택 + 설명 두 호출)이다
- B. LLM 단독: 생성 `gpt-5.4-mini` · 추천 이유 36개 × 반복 3 · 생성 2026-10-30T10:57:44 · 생성 비용 $0.158 · Judge `gpt-5.5`(medium) 토큰 51,660/28,196 $1.104 · 2026-10-30T12:09:43
- C. LLM + 웹 검색: 생성 `gpt-5.4-mini` · 추천 이유 36개 × 반복 1 · 생성 2026-10-30T10:58:20 · 생성 비용 $0.550 · 웹 검색 36회 · Judge `gpt-5.5`(medium) 토큰 68,673/40,300 $1.552 · 2026-10-30T12:11:43

## 재측정 메모

- 오전 측정(`2026-10-30-rag-compare.md`)에서 A의 주장 불일치 5건이 모두 Toussaint 2021이라 P18 레코드의 라벨을 고쳤고(#131),
  고친 뒤 A만 다시 쟀다. **라벨만 고친 재측정(15:00)에서는 불일치 5건이 그대로였다** — 문장이 "불안 · 주관적 스트레스와
  정서 · 기분"에서 "불안 · 주관적 스트레스"로 바뀌었을 뿐이고, 이는 `SUBJECTIVE_STRESS` 지표의 범주 이름이다(초록은 '이완
  상태'만 말한다). 그래서 근거 줄의 '개선 지표'를 범주 이름이 아니라 그 근거가 잰 것(`results[].measure` → '이완 상태' ·
  '불안' · '주관적 스트레스')으로 적게 고치고(`app/agent/prompts.py` `outcome_display`) 다시 쟀다 — 이 기록이 그것이다.
  라벨만 고친 측정은 OpenAlex 하루 예산 소진으로 36개 계획 중 13개의 초록이 빠져 숫자로 쓰지 않는다.
- B · C는 오전 측정 그대로다. 단, 새 A가 다른 활동을 고른 S2-fatigue-a(PR-19 → PR-07)는 B(3회) · C도 그 활동으로 다시
  생성 · 판정해 바꿔 넣었다(활동 고정 유지). 오전 측정과 다른 B · C 숫자는 이 한 건 때문이다.
- A의 인용 검증(실존 · 초록)은 오전 측정의 같은 논문 결과를 재사용했다 — OpenAlex 하루 예산 소진으로 새 조회가 Crossref ·
  Europe PMC로 빠져 자가 달라지는 것을 막기 위해서다(새 논문은 없었다). Judge · 생성 모델 · 프롬프트 · 시나리오 · 시드는 같다.
- '출처 없는 연구 주장'에서 '다만' 없이 쓴 AI 이미지 고지("AI 생성 이미지는 연구에서 사용한 사진과는 다릅니다")를 빼도록
  규칙을 고쳤다(`_DISCLOSURE`). 오전 측정의 B · C 수는 이 규칙으로 다시 세어도 같다.
- 비용 — 라벨만 고친 측정 $1.63(plan-eval $0.25 · Judge $1.31 · S2-fatigue-a B · C $0.08) + 표시 이름 수정 후 $1.61
  (plan-eval $0.24 · Judge $1.27 · S2-fatigue-a B · C $0.10) = $3.24.

## 비교표

| | A. LUMI RAG | B. LLM 단독 | C. LLM + 웹 검색 |
| --- | ---: | ---: | ---: |
| 추천 이유 수 | 36 | 36 | 36 |
| 인용 수 / 추천 이유 | 1.86 | 1.56 | 1.72 |
| 인용 없는 추천 이유 | 2/36 | 0/36 | 5/36 |
| 출처 없는 연구 주장 문장(건 / 추천 이유 수) | 1 / 36 | 3 / 36 | 11 / 36 |
| 인용 실존 — 일치(FOUND) | 67/67 (100%) | 37/56 (66%) | 53/62 (85%) |
| 인용 실존 — 서지 오류(MISMATCH) | 0/67 (0%) | 9/56 (16%) | 9/62 (15%) |
| 인용 실존 — 없음(NOT_FOUND) | 0/67 (0%) | 10/56 (18%) | 0/62 (0%) |
| 인용 실존 — 인용 목록에 없음(UNDECLARED) | 0/67 (0%) | 0/56 (0%) | 0/62 (0%) |
| 학술 출처 비율 | 67/67 (100%) | 46/56 (82%) | 59/62 (95%) |
| 출처 유형 | 학술 67 | 학술 46 · 없음 10 | 학술 59 · 상업 2 · 없음 1 |
| URL 응답 OK(web) | — | — | 61/61 |
| 주장 지지(SUPPORTED) / 인용 전체 | 54/67 (81%) | 13/56 (23%) | 55/62 (89%) |
| 주장 지지 / 출처 텍스트 있는 인용 | 54/60 (90%) | 13/28 (46%) | 55/61 (90%) |
| 주장 불일치(NOT_SUPPORTED) | 2/67 (3%) | 8/56 (14%) | 5/62 (8%) |
| 연구 조건 — 사용자 상태와 맞음(MATCH) | 37/67 (55%) | 10/56 (18%) | 27/62 (44%) |
| 연구 조건 — 어긋남(MISMATCH) | 3/67 (4%) | 7/56 (12%) | 14/62 (23%) |
| hedged PASS | 36/36 | 36/36 | 36/36 |
| personal PASS | 36/36 | 36/36 | 36/36 |
| 재현성 — 반복 간 인용 집합 Jaccard | — | 0.086 | — |
| 재현성 — 1회차 인용이 매번 다시 나옴 | — | 7/56 | — |
| 비용 / 추천 이유(USD) | $0.0068 | $0.0015 | $0.0156 |
| 토큰 / 추천 이유(입력/출력) | 7,565/243 | 654/216 | 9,286/393 |
| 초 / 추천 이유 | 3.4 | 3.7 | 5.6 |

- 인용은 추천 이유 문장의 '저자(연도)'를 코드가 찾은 것이다. 실존은 OpenAlex(DOI → 제목 검색, 없으면 Crossref)로 제목 · 연도(±1) · 1저자 성을 대조했다. 주장 지지 · 연구 조건 · hedged · personal은 Judge 판정이다(출처 텍스트 = 초록, web은 페이지 본문).
- rag의 비용 · 토큰은 plan 노드 한 번(활동 선택 + 설명 두 호출)이고 llm · web은 설명 한 호출이다.

## 어긴 것

### A. LUMI RAG

출처가 뒷받침하지 않는 주장 2건

| 계획 | 인용 | 주장 문장 | 이유 |
| --- | --- | --- | --- |
| S2-unknown-b | Chen et al. (2025) | 이 활동은 실내에서 바로 보기 좋고, 연구에서도 자연 이미지를 본 뒤 불안·주관적 스트레스가 좋아졌습니다(Chen et al., 2025; Fan & Baharum, 2024). | 출처는 자연 이미지 감상 뒤 스트레스 회복과 정서·반추 변화는 보고하지만, 불안 감소가 좋아졌다고 보고하지는 않습니다. |
| S2-unknown-b | Fan & Baharum (2024) | 이 활동은 실내에서 바로 보기 좋고, 연구에서도 자연 이미지를 본 뒤 불안·주관적 스트레스가 좋아졌습니다(Chen et al., 2025; Fan & Baharum, 2024). | 출처는 디지털 자연 환경의 스트레스 감소를 다루지만, 자연 이미지 감상 뒤 불안이 좋아졌다는 결과는 제시하지 않습니다. |

연구 조건이 사용자 상태와 어긋난 인용 3건

| 계획 | 인용 | 이유 |
| --- | --- | --- |
| S1-daily-a | Drew (2018) | 출처의 활동은 어려운 애너그램으로 실험실 스트레스를 유발한 뒤 시행된 연구인데, 사용자는 특별한 계기가 없는 상태입니다. |
| S1-daily-b | Chen et al. (2025) | 출처는 스트레스 유발 뒤 자연 이미지 보기의 회복 효과를 본 연구인데, 사용자 상태는 특별한 계기나 스트레스 유발이 없는 반복 생각 완화 상황입니다. |
| S3-post-a | Van Hedger et al. (2019) | 출처의 연구는 스트레스 유발 뒤 회복 상황이 아니라 일반 참가자가 자연음 또는 도시 소리를 듣기 전후의 인지 수행을 비교한 상황입니다. |

출처 없는 연구 주장 문장 1건

- S3-pre-a: 그래서 야외 자연 보행을 골랐고, 자연환경에 노출되었을 때 주의·인지가 좋아졌다는 연구 근거를 썼습니다.


### B. LLM 단독

찾지 못한 인용 10건

| 계획 | 활동 | 인용 | 모델이 낸 제목 |
| --- | --- | --- | --- |
| S1-daily-b | PR-21 | Kang et al. (2014) | The effect of viewing nature on stress and anxiety: A meta-analysis |
| S1-fatigue-a | PR-09 | Gu et al. (2015) | Mindfulness meditation and self-related processing: A review and synthesis |
| S1-post-a | PR-15 | Mills & Dyer (2019) | The effects of coloring on anxiety, mood, and mindfulness in adults: A systematic review |
| S1-unknown-b | PR-09 | MacLean et al. (2010) | Mystical experiences occasioned by the hinsight of the development of mindfulness meditation: a prospective study |
| S2-daily-b | PR-06 | Ziporyn et al. (2023) | Acute effects of physiological sigh breathing on mood and arousal in healthy adults |
| S2-post-a | PR-14 | Dyson et al. (2016) | The art of letting go: An investigation of the benefits of mandala coloring for anxiety reduction |
| S2-post-b | PR-01 | Browning et al. (2020) | The health benefits of viewing nature images: systematic review and meta-analysis |
| S2-pre-b | PR-08 | La Torre et al. (2005) | The effects of imagery on anxiety and physiological responses in healthy adults |
| S2-unknown-a | PR-01 | van den Berg et al. (2003) | Coping with stress: The support of nature versus urban environments |
| S2-unknown-b | PR-01 | Van den Berg et al. (2015) | Nature, threat and personality in mood and stress recovery: A cross-sectional study |

서지 오류 9건

| 계획 | 인용 | 찾은 논문 | 다른 점 |
| --- | --- | --- | --- |
| S1-daily-b | Pascoe et al. (2017) | Yoga, mindfulness-based stress reduction and stress-related physiological measures: A meta-analysis (2017) | 제목 다름: Yoga, mindfulness-based stress reduction and stress-related physiological measur |
| S1-fatigue-b | van der Vennet & Serice (2012) | Can Coloring Mandalas Reduce Anxiety? A Replication Study (2012) | 제목 다름: Can Coloring Mandalas Reduce Anxiety? A Replication Study |
| S1-unknown-a | Jha et al. (2010) | Mindfulness training modifies subsystems of attention (2007) | 연도 2007 ≠ 2010 |
| S1-unknown-a | Chamberlain et al. (2016) | Mindfulness-based interventions for the treatment of depressive rumination: Systematic review and meta-analysis (2017) | 저자 Lilisbeth Perestelo‐Pérez, Jorge Barraca Mairal, Wenceslao Peñate ≠ Chamberlain et al. |
| S2-fatigue-b+change | King (2001) | The health benefits of writing about intensely positive experiences (2003) | 연도 2003 ≠ 2001 |
| S2-pre-b | Seers & Carroll (1999) | Relaxation techniques for acute pain management: a systematic review (1998) | 제목 다름: Relaxation techniques for acute pain management: a systematic review |
| S2-unknown-b | Browning et al. (2020) | Life Course Nature Exposure and Mental Health Outcomes: A Systematic Review and Future Directions (2021) | 저자 Dongying Li, Tess Menotti, Yizhen Ding ≠ Browning et al. |
| S3-fatigue-a | Song et al. (2016) | Forest Bathing Enhances Human Natural Killer Activity and Expression of Anti-Cancer Proteins (2007) | 연도 2007 ≠ 2016 · 저자 Q. Li, K. Morimoto, A. Nakadai ≠ Song et al. |
| S3-pre-b | Berto (2005) | Exposure to restorative environments helps restore attentional capacity (2005) | 제목 다름: Exposure to restorative environments helps restore attentional capacity |

출처가 뒷받침하지 않는 주장 8건

| 계획 | 인용 | 주장 문장 | 이유 |
| --- | --- | --- | --- |
| S1-daily-a | Kaimal et al. (2016) | 건강한 성인 연구에서도 색칠하기 같은 구조화된 미술 활동이 상태불안과 긴장을 낮추고 이완을 높이는 경향이 보고되었습니다(Kaimal et al. (2016)). | 출처는 미술 활동 후 코르티솔 감소와 ‘편안했다’는 반응을 보고하지만, 상태불안과 긴장이 낮아졌다는 지표는 제시하지 않습니다. |
| S1-fatigue-a | Holzel et al. (2011) | Holzel et al. (2011)은 마음챙김 훈련 후 스트레스 관련 주관적 지표와 주의 조절이 개선되었음을 보고했고, Gu et al. (2015)은 반추와 관련된 지표가 낮아지는 경향을 확인했습니다. | 출처는 8주 MBSR 후 회백질 농도 변화와 관련 뇌 영역을 보고할 뿐, 스트레스 관련 주관적 지표나 주의 조절 개선을 보고하지 않습니다. |
| S1-post-a | Curry & Kasser (2005) | 또한 색칠 활동은 상태 불안과 스트레스, 그리고 부정적 정서를 낮추는 데 도움이 될 수 있다는 결과가 보고되었습니다(Mills & Dyer (2019); Curry & Kasser (2005)). | 출처는 구조화된 색칠 후 불안 감소를 보고하지만 스트레스와 부정적 정서 감소 결과는 제시하지 않습니다. |
| S1-unknown-a | Jha et al. (2010) | 연구에서도 마음챙김 훈련은 반추를 줄이는 데 도움이 되었고, Jha et al. (2010)은 주의 조절과 반추, Chamberlain et al. (2016)은 반추 수준의 개선을 확인했습니다. | 출처는 마음챙김 훈련 후 주의 하위 기능 변화만 보고하며 반추 감소나 반추 수준 개선은 다루지 않습니다. |
| S2-unknown-b | Browning et al. (2020) | 연구에서도 자연 이미지를 본 뒤 기분과 스트레스 관련 지표가 개선되는 경향이 보고되었고, Van den Berg et al. (2015)은 정서적 회복과 주관적 스트레스, Browning et al. (2020)은 기분과 스트레스 지표의 개선을 확인했습니다. | 출처는 초기 생애 자연 노출과 이후 정신건강의 관련성을 검토한 내용으로, 자연 이미지를 본 뒤 기분이나 스트레스 지표가 개선되었다는 결과를 확인하지 않습니다. |
| S3-fatigue-a | Song et al. (2016) | 자연 노출 후 주의력과 정서가 더 좋아졌다고 보고한 Berman et al. (2008)과, 산림 산책 후 기분과 긴장, 피로가 개선되었다고 본 Song et al. (2016)이 근거입니다. | 출처는 산림욕 여행 후 NK 활성과 면역 관련 지표 증가를 보고할 뿐, 기분·긴장·피로 개선을 보고하지 않습니다. |
| S3-pre-a | Barton & Pretty (2010) | Kaplan (1995)은 자연 환경이 주의 회복에 도움이 된다고 보고했고, Barton & Pretty (2010)은 걷기 후 기분, 자존감, 활력의 개선을 확인했습니다. | 출처는 녹색 운동 후 자존감과 기분 개선을 보고하지만, 활력 개선을 확인했다는 내용은 출처에 제시되지 않습니다. |
| S2-fatigue-a | Manzoni et al. (2008) | Jacobson (1938)은 근육 이완 훈련에서 긴장 감소가 확인되었고, Manzoni et al. (2008)은 불안과 스트레스 지표의 개선을 보고했습니다. | 출처는 이완 훈련이 불안을 줄였다고 보고하지만, 스트레스 지표 개선은 제시하지 않습니다. |

연구 조건이 사용자 상태와 어긋난 인용 7건

| 계획 | 인용 | 이유 |
| --- | --- | --- |
| S1-pre-a | Hofmann et al. (2010) | 출처는 임상 표본에서의 마음챙김 기반 치료 효과를 다룬 것으로, 면접을 앞둔 비임상적 스트레스 상황과는 연구 상황이 다릅니다. |
| S1-pre-b | Curry & Kasser (2005) | 출처 연구는 불안 유발 뒤에 컬러링을 하며 불안 감소를 본 상황이라, 발표 같은 스트레스 상황을 앞둔 사용자 상태와는 시점이 다릅니다. |
| S1-unknown-a | Chamberlain et al. (2016) | 출처의 연구 대상은 우울증 환자의 우울 반추로, 사용자 상태가 임상 환자로 제시되지 않은 상황과는 다릅니다. |
| S2-fatigue-b | Jacobson (1938) | 출처는 이완을 치료적 조치로 다루고 유기적·기능적 상태의 치료 효능을 말하지만, 사용자는 야근 후 피로·긴장 상태입니다. |
| S2-unknown-b | Browning et al. (2020) | 출처의 연구 상황은 자연 사진 감상이 아니라 초기 생애 자연 노출과 이후 정신건강의 관련성입니다. |
| S3-daily-b | Bratman et al. (2015) | 출처는 90분 자연 속 산책 후 반추와 sgPFC 활동 감소를 본 연구로, 사용자의 20분 주의·피로 회복 목적 및 자연환경 알아차리기 활동과는 연구 상황이 다릅니다. |
| S3-fatigue-a | Song et al. (2016) | 출처의 연구는 3일 2박 산림욕 여행과 장시간 산림 보행 및 면역 지표를 다룬 상황이라, 20분 야외 자연 보행으로 주의·피로 회복을 하려는 사용자 상황과 분명히 다릅니다. |

출처 없는 연구 주장 문장 3건

- S1-fatigue-a: 연구에서도 마음챙김 훈련은 반추를 줄이고 정서 조절을 돕는 데 관련이 있었고, 고요한 상태에서 현재 순간에 주의를 두는 능력이 개선되는 것으로 보고되었습니다.
- S3-post-a: 연구에서도 자연음을 들은 뒤 스트레스와 긴장이 낮아지고 주관적 회복감이 좋아졌다는 결과가 보고되었습니다.
- S3-unknown-b: 연구에서도 자연 이미지를 보는 것이 스트레스와 긴장을 완화하고 주관적 회복감을 높이는 데 도움이 되는 경향이 보고되었습니다.


### C. LLM + 웹 검색

서지 오류 9건

| 계획 | 인용 | 찾은 논문 | 다른 점 |
| --- | --- | --- | --- |
| S1-post-a | Fung et al. (2022) | Brief mindful coloring for stress reduction in nurses working in a Hong Kong hospital during COVID-19 pandemic: A randomized controlled trial (2022) | 저자 Janet Shuk Yan Fong, Anna N. N. Hui, Ka Man Ho ≠ Fung et al. |
| S1-pre-a | Michalak et al. (2022) | The effectiveness of mindfulness-based interventions for ruminative thinking: A systematic review and meta-analysis of randomized controlled trials (2022) | 저자 Lingyun Mao, Pan Li, Yunhong Wu ≠ Michalak et al. |
| S1-unknown-b | Guo et al. (2022) | The effectiveness of mindfulness-based interventions for ruminative thinking: A systematic review and meta-analysis of randomized controlled trials (2022) | 저자 Lingyun Mao, Pan Li, Yunhong Wu ≠ Guo et al. |
| S2-daily-a | Sidhu et al. (2018) | The physical and psychological health benefits of positive emotional writing: Investigating the moderating role of Type D (distressed) personality (2018) | 저자 Michael Smith, Alexandra Thompson, Lynsey J. Hall ≠ Sidhu et al. |
| S2-fatigue-b+change | Osama et al. (2021) | Written benefit finding for improving psychological health during the Covid-19 pandemic first wave lockdown (2021) | 저자 Sarah R. Hansen, Mark A Wetherell, Michael Smith ≠ Osama et al. |
| S2-post-b | Huang et al. (2024) | Default mode network connectivity contributes the augment effect stress recovery by natural viewing (2025) | 저자 Zini Chen, Chanyu Wang, Timothea Toulopoulou ≠ Huang et al. |
| S2-unknown-b | Lee et al. (2020) | Physiological Benefits of Viewing Nature: A Systematic Review of Indoor Experiments (2019) | 저자 Hyunju Jo, Chorong Song, Yoshifumi Miyazaki ≠ Lee et al. |
| S3-post-a | Weinzimmer et al. (2019) | Of cricket chirps and car horns: The effect of nature sounds on cognitive performance (2018) | 저자 Stephen Charles Van Hedger, Howard C. Nusbaum, Luke Clohisy ≠ Weinzimmer et al. |
| S3-post-b | Berman et al. (2014) | Not Just Scenery: Viewing Nature Pictures Improves Executive Attention in Older Adults (2014) | 저자 Katherine R. Gamble, James H. Howard, Darlene V. Howard ≠ Berman et al. |

출처가 뒷받침하지 않는 주장 5건

| 계획 | 인용 | 주장 문장 | 이유 |
| --- | --- | --- | --- |
| S2-daily-a | Smith et al. (2019) | 건강한 성인과 유사한 일반 성인 대상 연구에서 positive emotional writing은 상태 불안, 특성 불안, 지각된 스트레스의 감소와 연결되었고(Sidhu et al. (2018)), 또 다른 연구에서도 긍정적 경험을 쓰는 과제가 상태 불안의 감소와 연결되었습니다(Smith et al. (2019)). | 출처는 긍정 정서 글쓰기 후 우울과 지각된 스트레스 반응성 감소를 보고하지만, 상태 불안 감소를 결과로 제시하지 않습니다. |
| S2-pre-b | Toussaint et al. (2021) | 건강한 성인 대상 연구에서도 유도 심상은 스트레스와 불안, 이완 상태 지표의 개선이 확인되었지만, 개인별 차이가 있어 임상적 효과로 단정하지는 않습니다(Toussaint et al. (2021)). | 출처는 유도 심상이 이완 상태와 일부 생리적 이완 지표를 개선했다고 말하지만, 스트레스와 불안 개선 결과는 제시하지 않아 주장이 결과를 과장했습니다. |
| S3-fatigue-a | Trammell et al. (2024) | 자연 속 보행 뒤 자기보고된 회복감과 지속적 주의가 좋아졌고, 야외 걷기/자연 노출에서 주의나 기분이 개선되었다는 결과가 보고되었습니다(Trammell et al. (2024); Wong et al. (2025)). | 출처는 자연 환경 보행 뒤 긍정 정서 증가와 부정 정서 감소는 보고하지만, 인지 측정치에는 장소 효과가 없었다고 하므로 지속적 주의가 좋아졌다는 주장은 어긋납니다. |
| S3-unknown-b | Kimura et al. (2021) | 연구에서는 자연 사진을 본 뒤 집행 주의가 개선되었고, 자연 장면 노출 후 주의 회복과 함께 피로 관련 지표가 나아졌습니다(Gamble et al. (2014), Kimura et al. (2021)). | 출처는 자연 영상 휴식이 주관적 정신작업부하와 과제 수행을 개선하지 않았고 SCL만 감소했다고 하므로 피로 관련 지표 개선 주장은 어긋납니다. |
| S2-fatigue-a | Toussaint et al. (2021) | 연구에서도 PMR은 성인에서 스트레스와 불안을 낮추는 경향이 확인되었고, Toussaint et al. (2021)은 건강한 성인에서 이완 상태와 불안 지표 개선을, Khir et al. (2024)은 성인 대상 스트레스와 불안 개선을 보고했습니다. | 출처는 PMR이 이완 상태를 높였다고 보고하지만 불안 지표 개선을 측정·보고했다는 내용은 확인되지 않습니다. |

연구 조건이 사용자 상태와 어긋난 인용 14건

| 계획 | 인용 | 이유 |
| --- | --- | --- |
| S1-daily-b | Golding et al. (2018) | 출처의 자연 이미지 노출은 발표 과제로 반추와 기분을 유도한 뒤에 시행된 연구인데, 사용자 상태는 특별한 유발 과제가 없는 상황입니다. |
| S1-pre-b | Palmer-Cooper et al. (2024) | 출처는 발표나 시험 직전 같은 스트레스 상황을 앞둔 3–5분 활동이 아니라, 대학생이 2주 동안 10분 이상 색칠을 반복한 개입을 다룹니다. |
| S1-unknown-a+change | Curry & Kasser (2005) | 출처 연구는 불안 유발 뒤의 불안 감소를 다루지만, 사용자 목표는 반복되는 생각에서 벗어나기인 반추 완화입니다. |
| S1-unknown-a+change | Støre & Jakobsson (2022) | 출처 연구는 성인의 상태불안 감소를 다루지만, 사용자 목표는 반복되는 생각에서 벗어나기인 반추 완화입니다. |
| S2-daily-b | Riedl et al. (2026) | 출처의 연구는 급성 위협 상황에서 호흡을 실시한 경우를 다루지만, 사용자 상태는 특별한 계기가 없는 상황입니다. |
| S2-fatigue-b+change | Crawford et al. (2019) | 출처 연구는 당뇨병이 있고 당뇨 관련 distress가 있는 성인을 대상으로 한 임상적 표본이라, 야근 뒤 피로와 긴장을 호소한 일반 사용자 상황과는 다릅니다. |
| S2-post-b | Brown et al. (2013) | 출처에서는 자연 장면을 스트레스 과제 전에 보게 한 뒤 회복을 측정했지만, 사용자는 이미 통화 스트레스를 겪은 뒤 자연 사진을 보는 상황입니다. |
| S2-pre-a | Lorenz et al. (2022) | 출처 연구는 발표 직전 같은 스트레스 상황을 앞둔 5분 활동이 아니라, 대학생들이 4주간 복합적인 6분 저널을 작성한 상황입니다. |
| S2-pre-a | O’Connell et al. (2017) | 출처 연구는 발표 직전 긴장 완화 상황이 아니라, 대인 감사 저널을 장기간 작성한 상황입니다. |
| S2-unknown-b+change | Pehlivan & Dalli (2026) | 출처는 성인 입원 환자를 대상으로 한 만다라 컬러링 연구라서, 임상 환자 표본이 아닌 사용자의 현재 상태와는 분명히 다릅니다. |
| S2-unknown-b+change | Kandemir et al. (2026) | 출처는 관상동맥조영술을 앞둔 환자를 대상으로 한 병원 기반 연구라서, 사용자의 일반적인 긴장·스트레스 완화 상황과는 분명히 다릅니다. |
| S3-post-b | Brown et al. (2013) | 출처 연구는 자연 장면을 스트레스 과제 전에 보게 한 뒤 회복을 측정했지만, 사용자는 이미 스트레스를 겪은 뒤 활동을 하는 상황입니다. |
| S3-pre-b | Kimura et al. (2021) | 출처는 자연 사진 감상이 아니라 자연 동영상을 휴식으로 보는 연구라서 매체가 다릅니다. |
| S3-unknown-b | Kimura et al. (2021) | 출처는 자연 사진 감상이 아니라 과제 중간에 자연 영상을 보는 휴식 상황을 다룹니다. |

웹 출처 도메인

- pubmed.ncbi.nlm.nih.gov ×58 (학술)
- api.crossref.org ×2 (학술)
- ? ×1 (상업)
- pmc.ncbi.nlm.nih.gov ×1 (학술)

출처 없는 연구 주장 문장 11건

- S1-fatigue-b: 자기주도 색칠은 대학생과 성인에서 상태 불안과 스트레스, 마음챙김 지표의 개선과 함께 보고되었고, 마음챙김 기반 중재의 반추 완화 가능성도 메타분석에서 확인되었습니다. ([pubmed.ncbi.nlm.nih.gov](https://pubmed.ncbi.nlm.nih.gov/39450667/?utm_source=openai))
- S1-post-b: 연구에서는 mindfulness-based interventions가 반복적 반추를 줄이고 mindfulness를 높였고, brief mindfulness training도 rumination을 포함한 부정정서 지표를 줄이는 경향이 확인됐습니다. ([pubmed.ncbi.nlm.nih.gov](https://pubmed.ncbi.nlm.nih.gov/36302490/?utm_source=openai))
- S1-pre-b: 자기주도 색칠은 시험 불안이나 상태 불안, 지각된 스트레스가 줄고 상태 마음챙김이 높아진 연구들이 있어, 지금처럼 스트레스를 앞둔 상황에 맞는 선택입니다.
- S2-fatigue-b: 연구에서는 성인 대상 PMR이 스트레스와 불안, 이완 상태 개선과 연관되었고, 건강한 성인에게 20분 PMR 후 심리적·생리적 이완이 증가했습니다.
- S2-post-a: 연구에서도 만다라 컬러링은 건강한 성인과 유사한 맥락에서 불안·스트레스 완화와 마음챙김 향상, 스트레스 지표 감소가 확인되었지만, 임상 효과로 일반화하기에는 근거의 범위가 제한적입니다.
- S2-pre-a: 이 활동은 5분 안에 혼자 할 수 있어 발표 직전 부담이 크지 않고, 긍정 정서와 스트레스 관련 지표를 함께 살펴본 연구에서도 개선이 관찰되었습니다.
- S2-unknown-a: 특히 실내 실험들에서 자연 이미지를 보는 조건이 대조 조건보다 더 이완된 신체 반응과 덜한 긴장감을 보였고, 자연 사진을 본 뒤 상태 불안과 혈압이 더 낮아졌다는 결과가 보고되었습니다.
- S2-unknown-b: 자연 사진 감상은 짧은 시간에도 마음의 회복감과 편안함을 높이는 쪽으로 연구되어, 현재처럼 20분 안에 할 수 있는 실내 활동으로 적절합니다.
- S3-daily-a: 짧고 부담이 적으면서도 주의 자원을 쉬게 해 집중 회복을 돕는 방향과 맞고, 건강한 성인 연구에서 자연 사진이나 자연 장면 노출 뒤 실행 주의와 작업 기억, 일부 주의 지표의 개선이 확인되었습니다.
- S3-daily-b: 관련 연구에서 자연 속에서의 짧은 주의적 참여가 주의 회복과 스트레스·정서 지표의 개선과 연결되었고, 20분 야외 자연 경험은 주의 회복 측면에서 비교적 유리한 결과를 보였습니다.
- S3-pre-a: 자연 환경에서의 보행은 주의 회복과 정서적 회복을 돕는 것으로 보고되었고, 특히 지친 상태에서 인지적 부담을 덜어주는 방향으로 관찰되었습니다.


