# controller component targets (owner: 10-clixon-controller). Included by the root Makefile; `make help` lists them.
#
# `make build` and `make up` build and start the controller with everything else (it is in the root compose.yaml).

CONTROLLER_IMAGE := netsim/clixon-controller:$(shell sed -n 's/^ARG CONTROLLER_VERSION=\(.*\)/\1/p' controller/Dockerfile | head -n1)

.PHONY: controller-build controller-up controller-down controller-logs controller-shell controller-config \
        controller-check controller-test

controller-build: ## Build the Clixon controller image (first build compiles CLIgen, Clixon and the controller)
	$(DOCKER) build -f controller/Dockerfile -t $(CONTROLLER_IMAGE) .

controller-up: env net ## Start the controller and wait until it is healthy (it connects the PEs by itself)
	$(COMPOSE) up -d --wait --wait-timeout 120 controller

controller-down: ## Stop and remove the controller (its volume, i.e. its SSH keys, is kept)
	$(COMPOSE) rm --stop --force controller

controller-logs: ## Follow the controller's logs
	$(COMPOSE) logs -f --tail=200 controller

controller-shell: ## The controller CLI as admin, for people (`show connections`, `show devices spoke-1 diff`, quit)
	$(DOCKER) exec -it -u admin controller controller-cli

controller-config: ## Regenerate controller/startup_db (the PEs, NACM) from contract/topology.yaml
	$(UV) run python controller/startup.py

controller-check: ## Verify controller/startup_db is current
	$(UV) run python controller/startup.py --check

controller-test: lab-wait-healthy ## On the running lab: PEs connected and in sync, vcid-typo out of sync, NACM without lab-api
	$(UV) run pytest controller/tests/integration -m integration -p no:cacheprovider -rA $(PYTEST_ARGS)

# The root `make e2e` also runs the controller's tests on the running lab (after bot-e2e, before lab-e2e).
e2e: controller-test
