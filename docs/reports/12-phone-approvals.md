# Session 12 — Approve from your phone: the NOC Manager's links open a tailnet-only approvals page

**Date:** 2026-10-24. **Prompt:** none; scoped in conversation (the user asked for an approval button in the Grok Bot
app on Android, and chose "option 2": a link to an approval page that needs the user, reached over the tailnet).
**Branch:** `feature/phone-approvals`, from `3c52c06` (`main`, which now holds session 11 and the NOC room's sound);
uncommitted until the user asks.

## First: what you do (once)

`.env` already has `BOT_PHONE_URL=https://<tailnet-host>:8097` (appended by `make env`, then set to this computer's
tailnet name; no other value changed). grok-bot reads it live.

1. **Put the phone listener on your tailnet** (it needs root; nothing goes on the internet):

   ```bash
   sudo tailscale serve --bg --https=8097 http://127.0.0.1:8097
   ```

   Never `tailscale funnel` this port (Funnel cannot publish 8097 anyway: only 443, 8443 and 10000). To undo:
   `sudo tailscale serve --https=8097 off`. `uv run botctl phone setup` prints both lines again.
2. **On your phone:** the Tailscale app, signed in to the same tailnet.
3. **Sign the phone in:** NOC room, tab **Approvals**, **Sign in a phone**; scan the QR code with the phone's camera,
   open the link, press **Sign in**. (`uv run botctl phone pair` prints the same QR code in a terminal.)
4. **Check the whole path:**

   ```bash
   uv run botctl phone status
   ```

   It should show the listener, `tailscale serve` on `<tailnet-host>:8097` (tailnet only), the path through the
   tailnet, and your phone signed in.
5. **In the NOC Manager's Bot:** re-add [`SKILL.md`](../../bot/grokbot-app/SKILL.md) and
   [`skills/noc-manager/SKILL.md`](../../bot/grokbot-app/skills/noc-manager/SKILL.md), and add to its routine prompt:
   "End with what you decided, then the phone links review_ticket gave you, if any." (the new prompt is in
   [the README](../../bot/grokbot-app/README.md#4-three-routines-let-zabbix-wake-them)).
6. **Try it:** demo 6 in [`DEMOS.md`](../../bot/grokbot-app/DEMOS.md#6-approve-from-your-phone): an AC fault, then
   approve the field ticket from the NOC Manager's report on your phone.

## Why a link, and not a button inside the app

The app (`/opt/Grok Bot`, now 0.75.0) can show a server's question as an approval card: its MCP layer has
elicitation (`elicitation/create`, form and URL modes), and its approval items include an `mcp_elicitation` kind shown
as "Approval needed: <the server's text>"; it has notifications ("when something needs your attention or is ready for
review") and an "Auto-review" mode that checks actions against rules. A card would have been one tap.

But the answer to such a card comes back to grok-bot through the NOC Manager's connector, with the NOC Manager's
token. The Bot can get that token (it is in the connector's JSON, which can go through its chat) and has a shell on
its cloud computer, and Auto-review might answer for the user: grok-bot could not tell the user's tap from the Bot
approving its own proposal. That breaks the rule the NOC is built on: no role can approve or apply.

So the Bot hands the user a **link**, and the approval needs what only the user has: a phone signed in to the office,
on the user's own tailnet. The Bot's cloud computer is not on the tailnet, and a link approves nothing by itself.

## What was built

### 1. A second listener, for the phone only

- `grok-bot` serves two apps in one process with one engine (`bot/src/netsim_bot/api/server.py`): the office on
  8095 as before, the phone's app on 8097 (`BOT_PHONE_PORT`, 0 = off). `Listeners` (`api/phone.py`) sends each
  connection to one or the other by the port it came in on, so the phone's app never sees the office's routes
  (`/api`, `/mcp`, the room) and the office's port has no `/m/`.
- `bot/compose.yaml` publishes `127.0.0.1:8097:8097` (loopback only, like 8095). `tailscale serve --https=8097` makes
  it `https://<tailnet-host>:8097` on the tailnet, with Tailscale's certificate.

### 2. The pages (`bot/src/netsim_bot/api/phone.py`, `web/css/phone.css`)

Server-rendered, no script, one column, big buttons, light or dark like the phone:

- `/m/`: what waits for the operator, by incident (P1 first): the incident's customers, **Approve INC-xxxx** for all
  its proposed tickets (make before break) once none is still in review, each ticket with the NOC Manager's reason;
  then the tickets in review (a count) and the recent decisions.
- `/m/t/<CHG>`: one ticket: headline, who filed it, the NOC Manager's review (or "not reviewed"), the diff (or the
  field repair), the engineer's reason, the services it touches, the predicted drift; **Approve**, **Reject**, an
  optional comment. After a decision it follows the ticket (`approved` → `applying` → `applied`, verdict) by itself.
- `/m/i/<INC>`: the incident (triage, root cause, priority and reason) and all its tickets.
- The decision forms name the plan shown (`plan_id`; `Engine.approve` gained `plan_id=`): a ticket whose plan
  changed since is refused (409) and shown again. **Approve INC-xxxx** names the tickets shown, as the room's
  approve-all does.

### 3. Signing a phone in (`bot/src/netsim_bot/phone.py`)

- The operator makes a one-time sign-in link: `POST /api/phone/pair` (operator token; the NOC room's **Sign in a
  phone** and `botctl phone pair` use it). It returns the link and its QR code (an SVG `data:` URI, made with `segno`,
  a pure-Python QR library: the one new dependency). The link works once, for 10 minutes; codes live in memory (a
  restart voids them), five at most.
- The phone opens `/m/pair/<code>` and presses **Sign in** (a GET never uses the code up, so link previews cannot).
  It gets a cookie `noc_phone` with a random token: HttpOnly, SameSite=Lax, Secure over https, path `/m`, for
  `BOT_PHONE_SESSION_DAYS` (30). grok-bot stores the token's SHA-256 only (table `phones`).
- A phone is named at sign-in (`--label`), else by what its browser says it is (`android`, `iphone`, `browser`). Its
  decisions are `phone:<name>`: the ticket's `decided_by`, the room's activity, lab-api's audit
  (`grok-bot/<ROLE> approved-by:phone:<name>`).
- Signing out (at the bottom of every phone page, in the NOC room's phone list, `botctl phone sign-out`) forgets the
  phone at once; so does running out of days.

### 4. The guards

- Anything that came through Funnel (`Tailscale-Funnel-Request`) is refused (403): a second wall, should someone ever
  publish the port.
- `Host` must be `BOT_PHONE_URL`'s host or a loopback name (DNS rebinding).
- A POST must come from the page itself (`Origin`, or `Referer`; the pages send `Referrer-Policy: same-origin`, which
  keeps `Origin` on their own forms) and carry the phone's form value (derived from its token): no other site can
  make one.
- No office token opens the phone's pages: not a role's, a terminal's or the operator's. Only a signed-in phone.
- Every page is `no-store`, cannot be framed (`frame-ancestors 'none'`, `X-Frame-Options: DENY`) and loads nothing
  from elsewhere (CSP `default-src 'none'`); bodies over 64 kB are refused; the redirect after a decision goes only to
  the phone's own pages.

### 5. The NOC Manager's links

- With `BOT_PHONE_URL` set, `review_ticket` with `propose` returns
  `phone: {ticket: <url>/m/t/<CHG>, incident: <url>/m/i/<INC>, note}`.
- The tool's description, the role's MCP instructions, `skills/noc-manager/SKILL.md` (review and report sections) and
  the shared `SKILL.md` (rule 1) tell it: end the report with the links as Markdown, copied exactly; they open only on
  the operator's phone and approve nothing by themselves.
- The README's NOC Manager routine prompt ends with "then the phone links review_ticket gave you, if any".

### 6. The NOC room and botctl

- Approvals tab: a strip "📱 Your phone: N signed in · opens on your tailnet at …" with **Sign in a phone** (a dialog
  with the QR code, the three steps and a 10-minute countdown) and **Phones** (each with **Sign out**). When the phone
  signs in, the dialog closes the QR code by itself (the `control` activity "phone:<name> signed in to the
  approvals").
- `botctl phone setup` (the `tailscale serve` line and `BOT_PHONE_URL` for this computer, from `tailscale status`),
  `status` (listener, `tailscale serve` target, Funnel off, the path through the tailnet, the phones; exit 1 on a
  problem), `pair [--label]` (QR code in the terminal), `list`, `sign-out <id>|--all`.

## Deviations and choices

- **Port 8097, not 443 or 8443.** Funnel can only publish 443, 8443 and 10000, so the phone's port can never be put
  on the internet by mistake with the same command family that publishes `/mcp`. `BOT_PHONE_URL` on a Funnel port
  gives a warning.
- **A bad `BOT_PHONE_URL` is a warning, not a stop.** http other than loopback, a path or a bad port turn the links
  off and show the warning (`botctl status`, the room's warnings); the office keeps working.
- **No passkey.** A phone signs in once with a one-time QR code and keeps a cookie; together with the tailnet that is
  the bar. A passkey (fingerprint per approval) could be added later on the same page.
- **No push from grok-bot.** The alert on the phone is the app's own: the NOC Manager's routine run ending with the
  links. grok-bot sends nothing to the phone.
- **Found while testing:** a real dry run's `drift_after_predicted` holds objects; the engine stores their messages
  (`slim_plan`), and the page now shows either. Also, the phones list ordered two phones made in the same millisecond
  at random (now by id as well), and signed-out phones would have piled up in the list (they are forgotten now).

## Edits outside `bot/`

`.env.example` (`BOT_PHONE_URL`, `BOT_PHONE_SESSION_DAYS`), `docs/CONTRACT.md` (§4 port 8097, §9.6 the operator's
phone, §11, §12, two change-log rows), `README.md` (one sentence), `prompts/README.md` (step 10), `uv.lock`
(`segno`), this report. Local only: `.env` (`BOT_PHONE_URL`).

## Timings

| Path | Measured | Note |
|---|---|---|
| phone approval → field ticket applied (bot e2e, live lab) | 10.13 s (full run), 10.11 s (final build) | includes the 10 s field delay |

## Checks

- `make test`: 817 passed (807 before, plus 10 in `bot/tests/test_phone.py`); `make lint` clean.
- Full `make e2e` on the live lab (2026-10-24, 22 min 6 s): bot 16 passed (the new `test_90_phone` included), controller
  7 passed, lab 23 passed; every timing within its budget.
- After the last small fixes (a malformed `Content-Length`, decisions counted per ticket, the sign-in page naming the
  phone as it will be named), on the rebuilt image: `make bot-e2e PYTEST_ARGS="-k phone"` passed, `make test` 817
  passed, `make lint` clean.
- By hand: `botctl phone pair` (QR code in the terminal), sign-in and the pages at phone size in a browser (dark mode,
  no horizontal scroll at 375 px, 50 px buttons), the room's Approvals strip and dialog (the QR code clears itself when
  the phone signs in), `botctl phone status` (it reports the missing `tailscale serve`, exit 1).
- At the end: pages go to the routines again, the routine sink is removed, no test phone is left signed in.

## How to verify

```bash
uv run pytest bot/tests/test_phone.py -q
```

```bash
make bot-e2e PYTEST_ARGS="-k phone"
```

```bash
uv run botctl phone status
```

**Verified afterwards by the user (2026-10-25):** `tailscale serve` on 8097 and the phone loading the page over the
tailnet. On the first try the link kept loading: the phone resolved the name in public DNS (Funnel's address, no port
8097 there); fixed on the phone's DNS settings (README troubleshooting).

**Not verified:** how the Grok Bot app on Android opens a link (in Chrome, or in
a browser of its own without Chrome's cookies: then the page offers **Open it in Chrome**); whether the app notifies
the phone when the NOC Manager's routine run ends.
