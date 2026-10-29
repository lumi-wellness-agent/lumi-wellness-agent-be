# lumi-wellness-agent-be

LUMI(Light + Mind)의 백엔드입니다. LUMI는 심리학 이론·임상/실험 연구 Reference 기반 AI 웰니스 에이전트로,
사용자 상태에 맞는 자연 이미지·소리 회복 세션을 추천하고 추천 근거를 Reference 카드로 보여 줍니다.
FastAPI, Supabase Postgres(pgvector), LangGraph·LangChain을 사용합니다. 프론트엔드는
[lumi-wellness-agent-fe](https://github.com/lumi-wellness-agent/lumi-wellness-agent-fe)에서 개발합니다.

현재는 에이전트 아키텍처 v2의 뼈대 단계입니다. 그래프 배선(intake → 사전 측정 → 계획 → 실행 대기 →
사후 측정 → 평가 → 재계획 루프 → 종료 측정), 노드 사이 계약, 활동 노드 JSON 21개(담당 셋이 채움, 검수 전)와 측정 도구 4개의
계약·채점, 근거 계약(스키마·도구 출력·검증 규칙)은 테스트로 확인했습니다. 근거 122건(arm 61개)은 Supabase에
적재해 하이브리드 검색·검증이 돌고, plan은 실제 모델(gpt-5.4-mini)로 세 시나리오(S1 · S2 · S3)를
통과했습니다(`make plan-trial`). intake는 State(대화) → Goal(단서표 추론) → Context(환경 칩)
질문 루프로 바꿨고(#49), 대역 모델로 흐름을 테스트했습니다. 목표 추론은 R1 논문의 원문 개념 정의(②)를
함께 보고, 추론한 목표의 정의를 근거로 남깁니다. 세션 API는 Supabase 로그인 · 게스트 체험 · 세션 기록과 함께
운영에 올라가 있고, plan이 계획마다 Reference 카드를 만듭니다. 콘텐츠 생성은 아직입니다. 누가 무엇을 채우는지는
[TEAM.md](TEAM.md)에 있습니다.

## 구조

### Offline: 연구 근거 구축 (`app/ingestion/`)

```text
collect.py   논문 수집 — OpenAlex · Europe PMC
parse.py     원문 파싱 — Docling(PDF) · JATS XML
extract.py   실험 단위 Evidence Record + 메타데이터 → evidence_records 테이블
```

### Online: LUMI Care Agent (`app/agent/`)

에이전트 아키텍처 v2를 LangGraph 그래프 하나로 옮겼습니다. `make graph`가 아래 다이어그램을 출력합니다.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	intake_read(intake_read)
	intake_ask(intake_ask)
	intake_goal(intake_goal)
	intake_context(intake_context)
	intake_finish(intake_finish)
	measure_pre(measure_pre)
	plan(plan)
	prepare(prepare)
	run_session(run_session)
	measure_post(measure_post)
	evaluate(evaluate)
	replan(replan)
	confirm(confirm)
	measure_final(measure_final)
	__end__([<p>__end__</p>]):::last
	__start__ --> intake_read;
	intake_read -.-> intake_ask;
	intake_read -.-> intake_goal;
	intake_read -.-> __end__;
	intake_ask -.-> intake_read;
	intake_ask -.-> intake_goal;
	intake_ask -.-> intake_finish;
	intake_goal --> intake_context;
	intake_context -.-> intake_ask;
	intake_context -.-> intake_finish;
	intake_finish --> measure_pre;
	confirm -.-> __end__;
	confirm -.-> measure_final;
	confirm -.-> plan;
	evaluate --> replan;
	measure_post --> evaluate;
	measure_pre --> plan;
	plan --> prepare;
	prepare --> run_session;
	replan --> confirm;
	run_session --> measure_post;
	measure_final --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

| 노드 | 아키텍처 | 하는 일 | 파일 |
| --- | --- | --- | --- |
| `intake_read` | intake ① State (LLM) | 문장(과 앞선 질문·답)에서 안전 신호 · 관련성 · 사용자 상태(사건 전·후 · 피로 후 · 계기 없음) · 사건 · 반복 사고 단서 · 문장에 나온 여건을 읽고, 질문 초안을 낸다. 안전 신호면 세션 종료(`ended = SAFETY`). 처음 입력이 관계없으면 사용자 말에 맞춘 입구 되묻기(`restate`)를 낸다. 점수는 추정하지 않는다 | `nodes/intake.py` |
| `intake_goal` | intake ③ Goal (LLM) | 개념 정의(`data/goal_definitions.json`, 원문) · 단서표(`data/intake_cues.json`, 팀 요약) · 상황 단서로 개선할 결과 지표 S1·S2·S3와 부목표, 개선할 지표(`target_outcomes`)를 정한다. 추론한 목표 · 부목표의 정의를 `goal_evidence`(paper_id · 원문 문장)로 남긴다(② — 열 개 남짓이라 검색하지 않고 통째로 넣는다). State 두 질문이 모두 '잘 모르겠어요'거나, 되묻기에서 '그냥 시작할게요'를 눌렀거나, 단서가 없으면 S2(`goal_source = DEFAULT`, 근거 없음) | `nodes/intake.py` |
| `intake_context` | intake ④ Context (코드) | setting · earphones · minutes 중 비어 있고 1순위 활동을 바꾸는 칸만 칩으로 묻는다 | `nodes/intake.py` |
| `intake_ask` ⏸ | intake 질문 | 입구 되묻기 · 시점 질문 · 재질문 · 환경 칩. 합계 3회, State는 2회까지(되묻기는 세지 않고 관련 있는 답이 올 때까지 돈다). 시점 질문에 '잘 모르겠어요'면 고정 재질문. 글 답은 `intake_read`로 돌아가 안전까지 다시 읽는다. payload `IntakeQuestion`, resume `IntakeAnswer`(`app/schemas/intake.py`) | `nodes/intake.py` |
| `intake_finish` | intake (코드) | 빈 칸을 기본값으로(시간 10분 · setting 모르면 조용한 실내 가정), 환경 조건(소리 · 조용함 · 화면 · 야외) · Place · 끝난 이유(`end_reason`)를 정한다 | `nodes/intake.py` |
| `measure_pre` ⏸ | measure (코드) | SAM(앵커) + 목표 척도(S2 STAI-6 · S1 BSRI · S3 MFVAS 임시 도구)를 FE에 제시하고 응답을 채점한다 | `nodes/measure.py` |
| `plan` | ① Adaptive Care Planning | 고정 절차 — 후보(코드, 목표 · 상태 · 시간 · 환경으로 거른다) → 활동 선택(LLM) → 근거 검색·검증(코드) → 추천 이유(LLM) → `CarePlan`. 선택은 `check_plan`이 후검증한다. 검증한 근거로 Reference 카드를 만든다(코드, 「Reference 카드」) | `nodes/planner.py` |
| `prepare` | ② Care Session Executor | 프로토콜의 단계 · 안내 문구 · 실행 명세(`content_spec`)와 자산(사진 · 영상 · 음원 · 컬러링 도안)으로 `ActivityContent`를 준비한다(음성 합성은 예정) | `nodes/executor.py` |
| `run_session` ⏸ | ② Care Session Executor | FE 플레이어가 프로토콜 1개를 실행하고 `SessionReport`(완료 여부·중단 사유·NOTICE 감정 단어 1~3개)를 보낼 때까지 기다린다 | `nodes/executor.py` |
| `measure_post` ⏸ | measure (코드) | 라운드 끝 SAM. S3는 MFVAS(정신 피로 VAS 3문항)도 함께 — 라운드 척도라 종료에 다시 묻지 않는다 | `nodes/measure.py` |
| `evaluate` | ③ Outcome Evaluator | Δ = 사후 − 사전(SAM 쾌·각성, S3는 MFVAS 피로·집중·기운도). S1 쾌↑ · S2 각성↓ · S3 피로 10점 이상↓ | `nodes/evaluator.py` |
| `replan` | ④ Replanning | 종료 · 활동 변경 · 추가 세션(같은 활동 한 번 더) 중 고를 수 있는 선택지와 규칙 제안을 코드가 정하고 — 라운드 3회면 종료만, 바꿀 활동이 없으면 활동 변경을, 중간에 멈춘 활동이면 추가 세션을 뺀다 (임시 규칙) — LLM(`NextStepChoice`)이 규칙 제안을 기본으로 멈춘 이유 · 감정 단어 · Δ를 보고 선택지 안에서 권할 단계와 이유를 고른다. 실패하면 규칙 제안 | `nodes/replanner.py` |
| `confirm` ⏸ | ④ Replanning | 사용자 선택·동의(`ReplanDecision`)를 기다린다. 활동 변경·추가 세션이면 `plan`으로 돌아간다(다음 라운드의 사전값 = 이번 사후값). 활동 변경은 바꾸기로 한 활동을 세션 내내 후보에서 빼고, 추가 세션은 활동을 다시 고르지 않고 같은 활동에 콘텐츠만 새로 고른다 | `nodes/replanner.py` |
| `measure_final` ⏸ | measure (코드) | 종료 경로에서 사전·종료에만 재는 목표 척도(STAI-6 · BSRI) 1회 → 세션 Δ. S3는 라운드마다 재서 건너뛴다 | `nodes/measure.py` |

⏸는 LangGraph interrupt입니다. 그래프가 멈추고 상태를 체크포인터에 남긴 뒤, 같은 thread_id로
`Command(resume=값)`을 받으면 그 자리부터 이어서 돕니다. interrupt가 있는 노드는 재개될 때 처음부터
다시 실행되므로, 콘텐츠 생성처럼 비용이 드는 일은 앞 노드(`prepare`)에서 끝내 state에 남깁니다.

| 목표(개선할 결과 지표) | 원하는 변화 | 목표 척도 | 주 활동 노드 |
| --- | --- | --- | --- |
| S1 반추 완화 | 반복적으로 떠오르는 생각에서 잠시 벗어나기 | BSRI(한국어 번역 초안) | PR-09 마음챙김 / PR-21 이미지+명상(실험) / PR-15 컬러링(실험) |
| S2 긴장·스트레스 완화 | 스트레스와 긴장감 완화 | STAI-6(한국어 번역 초안) | PR-06 호흡 / PR-02 영상+자연음 / PR-01 자연 사진 … |
| S3 주의·피로 회복 | 인지 피로 이후 주의력 회복 | MFVAS(정신 피로 VAS, 임시·미검증 — 주관적 피로만 잰다) | PR-10 ReST(실험) / PR-11 자연 보행 / PR-01 자연 사진 |

목표는 결과 지표 라벨이고 매체 묶음이 아닙니다. 활동 노드 21개는 `data/protocols/`에 있고, 라운드마다 하나를
고릅니다. 조합은 노드 자체로만 제공하고 플래너가 임의로 묶지 않습니다 — 조합이 곧 실험 자극인 검증된 다중매체
프로토콜(PR-02 · 12)이 있고, 구성 근거가 없는 노드(PR-10 · 15 · 21 · 22)는 `EXPERIMENTAL`로 두고 '실험적'으로 표시합니다.

공통 원칙

- 논문 RAG는 근거 검색에 쓰고, 세션 실행은 검수된 프로토콜만 씁니다.
- AI 생성 이미지는 보조 콘텐츠로 쓰고, 연구 자극과의 차이는 Reference 카드에 `ADAPTED_STIMULUS`로 밝힙니다.
- 콘텐츠 Factor(풍경 · 자연음 · 작품 주제)는 LLM이 근거 있는 Enum 값만 고르고, 검색어는 코드가 검수 목록에서
  고릅니다(아래 「콘텐츠 Factor」).
- 측정은 RAG가 아닙니다. 척도(`data/instruments/`)는 구조화 표에 있고 LLM은 문항을 만들거나 점수를 계산하지 않습니다.
- 의료적 진단·치료 효능을 주장하지 않습니다.

### 콘텐츠 Factor (`data/content_factors.json` · `app/content/factors.py`)

자연 자극 활동(PR-01 02 03 04 05 20 21, 그리고 Pixabay 풍경을 쓰는 PR-10 · 22)의 콘텐츠를 무엇으로 채울지 plan이
고릅니다. LLM 호출을 늘리지 않고 plan의 LLM 호출 2(추천 이유)가 같은 구조화 출력에서 `content_factors`를
냅니다.

| 칸 | Enum(`app/schemas/content.py`) | 근거(값마다 논문 · 쪽은 카탈로그) | 쓰는 활동 |
| --- | --- | --- | --- |
| `scenery` 풍경 0~4 | forest · lake river · sea · mountain · meadow | P03 · P24 · P25 · P28 · P33 · N17 | 사진(PR-01 · 03 · 21 · 22), 영상(PR-02 · 10 — 숲 · 호수·강만) |
| `soundscape` 자연음 1 | birdsong · stream · rain · ocean_waves · forest_wind | N14 · N15 · N16 · N17 · N18 | PR-02 · 04 |
| `art_subject` 작품 주제 1 | waterscape · landscape | E06 · E07 | PR-20 |
| `music` 이완 음악 추천 1 | miserere · mozart_k448 · vivaldi_spring | E09 · E08(표 20 · 23쪽) | PR-05 — 추천곡을 맨 앞에 두고 고르는 것은 사용자(E08: 목록에서 사용자가 고른 음악 g = 0.38, 1편) |

- **Enum = 논문 자극으로 쓰인 범주(`basis: STIMULUS`)만.** 근거 없이 선호용으로 둔 꽃 · 하늘(`DESIGN`)은 FE 칩으로만
  받습니다. 곤충 소리(N16)처럼 근거는 있어도 불편할 수 있는 값은 `excluded`에 이유와 함께 남겼습니다.
- **LLM은 값만, 검색어는 코드가.** 값마다 검수 검색어가 2~3개이고(include · exclude 태그 안), prepare가 세션마다
  하나를 무작위로 골라 같은 풍경이라도 매번 다른 사진 · 소리를 받습니다. 자연음은 값별 exclude(거친 새소리 등)를
  프로토콜 exclude에 더합니다.
- **우선순위:** 사용자 칩(`profile.preferred_scenery`) > LLM Factor > 프로토콜 기본 검색어. `factors.sanitize`가
  활동에 없는 칸 · 근거 없는 값 · 개수 초과를 지우고, 영상과 소리를 함께 쓰는 PR-02는 소리를 영상 풍경에
  맞춥니다(숲 → 새소리 · 숲 바람, 호수·강 → 시냇물).
- 자산 `metadata.content_factor`에 축 · 값 · 출처(`USER` · `LLM`) · 근거 논문 · 검색어가 남습니다.
  `preferred_scenery`는 사용자 칩일 때만 남겨 FE가 LLM이 고른 풍경을 "좋아하는 ○○"라고 쓰지 않습니다.
- Factor는 연구 자극 범위 안의 개인화이지 효과를 키운다는 근거가 아닙니다. 추천 이유(rationale)에도 쓰지 않습니다.
- **색 선호(profile.preferred_colors · avoided_colors)**: 선호 색은 사진 여러 장일 때 4장에 1장꼴을 Pixabay 색 필터로
  받고(`preferred_color` 메타데이터), 기피 색은 LLM이 고른 풍경에서 그 색이 주로 차지하는 풍경을 뺍니다(파랑 → 바다 ·
  호수·강). 사용자 칩 풍경은 그대로 둡니다. 표는 카탈로그의 `colors`이고, 색이 정서에 효과를 낸다는 근거는 없습니다.
- **이미지 프록시(`/media`, `app/api/media.py`)**: 미술관 IIIF(Cloudflare가 브라우저 직접 호출을 막음)와 Pixabay 사진
  (영구 핫링크 금지)은 BE가 받아 전달합니다. 자산 주소가 `/media/…`로 바뀌고 원래 주소는 `metadata.origin_url`에
  남습니다. 상위 주소는 고정 템플릿이라 오픈 프록시가 아닙니다(`app/content/proxy.py`).

### LangGraph·LangChain을 쓰는 곳

- **LangGraph — 세션 그래프 전체.** Replanning → Planner 순환, 사용자 선택에 따른 분기, 사용자가
  활동을 마치거나 동의할 때까지 멈췄다가 이어 가는 interrupt가 필요합니다. 상태가 체크포인터에 남아
  HTTP 요청이 나뉘어도 세션이 이어집니다.
- **LangChain — intake · plan · replan의 구조화 출력.** LLM 호출은 다섯 곳 모두
  `with_structured_output`(json_schema) 한 번씩입니다 — intake의 상황 읽기 `StateReading`과 목표 추론
  `GoalInference`, plan의 활동 선택 `ActivityChoice`, 추천 이유 `PlanRationale`, replan의 다음 단계 추천
  `NextStepChoice`. intake의 질문 여부 · 보기 검증 · 답 병합 · 종료는 코드다
  (`app/agent/intake_rules.py`) — 질문을 몇 번 해도 LLM은 2회이고, 시점 질문에 글로 답할 때만 한 번 더 읽는다.
  plan은 고정 절차입니다: 코드가 후보를 목표 · 사용자 상태 · 시간 · 환경(소리 · 조용함 · 화면 · 야외)으로
  거르고 등급 가중 추첨 순서로 세워 주면(`protocols.recommendation_order` — 세션 ID 시드, 목표 1차 지표
  최고 등급 A 4 · B 3 · C 2 · 그 밖 1, 최근에 한 활동 × 0.3) LLM이 앞 5개를 rank와 함께 보고 rank 1을
  고르고(제약 · 장소 · 꺼린 활동 · 직전 거부 사유일 때만 다음 것 — 자유 텍스트 대조), 코드가 그 활동의 `evidence[]` 안에서(맞는 레코드가 없으면 같은 매체
  연구 전체에서) 매체 · 사용자 상태 · 목표 지표로 근거를 검색·검증해 usable만 남기고, LLM이 그 근거로 추천
  이유를 씁니다. 처음에는 `create_agent` Tool
  루프였지만 gpt-4o-mini가 검색을 건너뛰고, 인자를 빠뜨리고, 한 번은 10분 넘게 돌아(2026-10-26 실측)
  절차를 코드로 옮겼습니다 — 종료가 보장되고 모델 호출이 4~5회에서 2회로 줍니다. 적응은 이 노드 안의
  루프가 아니라 측정 → 평가 → 재계획 → plan 순환에서 옵니다. 선택은 `check_plan`이 후검증하고, 어기면
  거부 사유를 붙여 한 번 다시 묻습니다. 모델은 OpenAI `gpt-5.4-mini` 하나로 통일했고
  (`LLM_MODEL`), Anthropic 키가 있으면 `claude-haiku-5-5`를 폴백으로 등록합니다(`FALLBACK_MODEL`,
  `app/agent/assembly.py`). 호출 규칙(`app/agent/llm.py`)은 cure-agent-be를 따릅니다 — 5xx·네트워크
  오류는 같은 공급자에 1회 재시도, 4xx는 재시도 없이 바로 폴백, 전부 실패하면 `LlmExhaustedError`.
  벤더 SDK의 자체 재시도는 끕니다.
- **나머지는 일반 Python입니다.** 측정·채점, 프로토콜 로더, Outcome Evaluator, Replanning 규칙,
  논문 수집·검색(psycopg)에는 LangChain을 쓰지 않습니다.

근거 계약(`app/schemas/evidence.py` 메타데이터 · `protocol.py` 활동 노드 JSON · `retrieval.py` 도구 출력)은
근거 arm 시트 65행과 활동 노드 JSON의 실제 값에서 뽑았고, 계약 테스트(`tests/schemas/`)가 시트의 distinct
값 전부에 대해 변환을 검사합니다.

### 세션 API (`app/api/sessions.py`)

세션 하나가 LangGraph thread 하나입니다. 요청마다 그래프가 멈춘 자리(interrupt)를 재개해 다음
interrupt(또는 끝)까지 돌리고, 멈춘 자리를 돌려줍니다. interrupt payload와 resume 값의 모양은 각 노드
docstring에 있습니다.

| 요청 | 그래프 | 응답 `step` |
| --- | --- | --- |
| `POST /sessions` — `CareRequest`(문장 + 환경 칩) | 새 thread → intake → 질문(관계없는 말이면 되묻기)이 필요하면 `intake_ask`에서, 아니면 `measure_pre`에서 멈춤. 안전 신호면 `ended = SAFETY`로 끝 | `ASK` · `MEASURE`(사전) · `END` |
| `POST /sessions/{id}/answer` — `IntakeAnswer` | 재개 → 다음 질문이나 `measure_pre`에서 멈춤(질문 합계 3회, 되묻기 제외). 되묻기의 `{"skip": true}`는 S2로 시작. 글 답에 안전 신호면 `ended = SAFETY`로 끝 | `ASK` · `MEASURE`(사전) · `END` |
| `POST /sessions/{id}/measure` — `{instrument_id: {item_id: value}}` | 사전: 재개 → plan → prepare → `run_session`에서 멈춤 | `RUN` |
| `POST /sessions/{id}/report` — `SessionReport` | 재개 → `measure_post`에서 멈춤 | `MEASURE`(사후 — SAM, S3는 + MFVAS) |
| `POST /sessions/{id}/measure` — 사후 응답 | 재개 → evaluate → replan → `confirm`에서 멈춤 | `CONFIRM` |
| `POST /sessions/{id}/decision` — `ReplanDecision` | 재개 → 활동 변경·추가 세션이면 plan → … → `run_session`, 종료면 `measure_final`(S3는 바로 끝) | `RUN` · `MEASURE`(종료) · `END` |
| `POST /sessions/{id}/measure` — 종료 응답(S1 BSRI · S2 STAI-6) | 재개 → `measure_final` 채점 → 끝 | `END` |
| `POST /sessions/{id}/retry` | `STALLED` 세션을 멈춘 노드부터 다시 돈다 | 위와 같다 |
| `GET /sessions/{id}` | 읽기만 한다 — 새로고침 · 끊긴 스트림 복구 | 지금 멈춘 자리 |

응답 본문은 `{session_id, step, node, payload}`입니다. `step`은 멈춘 노드로 정합니다 — `intake_ask` →
`ASK`(질문 `IntakeQuestion`, `app/schemas/intake.py`), `measure_*` → `MEASURE`(`payload.measure`가 `PRE` ·
`POST` · `FINAL`), `run_session` → `RUN`(계획과 그 Reference 카드 · 콘텐츠), `confirm` → `CONFIRM`(평가 · 제안 · `options`), 끝 →
`END`. 노드 이름이 API 계약이라 바꾸면 세션 API도 고칩니다. `END`의 `payload.reason`은 `DONE`이거나 intake가
세션을 끝낸 이유(`SAFETY`)이고, `DONE`이면 라운드 요약과 목표 척도의 세션 변화(`goal_scale` — 끝 − 사전,
S3는 마지막 사후값)를 싣습니다. 노드가 실패했거나 스트림이 끊겨 interrupt 사이에 멈추면 `STALLED`이고
`retry`로 잇습니다. 다른 요청이 아직 도는 중이어도 interrupt 사이라 `STALLED`로 보이므로, FE는 `GET`을 첫
로드와 끊김 복구 때만 부릅니다.

**RUN 페이로드.** `payload`는 `{plan, content}`입니다. `content`(`ActivityContent`)의 `steps` · `guide_text` ·
`notice_prompt`는 모든 활동에 공통이고, `assets`는 슬롯별 자산입니다(사진 · 영상 · 음원 · 컬러링 도안 SVG —
저장소 자산은 BE의 `/assets/…`). 활동마다 다른 실행 방법은 활동 노드 JSON의 `content_spec`을 그대로 실은
`content.content_spec`에 있습니다 — 슬롯별 대본(`slots[].scripts`)과 재생 시점(`render`), 고르는 방식(`selection`),
화면 명세(`session_ui` — 타이머 · 컨트롤 · 완료 조건 · 시작 전 안전 확인). 모양이 활동마다 달라 BE는 형을 고정하지
않습니다. NOTICE 단계가 있는 활동(PR-01 · 09 · 15 · 21)은 단계 모듈 PR-16의 입력 팝업 명세를
`content.notice_module`로 함께 받습니다. 안내 음성은 녹음하지 않습니다(녹음 음원은 PR-21만). 음성 안내 슬롯
(`GUIDE_AUDIO` — `generation.provider` `BROWSER_SPEECH_SYNTHESIS`)의 대본은 FE가 `render` 시점에 화면에 띄우고
브라우저 `speechSynthesis`(ko-KR, 한 활동은 같은 목소리)로 읽습니다. 야외 활동(PR-11 · 12)은 `session_ui.tts_policy`대로
같은 방식으로 읽습니다. 재생 명세가 없는 시작 안내(`guide.intro_text`)는 화면에만 보이고, `source_policy.tts`가
`FORBIDDEN`인 대본은 읽지 않습니다. 종료 알림음(`countdown_sound` — PR-06 · 08 · 09 · 22)도 음원이 없어 FE가 Web
Audio로 짧은 소리를 냅니다. 활동이 끝나면 `SessionReport`(`completed` · `stopped_reason` · `notice_words`)를
`report`로 보냅니다. `notice_words`는 NOTICE 팝업에 적은 감정 단어 1~3개(빈 칸은 빼고 각 20자 이하)이고,
실사용 데이터로 서버에 남습니다.

**SSE.** POST에 `Accept: text/event-stream`을 붙이면 같은 실행을 이벤트로 받습니다 — `session`
(`session_id`) → `progress`(노드가 끝날 때 `{node}` — intake는 `intake_read` · `intake_ask` · `intake_goal` ·
`intake_context` · `intake_finish`, plan 안 단계가 시작될 때 `{node: "plan", step}` — `choose_activity` ·
`search_evidence` · `verify_evidence` · `write_rationale`) → `result`(JSON 응답과 같은 본문). 실행이 실패하면
`error`(오류 본문과 같은 모양)로 끝나고, 15초 동안 이벤트가 없으면 `: ping` 주석을 보냅니다. 요청 검증
오류(404 · 409 · 422)는 스트림을 열기 전에 JSON으로 돌려줍니다. FE는 응답 전에 LLM이 도는 호출(`POST
/sessions` · `answer` · 사전 `measure` · `decision` · `retry`)만 SSE로 받고 나머지는 JSON으로 받습니다.
브라우저 `EventSource`는 GET만 되므로 fetch로 읽습니다.

**오류.** 본문은 `{"detail": {code, message, …}}`입니다.

| code | 상태 | 언제 |
| --- | --- | --- |
| `UNAUTHENTICATED` · `INVALID_TOKEN` · `SESSION_REVOKED` | 401 | 로그인 토큰이 없거나, 서명 · 만료 · 프로젝트가 맞지 않거나, 로그아웃 · 탈퇴로 로그인 세션이 끝났다(「인증 · 세션 기록」) — FE는 다시 로그인시킨다 |
| `CONSENT_REQUIRED` | 403 | 회원이 지금 버전(`version`)의 기록 동의 없이 `POST /sessions` |
| `MEMBERS_ONLY` | 403 | 게스트가 `PUT /me/consent` — 게스트 체험은 기록하지 않는다 |
| `SESSION_NOT_FOUND` | 404 | 없는 세션, 남의 세션(있다는 것도 알리지 않는다) |
| `SESSION_EXPIRED` | 410 | 끝나지 않은 채 오래 쉬어 정리 작업이 체크포인트를 지운 세션 — 새로 시작한다 |
| `STEP_MISMATCH` | 409 | 지금 멈춘 자리와 다른 요청. `step` · `node`를 함께 준다 |
| `INVALID_ANSWER` | 422 | 측정 응답에 빠진 도구·문항이나 범위 밖 값, 지금 질문이 아닌 답이나 받지 않는 칸·값·직접 입력, `options`에 없는 결정 |
| `PLAN_REJECTED` | 422 | 계획 실패. `retryable`이면 `retry`로 다시 고른다(후보가 없으면 아니다) |
| `LLM_UNAVAILABLE` | 503 | 모델 호출이 모두 실패했다(`retryable`) |
| `AGENT_UNAVAILABLE` | 503 | LLM 키가 없어 세션 그래프를 조립하지 않았다 |
| `AUTH_UNAVAILABLE` | 503 | `SUPABASE_URL`이 없거나, Supabase의 공개 키(JWKS)를 받지 못했거나, 로그인 세션을 확인하지 못했다 |

재개 값은 그래프에 넣기 전에 API가 검증합니다. interrupt 뒤에서 노드가 예외를 내면 LangGraph가 그 재개
값을 체크포인트에 남겨, 같은 interrupt를 다시 재개해도 처음 값으로 실패합니다(interrupt id를 지정해도
같습니다). 그래서 측정 응답은 노드와 같은 채점 함수로, intake 답은 같은 규칙(`intake_rules.apply_answer`)으로,
결정은 `options`로 먼저 거릅니다. FE 오리진은 `CORS_ORIGINS`(기본: 로컬 3000 · 운영 FE)와
`CORS_ORIGIN_REGEX`(기본: FE 프리뷰 배포)로 허용합니다.

체크포인터는 세션 상태를 Supabase Postgres의 `langgraph` 스키마에 저장합니다(`app/agent/checkpoint.py`).
요청마다 다른 서버(서버리스 인스턴스)가 받아도 같은 세션을 이어 갑니다. `DATABASE_URL`이 비어 있으면
`InMemorySaver`를 써서, 서버를 다시 띄우면 진행 중인 세션이 사라집니다. 끝난 세션의 체크포인트는 바로
지웁니다(아래 「인증 · 세션 기록」).

### 인증 · 세션 기록 (`app/auth.py` · `app/records.py`)

FE는 Supabase Auth(Google · Kakao)로 로그인하고, 받은 액세스 토큰을 모든 세션 API 요청에
`Authorization: Bearer <토큰>`으로 붙입니다. BE는 `SUPABASE_URL` 프로젝트의 공개 키(JWKS, ES256)로 서명을
검증하고 만료 · `aud`(`authenticated`) · `iss`를 보며, `sub`가 사용자 ID(`auth.users.id`)입니다. 대칭 키(HS256)
토큰은 받지 않습니다. `SUPABASE_URL`이 비어 있으면 세션 API는 인증 없이 열리지 않고 503입니다. 세션은 만든
사용자만 보고, 남의 세션은 없는 세션과 같은 404입니다.

**비로그인(게스트 체험).** 로그인하지 않은 사용자는 기록을 남기지 않는 1회성 체험으로 씁니다. 사용자에게는
로그인 화면이 없고, Supabase 익명 로그인으로 `auth.users`에 익명 사용자(`is_anonymous`)가 생겨 토큰 검증 ·
소유자 확인은 회원과 같습니다.

| | 회원 | 게스트 |
| --- | --- | --- |
| 시작 | 소셜 로그인 → 동의 화면 → `POST /sessions` | `supabase.auth.signInAnonymously()` → 바로 `POST /sessions`. 동의 화면 대신 고지 한 줄 |
| 서버에 남는 것 | 세션 기록 · 라운드 · (동의하면) 원문 | 세션 행에 주인과 진행(`step` · 끝난 이유 · END payload)만. 분류 값 · 칩 · 측정값 · 라운드 · 원문은 쓰지 않는다 |
| 지우는 때 | 탈퇴(`DELETE /me`) · 원문 기한 | 하루(`GUEST_RETENTION_DAYS`) 동안 쓰지 않으면 정리 작업이 사용자째 지운다 |
| 로그아웃 · 탈퇴 화면 | 있다 | 없다 |

- 고지 예: "체험 내용은 기록으로 남기지 않으며, 진행에 쓴 데이터는 1~2일 안에 지워집니다." 페이지를 나가는 순간
  지우는 것은 브라우저가 보장하지 않아, 남은 행과 체크포인트는 매일 도는 정리 작업이 지웁니다. 입력이 OpenAI로
  간다는 것은 회원과 같이 처리방침에 밝힙니다.
- FE는 게스트 세션을 `sessionStorage`에 둡니다(supabase-js `auth.storage`) — 탭을 닫으면 체험이 끝납니다.
- 게스트는 `PUT /me/consent`가 403(`MEMBERS_ONLY`)입니다. 기록을 남기려면 체험 뒤 소셜 로그인으로 회원이
  됩니다(게스트 체험은 이어지지 않습니다).
- 쓰지 않았다는 것은 만든 지, 로그인 세션을 갱신한 지, 세션 행을 쓴 지 모두 그 기간이 지났다는 뜻입니다.

Supabase 대시보드에서 Anonymous Sign-Ins를 켭니다. 익명 로그인은 IP마다 횟수 제한(Rate Limits)이 있으니
시연장에서 여럿이 같은 Wi-Fi로 들어오면 미리 올립니다.

서명이 맞아도 로그인 세션이 끝났으면 거절합니다(401 `SESSION_REVOKED`). 토큰의 `session_id`를 같은 DB의
`auth.sessions`에서 요청마다 기본키로 확인합니다 — 로그아웃 · 다른 기기 전체 로그아웃 · 탈퇴하면 Supabase가
그 행을 지우므로, 액세스 토큰의 만료(기본 1시간)를 기다리지 않고 바로 막힙니다. 토큰을 갱신해도
`session_id`는 같습니다. `auth` 스키마는 Supabase가 관리하므로 `id` · `not_after`만 보고, 확인하지 못하면
503으로 막습니다. `DATABASE_URL` 없이 띄운 서버는 세션을 확인하지 않습니다.

탈퇴는 `DELETE /me`(204)입니다. Supabase Auth 사용자 행(`auth.users`)을 지우면 Supabase의 로그인 세션 · 연동
계정 · MFA와 아래 기록 · 원문 · 동의가 모두 `on delete cascade`로 함께 지워지고(2026-10-28 운영 DB 제약 확인),
그 사용자의 체크포인트도 지웁니다. 되돌릴 수 없으니 FE가 확인 화면을 띄우고, 204를 받으면 브라우저에 남은
세션을 지우고(`supabase.auth.signOut({ scope: "local" })`) 첫 화면으로 갑니다.

회원의 동의는 둘입니다. FE가 소셜 로그인 뒤 `GET /me/consent`를 보고 `agreed`가 false면 동의 화면을 띄우고
`PUT /me/consent`로 보냅니다(게스트는 받지 않습니다).

| 동의 | 본문 | 담는 것 | 없으면 |
| --- | --- | --- | --- |
| 세션 기록(필수) | `"records": true` | 상황 분류 · 질문 slot과 칩 답 · 측정값 · 활동 · 결과. 심리 상태 측정값이 들어 있어 민감정보로 보고 따로 받는다 | `POST /sessions`가 403(`CONSENT_REQUIRED`). false로는 보낼 수 없다 |
| 원문 보관(선택) | `"raw_text": true` | 처음 입력 · 직접 쓴 답과 그 질문 문장 · PR-16 감정 단어 · 멈춘 이유, 그 글에서 나온 글(intake의 사건 · 표현 · 요약 · 판단 이유, 추천 이유) | 원문을 남기지 않는다. false로 바꾸면 남긴 원문을 바로 지운다 |

동의 문구는 FE에 있고 BE는 버전(`records.CONSENT_VERSION`)만 맞춥니다 — 문구를 바꾸면 버전을 올리고,
사용자는 다시 동의해야 세션을 시작합니다. 세션 API가 요청마다 그래프가 멈춘 뒤의 상태로 기록을 다시 씁니다.

| 테이블 | 내용 |
| --- | --- |
| `user_consents` | 사용자당 한 행 — 필수 동의 버전 · 시각, 원문 동의 |
| `care_sessions` | 세션 하나(id = thread_id) — 멈춘 자리(`step` · `node`), 목표, 환경 칩, intake 분류 값(`records.INTAKE_FIELDS`), 질문 slot과 칩 답, 사전 · 종료 측정, 끝난 이유와 END payload |
| `care_rounds` | 끝난 라운드 — 활동 · 시간 · 근거 ID · 푼 조건, 완료 여부, 사전 · 사후 측정과 변화, 권고 단계 · 선택지 · 사용자 선택 |
| `care_texts` | 원문(원문 동의한 사용자만) — 세션당 한 행, 보관 기한 `expires_at`(기본 90일, `TEXT_RETENTION_DAYS`) |

- SAFETY로 끝난 세션은 끝난 사실(`end_reason`)만 남깁니다 — 칩 · 분류 값 · 원문을 지웁니다(테이블 제약이 지킵니다).
- 세션이 끝나면 원문이 든 체크포인트를 바로 지우고, 그 뒤의 `GET /sessions/{id}`는 `care_sessions.end_summary`로 답합니다.
- 정리 작업 `GET /internal/cleanup`(Vercel Cron, 매일 03:00 KST, `CRON_SECRET`)이 기한이 지난 원문, 쓰지 않은
  게스트(하루, `GUEST_RETENTION_DAYS`), 24시간(`SESSION_IDLE_HOURS`) 넘게 쉰 미완료 세션의 체크포인트(세션은
  만료 — 410), 기록이 없는 체크포인트(인증 전 세션 · 지운 사용자)를 지웁니다.
- 모든 행이 `auth.users`를 따라 지워집니다(`on delete cascade`). 대시보드에서 사용자를 지워도 기록 · 원문 ·
  동의가 함께 사라지고, 남은 체크포인트는 다음 정리 작업이 지웁니다.

### Reference 카드 (`app/agent/reference_card.py`)

"왜 추천했나요?"에 답하는 카드입니다. plan 노드가 계획마다 만들어 `CarePlan.reference_card`에 싣고, FE는 `RUN`
payload의 `plan.reference_card`를 그대로 그립니다(계약 `app/schemas/card.py`). LLM을 쓰지 않고 추천 이유(`rationale`)
문장에 기대지 않습니다 — 카드의 사실은 모두 검증을 통과한 근거(검색 결과 + 판정), 프로토콜, intake 결과에서
옵니다. 연구 결과를 진단 · 치료 효과로 넓히지 않습니다.

| 칸 | 내용 |
| --- | --- |
| `protocol_id` · `name` · `minutes` · `composition` | 고른 활동 |
| `badges` | 화면 배지(`CardBadge`) — `EXPERIMENTAL` · `LIMITED_EVIDENCE` · `NO_VERIFIED_EVIDENCE` · `CONFLICTING_EVIDENCE` · `ADAPTED_STIMULUS` · `AI_IMAGE_UNVERIFIED` · `RELAXED_ORDER` · `RELAXED_ENVIRONMENT` · `DEFAULT_GOAL` · `PROVISIONAL_MEASURE`. 문구 · 색은 FE가 정하고 설명 문장은 `limits` · `goal.notes`에 있다 |
| `goal` | 목표와 라벨, 정한 방식(`INFERRED` · `DEFAULT` · `CHIP`)과 판단 이유, **목표 근거**(추론한 목표 · 부목표의 개념 정의 원문 · 개념 · 쪽 · 논문), 근거가 보는 1차 지표, LUMI가 재는 척도(`measured_by`, 임시 도구면 `provisional`), `notes`(기본 목표 · S3 "주의 용량은 재지 않는다" · 임시 척도) |
| `grades` | 이 활동의 목표 1차 지표별 근거 등급(프로토콜 시트, A–D · — · 미확인) |
| `evidence[]` | 검증을 통과한 실험 하나씩(같은 실험의 METHODS · RESULTS chunk를 묶고, 실험 ID가 없는 리뷰는 논문 단위) — 이론 → 논문(제목 · 저자 · 연도 · DOI 링크) → 대상 · 표본 · 노출 시간 · 연구 상황 → 목표 지표 결과(척도 · 비교 · 시점 · 통계 원문) → 지지 여부 · 플래그 · 한계 문장, 원문 구간(`excerpts`, 영어 원문), 검증 6항목(`checks`, 자세히 보기용). 목표를 지지하는 것이 앞 |
| `limits` | 카드 끝 한계 문장 — 고정 고지, 배지마다 한 문장(프로토콜 작성자의 `reference_card_note`가 있으면 그것으로), 프로토콜의 대상 메모 |

- `EXPERIMENTAL`은 `composition`이 `EXPERIMENTAL`이거나 프로토콜 `user_facing_flags`가 그 목표에서 실험적이라고 할 때,
  `ADAPTED_STIMULUS`는 프로토콜이 각색(대본 · 음원)을 밝혔거나 목표를 지지하는 근거가 모두 다른 자극일 때,
  `AI_IMAGE_UNVERIFIED`는 사용자가 AI 생성 이미지를 골라 제공 매체에 들어갈 때 답니다.
- S3는 근거의 1차 지표(주의 과제)와 LUMI가 재는 것(정신 피로 VAS, 팀이 만든 임시 도구)이 달라 `goal.notes`에 밝힙니다.
- 목표 척도가 `PROVISIONAL`이면 척도마다 한 문장을 `goal.notes`에 넣습니다(`reference_card.PROVISIONAL_NOTES`) — STAI-6 ·
  BSRI는 타당성이 확인된 영어판을 옮긴 번역 초안이라 번역본은 따로 검증하지 않았다고, MFVAS는 LUMI가 만든 질문이라
  검증되지 않았다고 적습니다.
- 근거 DB가 없거나 검증을 통과한 근거가 없으면 `evidence`가 비고 `NO_VERIFIED_EVIDENCE`가 붙습니다.

## 시작하기

Python 3.12 이상이 필요합니다.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # DB·LLM 키와 필요한 콘텐츠 공급자 키를 채운다(비운 공급자는 비활성화)
make run                # http://127.0.0.1:8000/health
```

| 명령 | 하는 일 |
| --- | --- |
| `make run` | 개발 서버 (코드 변경 시 자동 재시작) |
| `make lint` | ruff 검사 |
| `make test` | pytest (그래프 흐름과 Planner 배선은 대본 모델로 확인해 LLM을 부르지 않는다) |
| `make test-intake` `test-measure` `test-planner` `test-protocols` | 역할별 테스트([TEAM.md](TEAM.md)) |
| `make graph` | 세션 그래프를 Mermaid로 출력 |
| `make protocols-scaffold XLSX=<워크북>` | 프로토콜·근거 arm 시트에서 활동 노드 JSON 초안을 만든다(있는 파일은 건너뜀) |
| `make checkpoint-setup` | `DATABASE_URL`에 세션 체크포인트 테이블을 만든다 (다시 돌려도 된다) |
| `make plan-trial ARGS="S2 --repeat 3"` | 실제 모델로 intake → plan을 돌려 고른 활동 · 검색 질의 · 근거 · Reference 카드(배지 · 근거 실험 · 한계)를 본다 (LLM 비용, 근거 DB 읽기 전용). `--queries <jsonl>`이 검색 질의를 모은다 |
| `make rag-eval ARGS="run --misses"` | 근거 검색 평가 — `data/rag_eval.jsonl` 180문항으로 Hit@K · Recall@K · MRR · Foreign@5를 낸다 (근거 DB 읽기 전용, 질의 임베딩 비용). `collect`가 시나리오로 질의를 모으고(LLM 비용) `build`가 케이스를 만들고 `compare`가 결과 둘을 비교한다 (「근거 검색 평가」) |
| `make plan-eval ARGS="--database-url …"` | 계획 평가 — 시나리오 30개마다 실제 모델로 plan 노드를 돌려 규칙(계획 · 첫 선택 · 후보 · 풀린 조건 · check_plan · 활동 변경)으로 판정하고 Reference 카드 근거를 센다 (LLM 비용, 근거 DB 읽기 전용) (「계획 평가」) |

Windows(PowerShell)에서는 `py -3.12 -m venv .venv`로 만들고 `.venv\Scripts\pip install -e ".[dev]"`로
설치합니다. `make`가 없으면 Makefile의 명령을 `.venv\Scripts\python`으로 직접 실행합니다
(예: `-m ruff check .`, `-m pytest`, `-m uvicorn app.main:app --reload`).

## 데이터베이스 (Supabase)

1. Supabase 프로젝트에 마이그레이션을 이름 순서대로 적용합니다 — `supabase/migrations/20261021000000_init_evidence.sql`,
   `supabase/migrations/20261026000000_evidence_contracts.sql`, `supabase/migrations/20261027000000_care_records.sql`,
   `supabase/migrations/20261029000000_care_rounds_selection.sql`.
   대시보드 SQL Editor에 붙여 실행하거나 `psql "$DATABASE_URL" -f <파일>`로 실행합니다. 두 번째 파일은
   `papers`·`evidence_records`가 비어 있을 때만 적용되고, 행이 있으면 아무것도 바꾸지 않고 멈춥니다. 세 번째
   파일은 세션 기록 테이블이고 `auth.users`(Supabase Auth)를 참조합니다.
2. 대시보드의 **Connect** 창에서 연결 문자열을 복사해 `.env`의 `DATABASE_URL`에 넣습니다.
3. `make checkpoint-setup`으로 세션 체크포인트 테이블을 만듭니다. `langgraph-checkpoint-postgres`를
   올린 뒤에도 다시 실행합니다. 이미 적용한 마이그레이션은 건너뜁니다.
4. 근거를 적재합니다(`scripts/seed_evidence.py`). `data/manifest.csv` 84편이 `papers`로, 근거 arm 시트
   65행이 레코드 61개 × chunk 2개(METHODS · RESULTS) = 122행으로 `evidence_records`에 들어갑니다. 먼저
   dry-run으로 건수와 검토 목록(`evidence_review.csv`)을 보고 대상 DB를 적어 넣습니다. 임베딩에
   `OPENAI_API_KEY`가 필요합니다.

   ```bash
   XLSX=../Research_paper/LUMI_R_C_Research_2026-10-22/LUMI_측정도구_프로토콜_2026-10-23.xlsx
   make seed-evidence XLSX=$XLSX ARGS=--dry-run
   make seed-evidence XLSX=$XLSX ARGS="--database-url <연결 문자열>"
   ```

   대상 DB는 매번 적습니다 — `.env`의 공용 Supabase에 실수로 쓰지 않도록 기본값을 두지 않았습니다.
   다시 돌려도 (`paper_id`, `experiment_id`, `section`, `chunk_idx`)가 같으면 갱신만 해 id가 유지됩니다.
   세션 기록(`care_rounds.evidence_record_ids`)과 계획(`CarePlan`)이 이 id를 가리키므로 행을 지우고 다시 넣지 않습니다. 사람이
   원문으로 확정한 값은 `data/evidence_overrides.json`에 적고 다시 돌립니다(`data/README.md`).

   content는 `data/evidence_excerpts.json`에 발췌가 있는 레코드는 논문 원문 METHODS · RESULTS 구간, 없는
   레코드(미확보 논문 등)는 시트 요약입니다(`--no-excerpts`면 전부 요약). 발췌를 다시 만드는 순서는
   `make parse-papers ARGS="--with-records"`(Docling, 스캔 PDF는 RapidOCR 직접, `.[ingestion]` extras, 캐시는 Research_paper 비공개
   저장소의 `01_fulltext/`) → `make extract-excerpts XLSX=$XLSX`(LLM이 고르고 본문 문장으로 맞춤, 검토 목록
   `evidence_excerpts_review.csv`) → `make seed-evidence`입니다. 전후 비교는 「근거 검색 평가」로 합니다.
5. `make run` 후 `GET /health/db`로 연결, 확장(pgvector·pg_trgm) 설치, 체크포인트 테이블을 확인합니다.

| 연결 방식 | 포트 | 네트워크 | 쓰는 곳 |
| --- | --- | --- | --- |
| Direct connection | 5432 | IPv6 (IPv4 add-on이 없을 때) | 상주 서버(VM·컨테이너) |
| Shared pooler — session mode | 5432 | IPv4 | 로컬 개발, IPv4 네트워크의 상주 서버 |
| Shared pooler — transaction mode | 6543 | IPv4 | 서버리스 함수 |

트랜잭션 모드는 prepared statement를 지원하지 않습니다. `app/db.py`와 체크포인터 풀이 psycopg의 자동
prepare를 꺼 두어 세 방식 모두 같은 코드로 동작합니다. LangGraph의 `AsyncPostgresSaver.from_conn_string()`은
모든 쿼리를 prepare하므로 쓰지 않습니다.

| 테이블 | 내용 |
| --- | --- |
| `papers` | 논문 한 편 = `data/manifest.csv` 한 행. `id`는 R/C 마스터 ID(`rc_id`)이고 36편 시트의 옛 ID는 `legacy_36_id`에 둔다(조인 키 아님). `study_design`·`fulltext_status`는 manifest 값 그대로, DOI가 없으면 `url`로 원문을 가리킨다 |
| `evidence_records` | 실험 단위 Evidence Record. 원문 구간(`content`), 메타데이터(`metadata` jsonb), 임베딩(`vector(1536)`)과 만든 모델(`embedding_model`). (`paper_id`, `experiment_id`, `section`, `chunk_idx`)마다 한 행 |
| `user_consents` · `care_sessions` · `care_rounds` · `care_texts` | 동의와 세션 기록(「인증 · 세션 기록」). 행은 `auth.users`를 따라 지워진다 |
| `langgraph.checkpoints` 등 4개 | 세션 체크포인트. `make checkpoint-setup`이 만들고, 적용한 버전을 `checkpoint_migrations`에 남긴다 |

이 프로젝트는 Supabase Data API(`/rest/v1`)를 처음부터 꺼 두었습니다. BE는 Postgres에 직접 붙고 FE는
Supabase Auth만 쓰므로 필요하지 않습니다. 그래서 publishable 키로는 어떤 테이블에도 닿지 않습니다(호출하면
503 `PGRST002`). 나중에 켜더라도 노출되지 않도록 `public` 스키마의 테이블은 RLS를 켜고 정책은 두지
않았습니다. 테이블 소유자인 `postgres` 역할로 접속하는 백엔드에는 영향이 없습니다.

체크포인트 테이블은 Data API를 켜도 노출되지 않는 `langgraph` 스키마에 둡니다. 라이브러리가 테이블 이름에
스키마를 붙이지 않아, 체크포인터 풀이 연결 옵션 `-c search_path=langgraph`로 스키마를 가리킵니다.
트랜잭션 모드에서는 `SET`이 다음 트랜잭션까지 남지 않지만, Shared pooler(Supavisor)는 search_path마다
풀을 따로 두어 이 옵션을 유지합니다. Dedicated pooler(PgBouncer)로 바꾸면 이 부분을 다시 확인합니다.

DB 테스트(`tests/agent/test_checkpoint.py`·`tests/retrieval/test_hybrid_db.py`·
`tests/agent/test_validation_db.py`·`tests/api/test_records_db.py`)는 `TEST_DATABASE_URL`이 있을 때만 돌고, CI에서는 건너뜁니다.
체크포인터는 세션을 시작한 풀과 다른 풀에서 세션을 이어 가는지 봅니다. 근거 검색·검증은
`evidence_records`를 fixture 16건으로 바꾼 트랜잭션(`tests/evidence_db.py`) 안에서 필터·키워드·RRF와
판정을 보고 끝에 되돌립니다(적재해 둔 근거는 남습니다). 세션 기록도 대역 그래프로 세션을 끝까지 돌려 쓴 행을
트랜잭션 안에서 보고 되돌립니다. 로컬 Postgres는 Supabase처럼 `extensions` 스키마를 search_path에 넣고,
`auth.users` · `auth.sessions`를 흉내 낸 뒤 마이그레이션을 적용합니다.

```bash
docker run -d --name lumi-pg -e POSTGRES_PASSWORD=postgres -p 54329:5432 pgvector/pgvector:pg17
psql "postgresql://postgres:postgres@localhost:54329/postgres" -c 'create schema extensions' \
  -c 'alter database postgres set search_path = "$user", public, extensions' \
  -c 'create schema auth' \
  -c 'create table auth.users (id uuid primary key, is_anonymous boolean not null default false, created_at timestamptz default now())' \
  -c 'create table auth.sessions (id uuid primary key, user_id uuid not null references auth.users on delete cascade, created_at timestamptz default now(), updated_at timestamptz default now(), refreshed_at timestamp, not_after timestamptz)' \
  -f supabase/migrations/20261021000000_init_evidence.sql \
  -f supabase/migrations/20261026000000_evidence_contracts.sql \
  -f supabase/migrations/20261027000000_care_records.sql \
  -f supabase/migrations/20261029000000_care_rounds_selection.sql
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:54329/postgres make test
```

### 근거 검색 평가

`data/rag_eval.jsonl`은 근거 검색(`hybrid.search`)의 평가셋입니다(2026-10-28 갱신, 180문항). 운영 plan 노드가
마주칠 수 있는 검색 상황(활동 × 목표 × 사용자 상태)을 전부 덮습니다.

- **시나리오.** `data/eval_scenarios.jsonl`은 목표 3 × 사용자 상태 5(사건 전 · 사건 후 · 오래 집중한 뒤 ·
  계기 없음 · 잘 모르겠어요) × 문장 2(직접 · 돌려 말하기) = 30개입니다. 칩(`setting` · `earphones` ·
  `minutes`)은 같은 시나리오를 쓰는 계획 평가용입니다.
- **질의.** `make rag-eval ARGS="collect"`가 시나리오마다 그 목표 · 상태의 후보(`plan_candidates`, 칩 없이)를
  한 바퀴 돌며 운영 choose 프롬프트로 활동마다 `search_query`를 받습니다 — 고른 활동을 빼 가며 다시 묻고,
  목표 · 상태는 intake를 거치지 않고 직접 넣습니다. 상황 110개 × 문장 2 = 220개를 모았고(gpt-5.4-mini, 67초,
  입력 토큰 57만), 같은 상황에 같은 질의는 `build`가 하나로 합쳐 180문항입니다. 모은 질의
  (`data/rag_eval_queries.jsonl`)는 다시 만들 수 없어 케이스와 따로 두고, `collect`는 빠진 (시나리오, 활동)만
  채웁니다.
- **정답.** 활동 노드 JSON `evidence[]` 가운데 그 목표의 1차 지표를 잰 레코드(`paper_id`, `experiment_id`)이고
  `build`가 붙입니다. `run`은 DB에 없는 정답과, 사용자 상태에 인용할 수 없는 연구 조건(`ORDER_COMPATIBILITY`)의
  정답을 분모에서 빼고 따로 셉니다 — 뒤의 것은 검색의 순서 필터가 일부러 거르는 레코드입니다.
- **묶음.** `planner`(163)는 정답이 있는 상황, `fallback`(17)은 그 활동 `evidence[]`에 목표 지표 근거가 없어
  같은 필터로 전체에서 찾는 상황입니다(PR-15 · PR-21 S1, PR-03 S2). fallback은 상위 5가 늘 다른 활동의
  근거라 점수 대신 상위 5에 든 레코드 수(`found@5`)와 목록을 봅니다. `run --open`은 planner 질의를 필터 ·
  범위 없이도 돌립니다(임베딩 · 키워드만의 품질, RRF 가중치 실험용).
- **평균.** 문항마다 같은 무게입니다 — 운영에서 나올 수 있는 검색 상황 전체의 평균입니다. 활동이 S2에 17개
  (S1 · S3는 4개씩)라 planner 163문항 중 119문항이 S2입니다.
- `make rag-eval ARGS="run --misses"`가 hybrid(운영과 같은 RRF) · dense · keyword 세 방식으로 Hit@K ·
  Recall@K · MRR(K = 3 · 5 · 10)과 Foreign@5(상위 5 chunk에 든 레코드 중 그 활동 `evidence[]` 밖의 비율 —
  낮을수록 좋다)를 내고, `--out`으로 남긴 결과 둘을 `compare`가 비교합니다. DB는 lumi-pg로 돌립니다 — 운영
  Supabase의 session pooler(5432)는 클라이언트 한도가 15라 다른 연결과 겹치면 평가 풀이 연결을 못 엽니다.
- `tests/retrieval/test_rag_eval.py`가 커밋된 케이스가 `build` 결과와 같은지, 후보가 될 수 있는 상황마다 모은
  질의가 있는지 봅니다. 활동 JSON이 바뀌어 깨지면 `collect` → `build`를 다시 돌립니다.

2026-10-28 측정(180문항, text-embedding-3-small, lumi-pg — 운영 Supabase도 같은 값):

| view · 방식 | n | Hit@5 | Recall@3 | Recall@5 | Recall@10 | MRR | Foreign@5 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| planner · hybrid | 163 | 1.000 | 0.840 | 0.920 | 0.949 | 1.000 | 0 |
| planner · dense | 163 | 1.000 | 0.820 | 0.901 | 0.947 | 1.000 | 0 |
| planner · keyword | 163 | 0.951 | 0.783 | 0.829 | 0.849 | 0.951 | 0 |
| open · hybrid | 163 | 0.675 | 0.426 | 0.515 | 0.685 | 0.606 | 0.604 |
| open · dense | 163 | 0.877 | 0.540 | 0.676 | 0.819 | 0.805 | 0.435 |

| view · 방식 | n | found@5 평균 | 근거 0건 |
| --- | --- | --- | --- |
| fallback · hybrid | 17 | 2.47 | 2 (12%) |

- 운영 경로(planner · hybrid)는 첫 정답이 늘 1위이고(MRR 1.000) 다른 활동의 근거가 섞이지 않는다(Foreign@5 0).
  옛 평가셋(planner 9 · template 21)의 Recall@5 0.911 · 0.906과 같은 수준이다.
- 상위 5에 못 든 정답은 세 갈래다. ① PR-19의 E11-E1 · E12-E1, PR-18의 E15는 활동 JSON에는 AFFECT를 쟀다고
  돼 있지만 레코드 metadata에는 OTHER뿐이라 S2 지표 필터에 걸리지 않는다(데이터 문제, 아래와 같다). ② PR-14는
  정답이 7개라 상위 5에 다 들 수 없고, 원문이 없는 E02-E1 · P30-E1이 뒤로 밀린다. ③ S3의 PR-11 · PR-01은
  P07-E1 · P03-E1 · P01 · P04가 6~8위다.
- 사용자 상태 정책으로 정답 19건을 분모에서 뺐다(예: 사건 전 사용자에게 스트레스 뒤 회복 연구).
- fallback 2문항(PR-15를 사건 전 · 오래 집중한 뒤에 고른 경우)은 상위 5가 비어 Reference 카드에 근거가 없다 —
  빌려 올 S1 컬러링 연구 P32-E1이 스트레스 뒤(POST_STRESS) 조건이라 그 상태에 인용할 수 없다. PR-21은 PR-09의
  N26-E1 · P13 · P14(와 PR-01의 P24-E1 · P33-E1)를, PR-03은 PR-01의 P24-E1 · P33-E1 · N24를 빌려 온다.
- 필터 없는 open에서는 여전히 dense(Recall@5 0.676)가 hybrid(0.515)보다 낫다 — RRF 가중치 실험의 근거다.
  운영은 범위 · 필터 안에서 찾으므로 이 차이가 planner 결과에는 거의 드러나지 않는다.

아래 두 측정은 옛 평가셋(51문항 — `planner` 9 · `template` 21 · `open` 21, 2026-10-26 질의)으로 잰 것이다.
`template`은 프로토콜 × 목표의 연구 용어 질의, `open`은 같은 질의를 필터 없이 돌린 묶음이었다.

2026-10-27 측정(text-embedding-3-small, lumi-pg). '요약'은 content가 근거 arm 시트 요약 122 chunk일 때,
'원문'은 레코드 58개(chunk 116개)를 논문 원문 METHODS · RESULTS 발췌로 바꾼 뒤(`data/evidence_excerpts.json`,
스캔 P05 · P26은 쪽 이미지 대조)다.

| group · 방식 | n | Hit@5 요약 → 원문 | Recall@5 요약 → 원문 | MRR 요약 → 원문 |
| --- | --- | --- | --- | --- |
| planner · hybrid | 9 | 1.000 → 1.000 | 0.867 → 0.911 | 1.000 → 1.000 |
| template · hybrid | 21 | 1.000 → 1.000 | 0.875 → 0.882 | 0.863 → 0.843 |
| open · hybrid | 21 | 0.714 → 0.714 | 0.558 → 0.517 | 0.648 → 0.541 |
| open · dense | 21 | 0.714 → 0.762 | 0.558 → 0.638 | 0.648 → 0.690 |
| planner · keyword | 9 | 0 → 1.000 | 0 → 0.933 | 0 → 1.000 |

- 요약일 때 keyword는 세 묶음 모두 0이었다 — 한국어 요약에 영어 질의 낱말이 걸리지 않아 hybrid가 곧 dense였다.
  원문으로 바꾸자 keyword가 살아났고, 플래너가 실제로 쓰는 필터 경로(planner · template)는 같거나 나아졌다.
- 필터 없는 `open`에서는 dense가 오르고 hybrid가 내렸다. 아직 약한 keyword 순위가 RRF에서 dense 순위를 흐린다 —
  RRF 가중치(또는 keyword 후보 수) 조정이 다음 실험이다.
- 같은 날 `make plan-trial ARGS="S1 S2 S3"`는 요약 · 원문 모두 S1 → PR-09, S2 → PR-06, S3 → PR-01을 골랐다.
  계획 한 번의 입력 토큰은 3.9~5.6천에서 4.9~6.4천으로 늘었다(추천 이유 프롬프트의 발췌가 길어짐).

**활동 근거 범위 (2026-10-28).** plan은 그 활동의 `evidence[]`(활동 노드 담당이 원문을 보고 고른 레코드) 안에서
먼저 찾고, 목표 지표 · 사용자 상태에 맞는 레코드가 없거나 `evidence[]`가 비었을 때(PR-21)만 같은 필터로 전체에서
찾습니다(`nodes/planner.find_evidence`, SQL은 `coalesce(experiment_id, paper_id)`를 `evidence[]`의 key와 대조).
같은 매체를 쓰는 활동이 여럿이라(자연 이미지 4 · 명상 3 · 호흡 · 자연음 · 영상 · 보행 · 컬러링 · 글쓰기 각 2)
매체 필터만으로는 다른 활동의 근거가 Reference 카드에 섞였습니다. `evidence[].evidence_record_ids`는 그래서
역기입하지 않고 비워 둡니다 — key로 실행할 때 찾으므로 id를 적어 둘 필요가 없습니다.

| group · hybrid | n | Foreign@5 전 → 후 | Hit@3 전 → 후 | Recall@3 전 → 후 | Recall@5 전 → 후 | MRR 전 → 후 |
| --- | --- | --- | --- | --- | --- | --- |
| planner | 9 | 0 → 0 | 1.000 → 1.000 | 0.652 → 0.652 | 0.911 → 0.911 | 1.000 → 1.000 |
| template | 21 | 0.234 → 0 | 0.952 → 1.000 | 0.705 → 0.816 | 0.882 → 0.906 | 0.843 → 1.000 |
| open | 21 | 0.498 → 0.498 | 0.667 → 0.667 | 0.447 → 0.447 | 0.517 → 0.517 | 0.541 → 0.541 |

- 다른 활동의 근거가 섞이던 template 8건(PR-04 · 10 · 11 · 12 · 18 · 19 — 예: PR-19가 자기 근거 E11-E1 ·
  E12-E1 대신 PR-18의 E14-E1 · E13을 가져왔다)이 모두 사라졌다. planner(데모 S1 · S2 · S3 질의)는 원래 0이라
  그대로다. 같은 날 `make plan-trial ARGS="S1 S2 S3"`는 PR-09 · PR-06 · PR-01을 골랐고, 그 질의들은 범위가
  있든 없든 상위 5건이 같았다.
- template의 Recall · MRR이 오른 것은 정답이 그 활동의 `evidence[]`라 범위를 좁히면 어느 정도 당연하다. 볼 지표는
  Foreign@5다. `open`은 범위도 필터도 없어 그대로다 — RRF 가중치 실험은 여기서 한다.
- 범위 안에서도 못 찾는 정답은 검색이 아니라 데이터 문제다. E11-E1 · E12-E1 · E15는 활동 JSON에는 AFFECT를
  쟀다고 돼 있지만 레코드 metadata에는 OTHER뿐이라 S2 지표 필터에 걸리지 않는다(원문 확인 뒤
  `data/evidence_overrides.json` 또는 JSON을 고친다). E02-E1 · P30-E1은 원문이 없어 시트 요약이라 순위가 낮다.

### 계획 평가

`make plan-eval`은 같은 시나리오 30개(`data/eval_scenarios.jsonl`)마다 운영 plan 노드를 실제 모델로 돌려
규칙으로 판정합니다(LLM 비용, 근거 DB 읽기 전용 — lumi-pg로 돌립니다). 목표 · 사용자 상태 · 칩(장소 · 이어폰 ·
시간)은 intake 결과로 직접 넣고(intake는 따로 평가합니다), `change_activity` 시나리오 6개는 첫 계획의 활동을
바꾸기로 한 다음 라운드를 한 번 더 돕니다. 정답은 활동 하나가 아니라 규칙입니다 — 계획이 나온다 · LLM의 첫
선택이 재질의 없이 통과한다 · 고른 활동이 기대 후보(`plan_candidates`) 안이다 · 풀린 조건이 기대와 같다 · 최종
계획이 `check_plan`을 통과한다 · 활동 변경 뒤 다른 활동을 고른다. Reference 카드의 근거 수와 다른 활동에서 빌려
온 근거는 따로 셉니다(근거가 없는 카드는 규칙 위반이 아니라 근거 부족입니다).

2026-10-28 측정(gpt-5.4-mini, lumi-pg) — 계획 36개(시나리오 30 + 활동 변경 6):

| 규칙 | 통과 |
| --- | --- |
| 계획이 나온다 | 36/36 |
| 첫 선택이 재질의 없이 통과한다 | 36/36 |
| 기대 후보 안 · 풀린 조건이 기대와 같다 · `check_plan` 통과 | 36/36 |
| 활동 변경 뒤 다른 활동을 고른다 | 6/6 |
| Reference 카드에 근거가 있다 | 35/36 |

- 근거 없는 카드는 S1-pre-b(사건 전 · 사무실 · 이어폰 없음 · 5분 → 후보가 PR-15 하나)뿐이다. 근거 검색 평가의
  fallback 0건과 같은 원인이다 — 빌려 올 컬러링 연구 P32-E1이 스트레스 뒤 조건이다. PR-15를 고른 다른 두 카드는
  PR-14의 P32-E1을 빌려 왔다.
- 첫 계획에서 고른 활동은 S1 PR-09 8 · PR-15 2, S2 PR-06 7 · PR-01 2 · PR-19 1, S3 PR-01 6 · PR-11 3 · PR-10 1이다.
  빠듯한 칩에서는 남은 후보로 옮겨 간다(예: 발표 앞 · 사무실 · 이어폰 없음 · 5분 → PR-19 글쓰기).
- 계획 한 번에 평균 3.1초, 토큰 4,091/202(선택 · 설명 두 번 호출)였다.

2026-10-29부터 후보 순서가 등급 가중 추첨이라, 평가도 시나리오마다 세션 ID를 `<--seed>:<시나리오>`(기본
`plan-eval`)로 두어 운영처럼 돌립니다. 요약에 첫 계획에서 고른 활동의 종 수와 LLM이 추천 순서 맨 앞을 고른
비율이 함께 나옵니다. 위 측정은 등급 순 시절 결과입니다(`--seed ''`로 재현). 등급 순에서는 입력 조합 420개
(목표 × 사용자 상태 × 시간 칩 × 장소 · 이어폰) 중 1순위가 될 수 있는 활동이 20개 중 11개였고, 장소를 모르는 기본
경로에서는 목표마다 PR-09 · PR-06 · PR-01로 고정됐습니다 — 가중 추첨으로는 19개입니다(PR-12는 30분이라 20분
칩에서도 후보가 아니다).

2026-10-29 측정(gpt-5.4-mini, lumi-pg, `--seed plan-eval`) — 계획 36개:

| | 등급 순(10-28) | 가중 추첨 · 후보 전체 | 가중 추첨 · rank + 앞 5개 |
| --- | --- | --- | --- |
| 규칙 6개 | 모두 통과 | 모두 통과 | 모두 통과 |
| 첫 계획에서 고른 활동 | 7종 | 10종 | 13종 |
| 추천 순서 맨 앞을 고름 | — | 27/36 | 35/36 |
| Reference 카드에 근거가 있다 | 35/36 | 34/36 | 34/36 |

- 후보를 모두 보이고 "맨 앞을 고른다"고만 하면 '5분이라 부담이 적다'며 PR-06 · PR-01로 내려가고(9번째까지),
  6번째를 고르고도 '맨 앞'이라고 적었다. 후보에 rank를 붙이고 건너뛸 사유를 넷(제약 · 장소 · 꺼린 활동 · 직전
  거부 사유)으로 못 박고 앞 5개만 보이자 35/36이 맨 앞을 골랐다(남은 하나는 야외 · 자연음 활동을 건너뛴
  S3-daily-b).
- 첫 계획에서 고른 활동은 S1 PR-09 5 · PR-15 4 · PR-21 1, S2 PR-01 3 · PR-19 2 · PR-06 · PR-07 · PR-08 ·
  PR-14 · PR-18 각 1, S3 PR-01 6 · PR-11 2 · PR-04 · PR-10 각 1이다. S1 · S3는 후보가 적어(10분이면 2개) 여전히
  두세 활동에 모인다 — 공급 문제다.
- 근거 없는 카드가 하나 늘었다(S1-fatigue-b PR-15) — 근거가 약한 활동도 앞에 서기 때문이다. 카드는 근거를
  찾지 못했다고 밝힌다.

## 배포 (Vercel)

FastAPI 앱(`app/main.py`의 `app`)을 Vercel Python 런타임에 함수 하나로 올립니다. 저장소를 Vercel 프로젝트에
연결해 두면 `main`에 머지할 때 프로덕션으로, PR마다 Preview로 배포됩니다.

| 설정 | 값 | 이유 |
| --- | --- | --- |
| 리전 | `icn1`(서울) · `vercel.json` | Supabase(`ap-northeast-2`) 옆에서 돌아야 쿼리마다 태평양을 건너지 않는다. 기본값은 `iad1`이고 Hobby는 리전 1개 |
| Python | 3.12 · `.python-version` | CI와 같은 버전 |
| `DATABASE_URL` | Transaction pooler(포트 6543) | 서버리스 함수라서(위 연결 방식 표). 로컬 `.env`의 Session pooler 주소와 포트만 다르다 |
| `OPENAI_API_KEY` · `ANTHROPIC_API_KEY` | `.env`와 같은 값 | 폴백 키는 비워 둬도 된다 |
| `PIXABAY_API_KEY` · `FREESOUND_API_KEY` · `AI_HORDE_API_KEY` | `.env`와 같은 값 | A 콘텐츠 공급자. 비운 공급자는 호출하지 않는다 |
| `SUPABASE_URL` | `https://<ref>.supabase.co` | 로그인 토큰 검증(JWKS). 비우면 세션 API가 503 |
| `CRON_SECRET` | 긴 임의 문자열 | Vercel Cron이 정리 작업(`vercel.json` `crons`, 매일 18:00 UTC = 03:00 KST)을 부를 때 `Authorization: Bearer`로 보낸다. Hobby는 하루 한 번까지 |

환경 변수는 Vercel 프로젝트의 Settings → Environment Variables에 넣고, 바꾼 뒤에는 다시 배포해야 반영됩니다.
배포한 뒤 `https://<배포 주소>/health/db`가 200이면 DB 연결·확장·체크포인트 테이블이 준비된 것입니다.
ship은 머지한 뒤 이 확인까지 자동으로 합니다(`automation/pipeline.md` Step 3).
Hobby 플랜은 함수 실행을 300초에서 끊고, 프로젝트 소유자가 작성한 커밋만 배포해 팀원 커밋이 배포되지 않을 수
있습니다. 그때는 Vercel 대시보드에서 Redeploy합니다.

## 브랜치·PR 규칙

역할별 담당 파일과 이번 주 작업 순서는 [TEAM.md](TEAM.md)에 있습니다.

feature → `main`으로 올립니다. 인프라가 Supabase 하나라 `dev`를 따로 두어도 같은 DB를 써서
환경이 나뉘지 않기 때문입니다. 스테이징 환경(별도 Supabase 프로젝트 등)과 배포 파이프라인이 생기면
그때 `dev`를 추가합니다.

1. 이슈를 템플릿(기능 구현 · 기능 수정 · 오류 수정 · 리팩토링 · 문서 작업 · 기타 수정)으로 만듭니다.
2. `main`에서 `<타입>/#<이슈 번호>` 브랜치를 만듭니다. 예: `feat/#12`, `chore/#13`
3. `main`으로 PR을 올립니다. 제목은 `[타입/#이슈 번호] 설명`이고 본문에 `close #이슈 번호`를 적습니다.
   CI(`check`: ruff·pytest, `gitleaks`)를 통과해야 머지할 수 있고, squash merge합니다.

`main`은 PR 필수(승인 0명), 필수 체크 `check`·`gitleaks`, 강제 푸시·삭제 금지로 보호합니다.

위 1~3을 자동화한 것이 **ship 하네스**입니다. 코드 수정을 끝낸 뒤 Claude Code에서는 `/ship`, Codex에서는
`$ship`(또는 "ship"·"배포해줘")으로 부르면 검증(`make lint`·`make test`) → 이슈·브랜치 생성 → PR → CI 게이트
→ squash 머지 → main CI 확인 → Vercel 프로덕션 배포·`/health/db` 확인까지 진행합니다. 절차 원본은 하네스 중립 명세 `automation/ship.md`·
`automation/pipeline.md`이고, `.claude/commands/ship.md`와 `.agents/skills/ship/SKILL.md`는 각 하네스의
진입 어댑터입니다. 폴링·게이트 스크립트(`automation/bin/`)는 표준 라이브러리만 쓰는 Python이라
bash·jq가 필요 없고, `tests/automation/`이 `make test`로 회귀 검증합니다. 처음 쓰는 PC에서 할
준비(도구 설치·로그인, Codex 설정, Windows)는 [`automation/README.md`](automation/README.md)에 있습니다.

Supabase DB는 팀이 함께 쓰므로 마이그레이션은 `main`에 머지된 것만 적용합니다. PR 단계의 스키마
변경은 로컬 Postgres(예: `pgvector/pgvector` Docker 이미지)에서 먼저 확인합니다.

## 정하지 않은 것

- 이미지 생성 공급자와 키. 채팅·추출 모델은 gpt-5.4-mini 하나(폴백 claude-haiku-5-5), 임베딩은 text-embedding-3-small입니다.
- 후보 추천 순서의 가중치. 목표 1차 지표 최고 등급으로 A 4 · B 3 · C 2 · 그 밖 1, 이 사용자가 최근 5라운드에
  한 활동 × 0.3입니다(`protocols.GRADE_WEIGHT` · `RECENT_FACTOR` · `RECENT_ROUNDS`). 라운드 기록의
  `selection`(순서 · 가중치 · 맨 앞 확률 · 고른 위치)으로 무작위성을 보정해 분석하고, 활동별 Δ가 쌓이면
  가중치를 다시 정합니다.
- Replanning 기준. 지금은 임시 규칙(끝까지 못 함 → 활동 변경, 개선 → 종료, 판정 기준 없음 → 종료, 그 밖 →
  활동 변경, 라운드 3회 상한)입니다. 선택지는 라운드 3회면 종료만, 바꿀 활동이 없으면 활동 변경을, 중간에 멈춘
  활동이면 추가 세션(같은 활동 한 번 더)을 빼고, 권할 단계가 선택지에 없으면 종료를 권합니다
  (`replanner.next_step_options`). LLM은 이 규칙 제안을 기본으로 받아 멈춘 이유 · 감정 단어 · 처음 입력에
  다른 신호가 있을 때만 바꿉니다 — 라운드 기록의 `rule_recommended` · `recommended_by_llm`으로 비교합니다.
- S3의 순간 피로 자가보고 척도. 13편 안에 검증된 도구가 없어 임시 도구 `INS-MFVAS`(정신 피로 VAS 3문항, 팀 작성·미검증)로
  사전·라운드마다 재고, 피로가 10점 이상 줄었는지로 판정합니다(문턱도 임시). 주의 용량은 재지 않습니다(근거의 S3 1차
  지표 ATTENTION은 과제) — 사용자 데이터로 문항 신뢰도와 문턱을 본 뒤 ROS 같은 외부 척도로 바꿀지 정합니다.
- STAI-6·BSRI는 한국어 번역 초안을 `PROVISIONAL`로 두고 내부 시험에 사용합니다. 두 척도는 PRE/FINAL에만 측정하며, 검증된 한국어판으로 해석하지 않습니다.
- intake 환경 가정. 사무실·카페에서 이어폰이 있으면 소리 · 조용한 곳 활동을 할 수 있다고 보고, 지하철·버스는
  이어폰이 있어도 조용한 곳으로 보지 않습니다(`app/schemas/care.environment_for` 한 곳). 장소를 끝내 모르면
  조용한 실내로 가정하고 `missing_slots`에 `setting`을 남깁니다 — FE가 활동 시작 화면에 "소리가 나와요"와
  "지금은 어려워요"(활동 변경)를 함께 보여 줘야 합니다.
- intake 안전 판단은 ① 상황 읽기(LLM)의 `safety` 칸 하나입니다. 처음 입력과 모든 글 답(되묻기 · 시점
  질문)이 ①을 다시 지나므로 모두 판단됩니다. 정규식 단어 목록은 표현이 다양해 놓치는 것이 많아 쓰지
  않습니다. 걸리면 세션을 끝내고(`ended = SAFETY`) FE가 도움 안내를 보여 줍니다. LLM 판단은 실제 문장으로
  `make plan-trial`에서 확인해야 합니다.
- intake 입구: 처음 질문 "오늘 상황이 어떠세요?"는 FE 시작 화면에 고정입니다. 답이 관계없으면(메뉴 · 코드 ·
  아무 글자) 사용자 말에 맞춘 되묻기(`slot = restate`, 보기 없음, `skip_label` '그냥 시작할게요')가 관련 있는
  답이 올 때까지 돕니다. 질문 3회 예산은 관련 있는 답 뒤부터 셉니다. 시점 질문에 관계없는 글로 답하면 단서
  없음으로 보고 넘어갑니다(되묻지 않음).
- 후보가 모자랄 때 푸는 순서. 지금은 사용자 상태 → 환경이고 시간은 풀지 않습니다(`protocols.RELAX_STEPS`).
  plan · intake 환경 질문 판정 · replan 선택지가 같은 조회(`protocols.plan_candidates`)를 봅니다. 환경을 풀면
  계획의 `environment_gaps`(AUDIO · SCREEN · QUIET · OUTDOORS)로 FE가 "소리가 나와요" 같은 안내를 띄워야
  합니다. 2026-10-27 프로토콜에서 실제로 푸는 경우는 S3 + 사건 전(사용자 상태)뿐입니다.
- 동의 문구(FE)와 그 검토. 지금은 회원의 세션 기록(측정값 포함)을 민감정보로 보고 필수 동의로, 원문 보관을 선택
  동의로 받습니다. 원문 보관 기한은 90일(`TEXT_RETENTION_DAYS`)입니다. 게스트는 동의 없이 고지만 하므로, 실제
  서비스로 열 때는 진행 중 처리(측정값 포함)를 고지로 충분히 볼 수 있는지 법률 검토가 필요합니다.
- 탈퇴 전 재인증. 지금은 FE의 확인 화면뿐이라, 탈취된 토큰으로도 탈퇴할 수 있습니다. 소셜 로그인을 한 번 더
  거치게 할지는 FE와 함께 정합니다.
- 게스트 남용. 게스트도 세션마다 LLM을 부르므로, 자동화된 익명 로그인이 비용을 키울 수 있습니다. 지금은 Supabase의
  익명 로그인 횟수 제한뿐이고, 익명 로그인에 CAPTCHA(Turnstile · hCaptcha)를 걸지 정합니다.

## 디렉터리

```text
app/
├─ main.py              FastAPI 앱 (DATABASE_URL이 있으면 DB 풀 · Postgres 체크포인터 · 기록 저장소 · 세션 그래프 · 토큰 검증기 · CORS)
├─ config.py            .env 설정 (DB · LLM/임베딩 · A 콘텐츠 공급자 키와 제한 시간 · Supabase · 기록 기한)
├─ db.py                Supabase Postgres 커넥션 풀 (pgvector 타입 등록)
├─ auth.py              로그인 토큰 검증 — Supabase JWKS(ES256) · 로그인 세션 확인(auth.sessions) · current_user
├─ records.py           세션 기록 — 동의 · 세션 · 라운드 · 원문 행과 저장소(Postgres · 메모리) · 정리 작업
├─ api/routes.py        HTTP 엔드포인트 (/health, /health/db)
├─ api/sessions.py      세션 API — 인증 · 소유자 확인 · interrupt 재개 · step · SSE/JSON 응답 · 기록
├─ api/me.py            기록 동의 (GET · PUT /me/consent) · 탈퇴 (DELETE /me)
├─ api/internal.py      정리 작업 (GET /internal/cleanup, Vercel Cron)
├─ schemas/             care(요청·환경) · intake(질문·답 계약) · measure · evidence · protocol(활동 노드) · retrieval · card(Reference 카드)
├─ intake_cues.py       intake 단서표 · 개념 정의 — data/intake_cues.json · goal_definitions.json 로더
├─ instruments.py       측정 도구 — data/instruments 로더 · 제시 · 채점
├─ protocols.py         활동 노드 — data/protocols 로더 · 후보 조회 · LLM용 압축본
├─ agent/               LUMI Care Agent (LangGraph)
│  ├─ graph.py          노드 배선·라우팅 (v2)
│  ├─ state.py          그래프 상태와 노드 입출력 스키마 (IntakeResult, CarePlan, Round …)
│  ├─ assembly.py       설정으로 모델·그래프 조립 (OpenAI → Anthropic 폴백)
│  ├─ llm.py            재시도(5xx 1회)·폴백(4xx 즉시) 규칙
│  ├─ context.py        실행 컨텍스트 — AgentContext(DB 풀) · PlannerContext(plan 단계가 읽는 세션 값)
│  ├─ checkpoint.py     체크포인터(Postgres·메모리)와 직렬화 허용 타입
│  ├─ prompts.py        intake · Planner 프롬프트와 입력 메시지
│  ├─ intake_rules.py   intake 규칙 — 질문 검증 · 답 병합 · 환경 질문 판정 · 목표 · 끝 (LLM 없이 테스트)
│  ├─ nodes/            intake · measure · ① planner · ② executor · ③ evaluator · ④ replanner
│  ├─ validation.py     EvidenceValidationTool 본체 (judge: DB 없는 규칙)
│  └─ reference_card.py Reference 카드 — 검증한 근거 · 프로토콜 · intake로 만든다(LLM 없음)
├─ content/             콘텐츠 자산 — 외부 공급자 · 검수 자산 매니페스트 · 콘텐츠 Factor 카탈로그 (Executor가 쓴다)
├─ static/              검수 자산 — 음원(audio) · 컬러링 도안 SVG(coloring), /assets로 제공
├─ ingestion/           Offline 근거 구축 — 수집(collect) · Docling 섹션 파싱(parse) · 원문 발췌(extract)
└─ retrieval/           하이브리드 검색(메타 필터 → 벡터·키워드 → RRF) · 질의 임베딩
data/
├─ protocols/           활동 노드 JSON 21개 (A · B · C 담당이 채움, review DRAFT)
├─ instruments/         측정 도구 4개 (SAM · STAI-6 · BSRI · MFVAS)
├─ evidence_overrides.json 사람이 확정한 근거 메타데이터 (적재 때 시트 값을 덮어씀)
├─ evidence_excerpts.json  레코드별 논문 원문 METHODS · RESULTS 발췌 (적재 때 content로 씀)
├─ manifest.csv         논문 84편 매니페스트
├─ intake_cues.json     intake 단서표 S1·S2·S3(표현 단서 · 경계 · 요약 출처) + 상태 단서
├─ goal_definitions.json intake 개념 정의 S1·S2·S3 — R1 논문 원문 문장(paper_id · 쪽)
├─ eval_scenarios.jsonl 평가 시나리오 30개 — 목표 × 사용자 상태 × 문장 · 칩 (검색 질의 수집 · 계획 평가)
├─ rag_eval_queries.jsonl 운영 choose 프롬프트로 모은 검색 질의 220개 (make rag-eval ARGS="collect")
└─ rag_eval.jsonl       근거 검색 평가셋 180문항 — 질의 · 필터 · 정답 레코드 (make rag-eval)
scripts/scaffold_protocols.py   워크북 → 활동 노드 JSON 초안
scripts/seed_evidence.py        근거 arm 시트 → papers · evidence_records 적재(make seed-evidence)
scripts/plan_trial.py           실제 모델로 intake → plan 시나리오 실행 · 검색 질의 수집(make plan-trial)
scripts/rag_eval.py             근거 검색 평가 — 질의 수집 · 케이스 생성 · Hit@K · Recall@K · MRR · 전후 비교(make rag-eval)
scripts/plan_eval.py            계획 평가 — 시나리오마다 실제 모델로 plan 노드 실행 · 규칙 판정(make plan-eval)
scripts/parse_papers.py         논문 PDF → 섹션 텍스트 캐시, Docling · 스캔은 RapidOCR(make parse-papers)
scripts/extract_excerpts.py     레코드별 원문 METHODS · RESULTS 발췌 → data/evidence_excerpts.json(make extract-excerpts)
supabase/migrations/    DB 마이그레이션 (Supabase CLI 파일명 규칙)
vercel.json             Vercel 배포 설정 (리전 icn1 · 정리 작업 cron)
tests/
├─ agent/               그래프 흐름 · Planner 배선 · 검증 규칙(대본 모델) · 검증 DB 조회
├─ intake/ measure/ protocols/   역할별 테스트 (TEAM.md)
├─ retrieval/           근거 검색 (DB 테스트는 TEST_DATABASE_URL이 있을 때만)
└─ schemas/             근거 계약 테스트 (시트 → 적재 레코드는 워크북이 있을 때만)
```
