# devices component targets (owner: 02-device-simulator). Included by the root Makefile; `make help` lists them.

DEVICES_IMAGE   := netsim/netsim-pe:$(shell sed -n 's/^version = "\(.*\)"/\1/p' devices/simd/pyproject.toml)
# Services of devices/compose.yaml (core + one per device), read from the generated file.
DEVICE_SERVICES := $(shell awk '/^services:/{s=1;next} /^[^ #]/{s=0} s && /^  [a-z0-9-]+:$$/{sub(":","",$$1);print $$1}' devices/compose.yaml)

.PHONY: devices-build devices-up devices-down devices-wait devices-ps devices-logs devices-compose devices-openapi \
        devices-check devices-test devices-test-integration devices-shell

devices-build: ## Build the netsim-pe image (Clixon 7.9.0 from source on first build)
	$(DOCKER) build -f devices/Dockerfile -t $(DEVICES_IMAGE) .

devices-up: env net ## Start core + every PE and wait until they are healthy and every PW is up
	$(COMPOSE) up -d $(DEVICE_SERVICES)
	$(UV) run netsim-devices wait --timeout 90

devices-down: ## Stop and remove core + the PEs (volumes, i.e. running configs, are kept)
	$(COMPOSE) rm --stop --force $(DEVICE_SERVICES)

devices-wait: ## Wait until every device is healthy and every PW is up (published simd ports)
	$(UV) run netsim-devices wait --timeout 90

devices-ps: ## Device containers and their health
	$(COMPOSE) ps $(DEVICE_SERVICES)

devices-logs: ## Follow device logs (S=<host> for one device)
	$(COMPOSE) logs -f --tail=200 $(or $(S),$(DEVICE_SERVICES))

devices-shell: ## Clixon CLI of a device: make devices-shell S=spoke-1
	$(DOCKER) exec -it $(or $(S),spoke-1) clixon_cli

devices-compose: ## Regenerate devices/compose.yaml from contract/topology.yaml
	$(UV) run netsim-devices render-compose

devices-openapi: ## Regenerate devices/simd/openapi.json
	$(UV) run netsim-devices openapi

devices-check: ## Verify generated device files (compose, OpenAPI) are current
	$(UV) run netsim-devices render-compose --check
	$(UV) run netsim-devices openapi --check

devices-test: ## simd unit tests (no containers; runs a local snmpsim)
	$(UV) run pytest devices/simd/tests $(PYTEST_ARGS)

devices-test-integration: ## Container integration tests on an isolated stack, with the timing report
	$(UV) run pytest devices/tests/integration -m integration -s -p no:cacheprovider $(PYTEST_ARGS)
