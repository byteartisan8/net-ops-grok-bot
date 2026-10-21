"""Where the bot finds lab-api and Grok, and how it behaves.

Every value comes from the process environment, else the repository's ``.env``, else a default. ``.env`` is read
again on every :func:`load_settings` call (like lab-api), so an ``XAI_API_KEY`` added to ``.env`` while the bot runs
is used from the next investigation on, without a restart (the compose service mounts the repository read-only).

* ``BOT_LAB_API_URL``: lab-api (default ``http://127.0.0.1:8090``; the container uses ``http://172.30.10.50:8090``);
  ``LAB_API_TOKEN``: lab-api's write token (dry runs, approved changes, field repairs);
* ``XAI_API_KEY``: the xAI API key (empty: the rules brain runs instead of Grok); ``XAI_BASE_URL``
  (default ``https://api.x.ai/v1``); ``GROK_MODEL`` (default ``grok-4.7``); ``GROK_REASONING_EFFORT``
  (``low``, ``medium``, ``high``, ``xhigh``; empty = the model's default);
* ``BOT_BRAIN``: ``auto`` (Grok when a key is set, else rules), ``grok`` or ``rules``;
* ``BOT_AUTONOMY``: ``observe`` (diagnose only), ``propose`` (default: fixes wait for a human) or ``auto``
  (fixes are approved by policy; lab only);
* ``BOT_RESPONDERS`` (1-3, default 2), ``BOT_POLL_S`` (2), ``BOT_SETTLE_S`` (6), ``BOT_MAX_STEPS`` (24),
  ``BOT_FIELD_DELAY_S`` (6: how long the field technician is "on site"), ``BOT_DAILY_BUDGET_USD`` (5: Grok spend
  per UTC day, then the rules brain takes over);
* ``BOT_OPERATOR_TOKEN``: header ``X-Operator-Token`` for approvals and control (the NOC page served on this
  machine carries it); ``BOT_ALLOWED_HOSTS``: Host headers the web server answers (DNS-rebinding guard);
* ``BOT_DATA_DIR``: SQLite database (incidents, proposals, activity); default ``build/grok-bot`` in the repository;
* ``ZABBIX_PUBLIC_URL``, ``NETBOX_PUBLIC_URL``, ``LAB_API_PUBLIC_URL``: links shown in the NOC page;
  ``BOT_PUBLIC_URL``: the NOC room's own address (sent in pages);
* outside agents (the Grok Bot app, any MCP client): ``BOT_AGENT_TOKEN`` (``Authorization: Bearer`` for ``/mcp``;
  empty = the MCP endpoint is off), ``BOT_AGENT_NAME`` (the visitor's name in the room, default ``GROK``),
  ``BOT_MCP_HOSTS`` (extra Host names, e.g. a tunnel's, that may reach ``/mcp`` and nothing else);
* the pager (a webhook per incident, e.g. a Grok Bot routine): ``BOT_PAGER_URL`` (empty = off),
  ``BOT_PAGER_TOKEN`` (sent as ``Authorization: Bearer``), ``BOT_PAGER_EVENTS`` (``opened``, ``diagnosed``,
  ``resolved``; default ``opened``), ``BOT_PAGER_MIN_SEVERITY`` (default ``Average``), ``BOT_PAGER_MAX_PER_HOUR`` (6).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Brain = Literal["auto", "grok", "rules"]
Autonomy = Literal["observe", "propose", "auto"]
BRAINS: tuple[Brain, ...] = ("auto", "grok", "rules")
AUTONOMIES: tuple[Autonomy, ...] = ("observe", "propose", "auto")
EFFORTS = ("low", "medium", "high", "xhigh")
PAGER_EVENTS = ("opened", "diagnosed", "resolved")
RESERVED_NAMES = ("POLL", "PING", "TRACE", "DIG", "PATCH", "POLICY", "OPERATOR")
SEVERITIES = ("Not classified", "Information", "Warning", "Average", "High", "Disaster")


class SettingsError(RuntimeError):
    """A setting is missing or invalid."""


def find_repo_root(start: Path | None = None) -> Path | None:
    """The NetSim repository (the directory with ``contract/topology.yaml`` and ``.env.example``), if any."""
    candidates = [start or Path.cwd(), Path(__file__).resolve()]
    for base in candidates:
        for d in (base, *base.parents):
            if (d / "contract" / "topology.yaml").is_file() and (d / ".env.example").is_file():
                return d
    return None


def default_env_file() -> Path | None:
    explicit = os.environ.get("BOT_ENV_FILE")
    if explicit:
        return Path(explicit)
    root = find_repo_root()
    return root / ".env" if root else None


def read_env_file(path: Path | None) -> dict[str, str]:
    """``KEY=value`` lines (no interpolation; quotes around the whole value are stripped). Missing file: ``{}``."""
    if path is None or not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


@dataclass(frozen=True)
class BotSettings:
    lab_api_url: str
    lab_api_token: str | None
    xai_api_key: str | None
    xai_base_url: str
    model: str
    reasoning_effort: str | None
    brain: Brain
    autonomy: Autonomy
    responders: int
    poll_s: float
    settle_s: float
    max_settle_s: float
    resolve_hold_s: float
    max_steps: int
    field_delay_s: float
    daily_budget_usd: float
    operator_token: str | None
    allowed_hosts: list[str]
    data_dir: Path
    zabbix_public_url: str
    netbox_public_url: str
    lab_api_public_url: str
    env_file: Path | None
    public_url: str = "http://127.0.0.1:8095"
    agent_token: str | None = None
    agent_name: str = "GROK"
    mcp_hosts: list[str] = field(default_factory=list)
    pager_url: str | None = None
    pager_token: str | None = None
    pager_events: tuple[str, ...] = ("opened",)
    pager_min_severity: str = "Average"
    pager_max_per_hour: int = 6
    actor: str = "grok-bot"
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def grok_configured(self) -> bool:
        return bool(self.xai_api_key)

    def effective_brain(self, override: str | None = None) -> Literal["grok", "rules"]:
        """``auto`` means Grok when a key is configured, else the deterministic rules brain."""
        choice = override or self.brain
        if choice == "rules":
            return "rules"
        if choice == "grok":
            return "grok"
        return "grok" if self.grok_configured else "rules"

    def public(self) -> dict[str, object]:
        """What the NOC page and ``GET /api/state`` may show (no secrets)."""
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "brain": self.brain,
            "grok_configured": self.grok_configured,
            "autonomy": self.autonomy,
            "responders": self.responders,
            "poll_s": self.poll_s,
            "daily_budget_usd": self.daily_budget_usd,
            "lab_api_url": self.lab_api_url,
            "lab_write_token": bool(self.lab_api_token),
            "agent_endpoint": bool(self.agent_token),
            "agent_name": self.agent_name,
            "pager": {
                "configured": bool(self.pager_url),
                "events": list(self.pager_events),
                "min_severity": self.pager_min_severity,
                "max_per_hour": self.pager_max_per_hour,
            },
            "links": {
                "zabbix": self.zabbix_public_url,
                "netbox": self.netbox_public_url,
                "lab_api": self.lab_api_public_url,
            },
        }


def _default_data_dir() -> Path:
    root = find_repo_root()
    return root / "build" / "grok-bot" if root else Path("/var/lib/grok-bot")


def load_settings(environ: Mapping[str, str] | None = None, env_file: Path | None = None) -> BotSettings:
    env = os.environ if environ is None else environ
    file = env_file if env_file is not None else default_env_file()
    from_file = read_env_file(file)

    def get(name: str) -> str | None:
        value = env.get(name)
        if value is None or value == "":
            value = from_file.get(name)
        return value.strip() if value and value.strip() else None

    def number(name: str, default: float, lo: float, hi: float) -> float:
        raw = get(name)
        try:
            value = float(raw) if raw is not None else default
        except ValueError:
            raise SettingsError(f"{name} must be a number, not {raw!r}") from None
        if not lo <= value <= hi:
            raise SettingsError(f"{name} must be between {lo:g} and {hi:g}, not {value:g}")
        return value

    brain = (get("BOT_BRAIN") or "auto").lower()
    if brain not in BRAINS:
        raise SettingsError(f"BOT_BRAIN must be one of {', '.join(BRAINS)}, not {brain!r}")
    autonomy = (get("BOT_AUTONOMY") or "propose").lower()
    if autonomy not in AUTONOMIES:
        raise SettingsError(f"BOT_AUTONOMY must be one of {', '.join(AUTONOMIES)}, not {autonomy!r}")
    effort = (get("GROK_REASONING_EFFORT") or "").lower() or None
    if effort is not None and effort not in EFFORTS:
        raise SettingsError(f"GROK_REASONING_EFFORT must be one of {', '.join(EFFORTS)} or empty, not {effort!r}")
    key = get("XAI_API_KEY")
    hosts = get("BOT_ALLOWED_HOSTS") or "127.0.0.1,localhost,172.30.10.60,grok-bot"
    events = tuple(e.strip().lower() for e in (get("BOT_PAGER_EVENTS") or "opened").split(",") if e.strip())
    bad = [e for e in events if e not in PAGER_EVENTS]
    if bad:
        raise SettingsError(f"BOT_PAGER_EVENTS: unknown {', '.join(bad)} (use {', '.join(PAGER_EVENTS)})")
    min_sev = get("BOT_PAGER_MIN_SEVERITY") or "Average"
    by_lower = {x.lower(): x for x in SEVERITIES}
    if min_sev.lower() not in by_lower:
        raise SettingsError(f"BOT_PAGER_MIN_SEVERITY must be one of {', '.join(SEVERITIES)}, not {min_sev!r}")
    agent_name = (get("BOT_AGENT_NAME") or "GROK").upper()
    if not agent_name.isalnum() or len(agent_name) > 8:
        raise SettingsError(f"BOT_AGENT_NAME must be 1-8 letters or digits, not {agent_name!r}")
    if agent_name in RESERVED_NAMES:
        raise SettingsError(f"BOT_AGENT_NAME {agent_name!r} is taken by the crew ({', '.join(RESERVED_NAMES)})")
    pager_token = get("BOT_PAGER_TOKEN")
    if pager_token and pager_token.lower().startswith("bearer "):  # pasted as the whole header value
        pager_token = pager_token[7:].strip()
    return BotSettings(
        lab_api_url=(get("BOT_LAB_API_URL") or "http://127.0.0.1:8090").rstrip("/"),
        lab_api_token=get("LAB_API_TOKEN"),
        xai_api_key=key,
        xai_base_url=(get("XAI_BASE_URL") or "https://api.x.ai/v1").rstrip("/"),
        model=get("GROK_MODEL") or "grok-4.7",
        reasoning_effort=effort,
        brain=brain,  # type: ignore[arg-type]
        autonomy=autonomy,  # type: ignore[arg-type]
        responders=int(number("BOT_RESPONDERS", 2, 1, 3)),
        poll_s=number("BOT_POLL_S", 2.0, 0.2, 60),
        settle_s=number("BOT_SETTLE_S", 6.0, 0.0, 120),
        max_settle_s=number("BOT_MAX_SETTLE_S", 20.0, 0.0, 300),
        resolve_hold_s=number("BOT_RESOLVE_HOLD_S", 8.0, 0.0, 600),
        max_steps=int(number("BOT_MAX_STEPS", 24, 3, 80)),
        field_delay_s=number("BOT_FIELD_DELAY_S", 6.0, 0.0, 600),
        daily_budget_usd=number("BOT_DAILY_BUDGET_USD", 5.0, 0.0, 10_000),
        operator_token=get("BOT_OPERATOR_TOKEN"),
        allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        data_dir=Path(get("BOT_DATA_DIR") or _default_data_dir()),
        zabbix_public_url=(get("ZABBIX_PUBLIC_URL") or "http://127.0.0.1:8080").rstrip("/"),
        netbox_public_url=(get("NETBOX_PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/"),
        lab_api_public_url=(get("LAB_API_PUBLIC_URL") or "http://127.0.0.1:8090").rstrip("/"),
        env_file=file,
        public_url=(get("BOT_PUBLIC_URL") or "http://127.0.0.1:8095").rstrip("/"),
        agent_token=get("BOT_AGENT_TOKEN"),
        agent_name=agent_name,
        mcp_hosts=[h.strip() for h in (get("BOT_MCP_HOSTS") or "").split(",") if h.strip()],
        pager_url=get("BOT_PAGER_URL"),
        pager_token=pager_token,
        pager_events=events,
        pager_min_severity=by_lower[min_sev.lower()],
        pager_max_per_hour=int(number("BOT_PAGER_MAX_PER_HOUR", 6, 0, 120)),
    )
