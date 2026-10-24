"""Settings from env and .env, the lab tools on recorded lab-api data, the routine sink, the NOC page's assets."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import jsonschema
import pytest
from netsim_bot.roles import LAB_TOOLS, NOC_COMMON, PROPOSE, ROLE_TOOLS
from netsim_bot.settings import SettingsError, load_settings, read_env_file
from netsim_bot.tools import (
    ALL_TOOLS,
    MAX_RESULT_CHARS,
    ToolContext,
    clip,
    run_tool,
    shape_drift,
    shape_impact,
    shape_problems,
    shape_services,
)

WEB = Path(__file__).resolve().parents[1] / "src" / "netsim_bot" / "web"
STATIONS = {"console", "screen", "netbox", "rack", "desk", "whiteboard", "printer", "operator", "board", "terminal"}
HOSTS = ["hub-a", "hub-b", "hub-c", "hub-d", "spoke-1", "spoke-2", "spoke-3", "spoke-4", "spoke-5", "spoke-6"]


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------ settings
def test_env_file_parsing(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# c\nA=1\nexport B = two \nC='quoted value'\nD=\nE=x=y\n", encoding="utf-8")
    assert read_env_file(f) == {"A": "1", "B": "two", "C": "quoted value", "D": "", "E": "x=y"}
    assert read_env_file(tmp_path / "missing") == {}


def test_settings_roles_routines_and_live_reload(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("BOT_NOCENG_TOKEN=a\nBOT_ROUTINE_NOCMGR_URL=https://r/x\nBOT_ROUTINE_NOCMGR_KEY=Bearer k\n")
    s = load_settings(environ={}, env_file=f)
    assert s.role_tokens == {"noc-engineer": "a", "network-engineer": None, "noc-manager": None}
    assert s.routine("noc-manager").url == "https://r/x" and s.routine("noc-manager").key == "k"
    assert not s.routine("noc-engineer").configured and s.autonomy == "propose"
    assert (s.batch_s, s.max_batch_s, s.page_max_per_hour, s.field_delay_s) == (10.0, 30.0, 12, 10.0)
    f.write_text("BOT_ROUTINE_NOCENG_URL=https://r/e\n")  # added while the bot runs: read on the next load
    assert load_settings(environ={}, env_file=f).routine("noc-engineer").configured
    assert "https://r" not in json.dumps(load_settings(environ={}, env_file=f).public())


def test_settings_of_the_old_crew_are_ignored_or_downgraded(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("XAI_API_KEY=xai-1\nBOT_BRAIN=grok\nBOT_PAGER_URL=https://x\nBOT_AGENT_TOKEN=t\nBOT_AUTONOMY=auto\n")
    s = load_settings(environ={}, env_file=f)
    assert s.autonomy == "propose" and "auto is gone" in s.warnings[0]
    assert "xai-1" not in json.dumps(s.public())


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"BOT_AUTONOMY": "yolo"}, "BOT_AUTONOMY"),
        ({"BOT_POLL_S": "fast"}, "BOT_POLL_S"),
        ({"BOT_BATCH_S": "20", "BOT_MAX_BATCH_S": "10"}, "BOT_MAX_BATCH_S"),
        ({"BOT_NOCENG_TOKEN": "same", "BOT_NOCMGR_TOKEN": "same"}, "must differ"),
        ({"BOT_PAGE_MAX_PER_HOUR": "-1"}, "BOT_PAGE_MAX_PER_HOUR"),
    ],
)
def test_settings_validation(tmp_path: Path, env: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        load_settings(environ=env, env_file=tmp_path / "none")


# ------------------------------------------------------------------------------------------------ tools
def test_every_tool_schema_is_a_valid_object_schema() -> None:
    tools = [*LAB_TOOLS.values(), *PROPOSE.values(), *NOC_COMMON.values()]
    tools += [t for role in ROLE_TOOLS.values() for t in role.values()]
    for tool in tools:
        assert tool.parameters["type"] == "object" and len(tool.description) > 40, tool.name
        jsonschema.Draft202012Validator.check_schema(tool.parameters)
        assert tool.station in STATIONS, tool.name
        assert set(tool.parameters.get("required", [])) <= set(tool.parameters["properties"]), tool.name
    # nothing a role can call applies a change or reads the lab's answer key
    for name in ALL_TOOLS:
        assert not any(word in name for word in ("apply_change", "confirm", "scenario", "reset", "inject", "approve"))


def test_shapes_keep_what_matters_from_recorded_data(recorded) -> None:
    fr = recorded("fake-redundancy")
    svc = shape_services(fr["GET /services"])
    s2 = next(s for s in svc["services"] if s["service"] == "SVC-0002")
    assert s2["zabbix"] == "Warning" and s2["tier"] == "gold" and s2["rules"] == "redundant-pair"
    assert any("peer 10.255.0.2" in p["spoke_end"] for p in s2["pws"])
    assert shape_drift(fr["GET /drift"])["findings"][0]["kind"] == "redundancy-lost"
    assert shape_problems(fr["GET /zabbix/problems"])["correlation"][0]["service"] == "SVC-0002"
    imp = shape_impact(recorded("hub-down")["GET /impact/hub-b"])
    assert imp["host"] == "hub-b" and len(imp["affected"]) == 7 and "SVC-0010" in imp["unaffected"]
    assert len(json.dumps(svc)) < len(json.dumps(fr["GET /services"]))


def test_clip_cuts_huge_results() -> None:
    out = clip({"x": "y" * (MAX_RESULT_CHARS * 2)})
    assert out["truncated"] is True and len(out["partial"]) == MAX_RESULT_CHARS
    assert clip({"a": 1}) == {"a": 1}


def test_direct_reads_go_to_lab_api(fake_lab) -> None:
    lab = fake_lab("healthy")
    lab.set("GET /netbox/dcim/devices/", {"path": "dcim/devices", "data": {"count": 4}, "size": 9})
    lab.set("GET /devices/spoke-1/restconf/netsim-device:l2vpn", {"data": {"netsim-device:l2vpn": {}}, "size": 9})
    ctx = ToolContext(lab=lab.client(), bot="NOCENG", hosts=HOSTS)
    ok, r = run(run_tool(ALL_TOOLS["zabbix_get"], ctx, {"method": "service.get", "params": {"output": ["name"]}}))
    assert ok and r["method"] == "service.get" and len(r["result"]) == 6
    ok, r = run(run_tool(ALL_TOOLS["netbox_get"], ctx, {"path": "/api/dcim/devices/", "query": {"role": ["hub", "x"]}}))
    assert ok and r["data"] == {"count": 4}
    req = next(x for x in lab.requests if x.url.path.startswith("/netbox"))
    assert req.url.params.get_list("role") == ["hub", "x"]
    ok, r = run(run_tool(ALL_TOOLS["device_restconf_get"], ctx, {"host": "spoke-1", "path": "netsim-device:l2vpn"}))
    assert ok and r["data"] == {"netsim-device:l2vpn": {}}
    ok, r = run(run_tool(ALL_TOOLS["zabbix_get"], ctx, {"method": "host.delete", "params": {}}))
    assert not ok and "method must be one of" in r["error"]
    ok, r = run(run_tool(ALL_TOOLS["device_restconf_get"], ctx, {"host": "hub-z"}))
    assert not ok and "unknown device" in r["error"]


@pytest.mark.parametrize(
    ("tool", "args", "error"),
    [
        ("get_device_state", {"host": "hub-z"}, "unknown device"),
        ("get_device_state", {}, "'host' is required"),
        ("get_service", {"service_id": "nope"}, "SVC-0001"),
        ("get_open_problems", {"scope": "everything"}, "scope must be one of"),
        ("propose_field_repair", {"kind": "laser", "host": "hub-a", "reason": "x"}, "'kind' must be one of"),
        ("zabbix_get", {"method": "problem.get", "params": [1]}, "'params' must be an object"),
    ],
)
def test_bad_arguments_come_back_as_errors(fake_lab, tool, args, error) -> None:
    async def propose(**kw):
        return {"proposal_id": "CHG-0001"}

    ctx = ToolContext(lab=fake_lab().client(), bot="NOCENG", hosts=HOSTS, propose=propose)
    ok, result = run(run_tool(ALL_TOOLS[tool], ctx, args))
    assert not ok and error in result["error"]


# ------------------------------------------------------------------------------------------------ the routine sink
def test_the_routine_sink_records_pages_per_role() -> None:
    from http.server import ThreadingHTTPServer

    from netsim_bot.sink import Recorder, make_handler

    rec = Recorder()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(rec, 200))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        req = urllib.request.Request(
            f"{base}/noc-engineer",
            data=json.dumps({"event": "incident.opened"}).encode(),
            headers={"Authorization": "Bearer test-sink", "Content-Type": "application/json"},
        )
        assert json.loads(urllib.request.urlopen(req, timeout=5).read())["accepted"] is True
        pages = json.loads(urllib.request.urlopen(f"{base}/pages?role=noc-engineer", timeout=5).read())
        assert [(p["role"], p["authorization"], p["body"]["event"]) for p in pages] == [
            ("noc-engineer", "Bearer test-sink", "incident.opened")
        ]
        assert json.loads(urllib.request.urlopen(f"{base}/pages?after=1", timeout=5).read()) == []
        bad = urllib.request.Request(f"{base}/boss", data=b"{}")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad, timeout=5)
    finally:
        server.shutdown()


# ------------------------------------------------------------------------------------------------ the page
def test_page_assets_exist_and_reference_each_other() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "__BOOT__" in html and "/static/js/main.js" in html and "/static/css/noc.css" in html
    for name in ("main", "room", "screen", "sprites", "font", "panel"):
        assert (WEB / "js" / f"{name}.js").is_file()
    for js in (WEB / "js").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("import ") and " from './" in line:
                target = line.split(" from './")[1].split("'")[0]
                assert (WEB / "js" / target).is_file(), f"{js.name} imports missing {target}"
        for img in re.findall(r"'/static/(img/[\w.-]+)'", text):
            assert (WEB / img).is_file(), f"{js.name} draws missing /static/{img}"


def test_no_css_class_rule_is_defined_twice() -> None:
    """Two top-level rules for one class mix two things (a ticket's ``.stage`` chips once rounded the room's
    ``<section class="stage">`` into an ellipse). Overrides inside ``@media`` blocks are indented and allowed."""
    css = (WEB / "css" / "noc.css").read_text(encoding="utf-8")
    seen: dict[str, int] = {}
    for n, line in enumerate(css.splitlines(), 1):
        m = re.match(r"^\.([\w-]+)\s*\{", line)
        if m:
            assert m.group(1) not in seen, f".{m.group(1)} defined on lines {seen[m.group(1)]} and {n}"
            seen[m.group(1)] = n


def test_the_room_has_exactly_the_three_roles() -> None:
    room = (WEB / "js" / "room.js").read_text(encoding="utf-8")
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for name in ("NOCENG", "NETENG", "NOCMGR"):
        assert name in room and name in html
    for gone in ("POLL", "PATCH", "PING", "TRACE", "visitor", "couch", "bench"):
        assert gone not in room and gone not in html, gone


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_scripts_parse() -> None:
    for js in sorted((WEB / "js").glob("*.js")):
        result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"{js.name}: {result.stderr}"
