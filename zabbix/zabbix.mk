# zabbix component targets (owner: 04-zabbix). Included by the root Makefile; `make help` lists them.

ZABBIX_SERVICES := zabbix-postgres zabbix-server zabbix-web zabbix-snmptraps
SYNC_ARGS       ?=
PROVISION_ARGS  ?=
VARIANT         ?= healthy

.PHONY: zabbix-images zabbix-up zabbix-down zabbix-bootstrap zabbix-template zabbix-check zabbix-provision zabbix-plan \
        zabbix-sync zabbix-status zabbix-dashboard-export zabbix-ps zabbix-logs zabbix-shell zabbix-test \
        zabbix-test-integration zabbix-test-fixtures zabbix-fixtures zabbix-fixtures-up zabbix-fixtures-down

zabbix-images: ## Build the Zabbix 8.0.0rc1 images from Zabbix's own Dockerfiles (skipped when present; ~7 min)
	zabbix/images/build-images.sh

# The 8.0.0rc1 images exist only after zabbix-images (no registry has them): make the root `make up` / `make build`
# build them first on a clean host (a no-op once they exist). Adds a prerequisite; the root recipes are unchanged.
up build: zabbix-images

zabbix-up: env net zabbix-images ## Start Zabbix (server, web, snmptraps, PostgreSQL), wait until healthy, bootstrap
	$(COMPOSE) up -d --wait --wait-timeout 300 $(ZABBIX_SERVICES)
	$(UV) run zabbix-bootstrap

zabbix-down: ## Stop and remove the Zabbix containers (volumes, i.e. the database, are kept)
	$(COMPOSE) rm --stop --force $(ZABBIX_SERVICES)

zabbix-bootstrap: ## Admin password, API user/role/token (written into .env), webhook script (idempotent)
	$(UV) run zabbix-bootstrap

zabbix-template: ## Regenerate zabbix/template/netsim_l2vpn_pe_snmp.yaml from contract/oids.yaml
	$(UV) run zabbix-template

zabbix-check: ## Verify the generated template is current
	$(UV) run zabbix-template --check

zabbix-provision: zabbix-bootstrap ## Template, hosts, trap action, BSM/SLA/map from NetBox, dashboard (PROVISION_ARGS=...)
	$(UV) run zabbix-provision $(PROVISION_ARGS)

zabbix-plan: ## What zabbix-provision would change, without changing anything
	$(UV) run zabbix-provision --dry-run $(PROVISION_ARGS)

zabbix-sync: ## Spec macros + BSM + SLAs + map from NetBox (fast, idempotent; SYNC_ARGS="--source topology" etc.)
	$(UV) run zabbix-sync $(SYNC_ARGS)

zabbix-status: ## BSM tree with statuses, current L2VPN problems, PW item summary
	$(UV) run zabbix-status

zabbix-dashboard-export: ## After editing the dashboard in the UI: write it back into zabbix/dashboards/ (then commit)
	$(UV) run zabbix-dashboard export

zabbix-ps: ## Zabbix containers and their health
	$(COMPOSE) ps $(ZABBIX_SERVICES)

zabbix-logs: ## Follow Zabbix logs (S=<service> for one service)
	$(COMPOSE) logs -f --tail=200 $(or $(S),$(ZABBIX_SERVICES))

zabbix-shell: ## psql in the Zabbix database
	$(DOCKER) exec -it zabbix-postgres sh -c 'psql -U "$$POSTGRES_USER" "$$POSTGRES_DB"'

zabbix-test: ## Zabbix tools unit tests (no containers)
	$(UV) run pytest zabbix/tools/tests $(PYTEST_ARGS)

zabbix-test-integration: ## Integration tests against the running, provisioned lab (faults, NetBox changes, timings)
	$(UV) run pytest zabbix/tests/integration -m integration -s -p no:cacheprovider $(PYTEST_ARGS)

zabbix-fixtures: ## Regenerate the snmpsim fixtures (zabbix/fixtures/<variant>/) from contract/oids.yaml + topology
	$(UV) run zabbix-fixtures render

zabbix-fixtures-up: ## Run snmpsim fixtures on the device IPs instead of the devices (VARIANT=healthy|pw-down|pw-missing|wrong-peer)
	$(UV) run zabbix-fixtures up --variant $(VARIANT)

zabbix-fixtures-down: ## Remove the fixture containers (start the real devices again with `make devices-up`)
	$(UV) run zabbix-fixtures down

zabbix-test-fixtures: ## DISRUPTIVE: stop the devices, check every snmpsim fixture variant in Zabbix, restart the devices
	NETSIM_ZABBIX_FIXTURES=1 $(UV) run pytest zabbix/tests/integration/test_fixtures.py -m integration -s -p no:cacheprovider $(PYTEST_ARGS)
