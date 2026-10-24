# pipeline — 배포 파이프라인 (하네스 중립 명세)

이슈 브랜치의 변경을 커밋 → `main` PR → CI 체크 → squash 머지 → main push CI 확인까지 자동화한다. `ship`의 Phase 4에서 사용한다.
브랜치명에 `#`이 포함되므로 모든 git/gh 명령에서 브랜치명은 따옴표로 감싼다.

> **이 파일은 하네스 중립 「진실의 원천」이다.** 진입 어댑터(`ship`)를 통해 실행되며, 어댑터가 「폴링 실행 규칙」의 모드와 아래 하네스별 값을 지정한다:
>
> | 하네스 | 커밋 Co-Author 트레일러 (Step 1-1 커밋 메시지 끝 한 줄) | PR 본문 서명 (Step 1-4 본문 끝) |
> |--------|---------------------------------------------------------|---------------------------------|
> | Claude Code | 세션 시스템 규칙이 지정하는 `Co-Authored-By:` 트레일러 | `🤖 Generated with [Claude Code](https://claude.com/claude-code)` |
> | Codex | `Co-Authored-By: Codex <noreply@openai.com>` | 없음 |

**실행 주체**: 전 과정(Step 1~3)을 하나의 실행 흐름이 직접 수행한다. 파이프라인은 "액션 → 체크 폴링 → 다음 액션"의 반복이라, 폴링을 서브에이전트에 위임하면 완료 후 재개가 안 돼 대기 지점에서 멈춘다.
- **Claude Code**: 메인 세션이 직접 수행하고 서브에이전트에 위임하지 않는다. 메인 세션의 `run_in_background` Bash는 완료 시 세션을 확실히 재호출한다.
- **Codex**: 단일 호출 흐름에서 순차 수행한다. 서브에이전트(`spawn_agent`)에 폴링을 맡기지 않는다.

## 폴링 실행 규칙

CI·PR 체크 대기는 `automation/bin/`의 **폴링 스크립트**(`pr-gate.sh`, `run-wait.sh`)로 실행한다 — 루프를 즉석에서 작성하지 않는다. 스크립트의 *실행 모드*는 진입 어댑터가 지정한 **하네스별 폴링 모드**를 따른다:

| 하네스 | 폴링 모드 |
|--------|-----------|
| **Claude Code** | 스크립트를 **`run_in_background: true` Bash**로 실행. 포그라운드 `sleep` 차단·Monitor 도구 금지(단일 완료 대기에 부적합). 스크립트 완료 시 세션이 자동 재호출된다. |
| **Codex** | 스크립트를 포그라운드 **블로킹** 호출로 완료까지 대기 — 배경 재호출 프리미티브가 없으므로 스크립트가 종료 마커를 출력하고 반환할 때까지 그 호출이 블로킹된다. 출력은 파일로 리다이렉트(`> "$LOG" 2>&1`)하고, **셸 명령 타임아웃을 스크립트 내부 타임아웃보다 넉넉히** 둔다(게이트 10분 → 셸 15분+). 샌드박스에 네트워크가 열려 있어야 한다(어댑터 참조). |

- 스크립트는 성공·실패·타임아웃 **모든 종료 상태에서 exit**하고, 종료 직전 결과 마커 1줄(`... result=...`)을 출력한다. 출력의 마커·종료코드로 분기한다. 게이트 정의·간격·타임아웃·마커 규약은 **각 스크립트 상단 주석이 원천**이다.
- GitHub 조회 실패는 pending으로 삼키지 않는다 — 3회 연속 실패 시 `result=API_ERROR` 마커로 종료된다(스크립트에 구현됨). `API_ERROR`는 체크 실패나 `TIMEOUT`과 구분해 실제 stderr를 요약하고 중단한다. GitHub 장애로 단정하지 말고, 하네스의 네트워크 권한·DNS·인증 상태를 함께 확인한다.
- `result=ERROR`는 jq 부재·판정 출력 이상 등 **실행 환경 문제**다(fail-closed) — 통과로 취급하지 않고 환경을 확인해 보고 후 중단한다.
- **알림이 애매하거나 오래 소식이 없으면 추측·무한 대기 말고 즉시 `gh pr view/checks`·`gh run list`로 실제 상태를 확인**한 뒤 분기한다.

## 제목 규범 — main 한 줄 = 한 변경

| 대상 | 형식 | 예 |
|------|------|----|
| 커밋 제목 (Step 1-1) | `[TYPE] 설명` | `[FEAT] 세션 체크포인터를 Supabase Postgres에 저장한다` |
| PR 제목 (Step 1-4) | `[<타입>/#<이슈번호>] 설명` | `[feat/#1] 세션 체크포인터를 Supabase Postgres에 저장한다` |
| squash 머지 제목 (Step 1-5) | `[TYPE] 설명 (#<PR번호>)` | `[FEAT] 세션 체크포인터를 Supabase Postgres에 저장한다 (#2)` |

- **squash 제목은 `--subject`로 고정한다.** GitHub의 squash 기본 제목은 PR 커밋이 하나면 그 커밋 제목, 둘 이상이면 PR 제목이라 재시도 커밋이 쌓이면 main 히스토리 형식이 흔들린다. `(#<PR번호>)` 접미사는 커밋↔PR 자동 링크이므로 기본값 그대로 붙인다.
- **머지는 `--squash`다. `--merge`·`--rebase`로 바꾸지 않는다.** GitHub Actions는 `push` 이벤트 런의 제목으로 **head 커밋 메시지 첫 줄**을 쓴다(`pull_request` 이벤트만 PR 제목을 쓴다). `ci.yml`은 `push: [main]`과 `pull_request` 양쪽에 걸려 있으므로, squash여야 main push CI 런이 `[FEAT] … (#2)`로 어느 변경의 CI인지 런 목록에서 식별된다. merge commit이면 런 제목이 `Merge pull request #N from lumi-wellness-agent/<브랜치>`가 되고 main 히스토리에 브랜치 커밋이 그대로 섞여 「한 줄 = 한 변경」이 깨진다. `run-name`으로는 이 접두사를 걷어낼 수 없어 워크플로우 쪽에 우회책이 없다.
- 저장소는 squash/merge/rebase 세 방식을 모두 허용한다 — 이 규범을 지키는 장치는 이 문서뿐이므로 명령을 그대로 따른다.

## Step 1: 커밋 & PR & 머지 (main)

1. `git status --short`를 확인해 커밋 안 된 변경이 있으면 `git add -A` → **민감 파일 게이트 통과 후** 커밋한다. 제목은 「제목 규범」의 커밋 제목, 본문은 변경 요점을 `-` 불릿으로, 끝에 **현재 하네스의 Co-Author 트레일러** 한 줄. 이미 커밋돼 있고 스테이징 후에도 변경이 없으면 커밋을 스킵한다. 게이트는 반드시 스테이징 **후**에 돌린다 — add 이전 검사로는 untracked였던 민감 파일이 잡히지 않는다:
   - 게이트: `automation/bin/sensitive-gate.sh` 실행. `SENSITIVE_GATE result=PASS` → 커밋 진행. `result=BLOCKED`(종료코드 1) → 출력된 파일을 `git reset HEAD <파일>`로 해제하고 보고 후 중단. `result=ERROR` → git 조회 실패(fail-closed) — 통과로 취급하지 않고 보고 후 중단.
2. `git pull origin main --rebase` — 충돌 시 `git rebase --abort` 후 보고하고 중단. (ship Preflight에서 이미 rebase했으므로 보통 no-op이다 — Phase 2 검증이 도는 동안 main이 움직인 경우의 안전망.)
3. `git push --force-with-lease -u origin "<브랜치명>"`
4. PR 확보 — **기존 PR 확인부터** (재시작 시 같은 브랜치에 열린 PR이 이미 있다):
   - `gh pr list --head "<브랜치명>" --base main --state open --json number --jq '.[0].number // empty'` — 번호가 나오면 **생성을 스킵**하고 그 번호를 `<PR번호>`로 재사용한다 (3의 push만으로 같은 PR이 업데이트되고 체크가 재실행된다).
   - 없으면 `.github/PULL_REQUEST_TEMPLATE.md`를 채워 `gh pr create --base main --head "<브랜치명>" --title "[<타입>/#<이슈번호>] 설명"`:
     - **변경 사항**: 변경 요점을 불릿으로. **변경 타입**: 타입 표의 해당 항목 하나만 `[x]`. **테스트 방법**: Phase 2에서 실제 수행한 명령과 결과(`make lint`, `make test` 통과 개수·건너뜀 등). **참고 사항**: 마이그레이션 적용 필요, 환경변수 추가 등 머지 후 사람이 할 일 — 없으면 "없음". **연결된 이슈**: `close #<이슈번호>`. **체크리스트**: 실제 확인한 항목만 `[x]`, 환경변수 변경이 있으면 키 이름만 적는다.
     - 본문 끝에 현재 하네스의 **PR 본문 서명**(있으면)을 붙인다. **라벨·담당자를 붙이지 않는다.**
     - 출력 URL 끝 숫자가 `<PR번호>`.
5. 머지 게이트 폴링: **`automation/bin/pr-gate.sh <PR번호> 600 check gitleaks`** (30초 간격, 최대 10분) → `PR_GATE_DONE` 마커. **게이트 = `mergeable` MERGEABLE + 앵커 체크(`check`·`gitleaks`) 존재 + 비차단 예외를 제외한 모든 체크가 완료·성공 (실패 0, 대기 0, 체크 1개 이상)** — 정확한 판정 로직·비차단 예외 목록은 `automation/bin/pr-gate.jq`가 원천이다(`smoke-test.sh`가 fixture로 검증). 체크를 이름 허용목록으로 고르지 않으므로 `scripts` 등 다른 잡도 자동으로 게이트에 포함되고, 잡이 추가·개명돼도 따라간다. 이 클라이언트 게이트는 관측·진행 판단용이고, **강제는 `main` 브랜치 보호(required checks `check`·`gitleaks`)가 서버에서 한다** — CI 잡 이름을 바꾸면 브랜치 보호 설정도 함께 갱신한다. 의심스러우면 `gh api repos/lumi-wellness-agent/lumi-wellness-agent-be/branches/main/protection`으로 확인하고, 확인한 값과 다르면 이 문장을 고친다.
   - `result=PASS` → `gh pr merge <PR번호> --squash --subject "[TYPE] 설명 (#<PR번호>)"`. **`--auto`를 쓰지 않는 이유**: 머지를 서버에 위임하면 게이트 실패 시 아무 일도 일어나지 않은 채 대기만 남아 "실패 → 즉시 로그 요약·보고"라는 이 파이프라인의 실행 흐름이 끊긴다. 서버가 머지를 거부하면 `--admin`으로 우회하지 않고 게이트 실패로 보고한다.
   - `result=FAIL*` → 마커의 `failed=` 목록에 있는 체크의 로그를 요약해(`gh run view <run-id> --log-failed` 등) 보고하고 중단. `mergeable=CONFLICTING`·`TIMEOUT`·`API_ERROR`·`ERROR`도 각각 요약해 중단 (「폴링 실행 규칙」의 구분 참조).
6. `MERGE_SHA=$(gh pr view <PR번호> --json mergeCommit --jq '.mergeCommit.oid')`
7. `git checkout main && git pull origin main`
   - `main`이 기본 브랜치이므로 PR 본문의 `close #<이슈번호>`가 머지 시 이슈를 **자동으로 닫는다**. 따로 닫지 않는다.

## Step 2: main push CI 확인

`main` push(squash 머지)가 CI(`Backend CI`, `ci.yml`)를 다시 트리거한다. PR에서 이미 검증한 트리지만, main 쪽 런이 남아야 배포 대상 커밋의 검증 기록이 된다(`ci.yml`의 `concurrency` 주석).

CI 폴링: **`automation/bin/run-wait.sh ci.yml main "<MERGE_SHA>" 600`** (30초 간격, 최대 10분) → `RUN_RESULT` 마커.
- `result=success` → Step 3으로.
- `result=failure` 등 실패 conclusion → `main`이 깨진 상태다. 마커의 `run_id=`로 `gh run view <run-id> --log-failed`를 요약하고 **즉시 보고**한다 (「실패 시 재시작 규칙」).
- `result=NOT_FOUND` → run이 아예 생성되지 않았다 — 워크플로우 미트리거(트리거 조건·파일명 변경 등) 신호이므로 `TIMEOUT`(진행 중일 수 있음)과 구분해 보고하고 중단. `TIMEOUT`·`API_ERROR`·`ERROR`는 「폴링 실행 규칙」대로 처리.

> **CD 확장 지점**: FastAPI 호스트가 정해져 배포 워크플로우가 생기면 이 뒤에 Step을 추가한다 — `run-wait.sh <배포워크플로우.yml> main "<MERGE_SHA>" <타임아웃>`으로 같은 방식으로 기다린다.

## Step 3: 정리 & 보고

각 명령이 실패해도 **중단하지 않고 계속 진행**한다(실패 내역만 기록):
1. `git branch -D "<브랜치명>"`
2. `git push origin --delete "<브랜치명>"` — 레포 설정이 머지 시 원격 브랜치를 자동 삭제하므로 보통 "remote ref does not exist"다. 무시한다.
3. 최종 보고:
   - main 머지 커밋(`MERGE_SHA`)·main CI 결과, 닫힌 이슈 번호
   - **마이그레이션 적용 안내**(해당 시): `supabase/migrations/<파일>`을 Supabase에 적용해야 한다 — 대시보드 SQL Editor 또는 `psql "$DATABASE_URL" -f <파일>` (README 「데이터베이스」). 적용 전까지 관련 기능은 동작하지 않는다.
   - 정리 실패 내역(있으면). 머지 성공 + 정리 실패는 파이프라인 실패로 취급하지 않는다.
   - **프로덕션 배포는 없다** — CD가 없으므로 "배포"는 `main` 반영까지다. 배포됐다고 오해하지 않도록 명시한다.

## 실패 시 재시작 규칙

이슈 브랜치와 PR은 재사용한다 — 수정 커밋을 **같은 브랜치에 push**하면 CI 체크가 재실행되고 같은 PR이 업데이트된다. **코드 수정이 수반되면 반드시 Step 1부터 다시 거친다 — CI 검증 없는 머지 금지.**

| 실패 시점 | 원인 | 재시작 |
|-----------|------|--------|
| Step 1-5 | CI 체크 실패 | 코드 수정 → **Step 1** (같은 브랜치에 새 커밋 push, 게이트 폴링 재개) |
| Step 1-5 | PR 충돌(`CONFLICTING`) | `git pull origin main --rebase`로 해결(자동 해결 금지 — 충돌 내용을 보고하고 사용자가 정한다) → **Step 1** |
| Step 2 | main push CI 실패 | `main`이 깨졌다. 즉시 보고하고 사용자 판단으로 수정 커밋(새 이슈·브랜치) → **ship 처음부터** |

- 재시작에 새 브랜치가 필요하면 같은 이슈 번호로 `fix/#<이슈번호>`를 쓴다 (기존 로컬 브랜치는 삭제 후 생성).

## DB 마이그레이션 원칙 (Supabase 공용 DB)

- Supabase DB는 팀이 함께 쓰므로 **`main`에 머지된 마이그레이션만 적용**하고, PR 단계의 스키마 변경은 로컬 Postgres(README의 `pgvector/pgvector` Docker 절차)에서 먼저 확인한다. 적용은 사람이 한다(SQL Editor 또는 `psql`) — ship은 Step 3에서 안내만 한다.
- 파일명은 Supabase CLI 규칙(`supabase/migrations/<YYYYMMDDHHMMSS>_<이름>.sql`)을 따른다.
- **파괴적 변경은 2단계로 나눈다.** 파괴적 마이그레이션은 앱 코드를 되돌려도 이미 적용된 스키마가 되돌아가지 않아, 구버전 코드가 사라진 컬럼·테이블을 참조하며 런타임 오류를 낸다.
  - **2단계 필수**: `DROP TABLE/COLUMN`, 타입 축소, `RENAME COLUMN/TABLE`, 기존 데이터가 있는 컬럼의 `ADD NOT NULL`
  - **1단계로 충분**: `CREATE TABLE`, `ADD COLUMN`(nullable/default), 타입 확장, 인덱스·확장(extension) 추가
  - 분리 방법 — **1차**: 앱 코드에서 기존 스키마 참조 제거 + additive 마이그레이션(있으면). **2차**: 파괴적 마이그레이션(1차가 적용돼 안정된 뒤 별도 ship).
- 세션 체크포인트 테이블(`langgraph` 스키마)은 마이그레이션 파일이 아니라 `make checkpoint-setup`(라이브러리 마이그레이션)이 만든다 — `langgraph-checkpoint-postgres`를 올린 변경이면 머지 후 재실행을 안내한다.

## 환경변수 추가 체크리스트

**새 환경변수** 도입 시 머지 전 아래를 모두 확인한다 (누락 시 앱 시작 실패 또는 기능 무동작):

1. `.env.example` — 키·주석 추가 (실값 금지. `.env.example`만 커밋 허용이다)
2. `.env` — 로컬 값 추가 (커밋 금지)
3. `app/config.py` — 설정 모델에 필드 추가 (비어 있을 때의 동작을 정한다 — `DATABASE_URL`처럼 없어도 뜨는지, 필수인지)
4. `README.md` — 관련 절에 설명
5. 배포 호스트의 시크릿 — **아직 CD가 없다.** 호스트가 정해지면 등록 절차(예: `gh secret set`, 호스트 대시보드)를 여기에 추가한다. PR 본문 「참고 사항」에 키 이름만 적는다.

## 민감 파일 커밋 금지

`git add/commit/push` 대상에 아래 민감 파일이 포함되지 않도록 커밋 전 확인한다 (기본 이름 및 `<이름>.*` 확장 변형 포함). 기계적 검사는 Step 1의 **`automation/bin/sensitive-gate.sh`**(스테이징 후 `git diff --cached` 검사)가 수행한다 — **스크립트의 정규식이 패턴의 단일 원천**이고, 아래 목록과 훅(`automation/hooks/validate-git-sensitive.sh`)의 배열은 사람용 요약/이중 방어다(불일치하면 스크립트가 우선). 패턴 변경은 스크립트에서 하고 훅을 같이 갱신한다:

- `.env` (및 `.env.local`, `.env.prod` 등 `.env.*` — `.env.example` 제외)
- `.claude/settings.local.json` (Claude Code 개인 권한 설정)
- `.mcp.json` (로컬 IDE MCP 연결 — 개인 환경 전용)
- `*.pem`, `*.p12`, `*.p8`, `*.key`
- `firebase-adminsdk*.json`, GCP 서비스 계정 키 등 자격증명 JSON
- `id_rsa*`

발견 시 커밋하지 말고 사용자에게 보고한다 — 스테이징돼 있으면 `git reset HEAD <file>`로 해제하고, 이미 커밋에 포함됐으면 해당 커밋을 정리한 뒤 재시도한다.

> **하네스별 적용**: 모든 하네스가 Step 1에서 `sensitive-gate.sh`를 실행한다. 훅을 지원하는 하네스(Claude Code `.claude/settings.json`, Codex `.codex/hooks.json`)는 같은 스크립트 `automation/hooks/validate-git-sensitive.sh`가 PreToolUse에서 `git add/commit/push`를 추가로 자동 차단한다(이중 방어). Codex 훅은 프로젝트 신뢰 + `/hooks` 승인 뒤에만 동작하므로, 승인 전에는 규칙을 직접 준수한다. 어느 하네스든 `.gitignore` + GitHub push protection + CI 단 gitleaks와 병행되는 것을 전제로 한다.

## 규칙

- 모든 `gh`/`git` 실패 시 에러 내용을 사용자에게 보고한다.
- PR 생성 시 `.github/PULL_REQUEST_TEMPLATE.md` 형식을 준수하고, **라벨·담당자는 부여하지 않는다**(라벨은 이슈 몫이다).
- **머지는 항상 `--squash` + `--subject`다** — `--merge`·`--rebase`로 바꾸지 않는다. 이유는 「제목 규범」 참조.
- **CI 체크 통과 없이 머지하지 않는다.** 브랜치 보호가 이를 서버에서도 강제한다 — 머지가 거부되면 `--admin`으로 우회하지 말고 게이트 실패로 보고한다.
- **`main`에 직접 커밋·push하지 않는다.** 변경은 반드시 이슈 브랜치 → PR을 거친다.
- **실패 시 재시작 규칙을 반드시 따른다.**
- **「민감 파일 커밋 금지」 규칙을 준수한다.**
