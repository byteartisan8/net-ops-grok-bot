# bot component targets (owner: 07-grok-bot). Included by the root Makefile; `make help` lists them.
#
# make e2e also runs bot-e2e (prerequisite added below): the bot's own loop on the live lab with the deterministic
# rules brain, so the Definition of Done needs no xAI key. bot-eval is the opt-in live Grok check (costs money).

BOT_IMAGE := netsim/grok-bot:$(shell sed -n 's/^version = "\(.*\)"/\1/p' bot/pyproject.toml)
BOT_URL   ?= http://127.0.0.1:8095

.PHONY: bot-build bot-up bot-down bot-logs bot-test bot-e2e bot-eval bot-status bot-watch bot-grok-check bot-open

bot-build: ## Build the grok-bot image
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

bot-e2e: lab-wait-healthy ## grok-bot end to end on the running lab: incidents, approvals, field repair, chat (~4 min)
	$(UV) run pytest bot/e2e -m e2e -p no:cacheprovider -rA $(PYTEST_ARGS)

bot-eval: lab-wait-healthy ## OPT-IN, costs money: Grok investigates real scenarios; graded against the scenario files
	BOT_EVAL=1 $(UV) run pytest bot/e2e/test_grok_eval.py -m e2e -s -p no:cacheprovider -rA $(PYTEST_ARGS)

bot-status: ## botctl status: brain, autonomy, crew, open incidents, pending approvals
	$(UV) run botctl status

bot-watch: ## botctl watch: the crew's activity, live
	$(UV) run botctl watch

bot-grok-check: ## One function-calling round trip with XAI_API_KEY and GROK_MODEL from .env
	$(UV) run botctl grok-check

bot-open: ## Print the NOC room URL
	@echo "$(BOT_URL)"

# The root `make e2e` runs the bot's end-to-end tests first, then lab-e2e (which pauses the bot while it runs).
e2e: bot-e2e
