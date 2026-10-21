"""An outside agent (the Grok Bot app's path, through ``/mcp``) on the live lab: it finds the incident, diagnoses with
the same tools as the crew, files a ticket on the incident, waits while a human approves, then sees it applied and
verified, and leaves a note. Its visitor robot is in the room meanwhile."""

from __future__ import annotations

import json
from typing import Any


def _tool(agent, name: str, args: dict[str, Any] | None = None) -> tuple[bool, Any]:
    res = agent("tools/call", {"name": name, "arguments": args or {}})
    return not res["isError"], res.get("structuredContent") or json.loads(res["content"][0]["text"])


def test_the_mcp_handshake(agent) -> None:
    init = agent("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "e2e"}})
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "netsim-noc"
    names = {t["name"] for t in agent("tools/list")["tools"]}
    assert {"get_noc_status", "get_drift", "propose_apply_intent", "wait_for_proposal"} <= names
    assert not {"approve", "finish_investigation"} & names
    ok, status = _tool(agent, "get_noc_status")
    assert ok and status["lab_ok"] is True and status["autonomy"] == "propose"


def test_agent_fixes_vcid_typo_with_a_human_approval(bot, agent, lab_api, green, scenario, wait) -> None:
    green()
    t0 = scenario("vcid-typo")
    inc = bot.new_incident(t0)

    ok, drift = _tool(agent, "get_drift")
    assert ok and drift["drift"] is True
    paths = [d["path"] for d in drift["devices"]["spoke-1"].get("diffs") or []]
    assert "l2vpn/xconnect[SVC-0001]/pseudowire[backup]/vc-id" in paths
    ok, ticket = _tool(
        agent,
        "propose_apply_intent",
        {"host": "spoke-1", "reason": "spoke-1's SVC-0001 backup VC ID drifted from NetBox", "incident_id": inc["id"]},
    )
    assert ok and ticket["status"] == "pending", ticket
    assert ticket["diff"] and "2010" in " ".join(ticket["diff"])
    state = bot.get("/api/state")
    assert next(m for m in state["crew"] if m["name"] == "GROK")["status"] == "working"

    # the crew may have filed the same change: it is one ticket; a human approves every ticket of the incident
    wait(lambda: bot.incident(inc["id"])["status"] in ("awaiting-approval", "diagnosed"), 120, "the crew's report")
    bot.post(f"/api/incidents/{inc['id']}/approve-all", {"comment": "bot-e2e for the outside agent"})
    ok, done = _tool(agent, "wait_for_proposal", {"proposal_id": ticket["proposal_id"], "timeout_s": 150})
    assert ok and done["decided"] and done["status"] == "applied" and done["verdict"] == "ok", done
    assert done["decided_by"] == "bot-e2e"

    ok, _note = _tool(agent, "add_incident_note", {"incident_id": inc["id"], "text": "e2e: verified from outside"})
    assert ok
    bot.until_status(inc["id"], ("resolved",), timeout=150)
    ok, final = _tool(agent, "get_incident", {"incident_id": inc["id"]})
    assert ok and final["status"] == "resolved" and final["notes"][-1] == "GROK: e2e: verified from outside"
    assert any(a["bot"] == "GROK" and a["kind"] == "tool" for a in bot.incident(inc["id"])["activities"])
    assert lab_api.get("/drift").json()["drift"] is False
    green()
