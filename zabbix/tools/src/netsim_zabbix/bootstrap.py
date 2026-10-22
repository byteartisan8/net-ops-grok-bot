"""``zabbix-bootstrap``: the one-time Super-admin setup of Zabbix 8.0 (idempotent; run by ``make zabbix-up``).

1. Admin password: the factory default ``zabbix`` is replaced by ``ZABBIX_ADMIN_PASSWORD`` (``.env``).
2. Host groups ``L2VPN/Hubs``, ``L2VPN/Spokes`` and template group ``Templates/NetSim`` (Super admin only).
3. Dedicated API user ``ZABBIX_API_USER`` with the minimum it needs: user type Admin (required to "execute now"
   discovery rules, to create hosts and actions), role ``NetSim API`` (frontend: Dashboards only, API allow-list,
   services read-write, actions: dashboards, maps, SLA, problem comments/acknowledge, execute now, server-monitored
   hosts), user group ``NetSim API`` with read-write on exactly the groups above. Zabbix 8.0 disables API access for
   every role of a new installation except Super admin, so the role enables it explicitly.
4. ``ZABBIX_API_TOKEN``: kept if it still authenticates as that user, else a new token is generated and written into
   ``.env`` (old ``netsim-api`` tokens are deleted).
5. Global macros ``{$NETSIM.ZABBIX.API.URL}`` and ``{$NETSIM.ZABBIX.API.TOKEN}`` (secret text) for the webhook.
6. Global webhook script ``NetSim: check now`` (``netsim_zabbix/js/check_now.js``), scope "action operation".
7. The default host ``Zabbix server`` keeps "Zabbix server health" but loses "Linux by Zabbix agent" (there is no
   agent in the server container; it would only raise "agent not available").
8. The NOC side (:mod:`netsim_zabbix.noc`, contract 9.6): role and user group ``NetSim NOC``, media type
   ``NetSim NOC (grok-bot)``, users ``noc-engineer``, ``network-engineer``, ``noc-manager`` with their tokens
   (``ZABBIX_NOCENG_TOKEN``, ... in ``.env``), the macros ``{$NETSIM.BOT.HOOK.URL}``, ``{$NETSIM.BOT.HOOK.TOKEN}``,
   ``{$NETSIM.ESCALATE.AFTER}`` and the service action ``NetSim: customer alert``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Callable
from typing import Any

from netsim_netbox.envfile import update_env_file

from netsim_common.constants import ZABBIX_WEB
from netsim_common.topology import load_topology
from netsim_zabbix import names as N
from netsim_zabbix.api import ZabbixApi, ZabbixApiError, describe_token, wait_for_api
from netsim_zabbix.settings import TOKEN, Settings, load_settings
from netsim_zabbix.template import js

FACTORY_ADMIN_PASSWORD = "zabbix"
WEB_CONTAINER_PORT = 8080
USER_TYPE_ADMIN = 2
PERM_READ_WRITE = 3
SCRIPT_TYPE_WEBHOOK = 5
SCRIPT_SCOPE_ACTION = 1
MACRO_TEXT, MACRO_SECRET = 0, 1

#: Frontend sections an Admin-type role could see; the API user keeps only Dashboards (Zabbix requires at least one
#: enabled UI element per role).
UI_ENABLED = "monitoring.dashboard"
ADMIN_UI_ELEMENTS = [
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
    "monitoring.discovery",
    "services.sla",
    "reports.scheduled_reports",
    "reports.notifications",
    "configuration.template_groups",
    "configuration.host_groups",
    "configuration.templates",
    "configuration.hosts",
    "configuration.maintenance",
    "configuration.discovery",
    "configuration.trigger_actions",
    "configuration.service_actions",
    "configuration.discovery_actions",
    "configuration.autoregistration_actions",
    "configuration.internal_actions",
]

#: API methods the token may call (allow list): every read, plus exactly the writes of provisioning, sync, the
#: check-now webhook and lab-api (problem acknowledgement/comments).
API_METHODS = [
    "*.get",
    "configuration.export",
    "configuration.import",
    "configuration.importcompare",
    "host.create",
    "host.update",
    "host.delete",
    "usermacro.create",
    "usermacro.update",
    "usermacro.delete",
    "service.create",
    "service.update",
    "service.delete",
    "sla.create",
    "sla.update",
    "sla.delete",
    "sla.getsli",
    "map.create",
    "map.update",
    "map.delete",
    "dashboard.create",
    "dashboard.update",
    "dashboard.delete",
    "action.create",
    "action.update",
    "action.delete",
    "task.create",
    "event.acknowledge",
]

ROLE_ACTIONS = [
    "edit_dashboards",
    "edit_maps",
    "manage_sla",
    "acknowledge_problems",
    "add_problem_comments",
    "close_problems",
    "invoke_execute_now",
    "select_server_for_monitoring",
]


def api_url_labnet() -> str:
    """The API as the Zabbix server (webhook) reaches it on labnet."""
    return f"http://{load_topology().infra(ZABBIX_WEB).mgmt_ip}:{WEB_CONTAINER_PORT}/api_jsonrpc.php"


def fingerprint(secret: str) -> str:
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()[:16]


class Bootstrap:
    def __init__(self, api: ZabbixApi, settings: Settings, log: Callable[[str], None] = print) -> None:
        self.api = api
        self.settings = settings
        self.log = log
        self.changes: list[str] = []

    def change(self, what: str) -> None:
        self.changes.append(what)
        self.log(f"  {what}")

    # ------------------------------------------------------------------ groups
    def groups(self) -> dict[str, str]:
        ids: dict[str, str] = {}
        for method, name in [("hostgroup", g) for g in N.HOST_GROUPS.values()] + [("templategroup", N.TEMPLATE_GROUP)]:
            found = self.api.get_one(f"{method}.get", {"output": ["groupid"], "filter": {"name": [name]}})
            if found:
                ids[name] = found["groupid"]
            else:
                ids[name] = self.api.call(f"{method}.create", {"name": name})["groupids"][0]
                self.change(f"created {method} {name}")
        return ids

    # ------------------------------------------------------------------ role, user group, user
    def role(self) -> str:
        rules: dict[str, Any] = {
            "ui": [{"name": n, "status": int(n == UI_ENABLED)} for n in ADMIN_UI_ELEMENTS],
            "ui.default_access": 0,
            "services.read.mode": 1,
            "services.write.mode": 1,
            "modules.default_access": 0,
            "api.access": 1,
            "api.mode": 1,
            "api": API_METHODS,
            "actions": [{"name": a, "status": 1} for a in ROLE_ACTIONS],
            "actions.default_access": 0,
        }
        found = self.api.get_one(
            "role.get", {"output": ["roleid", "type"], "selectRules": "extend", "filter": {"name": [N.API_ROLE]}}
        )
        if not found:
            roleid = self.api.call("role.create", {"name": N.API_ROLE, "type": USER_TYPE_ADMIN, "rules": rules})[
                "roleids"
            ][0]
            self.change(f"created role {N.API_ROLE} (Admin type, API allow-list of {len(API_METHODS)} patterns)")
            return roleid
        if int(found["type"]) != USER_TYPE_ADMIN or not _rules_match(found["rules"], rules):
            self.api.call("role.update", {"roleid": found["roleid"], "type": USER_TYPE_ADMIN, "rules": rules})
            self.change(f"updated role {N.API_ROLE}")
        return found["roleid"]

    def user_group(self, group_ids: dict[str, str]) -> str:
        host_rights = [{"id": group_ids[g], "permission": PERM_READ_WRITE} for g in N.HOST_GROUPS.values()]
        tpl_rights = [{"id": group_ids[N.TEMPLATE_GROUP], "permission": PERM_READ_WRITE}]
        found = self.api.get_one(
            "usergroup.get",
            {
                "output": ["usrgrpid"],
                "selectHostGroupRights": ["id", "permission"],
                "selectTemplateGroupRights": ["id", "permission"],
                "filter": {"name": [N.API_USER_GROUP]},
            },
        )
        if not found:
            gid = self.api.call(
                "usergroup.create",
                {"name": N.API_USER_GROUP, "hostgroup_rights": host_rights, "templategroup_rights": tpl_rights},
            )["usrgrpids"][0]
            self.change(f"created user group {N.API_USER_GROUP} (read-write on the L2VPN host/template groups)")
            return gid
        if _rights(found["hostgroup_rights"]) != _rights(host_rights) or _rights(
            found["templategroup_rights"]
        ) != _rights(tpl_rights):
            self.api.call(
                "usergroup.update",
                {"usrgrpid": found["usrgrpid"], "hostgroup_rights": host_rights, "templategroup_rights": tpl_rights},
            )
            self.change(f"updated user group {N.API_USER_GROUP} rights")
        return found["usrgrpid"]

    def user(self, roleid: str, usrgrpid: str) -> str:
        name = self.settings.api_user
        password = self.settings.api_password
        if not password:
            raise SystemExit("zabbix-bootstrap: ZABBIX_API_PASSWORD is not set (run `make env`)")
        found = self.api.get_one(
            "user.get", {"output": ["userid", "roleid"], "selectUsrgrps": ["usrgrpid"], "filter": {"username": [name]}}
        )
        if not found:
            userid = self.api.call(
                "user.create",
                {
                    "username": name,
                    "name": "NetSim API",
                    "passwd": password,
                    "roleid": roleid,
                    "usrgrps": [{"usrgrpid": usrgrpid}],
                },
            )["userids"][0]
            self.change(f"created user {name}")
            return userid
        if found["roleid"] != roleid or [g["usrgrpid"] for g in found["usrgrps"]] != [usrgrpid]:
            self.api.call(
                "user.update", {"userid": found["userid"], "roleid": roleid, "usrgrps": [{"usrgrpid": usrgrpid}]}
            )
            self.change(f"updated user {name} (role/group)")
        return found["userid"]

    # ------------------------------------------------------------------ token
    def token(self, userid: str) -> str:
        current = self.settings.token
        if current and self._token_user(current) == userid:
            self.log(f"  {TOKEN} {describe_token(current)} kept (authenticates as {self.settings.api_user})")
            return current
        for old in self.api.call(
            "token.get", {"output": ["tokenid"], "userids": [userid], "filter": {"name": [N.API_TOKEN_NAME]}}
        ):
            self.api.call("token.delete", [old["tokenid"]])
        tokenid = self.api.call(
            "token.create",
            {
                "name": N.API_TOKEN_NAME,
                "userid": userid,
                "description": "NetSim provisioning, sync, check-now, lab-api",
            },
        )["tokenids"][0]
        token = self.api.call("token.generate", [tokenid])[0]["token"]
        env_file = self.settings.env_file
        if env_file is None:
            raise SystemExit("zabbix-bootstrap: no .env to write ZABBIX_API_TOKEN into")
        update_env_file(env_file, {TOKEN: token})
        self.change(f"{TOKEN} {describe_token(token)} created for {self.settings.api_user} -> {env_file.name}")
        return token

    def _token_user(self, token: str) -> str | None:
        try:
            with ZabbixApi(self.settings.url, token, timeout=self.settings.timeout) as probe:
                return probe.call("user.checkAuthentication", {"token": token}, auth=False).get("userid")
        except ZabbixApiError:
            return None

    # ------------------------------------------------------------------ global macros
    def global_macros(self, token: str) -> None:
        wanted = [
            (N.API_URL_MACRO, api_url_labnet(), MACRO_TEXT, "Zabbix API as the server reaches it (check-now webhook)."),
            (N.API_TOKEN_MACRO, token, MACRO_SECRET, f"ZABBIX_API_TOKEN of the API user ({fingerprint(token)})."),
        ]
        existing = {
            m["macro"]: m
            for m in self.api.call(
                "usermacro.get",
                {"globalmacro": True, "output": ["globalmacroid", "macro", "value", "type", "description"]},
            )
        }
        for macro, value, mtype, desc in wanted:
            cur = existing.get(macro)
            body = {"macro": macro, "value": value, "type": mtype, "description": desc}
            if cur is None:
                self.api.call("usermacro.createglobal", body)
                self.change(f"created global macro {macro}")
            elif (
                int(cur["type"]) != mtype
                or cur.get("description") != desc
                or (mtype == MACRO_TEXT and cur.get("value") != value)
            ):
                # Secret values are never returned: the description carries the token's fingerprint instead.
                self.api.call("usermacro.updateglobal", {"globalmacroid": cur["globalmacroid"], **body})
                self.change(f"updated global macro {macro}")

    # ------------------------------------------------------------------ check-now script
    def check_now_script(self) -> None:
        params = [
            {"name": "api_url", "value": N.API_URL_MACRO},
            {"name": "event", "value": "{EVENT.ID}"},
            {"name": "hostid", "value": "{HOST.ID}"},
            {"name": "token", "value": N.API_TOKEN_MACRO},
            {"name": "trap", "value": "{EVENT.TAGS.trap}"},
            {"name": "walk_key", "value": N.WALK_KEY},
        ]
        body = {
            "name": N.CHECK_NOW_SCRIPT,
            "type": SCRIPT_TYPE_WEBHOOK,
            "scope": SCRIPT_SCOPE_ACTION,
            "command": js("check_now"),
            "timeout": "10s",
            "parameters": params,
            "description": (
                "NetSim: after an SNMP trap, ask Zabbix to poll the device now (walk master item; on config/boot "
                "traps also the spec discovery). Traps are hints, polling is truth. Source: "
                "zabbix/tools/src/netsim_zabbix/js/check_now.js (managed by zabbix-bootstrap)."
            ),
        }
        found = self.api.get_one(
            "script.get",
            {
                "output": ["scriptid", "type", "scope", "command", "timeout", "description", "parameters"],
                "filter": {"name": [N.CHECK_NOW_SCRIPT]},
            },
        )
        if not found:
            self.api.call("script.create", body)
            self.change(f"created global script {N.CHECK_NOW_SCRIPT}")
            return
        same = (
            int(found["type"]) == SCRIPT_TYPE_WEBHOOK
            and int(found["scope"]) == SCRIPT_SCOPE_ACTION
            and found["command"].replace("\r\n", "\n") == body["command"]
            and found["timeout"] == body["timeout"]
            and found["description"].replace("\r\n", "\n") == body["description"]
            and sorted((p["name"], p["value"]) for p in found["parameters"])
            == sorted((p["name"], p["value"]) for p in params)
        )
        if not same:
            self.api.call("script.update", {"scriptid": found["scriptid"], **body})
            self.change(f"updated global script {N.CHECK_NOW_SCRIPT}")

    # ------------------------------------------------------------------ default host
    def default_host(self) -> None:
        host = self.api.get_one(
            "host.get",
            {
                "output": ["hostid"],
                "selectParentTemplates": ["templateid", "name"],
                "filter": {"host": ["Zabbix server"]},
            },
        )
        if not host:
            return
        agent = [t for t in host["parentTemplates"] if t["name"] == "Linux by Zabbix agent"]
        if agent:
            self.api.call(
                "host.update", {"hostid": host["hostid"], "templates_clear": [{"templateid": agent[0]["templateid"]}]}
            )
            self.change(
                'host "Zabbix server": unlinked and cleared "Linux by Zabbix agent" (no agent in the container)'
            )

    # ------------------------------------------------------------------ all
    def run(self) -> str:
        from netsim_zabbix.noc import NocSetup

        group_ids = self.groups()
        roleid = self.role()
        usrgrpid = self.user_group(group_ids)
        userid = self.user(roleid, usrgrpid)
        token = self.token(userid)
        self.global_macros(token)
        self.check_now_script()
        self.default_host()
        NocSetup(self.api, self.settings, self.change, self.log).run(group_ids)
        return token


def _rights(rights: list[dict]) -> list[tuple[str, int]]:
    return sorted((str(r["id"]), int(r["permission"])) for r in rights)


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


def admin_session(settings: Settings, log: Callable[[str], None] = print) -> tuple[ZabbixApi, list[str]]:
    """A session as Admin; switches the factory password to ``ZABBIX_ADMIN_PASSWORD`` on first use."""
    wanted = settings.admin_password
    if not wanted:
        raise SystemExit("zabbix-bootstrap: ZABBIX_ADMIN_PASSWORD is not set (run `make env`)")
    api = ZabbixApi(settings.url, timeout=settings.timeout)
    try:
        api.login("Admin", wanted)
        return api, []
    except ZabbixApiError:
        pass
    try:
        api.login("Admin", FACTORY_ADMIN_PASSWORD)
    except ZabbixApiError as exc:
        api.close()
        raise SystemExit(
            "zabbix-bootstrap: Admin login fails with ZABBIX_ADMIN_PASSWORD and with the factory default; "
            f"was the password changed by hand? ({exc})"
        ) from exc
    userid = api.get_one("user.get", {"output": ["userid"], "filter": {"username": ["Admin"]}})["userid"]
    api.call("user.update", {"userid": userid, "current_passwd": FACTORY_ADMIN_PASSWORD, "passwd": wanted})
    api.close()  # changing the own password ends the session
    api = ZabbixApi(settings.url, timeout=settings.timeout)
    api.login("Admin", wanted)
    msg = "Admin password changed from the factory default to ZABBIX_ADMIN_PASSWORD"
    log(f"  {msg}")
    return api, [msg]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zabbix-bootstrap", description=__doc__.split("\n\n")[0])
    parser.add_argument("--timeout", type=float, default=300, help="seconds to wait for the API (default 300)")
    args = parser.parse_args(argv)
    settings = load_settings()
    version = wait_for_api(settings.url, timeout=args.timeout)
    print(f"zabbix-bootstrap: Zabbix {version} at {settings.url}")
    api, changes = admin_session(settings)
    with api:
        boot = Bootstrap(api, settings)
        try:
            boot.run()
        except ZabbixApiError as exc:
            print(f"zabbix-bootstrap: {exc}", file=sys.stderr)
            return 1
    total = len(changes) + len(boot.changes)
    print(f"zabbix-bootstrap: {total} change(s)" if total else "zabbix-bootstrap: 0 changes (already bootstrapped)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
