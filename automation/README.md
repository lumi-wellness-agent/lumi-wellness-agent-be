# ship 준비

ship은 코드 수정을 끝낸 변경을 검증하고 이슈 → 브랜치 → PR → CI → `main` 머지까지 올리는 자동화입니다
(README 「브랜치·PR 규칙」). 이 문서는 ship을 처음 쓰는 PC에서 한 번 해 둘 준비를 정리합니다.
절차는 LLM 하네스가 읽는 명세 [`ship.md`](ship.md)·[`pipeline.md`](pipeline.md)에 있고, 하네스별 진입점은
Claude Code [`.claude/commands/ship.md`](../.claude/commands/ship.md), Codex
[`.agents/skills/ship/SKILL.md`](../.agents/skills/ship/SKILL.md)입니다.

## 1. 공통

| 도구 | macOS · Linux · WSL | Windows (PowerShell) |
| --- | --- | --- |
| git | 대부분 기본 설치 | `winget install Git.Git` |
| GitHub CLI | `brew install gh` · [Linux 설치 안내](https://github.com/cli/cli/blob/trunk/docs/install_linux.md) | `winget install GitHub.cli` |
| Python 3.12 이상 | `brew install python@3.12` · `sudo apt install python3 python3-venv` | `winget install Python.Python.3.12` |
| make | macOS는 Xcode Command Line Tools에 포함 · `sudo apt install make` | 필요 없음 |
| jq (훅용) | `jq --version`으로 확인, 없으면 `brew install jq` · `sudo apt install jq` | Codex는 필요 없음 · Claude Code는 `winget install jqlang.jq` |

WSL에서는 WSL 안에 설치한 git·gh·Python을 씁니다. Windows 쪽 설치·로그인과는 별개입니다.

1. **GitHub 로그인**: `gh auth login`을 실행해 GitHub.com → HTTPS를 고르고, "Authenticate Git with your
   GitHub credentials?"에 Yes로 답합니다. 그래야 ship의 `git push`가 통과합니다. `gh auth status`로 확인합니다.
2. **커밋 작성자**: `git config --global user.name "이름"`, `git config --global user.email "GitHub 계정 이메일"`
3. **레포 권한**: ship이 브랜치 push·이슈 생성·PR 머지를 하므로 이 레포에 write 권한이 있어야 합니다.
4. **가상환경과 검증**: 레포 루트에서 아래를 실행합니다. 여기서 통과해야 ship의 검증 단계도 통과합니다.

   ```bash
   # macOS · Linux · WSL
   python3 -m venv .venv
   .venv/bin/pip install -e ".[dev]"
   make lint && make test
   ```

   ```powershell
   # Windows (PowerShell)
   py -3.12 -m venv .venv
   .venv\Scripts\pip install -e ".[dev]"
   .venv\Scripts\python -m ruff check .
   .venv\Scripts\python -m pytest
   ```

## 2. Claude Code로 쓸 때

- 레포를 처음 열 때 폴더를 신뢰하면 `.claude/settings.json`의 민감 파일 차단 훅이 함께 적용됩니다.
  훅은 bash·jq로 돌아서 jq가 있어야 합니다.
- Windows 네이티브 Claude Code는 Git for Windows(Git Bash)가 있어야 합니다.
- 코드 수정을 끝낸 뒤 `/ship`으로 부릅니다. `/ship feat 세션 시작 API 추가`처럼 타입과 설명을 주면 확인
  단계를 건너뜁니다.

## 3. Codex로 쓸 때

Codex에는 사용자 정의 슬래시 커맨드가 없어 `/ship`은 동작하지 않습니다. 레포 루트에서 Codex를 띄우고
`$ship`으로 부릅니다.

1. **네트워크 열기**: 사용자 설정 파일(macOS·Linux·WSL은 `~/.codex/config.toml`, Windows는
   `%USERPROFILE%\.codex\config.toml`)에 아래를 넣습니다. 이미 `[sandbox_workspace_write]`가 있으면 그 아래에
   키만 추가합니다. ship은 CI를 기다리는 동안 `gh`를 계속 부르므로, 이게 없으면 대기 단계가 바로
   `result=API_ERROR`로 끝납니다.

   ```toml
   [sandbox_workspace_write]
   network_access = true
   ```

2. **`.git` 쓰기 열기**: `.codex/config.toml.example`을 `.codex/config.toml`로 복사하고(PowerShell은
   `Copy-Item`), `writable_roots`를 이 체크아웃의 `.git` 절대경로로 바꿉니다. 경로는 레포 루트에서 `pwd`
   (PowerShell은 `(Get-Location).Path`)로 확인합니다. 이게 없으면 ship의 커밋·브랜치 전환·pull이 샌드박스에
   막힙니다. 이 파일은 머신마다 달라 커밋하지 않습니다(`.gitignore` 대상).

   ```toml
   # macOS · Linux · WSL
   writable_roots = ["/절대/경로/lumi-wellness-agent-be/.git"]
   # Windows — 작은따옴표로 씁니다. 큰따옴표면 백슬래시를 \\로 적어야 합니다.
   writable_roots = ['C:\Users\<사용자>\…\lumi-wellness-agent-be\.git']
   ```

3. **처음 실행**: 레포 루트에서 `codex`를 띄우면 프로젝트를 신뢰할지 묻습니다. 신뢰해야 `.codex/`의 설정과
   훅을 읽습니다.
   - macOS · Linux · WSL: 시작할 때 "hooks need review"가 보이면 `/hooks`에서 `validate-git-sensitive.sh`
     훅을 승인합니다.
   - Windows: 훅을 **승인하지 않습니다.** bash 스크립트라 PowerShell에서 돌지 않습니다. 민감 파일은 ship 안의
     게이트(`automation/bin/sensitive_gate.py`)가 검사합니다.
4. **확인**: `$`를 입력했을 때 목록에 `ship`이 보이면 준비가 끝났습니다. `source-command-ship`이 보이면
   고르지 않습니다. Codex가 `.claude/commands/ship.md`를 변환해 만든 사본이라 경로가 깨져 있습니다.

Codex 데스크톱 앱에서 에이전트를 WSL로 돌리면(앱 설정에서 전환 후 재시작) macOS · Linux · WSL 기준을 따릅니다.

## 4. 처음 한 번 시험하기

오타 수정 같은 작은 문서 변경을 만들고 `$ship doc <설명>`(Claude Code는 `/ship doc <설명>`)으로 끝까지
올려 봅니다. 실제로 이슈·PR이 만들어지고 `main`에 머지되므로 의미 있는 변경으로 합니다.

- CI를 기다리는 동안 명령 하나가 최대 10분까지 응답 없이 붙잡혀 있는 것은 정상입니다. 중간에 끊지 않습니다.
- Codex가 명령 승인을 물으면 내용을 확인하고 승인합니다.
- 이 문서대로 했는데 동작이 다르면, 확인한 내용으로 이 파일을 고쳐 둡니다.

## 막힐 때

| 증상 | 원인과 해결 |
| --- | --- |
| 시작하자마자 `gh auth status` 실패로 멈춤 | gh에 로그인하지 않았습니다 → `gh auth login` |
| 커밋·브랜치 전환에서 `.git` 쓰기 거부 (Codex) | 3-2의 `writable_roots`가 없거나 경로가 다릅니다 |
| CI 대기가 바로 `result=API_ERROR`로 끝남 | Codex는 3-1의 `network_access`를, 그 밖에는 gh 인증·네트워크를 확인합니다 |
| `result=ERROR`와 "gh를 찾을 수 없다" | gh가 설치되지 않았거나 PATH에 없습니다 |
| `$` 목록에 `ship`이 없음 (Codex) | 레포 밖에서 띄웠거나 프로젝트를 신뢰하지 않았습니다 |
| 훅이 `bash\r: No such file or directory`로 실패 (WSL·Git Bash) | Windows git으로 받아 CRLF가 된 사본입니다 → 파일을 지우고 `git checkout -- <파일>`로 다시 받습니다 |
