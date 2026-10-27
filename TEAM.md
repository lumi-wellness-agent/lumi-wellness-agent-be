# 역할별 개발 가이드 (발표 2026-10-30, 코드 마감 10-29 목)

경로는 저장소 루트 기준입니다. 에이전트 아키텍처 v2(`서비스아키텍처/에이전트 아키텍처 v2.png`)를 LangGraph
그래프 하나로 옮긴 뼈대가 `main`에 있습니다. 노드·state·프로토콜 로더·측정 도구 계약은 다 연결돼 있고,
각 담당은 **자기 파일의 본문**(LLM 호출 · 척도 문항 · 활동 노드 JSON)을 채웁니다. 실행·검증 환경과
브랜치·PR 규칙은 [README](README.md)에 있습니다.

## 원칙

- **라운드당 활동 노드 1개.** 플래너는 검수된 프로토콜(`data/protocols/*.json`)에서 고를 뿐 조합하지 않는다.
  조합은 `VALIDATED_MULTIMODAL` 프로토콜 자체로만 제공한다(PR-02 · 10 · 12).
- **LLM은 두 곳뿐이다.** intake(구조화 출력 2회 — 상황 읽기 · 목표 추론, 글 답이면 읽기 1회 더)와 `plan`(구조화 출력
  2회 — 활동 선택 · 추천 이유). intake의 질문 여부·보기 검증·답 병합과 plan의 후보·검색·검증, 측정·평가·재계획은 코드다.
- **측정은 RAG가 아니다.** 척도는 `data/instruments/*.json`에 있고 LLM은 문항을 만들거나 바꾸거나 점수를
  계산하지 않는다. LLM 번역 금지 — 번역·타당화된 문항만 쓴다.
- **근거는 막지 않는다.** 검증은 출처 실재 · 중재 관련 · 지표 측정 세 가지만 차단하고, AI 생성·조합 콘텐츠는
  `ADAPTED_STIMULUS`로 Reference 카드에 밝힌다(`app/schemas/retrieval.py`).
- **발표 범위에서 뺀 것.** Docling·LLM 원문 추출
  파이프라인(근거 arm 시트 65행을 적재), 콘텐츠 생성(이미지·음성), Protocol DB 테이블(JSON 파일 로더).

## 구조

```text
POST /sessions …(API는 통합 담당이 붙인다)
  ↓
intake          ① 상황 읽기(LLM) → State (시점이 불명확하면 질문 ⏸, 최대 2회)
  ↓             ③ 목표 추론(LLM, ② 개념 정의 원문 + 단서표) → Goal S1·S2·S3 + 정의 근거
  ↓             ④ 추천을 바꾸는 환경 칸만 칩 질문 ⏸ → 기본값 · 환경 조건 (질문 합계 3회)
measure_pre ⏸   SAM(앵커) + 목표 척도(S2 STAI-6 · S1 BSRI · S3 MFVAS 임시)
  ↓
plan            후보(코드) → 활동 선택(LLM) → 근거 검색·검증(코드) → 추천 이유(LLM) → CarePlan
  ↓             check_plan 후검증(검수 ID · 목표 · 순서 조건 · 시간 · 활동 변경 시 같은 프로토콜 금지), 어기면 1회 재질의
prepare         프로토콜의 steps · guide → ActivityContent
run_session ⏸   FE 플레이어 실행 → SessionReport(completed · stopped_reason · notice_word)
measure_post ⏸  SAM (+ S3는 MFVAS — 라운드 척도)
evaluate        Δ = 사후 − 사전 (S1 쾌↑ · S2 각성↓ · S3 피로↓)
replan          종료 · 활동 변경 · 추가 세션 제안 + 선택지 (라운드 ≤ 3, 바꿀 활동이 없으면 변경 제외)
confirm ⏸       사용자 선택. 동의하면 plan으로(사전값 = 직전 사후값)
measure_final ⏸ 종료 경로에서 STAI-6 · BSRI 1회 (라운드마다 잰 S3는 건너뜀)
```

`app/agent/state.py`가 노드 사이 계약이고, `app/agent/graph.py`만 노드를 배선한다. ⏸는 interrupt —
payload·resume 모양은 각 노드 docstring에 있다. `make graph`가 Mermaid를 출력한다.

## 담당 파일과 할 일

### 공통 (통합 담당 · 구준휘)

| 상태 | 파일 | 내용 |
| --- | --- | --- |
| 완료 | `app/agent/state.py` `graph.py` `checkpoint.py` | v2 state · 배선 · 직렬화 허용 타입 |
| 완료 | `app/agent/nodes/intake.py` `measure.py` `executor.py` `evaluator.py` `replanner.py` | 노드 뼈대. intake·measure는 담당이 본문을 채운다 |
| 완료 | `app/schemas/protocol.py` `app/protocols.py` | 활동 노드 본문 스키마 · JSON 로더 · 후보 조회(`candidates` 등급 순 · 환경 필터, `plan_candidates` 완화 순서) · LLM용 압축본(`compact`) |
| 완료 | `app/instruments.py` `data/instruments/` | 측정 도구 계약 · 채점. SAM은 READY, STAI-6·BSRI는 TODO, S3용 MFVAS는 PROVISIONAL(팀 작성·미검증) |
| 완료 | `scripts/scaffold_protocols.py` `data/protocols/PR-02…22.json` | 워크북에서 만든 활동 노드 초안 20개(PR-01은 손으로 쓴 기준) |
| 완료 | `app/agent/assembly.py` `app/agent/llm.py` `app/config.py` `.env.example` | 모델 조립 — gpt-4o-mini 하나(`OPENAI_API_KEY` · `LLM_MODEL`), 폴백 claude-haiku-5-5(`ANTHROPIC_API_KEY`). 5xx 재시도 1회 · 4xx 즉시 폴백 |
| 완료 | `supabase/migrations/20261026000000_evidence_contracts.sql` `scripts/seed_evidence.py` `app/retrieval/hybrid.py` `app/agent/validation.py` | 근거 122건(arm 61개) 적재 · 하이브리드 검색 · 검증. 재적재는 `make seed-evidence`(id 유지) |
| 완료 | `app/agent/nodes/planner.py` `app/agent/prompts.py` `app/agent/context.py` `scripts/plan_trial.py` | plan을 실제 모델로 — 고정 절차(후보 → 선택 LLM → 검색 → 검증 → 설명 LLM). S1 · S2 · S3 시나리오 통과(`make plan-trial`) |
| 수 | `app/agent/reference_card.py` | Reference 카드 — 계획의 근거 ID · 검증 플래그(ADAPTED_STIMULUS · CONFLICTING_EVIDENCE) · 프로토콜 composition(EXPERIMENTAL)에서 만든다. rationale 문장에 기대지 않는다 |
| 수 | `app/api/sessions.py` | 세션 API — `sessions` · `answer` · `measure` · `report` · `decision` · `retry` · `GET`, SSE · JSON, CORS(README 「세션 API」)는 됐다. 남은 것: 인증(Supabase Auth — Google · Kakao) · 세션 기록 + FE |
| 목 | `data/protocols/*.json` `evidence[].evidence_record_ids` | 역기입 · 통합 · 리허설 |

### 활동 노드 담당 (JSON 21개 — 셋이 7개씩, 월·화)

| 파일 | 할 일 |
| --- | --- |
| `data/protocols/PR-*.json` | 초안을 완성한다. `review.open_items`에 적힌 확인 항목부터. 시나리오가 지나가는 PR-06 · PR-01 · PR-02 · PR-09 · PR-21 · PR-11을 먼저 |
| 같은 파일 `steps` · `content_spec.slots` · `guide.intro_text` | 단계 문안, 슬롯별 `selection` · `render` · `personalization` · `generation`(PR-01.json이 완성 예시), 시작 안내 문안 |
| 같은 파일 `grades` · `evidence` | 등급 셀에서 푼 `support`/`conflicts`가 맞는지, `outcomes`에 더해 둔 지표가 실제 측정된 것인지 원문으로 확인 |
| 합격 기준 | `make test-protocols` 통과. 스키마를 바꿔야 하면 통합 담당과 먼저 맞춘다 |

묶음 제안: A = PR-01 02 03 04 05 20 21(자연 자극 감상) · B = PR-06 07 08 09 10 17 22(호흡·이완·명상·그라운딩) ·
C = PR-11 12 14 15 16 18 19(보행·컬러링·글쓰기·NOTICE). 같은 논문을 한 사람이 읽도록 매체로 묶었다.

### measure 담당 (먼저 끝난 사람, 화)

| 파일 | 할 일 |
| --- | --- |
| `data/instruments/INS-STAI6.json` `INS-BSRI.json` | 문항 원문 `text`와 `instruction`을 채우고 `status`를 `READY`로. STAI는 Mind Garden 허가·한국어판, BSRI는 한국어판 출처 확인. **LLM 번역 금지** |
| `app/instruments.py` | 채점은 되어 있다(STAI-6 역채점 후 합 × 20/6, BSRI 평균, SAM 문항별). 제시 형식(`present`)에 FE가 필요한 값을 더한다 |
| `app/agent/nodes/measure.py` | interrupt payload·resume 모양을 FE와 맞춘다. 사전은 SAM + 목표 척도, 라운드 사이는 SAM(S3는 + MFVAS), 종료는 STAI-6 · BSRI만 |
| `tests/measure/` | `make test-measure` |

### intake 담당 (먼저 끝난 사람, 화)

| 파일 | 할 일 |
| --- | --- |
| `app/agent/prompts.py` `READ_SYSTEM_PROMPT` `GOAL_SYSTEM_PROMPT` | 상황 읽기 · 목표 추론 규칙. 점수 추정 금지, 사건을 자세히 묻지 않는다 |
| `app/agent/intake_rules.py` | 질문 여부(State 2회 · 합계 3회) · 보기 검증 · 답 병합(요청 칩 > 칩 답 > 문장) · 환경 질문 판정 · 목표 · 기본값. `StateReading` · `GoalInference` 필드 설명이 LLM이 읽는 설명이다 |
| `data/intake_cues.json` | 단서표(표현 단서 · 경계 · 요약 출처)와 상태 단서. ③의 판단 기준(팀 요약) |
| `data/goal_definitions.json` | ② 개념 정의 — 목표마다 R1 논문 원문 문장(paper_id · 쪽). ③ 입력에 통째로 넣고, 추론한 목표의 정의가 `goal_evidence`가 된다. 근거 담당이 원문과 대조해 고친다 |
| `app/schemas/intake.py` | FE와 주고받는 질문·답 JSON. 키를 바꾸면 FE와 먼저 맞춘다 |
| `tests/intake/` | `make test-intake`. 실제 호출 결과는 `tests/intake/`에 녹화본(JSON)으로 남긴다 |

### plan (통합 담당 겸임)

`app/agent/nodes/planner.py` · `prompts.py` · `context.py`. 고정 절차(후보 → 선택 LLM → 검색 → 검증 → 설명 LLM)이고
LLM 호출은 구조화 출력 2회다. 테스트(`tests/agent/`)는 fixture(`tests/fixtures/evidence_hits.json`)로 검색·검증을
흉내 내고 Planner 대역(FakeStructuredModel · RecordingPlanner)으로 돈다. 실제 모델은 `make plan-trial ARGS="S1 S2 S3"`로
돌린다 — gpt-4o-mini 실측(10/27): S1 → PR-09, S2 → PR-06, S3 → PR-01, 계획당 2.5~3.5초 · 모델 호출 2회 · 토큰 4~6천.
프롬프트를 고치면 세 시나리오를 다시 돌려 본다.

## 계약

| 항목 | 정의 | 위치 |
| --- | --- | --- |
| `CareRequest` | `text` + 환경 칩(`setting` `earphones` `minutes_available`) + `profile`. `Place`는 코드가 setting에서 정한다 | `app/schemas/care.py` |
| `IntakeResult` | Goal(goal · secondary_goal · goal_source · target_outcomes · goal_evidence[paper_id, sentence]) · State(order_condition · 사건 · 반복 단서) · Context(setting · place · earphones · minutes · environment) · end_reason | `app/agent/state.py` |
| intake 질문 interrupt | `IntakeQuestion{type, question_id, slot, turn, max_turns, text, groups[{key, select, options[{label, set}]}], allow_free_text}` ↔ `IntakeAnswer{question_id, answer \| text}` | `app/schemas/intake.py` |
| `Measurement` · `GOAL_SCALE` | 도구 × 시점 × 라운드, `answers` · `scores` | `app/schemas/measure.py` |
| 측정 interrupt | `{"measure": "PRE", "round": 0, "instruments": [...]}` ↔ `{"INS-SAM": {"valence": 3, "arousal": 7}, …}` | `app/agent/nodes/measure.py` |
| `CarePlan` | `activity{protocol_id, minutes, evidence_record_ids}` + `rationale` | `app/agent/state.py` |
| `CareProtocol` | 활동 노드 JSON 전체. 코드가 읽는 필드만 형 고정, 나머지 블록은 자유 | `app/schemas/protocol.py` |
| 후보 프로토콜 | `protocols.plan_candidates()` → `compact()` 목록. 목표 · 상태 · 시간 · 환경으로 거르고(없으면 상태 → 환경 순으로 푼다) 주 목표 · 목표 1차 지표 등급 순, 단계 모듈과 직전에 바꾸기로 한 활동 제외 | `app/protocols.py` |
| `EvidenceHit` · `EvidenceVerdict` | search · validate 도구 출력 | `app/schemas/retrieval.py` |
| 세션 종료 | `final`(목표 척도). intake는 ① 상황 읽기(LLM)가 처음 입력이나 글 답에서 안전 신호를 볼 때만 닫는다(`ended = SAFETY`). 목표 단서가 없거나 되묻기에서 '그냥 시작할게요'면 S2(`goal_source = DEFAULT`) | `app/agent/state.py` |

## 작업 순서 (10/26 월 ~ 10/29 목)

1. **월** 이 뼈대를 머지한다. 전원 `.venv`를 새로 만들고(`pip install -e ".[dev]"`) `make test`가 통과하는지,
   `make graph`가 그래프를 내는지 확인한다. 활동 노드 담당은 자기 묶음 JSON을 시작한다.
2. **화** 통합: 근거 적재·검색·검증. 먼저 끝난 두 사람이 measure · intake를 맡는다(각 하루).
3. **수** 통합: plan 실제 모델 · Reference 카드 · 세션 API. 나머지는 JSON 합류.
4. **목** 통합 실행 · 시나리오 반복("발표 앞두고 긴장돼요" → S2 → SAM·STAI-6 → PR-06 → 세션 → SAM Δ → replan) ·
   `evidence_record_ids` 역기입 · 리허설. **수요일 저녁까지 안 오면 대체:** intake는 폼 값으로 IntakeResult를 채우고,
   measure는 SAM만 받는다.

## PR 규칙과 테스트 명령

- 이슈 → `<타입>/#<번호>` 브랜치 → `main` PR(squash). 자세한 규칙과 `/ship` 하네스는 README.
- 자기 파일과 자기 테스트 디렉터리만 고친다. `state.py` · `graph.py` · `schemas/*`의 계약을 바꿔야 하면 통합 담당과 먼저 맞춘다.
- `.env`와 키는 커밋하지 않는다. `OPENAI_API_KEY`(와 폴백용 `ANTHROPIC_API_KEY`)는 통합 담당이 나눠 준다. 모델은 gpt-4o-mini 하나, 폴백은 claude-haiku-5-5다.

```bash
make test-intake ARGS="-q"      # intake
make test-measure ARGS="-q"     # 측정 도구 · 측정 노드
make test-protocols ARGS="-q"   # 활동 노드 JSON 21개 + 근거 계약
make test-planner ARGS="-q"     # 그래프 · 플래너 (대본 모델, LLM 없이)
make test ARGS="-q"
make lint
make graph                      # Mermaid 그래프 출력
make protocols-scaffold XLSX=../Research_paper/LUMI_R_C_Research_2026-10-22/LUMI_측정도구_프로토콜_2026-10-23.xlsx
```

대본 모델로 그래프를 테스트하는 방법은 `tests/agent/test_graph.py`(FakeIntaker · FakePlanner · interrupt 재개 대본)와
`tests/agent/fakes.py`(FakeStructuredModel · fixture 기반 가짜 검색·검증)를 참고한다.
