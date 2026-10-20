# netbox component targets (owner: 03-netbox). Included by the root Makefile; `make help` lists them.

NETBOX_SERVICES := netbox netbox-worker netbox-postgres netbox-redis netbox-redis-cache
SEED_ARGS       ?=

.PHONY: netbox-up netbox-down netbox-bootstrap netbox-seed netbox-plan netbox-prune netbox-render netbox-bsm-spec \
        netbox-ps netbox-logs netbox-shell netbox-test netbox-test-integration

netbox-up: env net ## Start NetBox (+ worker, PostgreSQL, Valkey), wait until healthy, provision the API tokens
	$(COMPOSE) up -d --wait --wait-timeout 1200 $(NETBOX_SERVICES)
	$(UV) run netbox-bootstrap

netbox-down: ## Stop and remove the NetBox containers (volumes, i.e. the database, are kept)
	$(COMPOSE) rm --stop --force $(NETBOX_SERVICES)

netbox-bootstrap: ## Wait for NetBox, then create/verify NETBOX_TOKEN_RW and NETBOX_TOKEN_RO (written into .env)
	$(UV) run netbox-bootstrap

netbox-seed: netbox-bootstrap ## Seed NetBox from contract/topology.yaml (idempotent; SEED_ARGS="--prune" etc.)
	$(UV) run netbox-seed $(SEED_ARGS)

netbox-plan: netbox-bootstrap ## Show what netbox-seed would change, without changing anything (--dry-run --prune)
	$(UV) run netbox-seed --dry-run --prune

netbox-prune: netbox-bootstrap ## Seed and remove netsim-lab tagged objects that are not in the topology
	$(UV) run netbox-seed --prune

netbox-render: ## Render the intended config of every device from NetBox into build/intent/ (and validate it)
	$(UV) run netbox-intent render-all --out build/intent --yanglint

netbox-bsm-spec: ## Print the BSM spec built from NetBox (contract section 9.3)
	$(UV) run netbox-intent bsm-spec

netbox-ps: ## NetBox containers and their health
	$(COMPOSE) ps $(NETBOX_SERVICES)

netbox-logs: ## Follow NetBox logs (S=<service> for one service)
	$(COMPOSE) logs -f --tail=200 $(or $(S),$(NETBOX_SERVICES))

netbox-shell: ## NetBox's Python shell (nbshell) in the netbox container
	$(DOCKER) exec -it netbox /opt/netbox/netbox/manage.py nbshell

netbox-test: ## NetBox tools unit tests (no containers)
	$(UV) run pytest netbox/tools/tests $(PYTEST_ARGS)

netbox-test-integration: ## Integration tests against a throwaway NetBox stack (NETBOX_IT_URL=... to reuse one)
	$(UV) run pytest netbox/tests/integration -m integration -p no:cacheprovider $(PYTEST_ARGS)
