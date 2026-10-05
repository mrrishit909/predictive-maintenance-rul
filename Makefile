export DATABASE_URL ?= postgresql://app:app-dev-only@127.0.0.1:55106/app
PY = ./venv/bin/python
BASE = http://127.0.0.1:8106

bootstrap:      ## local virtualenv + database container
	python3 -m venv venv && ./venv/bin/pip install -q -r requirements-dev.txt
	docker compose up -d db
seed:           ## migrate and load the synthetic world (no-op if already seeded)
	$(PY) -m pm.seed --if-empty
dev:            ## API with reload on :8106, jobs run by a local worker
	($(PY) -m core.jobs pm.api &) && $(PY) -m uvicorn pm.api:app --reload --port 8106
test:
	$(PY) -m pytest --cov=pm --cov=core --cov-report=term-missing
demo:           ## the full stack in Docker, then the six-step scenario against it
	docker compose up --build -d --wait && $(PY) -m core.scenario $(BASE)
load-test:      ## run `make demo` first
	$(PY) -m core.loadtest $(BASE) acme-technician-demo 32 10 "GET /v1/assets" "GET /v1/assets/$(ASSET)/health" "GET /v1/assets/$(ASSET)/rul" "GET /v1/assets/$(ASSET)/spectrum"
reset:          ## drop all data (volume included)
	docker compose down -v
.PHONY: bootstrap seed dev test demo load-test reset
