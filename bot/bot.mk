# bot component targets (owner: 07-grok-bot; three NOC roles since 09-noc-roles). Included by the root Makefile;
# `make help` lists them.
#
# make e2e also runs bot-e2e (prerequisite added below): the office on the live lab with the routine sink standing in
# for the three Grok Bot routines and a scripted role player driving /mcp, so the Definition of Done needs no cloud.

BOT_IMAGE := netsim/grok-bot:$(shell sed -n 's/^version = "\(.*\)"/\1/p' bot/pyproject.toml)
BOT_URL   ?= http://127.0.0.1:8095

.PHONY: bot-build bot-up bot-down bot-logs bot-test bot-e2e bot-status bot-watch bot-open bot-sink-up bot-sink-down \
        bot-tunnel

bot-build: ## Build the grok-bot image (also the routine sink's)
	$(DOCKER) build -f bot/Dockerfile -t $(BOT_IMAGE) .

bot-up: env net ## Start grok-bot (the NOC room at http://127.0.0.1:8095) and wait until it is healthy
	$(COMPOSE) up -d --wait --wait-timeout 90 grok-bot
	@echo "NOC room: $(BOT_URL)"

bot-down: ## Stop and remove grok-bot (its volume, i.e. incidents and activity, is kept)
	$(COMPOSE) rm --stop --force grok-bot

bot-logs: ## Follow grok-bot's logs
	$(COMPOSE) logs -f --tail=200 grok-bot

bot-test: ## grok-bot unit tests (no containers, no network)
	$(UV) run pytest bot/tests $(PYTEST_ARGS)

bot-e2e: lab-wait-healthy ## The office end to end on the running lab, routine sink + scripted roles (~10 min)
	$(COMPOSE) --profile sink up -d --wait --wait-timeout 60 routine-sink
	$(UV) run pytest bot/e2e -m e2e -p no:cacheprovider -rA $(PYTEST_ARGS)

bot-sink-up: ## Start the routine sink (172.30.10.61:8096) and send every page there (try the office without the app)
	$(COMPOSE) --profile sink up -d --wait --wait-timeout 60 routine-sink
	$(UV) run botctl sink on

bot-sink-down: ## Pages go to the routines again; remove the routine sink
	-$(UV) run botctl sink off
	$(COMPOSE) --profile sink rm --stop --force routine-sink

bot-tunnel: ## A Cloudflare quick tunnel to /mcp (needs cloudflared): prints the three roles' connector configs
	$(UV) run botctl tunnel

bot-status: ## botctl status: roles, open incidents, change tickets in review and waiting for a human
	$(UV) run botctl status

bot-watch: ## botctl watch: the office's activity, live
	$(UV) run botctl watch

bot-open: ## Print the NOC room URL
	@echo "$(BOT_URL)"

# The root `make e2e` runs the bot's end-to-end tests first, then lab-e2e (which pauses the bot while it runs).
e2e: bot-e2e
