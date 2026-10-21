"""Brains: the Grok function-calling loop (scripted model) and the rules triage on recorded lab states."""

from __future__ import annotations

import asyncio
import json

from netsim_bot.brains import GrokBrain, Job, RulesBrain, group_causes, summarize
from netsim_bot.tools import ToolContext, toolset

HOSTS = ["hub-a", "hub-b", "hub-c", "hub-d", "spoke-1", "spoke-2", "spoke-3", "spoke-4", "spoke-5", "spoke-6"]


def make_job(lab, propose=None, mode="incident", autonomy="propose", max_steps=10):
    activities = []
    usage = []
    ctx = ToolContext(lab=lab, bot="PING", hosts=HOSTS, incident_id="INC-0001", autonomy=autonomy, propose=propose)
    job = Job(
        bot="PING",
        ctx=ctx,
        tools=toolset(mode, autonomy),
        emit=activities.append,
        on_usage=lambda u, m: usage.append((u, m)),
        max_steps=max_steps,
    )
    return job, activities, usage


def test_grok_loop_runs_tools_feeds_results_back_and_files_the_report(fake_lab, grok_kit) -> None:
    lab = fake_lab("fake-redundancy")
    created = []

    async def propose(**kw):
        created.append(kw)
        return {"proposal_id": f"CHG-000{len(created)}", "status": "pending", "headline": kw["host"]}

    k = grok_kit
    grok = k.FakeGrok(
        [
            k.response(
                "r1", [k.call("get_open_problems", {}), k.call("get_drift", {})], text="Checking problems and drift."
            ),
            k.response("r2", [k.call("propose_apply_intent", {"host": "hub-a", "reason": "SVC-0002 primary missing"})]),
            k.response(
                "r3",
                [
                    k.call(
                        "finish_investigation",
                        {
                            "title": "SVC-0002 redundancy lost",
                            "summary": "Both PWs on hub-b.",
                            "root_cause": "spoke-2 primary peer 10.255.0.2",
                            "category": "config-drift",
                            "confidence": "high",
                            "proposals": ["CHG-0001"],
                        },
                    )
                ],
            ),
        ]
    )
    job, activities, usage = make_job(lab.client(), propose)
    report = asyncio.run(GrokBrain(grok).investigate(job, "brief"))
    assert report["category"] == "config-drift" and report["proposals"] == ["CHG-0001"]
    assert created == [{"kind": "apply-intent", "host": "hub-a", "reason": "SVC-0002 primary missing", "params": {}}]
    first, second, third = grok.requests
    assert first["previous_response_id"] is None
    assert first["input"][0]["role"] == "system" and first["input"][1] == {"role": "user", "content": "brief"}
    assert {t["name"] for t in first["tools"]} >= {"get_drift", "propose_apply_intent", "finish_investigation"}
    # results go back with the call ids, chained to the response they answer
    assert second["previous_response_id"] == "r1"
    assert [i["call_id"] for i in second["input"]] == ["call-get_open_problems", "call-get_drift"]
    assert json.loads(second["input"][1]["output"])["findings"][0]["kind"] == "redundancy-lost"
    assert third["previous_response_id"] == "r2"
    assert len(usage) == 3
    kinds = [a.kind for a in activities]
    assert "say" in kinds and kinds.count("tool") == 4 and kinds.count("result") == 4
    tool = next(a for a in activities if a.kind == "tool" and a.tool == "get_drift")
    assert tool.station == "desk" and tool.incident_id == "INC-0001"


def test_grok_loop_nudges_once_and_forces_the_report_at_the_step_limit(fake_lab, grok_kit) -> None:
    k = grok_kit
    grok = k.FakeGrok([k.response("r1", text="I think hub-b is down."), k.response("r2", [k.call("list_devices", {})])])
    job, _, _ = make_job(fake_lab("hub-down").client(), max_steps=3)
    report = asyncio.run(GrokBrain(grok).investigate(job, "brief"))
    assert grok.requests[1]["input"][0]["content"].startswith("File your report")
    last = grok.requests[2]
    assert last["tool_choice"] == "required" and [t["name"] for t in last["tools"]] == ["finish_investigation"]
    assert report["confidence"] == "low" and report["category"] == "unknown"  # the script never filed one


def test_unknown_tool_is_reported_to_the_model(fake_lab, grok_kit) -> None:
    k = grok_kit
    grok = k.FakeGrok([k.response("r1", [k.call("reset_lab", {})]), k.response("r2", text="ok")])
    job, activities, _ = make_job(fake_lab().client(), max_steps=2)
    asyncio.run(GrokBrain(grok).investigate(job, "brief"))
    out = json.loads(grok.requests[1]["input"][0]["output"])
    assert "no tool 'reset_lab'" in out["error"]
    assert any(a.kind == "error" for a in activities)


def test_rules_brain_on_fake_redundancy_proposes_intent_for_the_three_hosts(fake_lab) -> None:
    created = []

    async def propose(**kw):
        created.append(kw)
        return {"proposal_id": f"CHG-{len(created):04d}", "status": "pending", "headline": kw["host"]}

    job, _, _ = make_job(fake_lab("fake-redundancy").client(), propose)
    report = asyncio.run(RulesBrain().investigate(job, "brief"))
    assert sorted(c["host"] for c in created) == ["hub-a", "hub-b", "spoke-2"]
    assert {c["kind"] for c in created} == {"apply-intent"}
    assert report["category"] == "config-drift" and "redundancy lost" in report["root_cause"]
    assert report["impact_by_tier"] == {"gold": {"down": 0, "degraded": 1}}
    assert report["proposals"] == ["CHG-0001", "CHG-0002", "CHG-0003"]


def test_rules_brain_on_hub_down_dispatches_the_technician_and_weighs_tiers(fake_lab) -> None:
    created = []

    async def propose(**kw):
        created.append(kw)
        return {"proposal_id": "CHG-0001", "status": "pending"}

    lab = fake_lab("hub-down")
    job, _, _ = make_job(lab.client(), propose)
    report = asyncio.run(RulesBrain().investigate(job, "brief"))
    assert created == [
        {"kind": "field-repair", "host": "hub-b", "reason": created[0]["reason"], "params": {"kind": "device"}}
    ]
    assert report["category"] == "physical-fault"
    assert report["impact_by_tier"] == {"gold": {"down": 0, "degraded": 5}, "silver": {"down": 0, "degraded": 2}}
    assert report["root_cause"].startswith("hub-b powered off") and "7 services" in report["root_cause"]
    assert "/impact/hub-b" in [r.url.path for r in lab.requests]
    assert "/devices/hub-b/state" not in [r.url.path for r in lab.requests]  # nothing to read from a dead box


def test_rules_brain_in_observe_mode_proposes_nothing(fake_lab) -> None:
    job, _, _ = make_job(fake_lab("hub-down").client(), propose=None, autonomy="observe")
    report = asyncio.run(RulesBrain().investigate(job, "brief"))
    assert report["proposals"] == [] and report["category"] == "physical-fault"


def test_rules_chat_answers_impact_questions(fake_lab) -> None:
    job, _, _ = make_job(fake_lab("hub-down").client(), mode="chat")
    text, prev = asyncio.run(RulesBrain().chat(job, "what is the impact of hub-b maintenance?", None))
    assert prev is None and text.startswith("Losing hub-b now") and "SVC-0001" in text


def test_group_causes_and_summaries(recorded) -> None:
    findings = recorded("hub-down")["GET /drift"]["findings"]
    lines = group_causes(findings)
    assert len(lines) == 1 and lines[0].startswith("hub-b powered off (container stopped) (fault) (7 services:")
    assert summarize("get_drift", True, {"findings": []}) == "0 finding(s), devices match NetBox"
    assert summarize("get_drift", False, {"error": "boom", "detail": "x"}).startswith("error: boom")
