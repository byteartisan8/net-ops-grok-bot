# NetSim L2VPN lab - root Makefile (owner: 01-foundation). `make help` lists the targets.
#
# Component targets live in <dir>/<dir>.mk (devices/devices.mk, netbox/netbox.mk, zabbix/zabbix.mk,
# lab/lab.mk), created by their owners and included at the bottom. Until a component defines a target,
# `make devices-*`, `netbox-*`, `zabbix-*` or `lab-*` prints "not implemented yet (owner: ...)" and fails.
# An explicit rule in a .mk file always overrides those placeholders (no edits to this file needed).

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-builtin-rules

UV          ?= uv
DOCKER      ?= docker
COMPOSE     ?= $(DOCKER) compose
PYTEST_ARGS ?=
S           ?=
CONFIRM     ?=

# Lab network - must match contract/topology.yaml (tests/test_scaffolding.py checks it).
LABNET_NAME    := labnet
LABNET_SUBNET  := 172.30.10.0/24
LABNET_GATEWAY := 172.30.10.1

YANG_DIR  := contract/yang
YANG      := $(YANG_DIR)/netsim-device.yang
YANG_TREE := $(YANG_DIR)/netsim-device.tree
YANGLINT  := tools/yanglint.sh -D -p $(YANG_DIR)
FRAGMENTS := devices/compose.yaml netbox/compose.yaml zabbix/compose.yaml lab/compose.yaml bot/compose.yaml

# Fails with a clear message while every compose fragment is still a stub; $(1) = calling target.
define require_services
	@if [ -z "$$($(COMPOSE) config --services)" ]; then \
	  echo "make $(1): not implemented yet - every compose fragment is still a stub:" >&2; \
	  echo "  devices/ (owner: 02-device-simulator), netbox/ (03-netbox), zabbix/ (04-zabbix), lab/ (05-integration)" >&2; \
	  exit 1; \
	fi; \
	stubs="$$( { grep -l '^services: {}' $(FRAGMENTS) 2>/dev/null || true; } | xargs -r -n1 dirname | tr '\n' ' ')"; \
	if [ -n "$$stubs" ]; then echo "note: still stubs (skipped): $$stubs"; fi
endef

.PHONY: help doctor env sync net up down ps logs build seed reset test e2e lint lint-py lint-yang yang-clixon fmt \
        contract contract-check clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_%-]+:.*## / {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

doctor: ## Check host prerequisites (Docker Engine 24+, Compose v2.20+, uv)
	@fail=0; ge() { [ "$$(printf '%s\n%s\n' "$$2" "$$1" | sort -V | head -n1)" = "$$2" ]; }; \
	engine="$$($(DOCKER) version --format '{{.Server.Version}}' 2>/dev/null || true)"; \
	if [ -z "$$engine" ]; then echo "FAIL docker: daemon not reachable"; fail=1; \
	elif ge "$$engine" 24.0.0; then echo "ok   docker engine $$engine"; else echo "FAIL docker engine $$engine < 24"; fail=1; fi; \
	compose="$$($(COMPOSE) version --short 2>/dev/null | sed 's/^v//' || true)"; \
	if [ -z "$$compose" ]; then echo "FAIL docker compose plugin not found"; fail=1; \
	elif ge "$$compose" 2.20.0; then echo "ok   docker compose $$compose"; else echo "FAIL docker compose $$compose < 2.20"; fail=1; fi; \
	uvv="$$($(UV) --version 2>/dev/null | awk '{print $$2}' || true)"; \
	if [ -z "$$uvv" ]; then echo "FAIL uv not found (https://docs.astral.sh/uv/)"; fail=1; \
	elif ge "$$uvv" 0.13.1; then echo "ok   uv $$uvv"; else echo "FAIL uv $$uvv < 0.13.1"; fail=1; fi; \
	exit $$fail

env: ## Create .env from .env.example with random secrets (never overwrites existing values)
	@$(UV) run --no-project python tools/gen_env.py

sync: ## Install the Python workspace (all members + dev tools) into .venv
	$(UV) sync

net: ## Create the external Docker network labnet 172.30.10.0/24 (idempotent)
	@check() { \
	  subnet="$$($(DOCKER) network inspect -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' $(LABNET_NAME))"; \
	  gateway="$$($(DOCKER) network inspect -f '{{range .IPAM.Config}}{{.Gateway}}{{end}}' $(LABNET_NAME))"; \
	  if [ "$$subnet" != "$(LABNET_SUBNET)" ] || [ "$$gateway" != "$(LABNET_GATEWAY)" ]; then \
	    echo "ERROR: network $(LABNET_NAME) exists as $$subnet gw $$gateway, expected $(LABNET_SUBNET) gw $(LABNET_GATEWAY)." >&2; \
	    echo "       Remove it ('docker network rm $(LABNET_NAME)') and re-run 'make net'." >&2; \
	    return 1; \
	  fi; \
	}; \
	if $(DOCKER) network inspect $(LABNET_NAME) >/dev/null 2>&1; then \
	  check && echo "$(LABNET_NAME): already exists ($(LABNET_SUBNET), gateway $(LABNET_GATEWAY)) - nothing to do"; \
	elif $(DOCKER) network create --driver bridge --subnet $(LABNET_SUBNET) --gateway $(LABNET_GATEWAY) \
	       --label org.netsim.lab=labnet $(LABNET_NAME) >/dev/null; then \
	  echo "$(LABNET_NAME): created ($(LABNET_SUBNET), gateway $(LABNET_GATEWAY))"; \
	else \
	  check; \
	fi

up: env net ## Start every service defined so far (detached)
	$(call require_services,up)
	$(COMPOSE) up -d

down: ## Stop and remove containers (volumes are kept: running configs survive)
	$(COMPOSE) down

ps: ## Show lab containers
	$(COMPOSE) ps

logs: ## Follow logs (S=<service> for one service)
	$(COMPOSE) logs -f --tail=200 $(S)

build: ## Build the lab images (netsim-pe from 02, lab-api from 05, grok-bot from 07)
	$(call require_services,build)
	$(COMPOSE) build

seed: ## Seed NetBox from the topology, then provision Zabbix from NetBox
	@$(MAKE) --no-print-directory netbox-seed
	@$(MAKE) --no-print-directory zabbix-provision

reset: ## Every device back to golden config, all faults cleared (lab-reset, owner 05)
	@$(MAKE) --no-print-directory lab-reset

test: ## Unit tests (no running lab needed; YANG checks use the yanglint container)
	$(UV) run pytest -m "not integration and not e2e" $(PYTEST_ARGS)

e2e: ## End-to-end tests against the running lab (lab-e2e, owner 05)
	@$(MAKE) --no-print-directory lab-e2e

lint: lint-py lint-yang ## ruff (lint + format check), pyang --strict, yanglint

lint-py:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

lint-yang:
	$(UV) run pyang --strict -Werror -p $(YANG_DIR) $(YANG)
	$(YANGLINT) $(YANG)
	$(YANGLINT) -t config $(YANG) contract/golden/*.json

yang-clixon: ## Load the YANG into a real Clixon 7.9.0 and validate golden configs + examples (slow first build)
	tools/clixon-check.sh

fmt: ## Auto-format and auto-fix Python code
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

contract: ## Regenerate contract artifacts: golden configs, oids.yaml, JSON schemas, YANG tree
	$(UV) run python tools/render_golden.py
	$(UV) run python tools/gen_oids.py
	$(UV) run python tools/gen_schemas.py
	$(UV) run pyang -f tree -p $(YANG_DIR) -o $(YANG_TREE) $(YANG)

contract-check: ## Verify generated contract artifacts are current and MIB checksums match
	$(UV) run python tools/fetch_mibs.py --check
	$(UV) run python tools/render_golden.py --check
	$(UV) run python tools/gen_oids.py --check
	$(UV) run python tools/gen_schemas.py --check
	@diff -u $(YANG_TREE) <($(UV) run pyang -f tree -p $(YANG_DIR) $(YANG)) && echo "OK: $(YANG_TREE) is up to date"

clean: ## DANGER: remove lab containers AND ALL VOLUMES (device configs, NetBox/Zabbix DBs) and labnet
	@echo "WARNING: 'make clean' DELETES ALL LAB VOLUMES: device running configs (incl. drift), NetBox and Zabbix databases." >&2
	@if [ "$(CONFIRM)" != "yes" ]; then \
	  if [ -t 0 ]; then \
	    read -r -p "Type 'yes' to delete them: " answer; [ "$$answer" = yes ] || { echo "aborted" >&2; exit 1; }; \
	  else \
	    echo "refusing to delete volumes non-interactively; run 'make clean CONFIRM=yes'" >&2; exit 1; \
	  fi; \
	fi
	$(COMPOSE) down --volumes --remove-orphans
	@if $(DOCKER) network inspect $(LABNET_NAME) >/dev/null 2>&1; then \
	  $(DOCKER) network rm $(LABNET_NAME) >/dev/null && echo "removed network $(LABNET_NAME)"; fi
	rm -rf .pytest_cache .ruff_cache

# ------------------------------------------------------------------ component placeholders
devices-%:
	@echo "make $@: not implemented yet (owner: session 02-device-simulator; define it in devices/devices.mk)" >&2; exit 1

netbox-%:
	@echo "make $@: not implemented yet (owner: session 03-netbox; define it in netbox/netbox.mk)" >&2; exit 1

zabbix-%:
	@echo "make $@: not implemented yet (owner: session 04-zabbix; define it in zabbix/zabbix.mk)" >&2; exit 1

lab-%:
	@echo "make $@: not implemented yet (owner: session 05-integration; define it in lab/lab.mk)" >&2; exit 1

bot-%:
	@echo "make $@: not implemented yet (owner: session 07-grok-bot; define it in bot/bot.mk)" >&2; exit 1

COMPONENT_MKS ?= devices/devices.mk netbox/netbox.mk zabbix/zabbix.mk lab/lab.mk bot/bot.mk
-include $(COMPONENT_MKS)
