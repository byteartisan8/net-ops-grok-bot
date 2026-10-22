"""zabbix/compose.yaml, image pins, Makefile targets, bootstrap role and settings (no containers needed)."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest
import yaml
from netsim_zabbix import names as N
from netsim_zabbix.bootstrap import API_METHODS, ROLE_ACTIONS, api_url_labnet
from netsim_zabbix.settings import SettingsError, load_settings
from netsim_zabbix.template import js

from netsim_common.paths import repo_root
from netsim_common.topology import load_topology

ROOT = repo_root()
COMPOSE = yaml.safe_load((ROOT / "zabbix" / "compose.yaml").read_text())
SERVICES = COMPOSE["services"]


def build_script_default(name: str) -> str:
    text = (ROOT / "zabbix" / "images" / "build-images.sh").read_text()
    return re.search(rf'^{name}="\$\{{{name}:-(.*)\}}"$', text, re.M).group(1)


def test_services_addresses_follow_the_contract() -> None:
    topo = load_topology()
    for name in ("zabbix-server", "zabbix-web", "zabbix-snmptraps"):
        assert SERVICES[name]["networks"]["labnet"]["ipv4_address"] == topo.infra(name).mgmt_ip, name
    assert SERVICES["zabbix-web"]["ports"] == ["127.0.0.1:8080:8080/tcp"]
    assert COMPOSE["networks"]["labnet"] == {"external": True}
    assert SERVICES["zabbix-postgres"]["networks"] == ["zabbix-internal"]
    assert COMPOSE["networks"]["zabbix-internal"] == {"internal": True}


def test_images_are_the_pinned_8_0_build() -> None:
    tag = build_script_default("ZBX_IMAGE_TAG").replace("${ZBX_SOURCE_TAG}", build_script_default("ZBX_SOURCE_TAG"))
    assert tag == "8.0.0rc1-alpine"
    for svc, image in (
        ("zabbix-server", "server-pgsql"),
        ("zabbix-web", "web-nginx-pgsql"),
        ("zabbix-snmptraps", "snmptraps"),
    ):
        assert SERVICES[svc]["image"] == f"netsim/zabbix-{image}:{tag}"
    assert re.fullmatch(r"[0-9a-f]{40}", build_script_default("ZBX_DOCKER_COMMIT"))
    assert re.fullmatch(r"postgres:18\.\d+-alpine\d+\.\d+", SERVICES["zabbix-postgres"]["image"])


def test_traps_path_numeric_oids_and_shared_volume() -> None:
    traps = SERVICES["zabbix-snmptraps"]
    assert "n" in traps["environment"]["SNMPTRAP_OUTPUT_OPTIONS"]  # -On: numeric OIDs, matched by the trap items
    assert "../contract/mibs:/var/lib/zabbix/mibs:ro" in traps["volumes"]
    assert "zabbix-snmptraps:/var/lib/zabbix/snmptraps:rw" in traps["volumes"]
    assert "zabbix-snmptraps:/var/lib/zabbix/snmptraps:ro" in SERVICES["zabbix-server"]["volumes"]
    env = SERVICES["zabbix-server"]["environment"]
    assert env["ZBX_ENABLE_SNMP_TRAPS"] == "true"
    assert env["ZBX_ENABLEGLOBALSCRIPTS"] == "0"  # webhooks do not need it
    env_example = (ROOT / ".env.example").read_text()
    assert "TRAP_TARGETS=172.30.10.42:1162" in env_example  # snmptrapd listens on udp/1162 (contract change log)


def test_make_targets_exist() -> None:
    mk = (ROOT / "zabbix" / "zabbix.mk").read_text()
    for target in ("zabbix-up", "zabbix-provision", "zabbix-sync", "zabbix-test", "zabbix-test-integration"):
        assert re.search(rf"^{target}:", mk, re.M), target


def _allowed(method: str) -> bool:
    return any(fnmatch.fnmatchcase(method, pattern) for pattern in API_METHODS)


def test_role_allows_what_the_webhook_and_sync_call() -> None:
    code = js("check_now")
    called = set(re.findall(r"api\('([a-z]+\.[a-zA-Z]+)'", code))
    assert called == {"item.get", "discoveryrule.get", "task.create", "event.acknowledge"}
    assert all(_allowed(m) for m in called)
    # bootstrap.py and noc.py run in zabbix-bootstrap's Super-admin session, everything else with the API user's token
    super_admin = ("bootstrap.py", "noc.py")
    src = "".join(
        p.read_text() for p in (ROOT / "zabbix/tools/src/netsim_zabbix").glob("*.py") if p.name not in super_admin
    )
    for method in set(re.findall(r'"([a-z]+\.(?:get|create|update|delete|import|importcompare|export))"', src)):
        assert _allowed(method), method
    assert not _allowed("user.create") and not _allowed("script.create") and not _allowed("usermacro.createglobal")
    assert {"edit_dashboards", "edit_maps", "manage_sla", "select_server_for_monitoring"} <= set(ROLE_ACTIONS)
    assert "close_problems" in ROLE_ACTIONS  # the webhook closes each trap event (event.acknowledge, action 1)


def test_webhook_reaches_the_api_on_labnet() -> None:
    assert api_url_labnet() == "http://172.30.10.41:8080/api_jsonrpc.php"
    assert N.API_URL_MACRO != N.API_TOKEN_MACRO


def test_settings_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ZABBIX_API_TOKEN=from-file\nZABBIX_URL=http://file:8080/\nNETSIM_BSM_SOURCE=topology\n")
    s = load_settings({"ZABBIX_URL": "http://env:8080"}, env_file)
    assert (s.url, s.token, s.bsm_source) == ("http://env:8080", "from-file", "topology")
    assert load_settings({}, tmp_path / "missing").url == "http://127.0.0.1:8080"
    with pytest.raises(SettingsError):
        load_settings({"NETSIM_BSM_SOURCE": "csv"}, env_file)
    with pytest.raises(SettingsError):
        load_settings({}, tmp_path / "missing").api()


def test_dashboard_comparison_ignores_widget_order() -> None:
    """Zabbix exports widgets in creation order and leaves out x/y when 0: compare by position, not list order."""
    from netsim_zabbix.dashboard import _norm, dashboard_path

    widgets = yaml.safe_load(dashboard_path().read_text(encoding="utf-8"))["zabbix_export"]["dashboards"][0]
    widgets = widgets["pages"][0]["widgets"]
    assert {"x", "y"} - set(widgets[0])  # the first widget sits at x=0, y=0: no coordinates written
    assert [w["name"] for w in widgets if w["type"] == "slareport"] == [
        "SLA: L2VPN gold (99.95 %)",
        "SLA: L2VPN silver (99.9 %)",
        "SLA: L2VPN bronze (99.5 %)",
    ]
    assert _norm(list(reversed(widgets))) == _norm(widgets)
