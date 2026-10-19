# Build prompts — run order

All sessions open **this same repo folder** in Cursor. Every prompt depends on
[`docs/CONTRACT.md`](../docs/CONTRACT.md): the shared spec for IPs, YANG shape, OIDs, status rules and ownership.

| Step | Prompt | Run | Depends on |
|---|---|---|---|
| 1 | [01-foundation.md](01-foundation.md) | alone, first | — |
| 2 | [02-device-simulator.md](02-device-simulator.md) | parallel | 1 |
| 2 | [03-netbox.md](03-netbox.md) | parallel | 1 |
| 2 | [04-zabbix.md](04-zabbix.md) | parallel | 1 (uses fixtures until 2 and 3 land) |
| 3 | [05-integration.md](05-integration.md) | last | 2, 3, 4 |

How to start a session: open a new Cursor agent chat (Opus 5.5, max) and send:

> Read `prompts/0N-<name>.md` and `docs/CONTRACT.md`, then execute the prompt end to end.

Or paste the prompt file's contents directly.

Coordination rules (already included in each prompt):
- Each session only edits the paths it owns (contract §3).
- A contract change must be recorded in the contract's change log.
- Each session finishes by writing `docs/reports/<NN-name>.md` (what was built, deviations, how to verify).
  Session 5 reads all the reports first.

The Grok bot itself is **not** built by these prompts. Session 5's `lab-api` (with its OpenAPI spec)
is the tool surface the bot will use later.
