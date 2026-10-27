# lumi-wellness-agent-be

LUMI(Light + Mind)의 백엔드입니다. LUMI는 심리학 이론·임상/실험 연구 Reference 기반 AI 웰니스 에이전트로,
사용자 상태에 맞는 자연 이미지·소리 회복 세션을 추천하고 추천 근거를 Reference 카드로 보여 줍니다.
FastAPI, Supabase Postgres(pgvector), LangGraph·LangChain을 사용합니다. 프론트엔드는
[lumi-wellness-agent-fe](https://github.com/lumi-wellness-agent/lumi-wellness-agent-fe)에서 개발합니다.

현재는 에이전트 아키텍처 v2의 뼈대 단계입니다. 그래프 배선(intake → 사전 측정 → 계획 → 실행 대기 →
사후 측정 → 평가 → 재계획 루프 → 종료 측정), 노드 사이 계약, 활동 노드 JSON 21개(초안)와 측정 도구 4개의
계약·채점, 근거 계약(스키마·도구 출력·검증 규칙)은 테스트로 확인했습니다. 근거 122건(arm 61개)은 Supabase에
적재해 하이브리드 검색·검증이 돌고, plan은 실제 모델(gpt-4o-mini)로 세 시나리오(S1 · S2 · S3)를
통과했습니다(`make plan-trial`). intake는 State(대화) → Goal(단서표 추론) → Context(환경 칩)
질문 루프로 바꿨고(#49), 대역 모델로 흐름을 테스트했습니다 — 실제 모델 확인과 개념 정의 근거(②)는
아직입니다. Reference 카드, 콘텐츠 생성, 세션 API는 아직입니다. 누가 무엇을 채우는지는
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
| `intake_read` | intake ① State (LLM) | 문장(과 앞선 질문·답)에서 사용자 상태(사건 전·후 · 피로 후 · 계기 없음) · 사건 · 반복 사고 단서 · 문장에 나온 여건을 읽고, 개념 정의 검색어와 시점 질문 초안을 낸다. 점수는 추정하지 않는다 | `nodes/intake.py` |
| `intake_goal` | intake ③ Goal (LLM) | 단서표(`data/intake_cues.json`) · 상황 단서로 개선할 결과 지표 S1·S2·S3와 부목표, 개선할 지표(`target_outcomes`)를 정한다. State 두 질문이 모두 '잘 모르겠어요'거나 단서가 없으면 S2(`goal_source = DEFAULT`). ② 개념 정의 근거(paper_id · 문장)는 JSON을 받으면 붙인다 | `nodes/intake.py` |
| `intake_context` | intake ④ Context (코드) | setting · earphones · minutes 중 비어 있고 1순위 활동을 바꾸는 칸만 칩으로 묻는다 | `nodes/intake.py` |
| `intake_ask` ⏸ | intake 질문 | 시점 질문 · 재질문 · 환경 칩. 합계 3회, State는 2회까지. 시점 질문에 '잘 모르겠어요'면 고정 재질문. 자유 입력 답이 안전 검사에 걸리면 세션 종료(`ended = SAFETY`). payload `IntakeQuestion`, resume `IntakeAnswer`(`app/schemas/intake.py`) | `nodes/intake.py` |
| `intake_finish` | intake (코드) | 빈 칸을 기본값으로(시간 10분 · setting 모르면 조용한 실내 가정), 환경 조건(소리 · 조용함 · 화면 · 야외) · Place · 끝난 이유(`end_reason`)를 정한다 | `nodes/intake.py` |
| `measure_pre` ⏸ | measure (코드) | SAM(앵커) + 목표 척도(S2 STAI-6 · S1 BSRI · S3 MFVAS 임시 도구)를 FE에 제시하고 응답을 채점한다 | `nodes/measure.py` |
| `plan` | ① Adaptive Care Planning | 고정 절차 — 후보(코드, 목표 · 상태 · 시간 · 환경으로 거른다) → 활동 선택(LLM) → 근거 검색·검증(코드) → 추천 이유(LLM) → `CarePlan`. 선택은 `check_plan`이 후검증한다 | `nodes/planner.py` |
| `prepare` | ② Care Session Executor | 프로토콜의 단계·안내 문구로 `ActivityContent`를 준비한다(이미지·음성·Reference 카드는 예정) | `nodes/executor.py` |
| `run_session` ⏸ | ② Care Session Executor | FE 플레이어가 프로토콜 1개를 실행하고 `SessionReport`(완료 여부·중단 사유·NOTICE 한 단어)를 보낼 때까지 기다린다 | `nodes/executor.py` |
| `measure_post` ⏸ | measure (코드) | 라운드 끝 SAM. S3는 MFVAS(정신 피로 VAS 3문항)도 함께 — 라운드 척도라 종료에 다시 묻지 않는다 | `nodes/measure.py` |
| `evaluate` | ③ Outcome Evaluator | Δ = 사후 − 사전(SAM 쾌·각성, S3는 MFVAS 피로·집중·기운도). S1 쾌↑ · S2 각성↓ · S3 피로↓ | `nodes/evaluator.py` |
| `replan` | ④ Replanning | 종료 · 활동 변경 · 추가 세션 중 하나를 제안하고 고를 수 있는 선택지를 정한다 — 라운드 3회면 종료만, 바꿀 활동이 없으면 활동 변경을 뺀다 (임시 규칙) | `nodes/replanner.py` |
| `confirm` ⏸ | ④ Replanning | 사용자 선택·동의(`ReplanDecision`)를 기다린다. 활동 변경·추가 세션이면 `plan`으로 돌아간다(다음 라운드의 사전값 = 이번 사후값) | `nodes/replanner.py` |
| `measure_final` ⏸ | measure (코드) | 종료 경로에서 사전·종료에만 재는 목표 척도(STAI-6 · BSRI) 1회 → 세션 Δ. S3는 라운드마다 재서 건너뛴다 | `nodes/measure.py` |

⏸는 LangGraph interrupt입니다. 그래프가 멈추고 상태를 체크포인터에 남긴 뒤, 같은 thread_id로
`Command(resume=값)`을 받으면 그 자리부터 이어서 돕니다. interrupt가 있는 노드는 재개될 때 처음부터
다시 실행되므로, 콘텐츠 생성처럼 비용이 드는 일은 앞 노드(`prepare`)에서 끝내 state에 남깁니다.

| 목표(개선할 결과 지표) | 원하는 변화 | 목표 척도 | 주 활동 노드 |
| --- | --- | --- | --- |
| S1 반추 완화 | 반복적으로 떠오르는 생각에서 잠시 벗어나기 | BSRI | PR-09 마음챙김 / PR-21 이미지+명상(실험) / PR-15 컬러링(실험) |
| S2 긴장·스트레스 완화 | 스트레스와 긴장감 완화 | STAI-6 | PR-06 호흡 / PR-02 영상+자연음 / PR-01 자연 사진 … |
| S3 주의·피로 회복 | 인지 피로 이후 주의력 회복 | MFVAS(정신 피로 VAS, 임시·미검증 — 주관적 피로만 잰다) | PR-10 ReST / PR-11 자연 보행 / PR-01 자연 사진 |

목표는 결과 지표 라벨이고 매체 묶음이 아닙니다. 활동 노드 21개는 `data/protocols/`에 있고, 라운드마다 하나를
고릅니다. 조합은 검증된 다중매체 프로토콜(PR-02 · 10 · 12) 자체로만 제공하고 플래너가 임의로 묶지 않습니다.

공통 원칙

- 논문 RAG는 근거 검색에 쓰고, 세션 실행은 검수된 프로토콜만 씁니다.
- AI 생성 이미지는 보조 콘텐츠로 쓰고, 연구 자극과의 차이는 Reference 카드에 `ADAPTED_STIMULUS`로 밝힙니다.
- 측정은 RAG가 아닙니다. 척도(`data/instruments/`)는 구조화 표에 있고 LLM은 문항을 만들거나 점수를 계산하지 않습니다.
- 의료적 진단·치료 효능을 주장하지 않습니다.

### LangGraph·LangChain을 쓰는 곳

- **LangGraph — 세션 그래프 전체.** Replanning → Planner 순환, 사용자 선택에 따른 분기, 사용자가
  활동을 마치거나 동의할 때까지 멈췄다가 이어 가는 interrupt가 필요합니다. 상태가 체크포인터에 남아
  HTTP 요청이 나뉘어도 세션이 이어집니다.
- **LangChain — intake와 plan의 구조화 출력.** LLM 호출은 네 곳 모두 `with_structured_output`(json_schema)
  한 번씩입니다 — intake의 상황 읽기 `StateReading`과 목표 추론 `GoalInference`, plan의 활동 선택
  `ActivityChoice`, 추천 이유 `PlanRationale`. intake의 질문 여부 · 보기 검증 · 답 병합 · 종료는 코드다
  (`app/agent/intake_rules.py`) — 질문을 몇 번 해도 LLM은 2회이고, 시점 질문에 글로 답할 때만 한 번 더 읽는다.
  plan은 고정 절차입니다: 코드가 후보를 목표 · 사용자 상태 · 시간 · 환경(소리 · 조용함 · 화면 · 야외)으로
  거르고 목표 1차 지표 등급 순으로 정렬해 주면 LLM이 하나를 고르고
  (제약 · 장소 · 자유 텍스트 대조), 코드가 그 활동의 매체 · 사용자 상태 · 목표 지표로 근거를
  검색·검증해 usable만 남기고, LLM이 그 근거로 추천 이유를 씁니다. 처음에는 `create_agent` Tool
  루프였지만 gpt-4o-mini가 검색을 건너뛰고, 인자를 빠뜨리고, 한 번은 10분 넘게 돌아(2026-10-26 실측)
  절차를 코드로 옮겼습니다 — 종료가 보장되고 모델 호출이 4~5회에서 2회로 줍니다. 적응은 이 노드 안의
  루프가 아니라 측정 → 평가 → 재계획 → plan 순환에서 옵니다. 선택은 `check_plan`이 후검증하고, 어기면
  거부 사유를 붙여 한 번 다시 묻습니다. 모델은 OpenAI `gpt-4o-mini` 하나로 통일했고
  (`LLM_MODEL`), Anthropic 키가 있으면 `claude-haiku-5-5`를 폴백으로 등록합니다(`FALLBACK_MODEL`,
  `app/agent/assembly.py`). 호출 규칙(`app/agent/llm.py`)은 cure-agent-be를 따릅니다 — 5xx·네트워크
  오류는 같은 공급자에 1회 재시도, 4xx는 재시도 없이 바로 폴백, 전부 실패하면 `LlmExhaustedError`.
  벤더 SDK의 자체 재시도는 끕니다.
- **나머지는 일반 Python입니다.** 측정·채점, 프로토콜 로더, Outcome Evaluator, Replanning 규칙,
  논문 수집·검색(psycopg)에는 LangChain을 쓰지 않습니다.

근거 계약(`app/schemas/evidence.py` 메타데이터 · `protocol.py` 활동 노드 JSON · `retrieval.py` 도구 출력)은
근거 arm 시트 65행과 활동 노드 JSON의 실제 값에서 뽑았고, 계약 테스트(`tests/schemas/`)가 시트의 distinct
값 전부에 대해 변환을 검사합니다.

### 세션 API (설계안, 미구현)

세션 하나가 LangGraph thread 하나입니다. FE는 interrupt마다 응답을 받고, 다음 요청으로 재개합니다.
interrupt payload와 resume 값의 모양은 각 노드 docstring에 있습니다.

| 요청 | 그래프 | 응답 |
| --- | --- | --- |
| `POST /sessions` — `CareRequest`(문장 + 환경 칩) | 새 thread → intake → 질문이 필요하면 `intake_ask`에서, 아니면 `measure_pre`에서 멈춤 | 질문(`IntakeQuestion`) 또는 측정 문항 |
| `POST /sessions/{id}/answer` — `IntakeAnswer` | 재개 → 다음 질문이나 `measure_pre`에서 멈춤 (질문 합계 3회). 자유 입력 답이 안전 검사에 걸리면 `ended = SAFETY`로 끝 | 질문 · 측정 문항 · 또는 종료(SAFETY) |
| `POST /sessions/{id}/measure` — `{instrument_id: {item_id: value}}` | 재개 → plan → prepare → `run_session`에서 멈춤 | 계획·콘텐츠 |
| `POST /sessions/{id}/report` — `SessionReport` | 재개 → `measure_post`에서 멈춤 | SAM 문항(S3는 + MFVAS) |
| `POST /sessions/{id}/measure` — SAM 응답(S3는 + MFVAS) | 재개 → evaluate → replan → `confirm`에서 멈춤 | 평가·제안 |
| `POST /sessions/{id}/decision` — `ReplanDecision` | 재개 → plan으로 돌아가거나, 종료면 `measure_final`에서 멈춤(S3는 바로 끝) | 새 계획·콘텐츠, 종료 측정 문항(S1·S2) 또는 세션 종료(S3) |
| `POST /sessions/{id}/measure` — 종료 측정 응답(S1 BSRI · S2 STAI-6) | 재개 → `measure_final` 채점 → 끝 | 세션 종료 |

체크포인터는 세션 상태를 Supabase Postgres의 `langgraph` 스키마에 저장합니다(`app/agent/checkpoint.py`).
요청마다 다른 서버(서버리스 인스턴스)가 받아도 같은 세션을 이어 갑니다. `DATABASE_URL`이 비어 있으면
`InMemorySaver`를 써서, 서버를 다시 띄우면 진행 중인 세션이 사라집니다.

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
| `make plan-trial ARGS="S2 --repeat 3"` | 실제 모델로 intake → plan을 돌려 고른 활동 · 검색 질의 · 근거를 본다 (LLM 비용, 근거 DB 읽기 전용). `--queries <jsonl>`이 검색 질의를 모은다 |
| `make rag-eval ARGS="run --misses"` | 근거 검색 평가 — `data/rag_eval.jsonl`의 질의로 Hit@K · Recall@K · MRR을 낸다 (근거 DB 읽기 전용, 질의 임베딩 비용). `build`가 케이스를 다시 만들고 `compare`가 결과 둘을 비교한다 (「근거 검색 평가」) |

Windows(PowerShell)에서는 `py -3.12 -m venv .venv`로 만들고 `.venv\Scripts\pip install -e ".[dev]"`로
설치합니다. `make`가 없으면 Makefile의 명령을 `.venv\Scripts\python`으로 직접 실행합니다
(예: `-m ruff check .`, `-m pytest`, `-m uvicorn app.main:app --reload`).

## 데이터베이스 (Supabase)

1. Supabase 프로젝트에 마이그레이션을 이름 순서대로 적용합니다 — `supabase/migrations/20261021000000_init_evidence.sql`,
   `supabase/migrations/20261026000000_evidence_contracts.sql`. 대시보드 SQL Editor에 붙여 실행하거나
   `psql "$DATABASE_URL" -f <파일>`로 실행합니다. 두 번째 파일은 `papers`·`evidence_records`가 비어 있을 때만
   적용되고, 행이 있으면 아무것도 바꾸지 않고 멈춥니다.
2. 대시보드의 **Connect** 창에서 연결 문자열을 복사해 `.env`의 `DATABASE_URL`에 넣습니다.
3. `make checkpoint-setup`으로 세션 체크포인트 테이블을 만듭니다. `langgraph-checkpoint-postgres`를
   올린 뒤에도 다시 실행합니다. 이미 적용한 마이그레이션은 건너뜁니다.
4. 근거를 적재합니다(`scripts/seed_evidence.py`). `data/manifest.csv` 83편이 `papers`로, 근거 arm 시트
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
   활동 노드 JSON의 `evidence_record_ids`가 이 id를 가리키므로 행을 지우고 다시 넣지 않습니다. 사람이
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
| `langgraph.checkpoints` 등 4개 | 세션 체크포인트. `make checkpoint-setup`이 만들고, 적용한 버전을 `checkpoint_migrations`에 남긴다 |

`public` 스키마의 테이블은 Data API로 노출되므로 RLS를 켜고 정책은 두지 않았습니다. publishable 키로는
읽고 쓸 수 없고, 테이블 소유자인 `postgres` 역할로 접속하는 백엔드에는 영향이 없습니다.

체크포인트 테이블은 Data API에 노출되지 않는 `langgraph` 스키마에 둡니다. 라이브러리가 테이블 이름에
스키마를 붙이지 않아, 체크포인터 풀이 연결 옵션 `-c search_path=langgraph`로 스키마를 가리킵니다.
트랜잭션 모드에서는 `SET`이 다음 트랜잭션까지 남지 않지만, Shared pooler(Supavisor)는 search_path마다
풀을 따로 두어 이 옵션을 유지합니다. Dedicated pooler(PgBouncer)로 바꾸면 이 부분을 다시 확인합니다.

DB 테스트(`tests/agent/test_checkpoint.py`·`tests/retrieval/test_hybrid_db.py`·
`tests/agent/test_validation_db.py`)는 `TEST_DATABASE_URL`이 있을 때만 돌고, CI에서는 건너뜁니다.
체크포인터는 세션을 시작한 풀과 다른 풀에서 세션을 이어 가는지 봅니다. 근거 검색·검증은
`evidence_records`를 fixture 16건으로 바꾼 트랜잭션(`tests/evidence_db.py`) 안에서 필터·키워드·RRF와
판정을 보고 끝에 되돌립니다(적재해 둔 근거는 남습니다). 로컬 Postgres는 Supabase처럼 `extensions`
스키마를 search_path에 넣은 뒤 마이그레이션을 적용합니다.

```bash
docker run -d --name lumi-pg -e POSTGRES_PASSWORD=postgres -p 54329:5432 pgvector/pgvector:pg17
psql "postgresql://postgres:postgres@localhost:54329/postgres" -c 'create schema extensions' \
  -c 'alter database postgres set search_path = "$user", public, extensions' \
  -f supabase/migrations/20261021000000_init_evidence.sql \
  -f supabase/migrations/20261026000000_evidence_contracts.sql
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:54329/postgres make test
```

### 근거 검색 평가

`data/rag_eval.jsonl`은 근거 검색(`hybrid.search`)의 평가셋입니다. 케이스마다 질의 · 필터 · 정답 레코드
(`paper_id`, `experiment_id`)가 있고, 정답은 활동 노드 JSON `evidence[]` 가운데 그 목표의 1차 지표를 측정한
항목입니다(검색의 outcomes 필터와 같은 축). 질의는 세 묶음입니다 — `planner`(실제 모델이 S1 · S2 · S3 세션에서
만든 질의와 그때의 필터), `template`(프로토콜 × 목표의 연구 용어 질의, plan 노드와 같은 필터), `open`(같은 질의를
필터 없이 — 임베딩 · 키워드만의 품질). `make rag-eval ARGS="run --misses"`가 hybrid(운영과 같은 RRF) · dense ·
keyword 세 방식으로 Hit@K · Recall@K · MRR(K = 3 · 5 · 10)을 내고, `--out`으로 남긴 결과 둘을 `compare`가
비교합니다. content를 바꿔 다시 적재하기 전후에 돌립니다. `build`는 `data/protocols/*.json`에서 케이스를 다시
만듭니다(`evidence[]`가 바뀌면 다시 만듭니다).

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

- 이미지 생성 공급자와 키. 채팅·추출 모델은 gpt-4o-mini 하나(폴백 claude-haiku-5-5), 임베딩은 text-embedding-3-small입니다.
- Replanning 기준. 지금은 임시 규칙(끝까지 못 함 → 활동 변경, 개선 → 종료, 판정 기준 없음 → 종료, 그 밖 →
  활동 변경, 라운드 3회 상한)입니다. 선택지는 라운드 3회면 종료만, 바꿀 활동이 없으면 활동 변경을 빼고, 권할
  단계가 선택지에 없으면 종료를 권합니다(`replanner.next_step_options`).
- S3의 순간 피로 자가보고 척도. 13편 안에 검증된 도구가 없어 임시 도구 `INS-MFVAS`(정신 피로 VAS 3문항, 팀 작성·미검증)로
  사전·라운드마다 재고 피로↓로 판정합니다. 주의 용량은 재지 않습니다(근거의 S3 1차 지표 ATTENTION은 과제) — 사용자
  데이터로 문항 신뢰도를 본 뒤 ROS 같은 외부 척도로 바꿀지 정합니다.
- STAI-6(Mind Garden)·BSRI 문항 사용 허가와 한국어판. 확인 전에는 문항 원문을 넣지 않습니다.
- Session Outcome DB(측정값·라운드) 테이블 — 사용자 식별 방식과 함께 정합니다. Care Protocol은 JSON 파일로 둡니다.
- 세션 API (설계안은 위에)
- intake 환경 가정. 사무실·카페에서 이어폰이 있으면 소리 · 조용한 곳 활동을 할 수 있다고 보고, 지하철·버스는
  이어폰이 있어도 조용한 곳으로 보지 않습니다(`app/schemas/care.environment_for` 한 곳). 장소를 끝내 모르면
  조용한 실내로 가정하고 `missing_slots`에 `setting`을 남깁니다 — FE가 활동 시작 화면에 "소리가 나와요"와
  "지금은 어려워요"(활동 변경)를 함께 보여 줘야 합니다.
- intake 안전 검사는 질문에 대한 자유 입력 답에만 겁니다(규칙 기반, `intake_rules.safety_hit`). 처음
  입력 문장은 검사하지 않습니다. 걸리면 세션을 끝내고(`ended = SAFETY`) FE가 도움 안내를 보여 줍니다.
- 후보가 모자랄 때 푸는 순서. 지금은 사용자 상태 → 환경이고 시간은 풀지 않습니다(`protocols.RELAX_STEPS`).
  plan · intake 환경 질문 판정 · replan 선택지가 같은 조회(`protocols.plan_candidates`)를 봅니다. 환경을 풀면
  계획의 `environment_gaps`(AUDIO · SCREEN · QUIET · OUTDOORS)로 FE가 "소리가 나와요" 같은 안내를 띄워야
  합니다. 2026-10-27 프로토콜에서 실제로 푸는 경우는 S3 + 사건 전(사용자 상태)뿐입니다.

## 디렉터리

```text
app/
├─ main.py              FastAPI 앱 (DATABASE_URL이 있으면 DB 풀과 Postgres 체크포인터를 연다)
├─ config.py            .env 설정 (DB · LLM/임베딩 · A 콘텐츠 공급자 키와 제한 시간)
├─ db.py                Supabase Postgres 커넥션 풀 (pgvector 타입 등록)
├─ api/routes.py        HTTP 엔드포인트 (/health, /health/db)
├─ schemas/             care(요청·환경) · intake(질문·답 계약) · measure · evidence · protocol(활동 노드) · retrieval
├─ intake_cues.py       intake 단서표 — data/intake_cues.json 로더 · 상태 단서
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
│  └─ reference_card.py Reference 카드
├─ content/             이미지 · 안내 문구 (Executor가 쓴다)
├─ ingestion/           Offline 근거 구축 — 수집(collect) · Docling 섹션 파싱(parse) · 원문 발췌(extract)
└─ retrieval/           하이브리드 검색(메타 필터 → 벡터·키워드 → RRF) · 질의 임베딩
data/
├─ protocols/           활동 노드 JSON 21개 (PR-01 완성 예시, 나머지 초안)
├─ instruments/         측정 도구 4개 (SAM · STAI-6 · BSRI · MFVAS)
├─ evidence_overrides.json 사람이 확정한 근거 메타데이터 (적재 때 시트 값을 덮어씀)
├─ evidence_excerpts.json  레코드별 논문 원문 METHODS · RESULTS 발췌 (적재 때 content로 씀)
├─ manifest.csv         논문 83편 매니페스트
├─ intake_cues.json     intake 단서표 S1·S2·S3(표현 단서 · 경계 · 요약 출처) + 상태 단서
└─ rag_eval.jsonl       근거 검색 평가셋 — 질의 · 필터 · 정답 레코드 (make rag-eval)
scripts/scaffold_protocols.py   워크북 → 활동 노드 JSON 초안
scripts/seed_evidence.py        근거 arm 시트 → papers · evidence_records 적재(make seed-evidence)
scripts/plan_trial.py           실제 모델로 intake → plan 시나리오 실행 · 검색 질의 수집(make plan-trial)
scripts/rag_eval.py             근거 검색 평가 — 케이스 생성 · Hit@K · Recall@K · MRR · 전후 비교(make rag-eval)
scripts/parse_papers.py         논문 PDF → 섹션 텍스트 캐시, Docling · 스캔은 RapidOCR(make parse-papers)
scripts/extract_excerpts.py     레코드별 원문 METHODS · RESULTS 발췌 → data/evidence_excerpts.json(make extract-excerpts)
supabase/migrations/    DB 마이그레이션 (Supabase CLI 파일명 규칙)
vercel.json             Vercel 배포 설정 (리전 icn1)
tests/
├─ agent/               그래프 흐름 · Planner 배선 · 검증 규칙(대본 모델) · 검증 DB 조회
├─ intake/ measure/ protocols/   역할별 테스트 (TEAM.md)
├─ retrieval/           근거 검색 (DB 테스트는 TEST_DATABASE_URL이 있을 때만)
└─ schemas/             근거 계약 테스트 (시트 → 적재 레코드는 워크북이 있을 때만)
```
