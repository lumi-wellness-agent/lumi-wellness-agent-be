# 실행·검증 명령을 한곳에 모은다. 인자 전달: make test ARGS="tests/test_health.py -q"
# .venv가 있으면 그 인터프리터(Windows는 .venv/Scripts/python.exe)를, 없으면 PATH의 python3를 쓴다.

PY ?= $(firstword $(wildcard .venv/bin/python .venv/Scripts/python.exe) python3)

.PHONY: run lint test test-intake test-measure test-planner test-protocols graph checkpoint-setup protocols-scaffold seed-evidence

run:
	$(PY) -m uvicorn app.main:app --reload

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest $(ARGS)

# 역할별 테스트 (TEAM.md). 자기 디렉터리만 고치고 이 명령으로 확인한다
test-intake:
	$(PY) -m pytest tests/intake $(ARGS)

test-measure:
	$(PY) -m pytest tests/measure $(ARGS)

test-planner:
	$(PY) -m pytest tests/agent $(ARGS)

test-protocols:
	$(PY) -m pytest tests/protocols tests/schemas $(ARGS)

# 워크북(프로토콜·근거 arm 시트)에서 활동 노드 JSON 초안을 만든다. 있는 파일은 건너뛴다(--force로 덮어씀)
# 예: make protocols-scaffold XLSX=../Research_paper/LUMI_R_C_Research_2026-10-22/LUMI_측정도구_프로토콜_2026-10-23.xlsx
protocols-scaffold:
	$(PY) scripts/scaffold_protocols.py "$(XLSX)" $(ARGS)

# 근거 arm 시트 → papers · evidence_records 적재(README 「데이터베이스」). 대상 DB는 매번 ARGS로 적는다 —
# .env의 공용 Supabase에 실수로 쓰지 않도록 기본값이 없다. 다시 돌려도 id가 유지된다
# 예: make seed-evidence XLSX=<워크북> ARGS=--dry-run
#     make seed-evidence XLSX=<워크북> ARGS="--database-url postgresql://postgres:postgres@localhost:54329/postgres"
seed-evidence:
	$(PY) scripts/seed_evidence.py "$(XLSX)" $(ARGS)

# 세션 그래프(app/agent/graph.py)를 Mermaid로 출력한다. README·발표용
graph:
	$(PY) -m app.agent.graph

# .env의 DATABASE_URL에 세션 체크포인트 테이블(langgraph 스키마)을 만든다. 다시 돌려도 된다
checkpoint-setup:
	$(PY) -m app.agent.checkpoint
