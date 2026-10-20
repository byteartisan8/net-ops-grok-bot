# lab component targets (owner: 05-integration). Included by the root Makefile; `make help` lists them.
#
# make reset -> lab-reset, make e2e -> lab-e2e (contract section 3). `make seed` first waits until every container
# is healthy (prerequisite added below), so `make up && make seed` works on a clean host.

LAB_IMAGE := netsim/lab-api:$(shell sed -n 's/^version = "\(.*\)"/\1/p' lab/pyproject.toml)
E2E_ARGS  ?=

.PHONY: lab-build lab-up lab-down lab-logs lab-wait-healthy lab-wait lab-reset lab-e2e lab-test lab-openapi lab-check \
        lab-status lab-drift

lab-build: ## Build the lab-api image
	$(DOCKER) build -f lab/Dockerfile -t $(LAB_IMAGE) .

lab-up: env net ## Start lab-api and wait until it is healthy
	$(COMPOSE) up -d --wait --wait-timeout 60 lab-api

lab-down: ## Stop and remove lab-api (its volume, i.e. the audit log, is kept)
	$(COMPOSE) rm --stop --force lab-api

lab-logs: ## Follow lab-api's logs
	$(COMPOSE) logs -f --tail=200 lab-api

lab-wait-healthy: ## Wait until every lab container is running and healthy (clear error naming the ones that are not)
	$(UV) run labctl wait-healthy --timeout 900

lab-wait: lab-wait-healthy ## Wait until the lab is green: every PW up (simd), every service OK (Zabbix)
	$(UV) run labctl wait --timeout 300

# Bring-up order: containers healthy -> NetBox seed -> Zabbix provision (which waits for LLD, then builds the map).
seed: lab-wait-healthy

lab-reset: lab-wait-healthy ## Golden config everywhere, faults cleared, NetBox/Zabbix back to the topology, wait green
	$(UV) run labctl reset

lab-e2e: lab-wait-healthy ## End-to-end tests on the running lab (every scenario, timings, API, persistence; ~14 min)
	$(UV) run pytest lab/e2e -m e2e -p no:cacheprovider -rA $(E2E_ARGS) $(PYTEST_ARGS)

lab-test: ## lab unit tests (no containers)
	$(UV) run pytest lab/tests $(PYTEST_ARGS)

lab-openapi: ## Export lab-api's OpenAPI document to lab/openapi.json
	$(UV) run lab-api --openapi lab/openapi.json

lab-check: ## Verify lab/openapi.json is current
	$(UV) run lab-api --openapi lab/openapi.json --check

lab-status: ## labctl status --watch (intent, live state, Zabbix side by side)
	$(UV) run labctl status --watch

lab-drift: ## labctl drift (exit 1 on drift)
	$(UV) run labctl drift
