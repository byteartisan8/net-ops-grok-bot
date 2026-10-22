"""The NOC side of Zabbix (contract 9.6): role users, media type, macros, the customer-alert action, the webhook.

The fake API keeps just what :class:`netsim_zabbix.noc.NocSetup` and :func:`sync_customer_action` call, with
Zabbix 8.0's shapes (string ids, ``selectX`` output names). The webhook script runs with Node.js like the
template's JavaScript (``test_js.py``).
"""

from __future__ import annotations

import copy
import itertools
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from netsim_netbox.envfile import read_env_file
from netsim_zabbix import names as N
from netsim_zabbix import noc
from netsim_zabbix.noc import NOC_USERS, NocSetup, customer_action_body, media_type_body, sync_customer_action
from netsim_zabbix.settings import Settings
from netsim_zabbix.template import js

GROUPS = {"L2VPN/Hubs": "21", "L2VPN/Spokes": "22"}


class FakeNocZabbix:
    def __init__(self) -> None:
        self.ids = itertools.count(500)
        self.objects: dict[str, dict[str, dict[str, Any]]] = {
            k: {} for k in ("role", "usergroup", "mediatype", "user", "token", "usermacro", "action")
        }
        self.calls: list[str] = []

    def writes(self) -> list[str]:
        return [c for c in self.calls if not c.endswith(".get")]

    def call(self, method: str, params: Any = None) -> Any:
        self.calls.append(method)
        kind, verb = method.split(".", 1)
        params = copy.deepcopy(params)
        if kind == "usermacro":
            return self._macro(verb, params)
        if kind == "token" and verb == "generate":
            return [{"tokenid": t, "token": f"tok{t}" + "0" * 58} for t in params]
        store = self.objects[kind]
        if verb == "get":
            names = (params.get("filter") or {}).get("name") or (params.get("filter") or {}).get("username")
            out = []
            for oid, obj in store.items():
                key = obj.get("username") or obj.get("name")
                if names is not None and key not in names:
                    continue
                if kind == "token" and params.get("userids") and obj["userid"] not in params["userids"]:
                    continue
                wanted_source = (params.get("filter") or {}).get("eventsource")
                if kind == "action" and wanted_source is not None and int(obj["eventsource"]) != int(wanted_source):
                    continue
                out.append({**obj, f"{kind}id" if kind != "usergroup" else "usrgrpid": oid})
            return out
        if verb == "create":
            oid = str(next(self.ids))
            store[oid] = self._stored(kind, params)
            key = {"usergroup": "usrgrpids"}.get(kind, f"{kind}ids")
            return {key: [oid]}
        if verb == "update":
            idkey = "usrgrpid" if kind == "usergroup" else f"{kind}id"
            oid = params.pop(idkey)
            store[oid].update(self._stored(kind, params))
            return {f"{kind}ids": [oid]}
        if verb == "delete":
            for oid in params:
                del store[oid]
            return {f"{kind}ids": params}
        raise AssertionError(method)

    def get_one(self, method: str, params: Any) -> dict | None:
        result = self.call(method, params)
        assert len(result) <= 1
        return result[0] if result else None

    @staticmethod
    def _str(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: FakeNocZabbix._str(v) for k, v in value.items()}
        if isinstance(value, list):
            return [FakeNocZabbix._str(v) for v in value]
        return value if isinstance(value, str) else str(value)

    def _stored(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        out = self._str({k: v for k, v in params.items() if k != "passwd"})
        if kind == "usergroup" and "hostgroup_rights" in out:
            out["hostgroup_rights"] = out["hostgroup_rights"]
        if kind == "action":
            for key in ("operations", "recovery_operations", "update_operations"):
                for op in out.get(key, []):
                    op.setdefault("esc_step_from", "1")
                    op.setdefault("esc_step_to", "1")
        return out

    def _macro(self, verb: str, params: Any) -> Any:
        store = self.objects["usermacro"]
        if verb == "get":
            out = []
            for oid, m in store.items():
                shown = {**m, "globalmacroid": oid}
                if m["type"] == "1":  # secret values are never returned
                    shown.pop("value")
                out.append(shown)
            return out
        if verb == "createglobal":
            oid = str(next(self.ids))
            store[oid] = self._str(params)
            return {"globalmacroids": [oid]}
        if verb == "updateglobal":
            oid = params.pop("globalmacroid")
            store[oid].update(self._str(params))
            return {"globalmacroids": [oid]}
        raise AssertionError(verb)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("BOT_ZABBIX_HOOK_TOKEN=hook-secret\nZABBIX_NOCENG_TOKEN=\n", encoding="utf-8")
    return path


def setup(env_file: Path, api: FakeNocZabbix, valid: dict[str, str] | None = None) -> tuple[NocSetup, list[str]]:
    env = read_env_file(env_file)
    settings = Settings(
        url="http://zabbix",
        env_file=env_file,
        bot_hook_token=env.get("BOT_ZABBIX_HOOK_TOKEN"),
        noc_tokens={u.token_env: env.get(u.token_env) for u in NOC_USERS},
    )
    changes: list[str] = []
    s = NocSetup(api, settings, changes.append, log=lambda _m: None)  # type: ignore[arg-type]
    tokens = valid if valid is not None else {}
    s._token_user = lambda token: tokens.get(token)  # type: ignore[method-assign]
    return s, changes


def test_bootstrap_creates_the_noc_then_changes_nothing(env_file: Path) -> None:
    api = FakeNocZabbix()
    s, changes = setup(env_file, api)
    s.run(GROUPS)
    assert {o["username"] for o in api.objects["user"].values()} == {"noc-engineer", "network-engineer", "noc-manager"}
    [media] = api.objects["mediatype"].values()
    assert media["status"] == "0"  # 8.0.0rc1 creates a media type disabled unless told otherwise
    for user in api.objects["user"].values():
        [m] = user["medias"]
        assert (m["mediatypeid"], m["sendto"], m["active"], m["severity"]) == (
            next(iter(api.objects["mediatype"])),
            user["username"],
            "0",
            "63",
        )
        assert user["usrgrps"] == [{"usrgrpid": next(iter(api.objects["usergroup"]))}]
    [group] = api.objects["usergroup"].values()
    assert sorted((r["id"], r["permission"]) for r in group["hostgroup_rights"]) == [("21", "2"), ("22", "2")]
    [role] = api.objects["role"].values()
    assert role["type"] == "1" and sorted(role["rules"]["api"]) == ["event.acknowledge", "event.get", "problem.get"]
    assert role["rules"]["services.read.mode"] == "1"  # service alerts reach only users who may read the service
    env = read_env_file(env_file)
    tokens = {u.token_env: env[u.token_env] for u in NOC_USERS}
    assert all(t.startswith("tok") for t in tokens.values()) and len(set(tokens.values())) == 3
    macros = {m["macro"]: m for m in api.objects["usermacro"].values()}
    assert macros[N.BOT_HOOK_URL_MACRO]["value"] == "http://172.30.10.60:8095/hooks/zabbix"
    assert macros[N.BOT_HOOK_TOKEN_MACRO]["type"] == "1" and macros[N.BOT_HOOK_TOKEN_MACRO]["value"] == "hook-secret"
    assert macros[N.ESCALATE_AFTER_MACRO]["value"] == "10m"
    assert [a["name"] for a in api.objects["action"].values()] == [N.CUSTOMER_ACTION]
    assert len(changes) >= 10
    # second run: tokens authenticate as their users, nothing to write
    userids = {o["username"]: oid for oid, o in api.objects["user"].items()}
    valid = {tokens[u.token_env]: userids[u.username] for u in NOC_USERS}
    api.calls.clear()
    s2, changes2 = setup(env_file, api, valid)
    s2.run(GROUPS)
    assert changes2 == [] and api.writes() == []


def test_a_stale_token_is_replaced_and_the_escalation_delay_is_left_alone(env_file: Path) -> None:
    api = FakeNocZabbix()
    s, _ = setup(env_file, api)
    s.run(GROUPS)
    env = read_env_file(env_file)
    macro = next(oid for oid, m in api.objects["usermacro"].items() if m["macro"] == N.ESCALATE_AFTER_MACRO)
    api.objects["usermacro"][macro]["value"] = "3m"  # the operator tuned it
    userids = {o["username"]: oid for oid, o in api.objects["user"].items()}
    valid = {env[u.token_env]: userids[u.username] for u in NOC_USERS if u.username != "noc-manager"}
    s2, changes = setup(env_file, api, valid)
    s2.run(GROUPS)
    assert (
        len(changes) == 1 and changes[0].startswith("ZABBIX_NOCMGR_TOKEN") and "created for noc-manager" in changes[0]
    )
    after = read_env_file(env_file)
    assert after["ZABBIX_NOCMGR_TOKEN"] != env["ZABBIX_NOCMGR_TOKEN"]
    assert after["ZABBIX_NOCENG_TOKEN"] == env["ZABBIX_NOCENG_TOKEN"]
    assert api.objects["usermacro"][macro]["value"] == "3m"


def test_bootstrap_needs_the_hook_token(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("", encoding="utf-8")
    s, _ = setup(path, FakeNocZabbix())
    with pytest.raises(SystemExit, match="BOT_ZABBIX_HOOK_TOKEN"):
        s.run(GROUPS)


def test_the_customer_alert_action() -> None:
    body = customer_action_body("106", {"noc-engineer": "7", "network-engineer": "8", "noc-manager": "9"})
    assert body["eventsource"] == 4 and body["esc_period"] == N.ESCALATE_AFTER_MACRO
    [cond] = body["filter"]["conditions"]
    assert (cond["conditiontype"], cond["value2"], cond["value"]) == (26, "bsm_level", "customer")  # service tag value
    step1, step2 = body["operations"]
    assert (step1["esc_step_from"], step1["opmessage_usr"], step1["opmessage"]["default_msg"]) == (
        1,
        [{"userid": "7"}],
        1,
    )
    assert (step2["esc_step_from"], step2["esc_step_to"], step2["opmessage_usr"]) == (2, 2, [{"userid": "9"}])
    assert step2["opmessage"]["message"] == "escalation" and step2["opmessage"]["default_msg"] == 0
    assert [o["opmessage_usr"] for o in body["recovery_operations"] + body["update_operations"]] == [
        [{"userid": "7"}]
    ] * 2
    # parameters Zabbix 8.0.0rc1 refuses for service actions (verified: "unexpected parameter")
    assert "pause_symptoms" not in body and "notify_if_canceled" not in body


def test_sync_customer_action_is_idempotent_and_waits_for_bootstrap() -> None:
    api = FakeNocZabbix()
    plan = sync_customer_action(api)  # type: ignore[arg-type]
    assert plan.changes == [] and "run `make zabbix-bootstrap`" in plan.warnings[0]
    api.objects["mediatype"]["106"] = {"name": N.NOC_MEDIA_TYPE}
    for oid, name in (("7", "noc-engineer"), ("8", "network-engineer"), ("9", "noc-manager")):
        api.objects["user"][oid] = {"username": name}
    assert [c.action for c in sync_customer_action(api).changes] == ["create"]  # type: ignore[arg-type]
    api.calls.clear()
    assert sync_customer_action(api).changes == [] and api.writes() == []  # type: ignore[arg-type]
    [aid] = api.objects["action"]
    api.objects["action"][aid]["esc_period"] = "1h"
    assert [c.action for c in sync_customer_action(api).changes] == ["update"]  # type: ignore[arg-type]


def test_the_media_type_carries_what_the_plan_asks_for() -> None:
    body = media_type_body()
    params = {p["name"]: p["value"] for p in body["parameters"]}
    assert params["url"] == N.BOT_HOOK_URL_MACRO and params["token"] == N.BOT_HOOK_TOKEN_MACRO
    # role, event id, problem / recovery / update, severity, service name, service tags, root cause, event time
    for name, macro in [
        ("role", "{ALERT.SENDTO}"),
        ("event_id", "{EVENT.ID}"),
        ("kind", "{ALERT.MESSAGE}"),
        ("event_severity", "{EVENT.SEVERITY}"),
        ("update_severity", "{EVENT.UPDATE.SEVERITY}"),
        ("service_name", "{SERVICE.NAME}"),
        ("service_tags", "{SERVICE.TAGSJSON}"),
        ("root_cause", "{SERVICE.ROOTCAUSE}"),
        ("event_time", "{EVENT.TIMESTAMP}"),
    ]:
        assert params[name] == macro
    kinds = {t["recovery"]: t["message"] for t in body["message_templates"] if t["eventsource"] == 4}
    assert kinds == {0: "problem", 1: "recovery", 2: "update"}
    assert (body["status"], body["maxattempts"]) == (0, 3)
    assert noc.bot_hook_url() == "http://172.30.10.60:8095/hooks/zabbix"


# ------------------------------------------------------------------------------------------------ the webhook
NODE = shutil.which("node")
HTTP = """
const __sent = [];
let __status = 200;
function HttpRequest() { this.headers = []; }
HttpRequest.prototype.addHeader = function (h) { this.headers.push(h); };
HttpRequest.prototype.getStatus = function () { return __status; };
HttpRequest.prototype.post = function (url, body) {
    __sent.push({url: url, headers: this.headers, body: JSON.parse(body)});
    return __status === 200 ? '{"accepted": true}' : 'nope';
};
const Zabbix = {log: function () {}};
"""


def webhook(params: dict[str, str], status: int = 200) -> tuple[str, list[dict[str, Any]]]:
    program = (
        f"{HTTP}\n__status = {status};\n"
        f"let __out;\ntry {{ __out = (function (value) {{\n{js('noc_webhook')}\n}})({json.dumps(json.dumps(params))});"
        " }"
        "\ncatch (e) { process.stderr.write('THROWN ' + e); process.exit(3); }"
        "\nprocess.stdout.write(String(__out) + '\\n' + JSON.stringify(__sent));"
    )
    res = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=20)  # type: ignore[list-item]
    if res.returncode:
        return res.stderr, []
    out, sent = res.stdout.split("\n", 1)
    return out, json.loads(sent)


def alert(**over: str) -> dict[str, str]:
    """Parameters as Zabbix resolves them (unresolved macros stay literal, as on the running server)."""
    base = dict(noc.WEBHOOK_PARAMETERS)
    base.update(
        url="http://172.30.10.60:8095/hooks/zabbix",
        token="hook-secret",
        role="noc-engineer",
        kind="problem",
        subject="Customer-A: Warning (NetSim NOC page)",
        event_id="4681",
        event_name='Status of service "Customer-A" changed to Warning',
        event_status="PROBLEM",
        event_severity="Warning",
        event_time="1792552920",
        service_id="2",
        service_name="Customer-A",
        service_tags='[{"tag":"customer","value":"Customer-A"},{"tag":"sla_tier","value":"gold"},'
        '{"tag":"bsm_level","value":"customer"}]',
        root_cause='Host: "spoke-1" Problem name: "SVC-0001 primary on spoke-1: PW down (2)" Severity: "High"',
        action="NetSim: customer alert",
    )
    base.update(over)
    return base


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_webhook_posts_one_problem_with_bearer() -> None:
    out, [sent] = webhook(alert())
    assert json.loads(out) == {"kind": "problem", "role": "noc-engineer", "http": 200}
    assert sent["url"] == "http://172.30.10.60:8095/hooks/zabbix"
    assert "Authorization: Bearer hook-secret" in sent["headers"]
    body = sent["body"]
    assert body["kind"] == "problem" and body["severity"] == "Warning" and body["event_time"] == 1792552920
    assert body["service"] == {
        "id": "2",
        "name": "Customer-A",
        "tags": {"customer": "Customer-A", "sla_tier": "gold", "bsm_level": "customer"},
    }
    # macros that do not apply to a problem stay unresolved in Zabbix: they arrive as null
    assert body["recovery_event_id"] is None and body["update_severity"] is None and body["update_time"] is None


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_webhook_update_recovery_and_escalation() -> None:
    _, [upd] = webhook(alert(kind="update", update_severity="Disaster", update_time="1792553070"))
    assert (upd["body"]["kind"], upd["body"]["severity"], upd["body"]["event_severity"]) == (
        "update",
        "Disaster",
        "Warning",
    )
    _, [rec] = webhook(
        alert(kind="recovery", recovery_event_id="4682", recovery_time="1792553175", event_status="RESOLVED")
    )
    assert (rec["body"]["kind"], rec["body"]["severity"], rec["body"]["recovery_event_id"]) == (
        "recovery",
        "OK",
        "4682",
    )
    _, [esc] = webhook(alert(kind="escalation", role="noc-manager"))
    assert (esc["body"]["kind"], esc["body"]["role"]) == ("escalation", "noc-manager")
    _, [odd] = webhook(alert(kind="something else"))
    assert odd["body"]["kind"] == "problem"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_webhook_throws_on_a_refusal_so_zabbix_retries() -> None:
    err, sent = webhook(alert(), status=401)
    assert sent == [] and "THROWN grok-bot" in err and "HTTP 401" in err
