"""The responders' tools: schemas the xAI API accepts, compact results from recorded lab-api data, guards."""

from __future__ import annotations

import asyncio
import json

import jsonschema
import pytest
from netsim_bot.tools import (
    ALL_TOOLS,
    MAX_RESULT_CHARS,
    PROPOSE_TOOLS,
    READ_TOOLS,
    ToolContext,
    clip,
    run_tool,
    shape_drift,
    shape_impact,
    shape_problems,
    shape_services,
    toolset,
)

STATIONS = {"console", "screen", "netbox", "rack", "desk", "whiteboard", "printer", "operator"}
HOSTS = ["hub-a", "hub-b", "hub-c", "hub-d", "spoke-1", "spoke-2", "spoke-3", "spoke-4", "spoke-5", "spoke-6"]


def test_every_tool_schema_is_a_valid_object_schema() -> None:
    names = [t.name for t in ALL_TOOLS.values()]
    assert len(names) == len(set(names))
    for tool in ALL_TOOLS.values():
        schema = tool.schema()
        assert schema["type"] == "function" and schema["name"] == tool.name and len(tool.description) > 40
        assert schema["parameters"]["type"] == "object"  # the xAI API requires an object root
        jsonschema.Draft202012Validator.check_schema(schema["parameters"])
        assert tool.station in STATIONS
        assert set(schema["parameters"].get("required", [])) <= set(schema["parameters"]["properties"])


def test_toolsets_follow_mode_and_autonomy() -> None:
    names = lambda ts: {t.name for t in ts}  # noqa: E731
    assert names(toolset("incident", "propose")) == {t.name for t in (*READ_TOOLS, *PROPOSE_TOOLS)} | {
        "finish_investigation"
    }
    assert names(toolset("incident", "observe")) == {t.name for t in READ_TOOLS} | {"finish_investigation"}
    assert "finish_investigation" not in names(toolset("chat", "propose"))
    # nothing the bot can call applies a change or reads the lab's answer key
    for name in ALL_TOOLS:
        assert not any(word in name for word in ("apply_change", "confirm", "scenario", "reset", "inject"))


def test_shapes_keep_what_matters_from_recorded_data(recorded) -> None:
    fr = recorded("fake-redundancy")
    svc = shape_services(fr["GET /services"])
    s2 = next(s for s in svc["services"] if s["service"] == "SVC-0002")
    assert s2["zabbix"] == "Warning" and s2["tier"] == "gold" and s2["rules"] == "redundant-pair"
    assert {p["role"] for p in s2["pws"]} == {"primary", "backup"}
    assert any("peer 10.255.0.2" in p["spoke_end"] for p in s2["pws"])
    drift = shape_drift(fr["GET /drift"])
    assert drift["findings"][0]["kind"] == "redundancy-lost"
    assert drift["devices"]["spoke-2"]["diffs"][0]["path"].endswith("peer-ipv4")
    assert "diffs" not in drift["devices"]["hub-c"]
    probs = shape_problems(fr["GET /zabbix/problems"])
    assert probs["count"] >= 2 and probs["correlation"][0]["service"] == "SVC-0002"
    hd = recorded("hub-down")
    imp = shape_impact(hd["GET /impact/hub-b"])
    assert imp["host"] == "hub-b" and len(imp["affected"]) == 7 and "SVC-0010" in imp["unaffected"]
    # compact: the model sees far less than lab-api sends
    assert len(json.dumps(svc)) < len(json.dumps(fr["GET /services"]))


def test_clip_cuts_huge_results() -> None:
    big = {"x": "y" * (MAX_RESULT_CHARS * 2)}
    out = clip(big)
    assert out["truncated"] is True and len(out["partial"]) == MAX_RESULT_CHARS
    assert clip({"a": 1}) == {"a": 1}


def run(coro):
    return asyncio.run(coro)


def test_read_tools_call_lab_api(fake_lab, recorded) -> None:
    lab = fake_lab("hub-down")
    ctx = ToolContext(lab=lab.client(), bot="PING", hosts=HOSTS)
    ok, result = run(run_tool(ALL_TOOLS["get_impact"], ctx, {"host": "hub-b"}))
    assert ok and result["by_tier"]
    ok, result = run(run_tool(ALL_TOOLS["list_devices"], ctx, {}))
    assert ok and result["hub-b"]["powered_off"] is True
    assert [r.url.path for r in lab.requests] == ["/impact/hub-b", "/devices"]
    assert all(r.headers["x-lab-actor"] == "grok-bot" and "x-lab-token" not in r.headers for r in lab.requests)


@pytest.mark.parametrize(
    ("tool", "args", "error"),
    [
        ("get_device_state", {"host": "hub-z"}, "unknown device"),
        ("get_device_state", {}, "'host' is required"),
        ("get_service", {"service_id": "nope"}, "SVC-0001"),
        ("get_open_problems", {"scope": "everything"}, "scope must be one of"),
        ("propose_field_repair", {"kind": "laser", "host": "hub-a", "reason": "x"}, "'kind' must be one of"),
    ],
)
def test_bad_arguments_come_back_as_errors(fake_lab, tool, args, error) -> None:
    async def propose(**kw):
        return {"proposal_id": "CHG-0001"}

    ctx = ToolContext(lab=fake_lab().client(), bot="PING", hosts=HOSTS, propose=propose)
    ok, result = run(run_tool(ALL_TOOLS[tool], ctx, args))
    assert not ok and error in result["error"]


def test_lab_api_errors_are_returned_not_raised(fake_lab) -> None:
    ctx = ToolContext(lab=fake_lab().client(), bot="PING", hosts=HOSTS)
    ok, result = run(run_tool(ALL_TOOLS["get_drift"], ctx, {}))
    assert not ok and result["status"] == 404


def test_proposals_go_through_the_engine_callback_and_observe_mode_refuses(fake_lab) -> None:
    seen = []

    async def propose(**kw):
        seen.append(kw)
        return {"proposal_id": "CHG-0007", "status": "pending"}

    ctx = ToolContext(lab=fake_lab().client(), bot="PING", hosts=HOSTS, propose=propose)
    ok, result = run(
        run_tool(
            ALL_TOOLS["propose_field_repair"],
            ctx,
            {"kind": "cut", "host": "spoke-1", "peer": "hub-a", "reason": "core cut"},
        )
    )
    assert ok and result["proposal_id"] == "CHG-0007" and ctx.proposals == ["CHG-0007"]
    assert seen == [
        {"kind": "field-repair", "host": "spoke-1", "reason": "core cut", "params": {"kind": "cut", "peer": "hub-a"}}
    ]
    ctx.autonomy = "observe"
    ok, result = run(run_tool(ALL_TOOLS["propose_apply_intent"], ctx, {"host": "hub-a", "reason": "x"}))
    assert not ok and "observe" in result["error"]


def test_finish_records_the_report(fake_lab) -> None:
    ctx = ToolContext(lab=fake_lab().client(), bot="PING", hosts=HOSTS, proposals=["CHG-0001"])
    ok, _ = run(run_tool(ALL_TOOLS["finish_investigation"], ctx, {"title": "t"}))
    assert not ok and ctx.report is None
    args = {"title": "t", "summary": "s", "root_cause": "r", "category": "config-drift", "confidence": "high"}
    ok, _ = run(run_tool(ALL_TOOLS["finish_investigation"], ctx, args))
    assert ok and ctx.report["category"] == "config-drift" and ctx.report["proposals"] == ["CHG-0001"]
