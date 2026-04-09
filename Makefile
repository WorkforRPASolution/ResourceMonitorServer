.PHONY: install test-fast test-integration test-e2e test-full test-watch lint fmt clean \
        dev-up dev-down dev-status dev-clean-test docker-build

ARS_COMPOSE := /Users/hyunkyungmin/Developer/ARS/docker/docker-compose.yml

install:
	pip install -e ".[dev]"

# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------
test-fast:
	pytest tests/unit -m unit -x -q

test-integration: dev-up
	pytest tests/unit tests/integration --ignore=tests/e2e

test-e2e: dev-up                 ## E2E — real uvicorn subprocess + multi-instance ZK
	pytest tests/e2e -m e2e -v

test-full: dev-up                ## Unit + integration + e2e against OrbStack
	pytest tests/

test-watch:
	ptw -- -m unit -x -q

# ----------------------------------------------------------------------
# Dev infrastructure (OrbStack)
# ----------------------------------------------------------------------
dev-up:                          ## ZK 3.5.5 + ES 7.11.x + Redis 5.0.6 기동
	docker-compose -f $(ARS_COMPOSE) up -d redis zookeeper elasticsearch
	@echo "waiting for ZK + ES healthy..."
	@for i in 1 2 3 4 5 6 7 8 9 10 11 12; do \
		if docker exec ars-zookeeper sh -c 'echo ruok | nc -w 2 localhost 2181' 2>/dev/null | grep -q imok \
		   && curl -sf http://localhost:9200/_cluster/health > /dev/null 2>&1; then \
			echo "ready"; exit 0; \
		fi; \
		echo "  attempt $$i/12..."; sleep 5; \
	done; \
	echo "TIMEOUT — check 'make dev-status'"; exit 1

dev-down:                        ## ZK + ES만 정지 (Redis는 다른 프로젝트 공유)
	docker-compose -f $(ARS_COMPOSE) stop zookeeper elasticsearch

dev-status:                      ## 개발 인프라 상태
	@docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' \
		| grep -E 'NAMES|ars-(redis|zookeeper|elasticsearch)|mongodb-44' || true

dev-clean-test:                  ## 테스트 namespace 잔재 청소
	@echo "cleaning Mongo test DBs..."
	@docker exec mongodb-44 mongo --quiet --eval \
		'db.adminCommand("listDatabases").databases.filter(d => /^EARS_test_/.test(d.name)).forEach(d => db.getSiblingDB(d.name).dropDatabase())' \
		2>/dev/null || true
	@echo "cleaning Redis test keys..."
	@docker exec ars-redis sh -c 'redis-cli --scan --pattern "RESOURCE_ALERT_test_*" | xargs -r redis-cli del' \
		2>/dev/null || true
	@echo "cleaning ZK test trees..."
	@docker exec ars-zookeeper zkCli.sh deleteall /resource-monitor-test 2>/dev/null || true
	@echo "done."

# ----------------------------------------------------------------------
# Docker / build
# ----------------------------------------------------------------------
docker-build:
	docker build -t resource-monitor-server:dev .

# ----------------------------------------------------------------------
# Lint / format / clean
# ----------------------------------------------------------------------
lint:
	ruff check src tests

fmt:
	ruff format src tests

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +
	rm -rf .coverage htmlcov build dist *.egg-info
