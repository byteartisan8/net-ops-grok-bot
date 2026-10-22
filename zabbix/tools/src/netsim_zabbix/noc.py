"""The NOC side of Zabbix (contract 9.6): customer alerts go to the three NOC roles through grok-bot.

``zabbix-bootstrap`` (Super admin) creates, idempotently:

* role ``NetSim NOC`` (User type): frontend Problems and Services, API allow-list ``event.acknowledge``,
  ``event.get``, ``problem.get``, actions "acknowledge" and "comment", read access to every service (Zabbix sends a
  service alert only to users who may read the service);
* user group ``NetSim NOC``: read on ``L2VPN/Hubs`` and ``L2VPN/Spokes`` (to see and acknowledge their problems);
* media type ``NetSim NOC (grok-bot)``: a webhook (``js/noc_webhook.js``) that POSTs JSON to
  ``{$NETSIM.BOT.HOOK.URL}`` with ``Authorization: Bearer {$NETSIM.BOT.HOOK.TOKEN}``. Its message templates for
  service events name the alert kind (``problem``, ``recovery``, ``update``), because Zabbix 8.0.0rc1 keeps
  ``{EVENT.UPDATE.STATUS}`` at 0 in service update operations. 3 attempts, 10 s apart;
* users ``noc-engineer``, ``network-engineer``, ``noc-manager`` in that group, each with that media (send-to = its
  role) and an API token (``ZABBIX_NOCENG_TOKEN``, ``ZABBIX_NETENG_TOKEN``, ``ZABBIX_NOCMGR_TOKEN``: kept while they
  authenticate as their user, else replaced and written into ``.env``). lab-api acknowledges problems with them, so
  Zabbix shows who acknowledged;
* global macros ``{$NETSIM.BOT.HOOK.URL}`` (grok-bot, contract 4), ``{$NETSIM.BOT.HOOK.TOKEN}`` (secret:
  ``BOT_ZABBIX_HOOK_TOKEN``), ``{$NETSIM.ESCALATE.AFTER}`` (created as ``10m``, then the operator's to tune);
* the service action ``NetSim: customer alert`` (:func:`sync_customer_action`): service tag ``bsm_level=customer``;
  step 1 at once to ``noc-engineer``; step 2 after ``{$NETSIM.ESCALATE.AFTER}``, if the customer is still not OK,
  to ``noc-manager`` (its own message: ``escalation``); recovery and update operations to ``noc-engineer``. The
  bootstrap creates it, not ``zabbix-provision``: its operations name the role users, and the provisioning API user
  (Admin type) cannot see them (``user.get`` returns only the users of its own groups).

Zabbix 8.0.0rc1 behaviour this relies on (verified on the running server, docs/reports/09-noc-roles.md):

* service events exist only for services an action matches;
* a customer going from degraded (Warning) to down (Disaster) is an *update* of its open event, not a new event;
* service events cannot be acknowledged (``event.acknowledge``: "No permissions"): lab-api acknowledges the trigger
  problems behind them (``service.get`` ``selectProblemEvents`` on the customer's subtree: only the PW leaves list
  problem events, the customer node's list stays empty);
* ``mediatype.create`` leaves a media type disabled unless ``status`` is given.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from netsim_netbox.envfile import update_env_file

from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi, ZabbixApiError, describe_token
from netsim_zabbix.plan import Plan
from netsim_zabbix.settings import Settings
from netsim_zabbix.template import js

USER_TYPE_USER = 1
PERM_READ = 2
MEDIATYPE_WEBHOOK = 4
MACRO_TEXT, MACRO_SECRET = 0, 1
EVENTSOURCE_SERVICE = 4
COND_SERVICE_TAG_VALUE = 26
OPERATION_SEND_MESSAGE = 0
SEVERITY_ALL = 63
ALWAYS = "1-7,00:00-24:00"

#: Contract 4: grok-bot at 172.30.10.60, port 8095 (``NETSIM_BOT_HOOK_URL`` overrides it, e.g. for tests).
BOT_HOOK_URL = "http://172.30.10.60:8095/hooks/zabbix"
ESCALATE_AFTER_DEFAULT = "10m"


@dataclass(frozen=True)
class NocUser:
    username: str
    name: str
    token_env: str


NOC_USERS = (
    NocUser("noc-engineer", "NOC Engineer", "ZABBIX_NOCENG_TOKEN"),
    NocUser("network-engineer", "Network Engineer", "ZABBIX_NETENG_TOKEN"),
    NocUser("noc-manager", "NOC Manager", "ZABBIX_NOCMGR_TOKEN"),
)
NOC_USER_BY_NAME = {u.username: u for u in NOC_USERS}
FIRST_RESPONDER = "noc-engineer"
ESCALATION_TARGET = "noc-manager"

NOC_UI = ["monitoring.problems", "services.services"]
USER_UI_ELEMENTS = [
    "monitoring.dashboard",
    "monitoring.problems",
    "monitoring.hosts",
    "monitoring.latest_data",
    "monitoring.maps",
    "services.services",
    "services.sla_report",
    "inventory.overview",
    "inventory.hosts",
    "reports.availability_report",
    "reports.top_triggers",
]
NOC_API_METHODS = ["event.acknowledge", "event.get", "problem.get"]
NOC_ACTIONS = ["acknowledge_problems", "add_problem_comments"]

#: The webhook's parameters: what Zabbix resolves per alert (contract 9.6).
WEBHOOK_PARAMETERS = [
    ("url", N.BOT_HOOK_URL_MACRO),
    ("token", N.BOT_HOOK_TOKEN_MACRO),
    ("role", "{ALERT.SENDTO}"),
    ("kind", "{ALERT.MESSAGE}"),
    ("subject", "{ALERT.SUBJECT}"),
    ("event_id", "{EVENT.ID}"),
    ("event_name", "{EVENT.NAME}"),
    ("event_status", "{EVENT.STATUS}"),
    ("event_severity", "{EVENT.SEVERITY}"),
    ("update_severity", "{EVENT.UPDATE.SEVERITY}"),
    ("event_time", "{EVENT.TIMESTAMP}"),
    ("update_time", "{EVENT.UPDATE.TIMESTAMP}"),
    ("recovery_event_id", "{EVENT.RECOVERY.ID}"),
    ("recovery_time", "{EVENT.RECOVERY.TIMESTAMP}"),
    ("service_id", "{SERVICE.ID}"),
    ("service_name", "{SERVICE.NAME}"),
    ("service_tags", "{SERVICE.TAGSJSON}"),
    ("root_cause", "{SERVICE.ROOTCAUSE}"),
    ("action", "{ACTION.NAME}"),
]

#: Service-event message templates (recovery 0 = problem, 1 = recovery, 2 = update): the message is the alert kind.
MESSAGE_TEMPLATES = [
    (0, "{SERVICE.NAME}: {EVENT.SEVERITY} (NetSim NOC page)", "problem"),
    (1, "{SERVICE.NAME}: OK again (NetSim NOC page)", "recovery"),
    (2, "{SERVICE.NAME}: now {EVENT.UPDATE.SEVERITY} (NetSim NOC page)", "update"),
]
ESCALATION_SUBJECT = "{SERVICE.NAME}: still not OK after " + N.ESCALATE_AFTER_MACRO + " (NetSim NOC escalation)"
ESCALATION_MESSAGE = "escalation"


def bot_hook_url() -> str:
    return os.environ.get("NETSIM_BOT_HOOK_URL") or BOT_HOOK_URL


def fingerprint(secret: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:16]


def _rules_match(current: dict[str, Any], wanted: dict[str, Any]) -> bool:
    for key, value in wanted.items():
        cur = current.get(key)
        if key in ("ui", "actions"):
            got = {e["name"]: int(e["status"]) for e in cur or []}
            if any(got.get(e["name"]) != e["status"] for e in value):
                return False
        elif key == "api":
            if sorted(cur or []) != sorted(value):
                return False
        elif str(cur) != str(value):
            return False
    return True


def _rights(rights: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return sorted((str(r["id"]), int(r["permission"])) for r in rights)


def media_type_body() -> dict[str, Any]:
    return {
        "name": N.NOC_MEDIA_TYPE,
        "type": MEDIATYPE_WEBHOOK,
        "status": 0,
        "script": js("noc_webhook"),
        "timeout": "10s",
        "maxsessions": 4,
        "maxattempts": 3,
        "attempt_interval": "10s",
        "process_tags": 0,
        "show_event_menu": 0,
        "parameters": [{"name": n, "value": v} for n, v in WEBHOOK_PARAMETERS],
        "message_templates": [
            {"eventsource": EVENTSOURCE_SERVICE, "recovery": r, "subject": s, "message": m}
            for r, s, m in MESSAGE_TEMPLATES
        ],
        "description": (
            "NetSim: customer alerts (service action NetSim: customer alert) to grok-bot, which batches them into "
            "incidents and pages the NOC roles' routines. Send-to = the role (noc-engineer, network-engineer, "
            "noc-manager). Managed by zabbix-bootstrap; source zabbix/tools/src/netsim_zabbix/js/noc_webhook.js."
        ),
    }


def _same_media_type(cur: dict[str, Any], want: dict[str, Any]) -> bool:
    def norm(text: str) -> str:
        return (text or "").replace("\r\n", "\n")

    simple = ("type", "status", "maxsessions", "maxattempts", "process_tags", "show_event_menu")
    return (
        all(str(cur.get(k)) == str(want[k]) for k in simple)
        and cur.get("timeout") == want["timeout"]
        and cur.get("attempt_interval") == want["attempt_interval"]
        and norm(cur.get("script")) == want["script"]
        and norm(cur.get("description")) == want["description"]
        and sorted((p["name"], p["value"]) for p in cur.get("parameters") or [])
        == sorted((p["name"], p["value"]) for p in want["parameters"])
        and sorted(
            (int(t["eventsource"]), int(t["recovery"]), t["subject"], norm(t["message"]))
            for t in cur.get("message_templates") or []
        )
        == sorted((t["eventsource"], t["recovery"], t["subject"], t["message"]) for t in want["message_templates"])
    )


class NocSetup:
    """The bootstrap part (runs inside :class:`netsim_zabbix.bootstrap.Bootstrap` with a Super-admin session)."""

    def __init__(
        self, api: ZabbixApi, settings: Settings, change: Callable[[str], None], log: Callable[[str], None] = print
    ) -> None:
        self.api = api
        self.settings = settings
        self.change = change
        self.log = log

    # ------------------------------------------------------------------ role, group
    def role(self) -> str:
        rules: dict[str, Any] = {
            "ui": [{"name": n, "status": int(n in NOC_UI)} for n in USER_UI_ELEMENTS],
            "ui.default_access": 0,
            "services.read.mode": 1,
            "services.write.mode": 0,
            "modules.default_access": 0,
            "api.access": 1,
            "api.mode": 1,
            "api": NOC_API_METHODS,
            "actions": [{"name": a, "status": 1} for a in NOC_ACTIONS],
            "actions.default_access": 0,
        }
        found = self.api.get_one(
            "role.get", {"output": ["roleid", "type"], "selectRules": "extend", "filter": {"name": [N.NOC_ROLE]}}
        )
        if not found:
            roleid = self.api.call("role.create", {"name": N.NOC_ROLE, "type": USER_TYPE_USER, "rules": rules})[
                "roleids"
            ][0]
            self.change(f"created role {N.NOC_ROLE} (User type: acknowledge problems, read services)")
            return roleid
        if int(found["type"]) != USER_TYPE_USER or not _rules_match(found["rules"], rules):
            self.api.call("role.update", {"roleid": found["roleid"], "type": USER_TYPE_USER, "rules": rules})
            self.change(f"updated role {N.NOC_ROLE}")
        return found["roleid"]

    def user_group(self, group_ids: dict[str, str]) -> str:
        rights = [{"id": group_ids[g], "permission": PERM_READ} for g in N.HOST_GROUPS.values()]
        found = self.api.get_one(
            "usergroup.get",
            {
                "output": ["usrgrpid"],
                "selectHostGroupRights": ["id", "permission"],
                "filter": {"name": [N.NOC_USER_GROUP]},
            },
        )
        if not found:
            gid = self.api.call("usergroup.create", {"name": N.NOC_USER_GROUP, "hostgroup_rights": rights})[
                "usrgrpids"
            ][0]
            self.change(f"created user group {N.NOC_USER_GROUP} (read on the L2VPN host groups)")
            return gid
        if _rights(found["hostgroup_rights"]) != _rights(rights):
            self.api.call("usergroup.update", {"usrgrpid": found["usrgrpid"], "hostgroup_rights": rights})
            self.change(f"updated user group {N.NOC_USER_GROUP} rights")
        return found["usrgrpid"]

    # ------------------------------------------------------------------ media type
    def media_type(self) -> str:
        want = media_type_body()
        found = self.api.get_one(
            "mediatype.get",
            {"output": "extend", "selectMessageTemplates": "extend", "filter": {"name": [N.NOC_MEDIA_TYPE]}},
        )
        if not found:
            mtid = self.api.call("mediatype.create", want)["mediatypeids"][0]
            self.change(f"created media type {N.NOC_MEDIA_TYPE} (webhook -> grok-bot /hooks/zabbix)")
            return mtid
        if not _same_media_type(found, want):
            self.api.call("mediatype.update", {"mediatypeid": found["mediatypeid"], **want})
            self.change(f"updated media type {N.NOC_MEDIA_TYPE}")
        return found["mediatypeid"]

    # ------------------------------------------------------------------ users, tokens
    def users(self, roleid: str, usrgrpid: str, mediatypeid: str) -> dict[str, str]:
        ids: dict[str, str] = {}
        for u in NOC_USERS:
            media = {
                "mediatypeid": mediatypeid,
                "sendto": u.username,
                "active": 0,
                "severity": SEVERITY_ALL,
                "period": ALWAYS,
            }
            found = self.api.get_one(
                "user.get",
                {
                    "output": ["userid", "roleid", "name"],
                    "selectUsrgrps": ["usrgrpid"],
                    "selectMedias": ["mediatypeid", "sendto", "active", "severity", "period"],
                    "filter": {"username": [u.username]},
                },
            )
            if not found:
                ids[u.username] = self.api.call(
                    "user.create",
                    {
                        "username": u.username,
                        "name": u.name,
                        "surname": "(NetSim NOC role)",
                        # nobody logs in as a role: the API token is what it uses (through lab-api only)
                        "passwd": secrets.token_urlsafe(24),
                        "roleid": roleid,
                        "usrgrps": [{"usrgrpid": usrgrpid}],
                        "medias": [media],
                    },
                )["userids"][0]
                self.change(f"created user {u.username} (media {N.NOC_MEDIA_TYPE}, send-to {u.username})")
                continue
            ids[u.username] = found["userid"]
            medias = [
                {k: str(m[k]) for k in ("mediatypeid", "sendto", "active", "severity", "period")}
                for m in found["medias"]
            ]
            same = (
                found["roleid"] == roleid
                and found["name"] == u.name
                and [g["usrgrpid"] for g in found["usrgrps"]] == [usrgrpid]
                and medias == [{k: str(v) for k, v in media.items()}]
            )
            if not same:
                self.api.call(
                    "user.update",
                    {
                        "userid": found["userid"],
                        "name": u.name,
                        "roleid": roleid,
                        "usrgrps": [{"usrgrpid": usrgrpid}],
                        "medias": [media],
                    },
                )
                self.change(f"updated user {u.username} (role, group or media)")
        return ids

    def tokens(self, userids: dict[str, str]) -> dict[str, str]:
        """One API token per role user: kept while it authenticates as that user, else replaced (into ``.env``)."""
        current = self.settings.noc_tokens
        updates: dict[str, str] = {}
        out: dict[str, str] = {}
        for u in NOC_USERS:
            token = current.get(u.token_env)
            if token and self._token_user(token) == userids[u.username]:
                self.log(f"  {u.token_env} {describe_token(token)} kept (authenticates as {u.username})")
                out[u.username] = token
                continue
            for old in self.api.call(
                "token.get",
                {"output": ["tokenid"], "userids": [userids[u.username]], "filter": {"name": [N.NOC_TOKEN_NAME]}},
            ):
                self.api.call("token.delete", [old["tokenid"]])
            tokenid = self.api.call(
                "token.create",
                {
                    "name": N.NOC_TOKEN_NAME,
                    "userid": userids[u.username],
                    "description": "NetSim NOC: lab-api acknowledges Zabbix problems as this role",
                },
            )["tokenids"][0]
            token = self.api.call("token.generate", [tokenid])[0]["token"]
            updates[u.token_env] = token
            out[u.username] = token
            self.change(f"{u.token_env} {describe_token(token)} created for {u.username}")
        if updates:
            if self.settings.env_file is None:
                raise SystemExit("zabbix-bootstrap: no .env to write the NOC role tokens into")
            update_env_file(self.settings.env_file, updates)
        return out

    def _token_user(self, token: str) -> str | None:
        try:
            with ZabbixApi(self.settings.url, token, timeout=self.settings.timeout) as probe:
                return probe.call("user.checkAuthentication", {"token": token}, auth=False).get("userid")
        except ZabbixApiError:
            return None

    # ------------------------------------------------------------------ macros
    def macros(self) -> None:
        hook_token = self.settings.bot_hook_token
        if not hook_token:
            raise SystemExit("zabbix-bootstrap: BOT_ZABBIX_HOOK_TOKEN is not set (run `make env`)")
        wanted = [
            (
                N.BOT_HOOK_URL_MACRO,
                bot_hook_url(),
                MACRO_TEXT,
                "grok-bot's alert intake (contract 9.6): the NetSim NOC media type posts customer alerts here.",
                True,
            ),
            (
                N.BOT_HOOK_TOKEN_MACRO,
                hook_token,
                MACRO_SECRET,
                f"BOT_ZABBIX_HOOK_TOKEN: Bearer token of grok-bot's /hooks/zabbix ({fingerprint(hook_token)}).",
                True,
            ),
            (
                N.ESCALATE_AFTER_MACRO,
                ESCALATE_AFTER_DEFAULT,
                MACRO_TEXT,
                "NetSim: customer alert -> the NOC Manager is paged when a customer is still not OK after this "
                "long (escalation step 2; at least 60s). Created as 10m; tune it here.",
                False,  # the operator's knob: created with the default, never reset
            ),
        ]
        existing = {
            m["macro"]: m
            for m in self.api.call(
                "usermacro.get",
                {"globalmacro": True, "output": ["globalmacroid", "macro", "value", "type", "description"]},
            )
        }
        for macro, value, mtype, desc, managed_value in wanted:
            cur = existing.get(macro)
            body = {"macro": macro, "value": value, "type": mtype, "description": desc}
            if cur is None:
                self.api.call("usermacro.createglobal", body)
                self.change(f"created global macro {macro}")
                continue
            if not managed_value:
                body.pop("value")
            stale = (
                int(cur["type"]) != mtype
                or cur.get("description") != desc
                or (managed_value and mtype == MACRO_TEXT and cur.get("value") != value)
            )
            if stale:
                # secret values are never returned: the description carries the token's fingerprint instead
                self.api.call("usermacro.updateglobal", {"globalmacroid": cur["globalmacroid"], **body})
                self.change(f"updated global macro {macro}")

    # ------------------------------------------------------------------ all
    def run(self, group_ids: dict[str, str]) -> dict[str, Any]:
        self.macros()
        roleid = self.role()
        usrgrpid = self.user_group(group_ids)
        mediatypeid = self.media_type()
        userids = self.users(roleid, usrgrpid, mediatypeid)
        self.tokens(userids)
        plan = sync_customer_action(self.api)
        for c in plan.changes:
            self.change(f"{c.action} action {c.name}{': ' + c.detail if c.detail else ''}")
        return {"mediatypeid": mediatypeid, "userids": userids}


# ------------------------------------------------------------------------------------------------ the action
def customer_action_body(mediatypeid: str, userids: dict[str, str]) -> dict[str, Any]:
    default = {"default_msg": 1, "mediatypeid": mediatypeid}
    escalation = {
        "default_msg": 0,
        "mediatypeid": mediatypeid,
        "subject": ESCALATION_SUBJECT,
        "message": ESCALATION_MESSAGE,
    }
    first = [{"userid": userids[FIRST_RESPONDER]}]
    return {
        "name": N.CUSTOMER_ACTION,
        "eventsource": EVENTSOURCE_SERVICE,
        "status": 0,
        "esc_period": N.ESCALATE_AFTER_MACRO,
        "filter": {
            "evaltype": 0,
            "conditions": [
                {
                    "conditiontype": COND_SERVICE_TAG_VALUE,
                    "operator": 0,
                    "value": N.LEVEL_CUSTOMER,
                    "value2": N.BSM_LEVEL_TAG,
                }
            ],
        },
        "operations": [
            {
                "operationtype": OPERATION_SEND_MESSAGE,
                "esc_step_from": 1,
                "esc_step_to": 1,
                "opmessage": default,
                "opmessage_usr": first,
            },
            {
                "operationtype": OPERATION_SEND_MESSAGE,
                "esc_step_from": 2,
                "esc_step_to": 2,
                "opmessage": escalation,
                "opmessage_usr": [{"userid": userids[ESCALATION_TARGET]}],
            },
        ],
        "recovery_operations": [
            {"operationtype": OPERATION_SEND_MESSAGE, "opmessage": default, "opmessage_usr": first}
        ],
        "update_operations": [{"operationtype": OPERATION_SEND_MESSAGE, "opmessage": default, "opmessage_usr": first}],
    }


def _op_signature(op: dict[str, Any]) -> tuple[Any, ...]:
    msg = op.get("opmessage") or {}
    users = sorted(str(u["userid"]) for u in op.get("opmessage_usr") or [])
    custom = (msg.get("subject", ""), msg.get("message", "")) if str(msg.get("default_msg")) == "0" else ("", "")
    return (
        int(op.get("operationtype", 0)),
        int(op.get("esc_step_from", 1) or 1),
        int(op.get("esc_step_to", 1) or 1),
        str(msg.get("default_msg")),
        str(msg.get("mediatypeid")),
        custom,
        tuple(users),
    )


def _same_action(cur: dict[str, Any], want: dict[str, Any]) -> bool:
    conds = sorted(
        (int(c["conditiontype"]), int(c["operator"]), c["value"], c.get("value2", ""))
        for c in cur["filter"]["conditions"]
    )
    want_conds = sorted(
        (c["conditiontype"], c["operator"], c["value"], c.get("value2", "")) for c in want["filter"]["conditions"]
    )

    def ops(key: str, data: dict[str, Any], defaults: bool = False) -> list[tuple[Any, ...]]:
        out = []
        for op in data.get(key) or []:
            if defaults:  # recovery/update operations have no escalation steps
                op = {**op, "esc_step_from": 1, "esc_step_to": 1}
            out.append(_op_signature(op))
        return sorted(out)

    return (
        int(cur["status"]) == 0
        and cur["esc_period"] == want["esc_period"]
        and int(cur["filter"]["evaltype"]) == want["filter"]["evaltype"]
        and conds == want_conds
        and ops("operations", cur) == ops("operations", want)
        and ops("recovery_operations", cur, True) == ops("recovery_operations", want, True)
        and ops("update_operations", cur, True) == ops("update_operations", want, True)
    )


def sync_customer_action(api: ZabbixApi, *, dry_run: bool = False) -> Plan:
    """The service action ``NetSim: customer alert`` (needs the media type and the role users)."""
    plan = Plan()
    media = api.get_one("mediatype.get", {"output": ["mediatypeid"], "filter": {"name": [N.NOC_MEDIA_TYPE]}})
    users = {
        u["username"]: u["userid"]
        for u in api.call(
            "user.get", {"output": ["userid", "username"], "filter": {"username": list(NOC_USER_BY_NAME)}}
        )
    }
    if media is None or {FIRST_RESPONDER, ESCALATION_TARGET} - set(users):
        plan.warnings.append(f"{N.CUSTOMER_ACTION}: media type or NOC users missing (run `make zabbix-bootstrap`)")
        return plan
    want = customer_action_body(media["mediatypeid"], users)
    cur = api.get_one(
        "action.get",
        {
            "output": "extend",
            "selectFilter": "extend",
            "selectOperations": "extend",
            "selectRecoveryOperations": "extend",
            "selectUpdateOperations": "extend",
            "filter": {"name": [N.CUSTOMER_ACTION], "eventsource": EVENTSOURCE_SERVICE},
        },
    )
    if cur is None:
        plan.add("create", "action", N.CUSTOMER_ACTION, "bsm_level=customer -> noc-engineer, escalation noc-manager")
        if not dry_run:
            api.call("action.create", want)
        return plan
    if not _same_action(cur, want):
        plan.add("update", "action", N.CUSTOMER_ACTION)
        if not dry_run:
            api.call(
                "action.update", {"actionid": cur["actionid"], **{k: v for k, v in want.items() if k != "eventsource"}}
            )
    return plan
