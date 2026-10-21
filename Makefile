# 실행·검증 명령을 한곳에 모은다. 인자 전달: make test ARGS="tests/test_health.py -q"
# .venv가 있으면 그 인터프리터를, 없으면 PATH의 python3를 쓴다.

PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: run lint test graph checkpoint-setup

run:
	$(PY) -m uvicorn app.main:app --reload

lint:
	$(PY) -m ruff check .

test:
	$(PY) -m pytest $(ARGS)

# 세션 그래프(app/agent/graph.py)를 Mermaid로 출력한다. README·발표용
graph:
	$(PY) -m app.agent.graph

# .env의 DATABASE_URL에 세션 체크포인트 테이블(langgraph 스키마)을 만든다. 다시 돌려도 된다
checkpoint-setup:
	$(PY) -m app.agent.checkpoint
