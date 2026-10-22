"""The office end to end in one process: Zabbix alerts -> batch -> incident -> pages -> the roles' tools -> tickets
-> a human's approval -> the executor -> recovered -> closed. lab-api and the routines are fakes."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from netsim_bot.engine import Engine
from netsim_bot.store import Store


async def no_sleep(_s: float) -> None:
    await asyncio.sleep(0)


@pytest.fixture
def office(tmp_path: Path, settings_factory, fake_lab, routines):
    made: list[Engine] = []

    def make(*states: str, lab=None, **env: str) -> Engine:
        s = settings_factory(**env)
        lab = lab or fake_lab(*(states or ("healthy",)))
        eng = Engine(
            settings_loader=lambda: s,
            store=Store(tmp_path / f"bot{len(made)}.db"),
            lab=lab.client(),
            page_transport=routines.transport(),
            sleep=no_sleep,
        )
        eng.paging.delays = (0.0, 0.0, 0.0)
        eng.fake = lab  # type: ignore[attr-defined]
        made.append(eng)
        return eng

    return make


def run(coro):
    return asyncio.run(coro)


async def settle(eng: Engine) -> None:
    eng.office_tick()
    await eng.paging.drain()


async def tool(eng: Engine, role: str, name: str, **args: Any) -> dict[str, Any]:
    ok, result = await eng.gateway.call(role, name, args)
    assert ok, f"{role} {name}: {result}"
    return result


def test_alert_storm_to_closed_incident(office, routines, kit) -> None:
    async def scenario():
        eng = office("healthy")
        await eng.poll_lab()  # the topology: loopbacks for the PW peers
        for body in kit.hub_a_storm():
            eng.receive_alert(body)
        await settle(eng)
        [inc] = eng.incidents.values()
        assert routines.events() == ["incident.opened"] and routines.pages[0]["path"] == "/noceng"
        page = routines.pages[0]["body"]
        assert [c["name"] for c in page["incident"]["customers"]][:3] == ["Customer-A", "Customer-B", "Customer-E"]
        assert len(page["incident"]["customers"]) == 6 and page["customers"][0] == "Customer-A"
        assert "take_incident" in page["how_to_respond"] and page["links"]["noc_room"] == "http://127.0.0.1:8095"
        assert eng.roles["noc-engineer"].status == "paged"

        # the NOC Engineer works it
        await tool(eng, "noc-engineer", "take_incident", incident_id=inc.id)
        ack = await tool(eng, "noc-engineer", "ack_zabbix_problems", incident_id=inc.id, message="on it, hub-a dark")
        assert "noc-engineer" in ack["summary"]
        [sent] = eng.fake.acks
        assert sent["role"] == "noc-engineer" and sent["actor"] == "grok-bot/NOCENG" and sent["token"] == "lab-token"
        assert sorted(sent["eventids"]) == [str(4683 + i) for i in range(6)]  # every customer's service event
        await tool(
            eng,
            "noc-engineer",
            "submit_triage",
            incident_id=inc.id,
            summary="hub-a powered off",
            category="physical-fault",
        )
        r = await tool(
            eng,
            "noc-engineer",
            "propose_field_repair",
            incident_id=inc.id,
            kind="device",
            host="hub-a",
            reason="hub-a is powered off: power it on",
        )
        ticket = r["proposal_id"]
        assert eng.incidents[inc.id].status == "awaiting-approval"
        await settle(eng)  # the ticket batch closes: the NOC Manager hears about it
        assert routines.events("/nocmgr") == ["tickets.filed"]
        await tool(
            eng, "noc-manager", "recommend_ticket", proposal_id=ticket, recommendation="approve", reason="gold at risk"
        )

        # a human approves; the field repair runs off-screen
        await eng.approve(ticket, by="alice")
        await eng._execute(ticket)
        p = eng.store.proposal(ticket)
        assert p.status == "applied" and [s["stage"] for s in p.progress] == ["dispatched", "on-site", "repaired"]
        assert p.recommendations[0]["by"] == "NOCMGR" and p.recommendations[0]["recommendation"] == "approve"
        assert "late" not in p.recommendations[0]
        # advice held up in the app (an approval card) lands after the human decided: kept, marked late
        late = await tool(
            eng, "noc-manager", "recommend_ticket", proposal_id=ticket, recommendation="approve", reason="x"
        )
        assert "already applied" in late["summary"]
        assert eng.store.proposal(ticket).recommendations[-1]["late"] == "applied"
        [(_, body, headers)] = eng.fake.posts("/faults")
        assert body == {"kind": "device", "host": "hub-a", "action": "restore"}
        assert headers["x-lab-actor"] == "grok-bot/NOCENG approved-by:alice"

        # Zabbix: everyone OK again -> one "recovered" page; the NOC Engineer verifies and closes
        eng.fake.customer_status = {"Customer-F": 5}
        for c in sorted(kit.HUB_A_DOWN):
            eng.receive_alert(kit.zabbix_body(c, kind="recovery"))
        await settle(eng)
        assert routines.events("/noceng") == ["incident.opened", "incident.recovered"]
        ok, refused = await eng.gateway.call("noc-engineer", "close_incident", {"incident_id": inc.id, "summary": "x"})
        assert not ok and "Customer-F status 5" in refused["error"]
        eng.fake.customer_status = {}
        await tool(eng, "noc-engineer", "close_incident", incident_id=inc.id, summary="hub-a powered on, all OK")
        closed = eng.store.incident(inc.id)
        assert closed.status == "closed" and closed.closing["by"] == "NOCENG" and inc.id not in eng.incidents
        kinds = [a.kind for a in eng.store.activities(incident_id=inc.id, limit=500)]
        for k in ("alarm", "page", "take", "ack", "triage", "propose", "approve", "field", "close"):
            assert k in kinds, k

    run(scenario())


def test_escalation_root_cause_and_a_config_ticket(office, routines, kit) -> None:
    async def scenario():
        eng = office("healthy", "fake-redundancy")
        await eng.poll_lab()
        eng.receive_alert(kit.zabbix_body("Customer-A", problems=[("spoke-2", "SVC-0002", "primary", "hub-a")]))
        await settle(eng)
        [inc] = eng.incidents.values()
        await tool(eng, "noc-engineer", "take_incident", incident_id=inc.id)
        await tool(eng, "noc-engineer", "submit_triage", incident_id=inc.id, summary="SVC-0002 primary off-spec")
        r = await tool(
            eng,
            "noc-engineer",
            "escalate",
            incident_id=inc.id,
            to="network-engineer",
            note="looks like drift on spoke-2",
        )
        assert "network-engineer" in r["summary"]
        await eng.paging.drain()
        [esc] = [p["body"] for p in routines.pages if p["path"] == "/neteng"]
        assert esc["event"] == "incident.escalated" and esc["escalation"]["note"] == "looks like drift on spoke-2"
        assert esc["triage"]["summary"] == "SVC-0002 primary off-spec" and "submit_root_cause" in esc["how_to_respond"]
        await tool(
            eng,
            "network-engineer",
            "submit_root_cause",
            incident_id=inc.id,
            title="spoke-2 repointed: SVC-0002 redundancy lost",
            summary="s",
            root_cause="spoke-2 primary peer 10.255.0.2",
            category="config-drift",
        )
        assert eng.incidents[inc.id].title == "spoke-2 repointed: SVC-0002 redundancy lost"
        r = await tool(
            eng, "network-engineer", "propose_apply_intent", incident_id=inc.id, host="hub-a", reason="NetBox wins"
        )
        p = eng.store.proposal(r["proposal_id"])
        assert (p.proposed_by, p.role, p.kind, p.status) == ("NETENG", "network-engineer", "apply-intent", "pending")
        assert p.plan["plan_id"] and p.plan["diff"]
        [(_, body, headers)] = eng.fake.posts("/devices/hub-a/apply-intent")
        assert body["confirm"] is False and headers["x-lab-actor"] == "grok-bot/NETENG"  # a dry run, nothing more
        # the same change filed twice is one ticket
        r2 = await tool(
            eng, "network-engineer", "propose_apply_intent", incident_id=inc.id, host="hub-a", reason="again"
        )
        assert r2["proposal_id"] == p.id
        await eng.approve(p.id, by="bob")
        await eng._execute(p.id)
        applied = eng.store.proposal(p.id)
        assert applied.status == "applied"
        confirm = eng.fake.posts("/devices/hub-a/apply-intent")[-1]
        assert confirm[1]["confirm"] is True and confirm[1]["plan_id"] == p.plan["plan_id"]
        assert confirm[2]["x-lab-actor"] == "grok-bot/NETENG approved-by:bob"

    run(scenario())


def test_roles_stay_in_their_lane(office, kit) -> None:
    async def scenario():
        eng = office("healthy")
        await eng.poll_lab()
        eng.receive_alert(kit.zabbix_body("Customer-C"))
        await settle(eng)
        [inc] = eng.incidents.values()
        ok, r = await eng.gateway.call("noc-manager", "propose_apply_intent", {"incident_id": inc.id, "host": "hub-a"})
        assert not ok and "belongs to the Network Engineer (network-engineer)" in r["error"]
        ok, r = await eng.gateway.call("network-engineer", "close_incident", {"incident_id": inc.id, "summary": "x"})
        assert not ok and "belongs to the NOC Engineer" in r["error"] and "you are the Network Engineer" in r["error"]
        ok, r = await eng.gateway.call("noc-engineer", "propose_field_repair", {"kind": "x"})
        assert not ok and "incident" in r["error"]
        ok, r = await eng.gateway.call(
            "network-engineer", "escalate", {"incident_id": inc.id, "to": "network-engineer", "note": "n"}
        )
        assert not ok and "escalates to noc-manager" in r["error"]
        ok, r = await eng.gateway.call(
            "noc-manager", "set_priority", {"incident_id": inc.id, "priority": "P9", "reason": "r"}
        )
        assert not ok and "P1" in r["error"]
        await tool(eng, "noc-manager", "set_priority", incident_id=inc.id, priority="P2", reason="silver degraded")
        assert eng.incidents[inc.id].priority == "P2"
        names = {t["name"] for t in eng.gateway.tools("noc-manager")}
        assert "approve" not in " ".join(names) and "propose_apply_intent" not in names
        await eng.set_control("op", autonomy="observe")
        assert not {t["name"] for t in eng.gateway.tools("network-engineer")} & {
            "propose_apply_intent",
            "propose_config_patch",
        }
        ok, r = await eng.gateway.call(
            "network-engineer", "propose_apply_intent", {"incident_id": inc.id, "host": "hub-a"}
        )
        assert not ok and "observe" in r["error"]

    run(scenario())


def test_maintenance_and_mute_record_alerts_only(office, routines, kit) -> None:
    async def scenario():
        eng = office("healthy")
        await eng.set_control("op", paused=True, reason="planned work")
        a = eng.receive_alert(kit.zabbix_body("Customer-A"))
        await eng.set_control("op", paused=False, pages_muted=True)
        b = eng.receive_alert(kit.zabbix_body("Customer-B"))
        await settle(eng)
        assert (a.outcome, b.outcome) == ("recorded: maintenance mode", "recorded: pages muted")
        assert eng.incidents == {} and routines.pages == []
        await eng.set_control("op", pages_muted=False)
        c = eng.receive_alert(kit.zabbix_body("Customer-B", event_id="4800"))
        await eng.set_control("op", paused=True)  # maintenance starts before the batch closes
        await settle(eng)
        assert eng.store.alerts(limit=1)[0].outcome == "recorded: maintenance mode" and c.id
        assert eng.incidents == {} and routines.pages == []

    run(scenario())


def test_gold_down_pages_the_manager_and_the_zabbix_escalation_too(office, routines, kit) -> None:
    async def scenario():
        eng = office("healthy")
        await eng.poll_lab()
        eng.receive_alert(kit.zabbix_body("Customer-E", severity="Disaster"))
        await settle(eng)
        assert routines.events("/noceng") == ["incident.opened"]
        assert routines.events("/nocmgr") == ["incident.gold-down"]
        eng.receive_alert(kit.zabbix_body("Customer-E", kind="escalation", role="noc-manager"))
        await settle(eng)
        assert routines.events("/nocmgr") == ["incident.gold-down", "incident.zabbix-escalation"]
        body = routines.pages[-1]["body"]
        assert body["customers"] == ["Customer-E"] and "set_priority" in body["how_to_respond"]

    run(scenario())


def test_ask_the_noc(office, routines) -> None:
    async def scenario():
        eng = office("healthy")
        q = await eng.ask("What happens if hub-c goes down for maintenance?", by="alice")
        await eng.paging.drain()
        [page] = [p["body"] for p in routines.pages]
        assert page["event"] == "question" and page["question"]["question_id"] == q.id
        assert page["question"]["text"].startswith("What happens if hub-c")
        status = await tool(eng, "noc-manager", "get_noc_status")
        assert status["open_questions"][0]["question_id"] == q.id
        await tool(eng, "noc-manager", "answer_question", question_id=q.id, answer="SVC-0009 (bronze) goes down; ...")
        chat = eng.store.chat(q.chat_id)
        assert [(m.role, m.bot) for m in chat] == [("user", "alice"), ("bot", "NOCMGR")] and chat[
            0
        ].status == "answered"
        ok, r = await eng.gateway.call("noc-manager", "answer_question", {"question_id": q.id, "answer": "twice"})
        assert not ok and "answered already" in r["error"]
        # unanswered questions time out with a message
        q2 = await eng.ask("anything else?")
        stored = eng.store.chat_message(q2.id)
        stored.ts = time.time() - 10_000
        eng.store.save_chat(stored)
        eng.office_tick()
        last = eng.store.chat(q2.chat_id)[-1]
        assert (
            eng.store.chat_message(q2.id).status == "timed-out" and last.role == "system" and "No answer" in last.text
        )

    run(scenario())


def test_a_question_without_a_routine_says_so(office) -> None:
    async def scenario():
        eng = office("healthy", BOT_ROUTINE_NOCMGR_URL="")
        q = await eng.ask("status?")
        [system] = [m for m in eng.store.chat(q.chat_id) if m.role == "system"]
        assert "BOT_ROUTINE_NOCMGR_URL is not set" in system.text and eng.store.chat_message(q.id).status == "open"

    run(scenario())


def test_a_restart_keeps_open_incidents_and_redoes_open_batches(office, routines, kit, tmp_path) -> None:
    async def scenario():
        eng = office("healthy", BOT_BATCH_S="30", BOT_MAX_BATCH_S="60")
        await eng.poll_lab()
        eng.receive_alert(kit.zabbix_body("Customer-D"))
        await settle(eng)
        assert routines.pages == []  # the batch is still open
        store = eng.store
        again = Engine(
            settings_loader=eng._load, store=store, lab=eng.fake.client(), page_transport=routines.transport()
        )
        again.paging.delays = (0.0, 0.0, 0.0)
        [batch] = again.batcher.open.values()
        assert len(batch.items) == 1 and batch.items[0].customer == "Customer-D"
        for b in again.batcher.due(time.time() + 100):
            again._close_batch(b.items, time.time())
        await again.paging.drain()
        assert routines.events() == ["incident.opened"]
        third = Engine(settings_loader=eng._load, store=store, lab=eng.fake.client())
        assert list(third.incidents) == list(again.incidents)

    run(scenario())


def test_an_old_crew_database_is_kept_aside(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE incidents (id TEXT PRIMARY KEY, created REAL, updated REAL, status TEXT, doc TEXT);"
        "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO incidents VALUES ('INC-0041', 1, 1, 'resolved', '{\"id\": \"INC-0041\", \"problems\": {}}');"
        "INSERT INTO meta VALUES ('seq:INC', '41');"
    )
    con.commit()
    con.close()
    st = Store(db)
    assert st.incidents() == [] and st.next_id("INC") == "INC-0042"
    tables = {r[0] for r in st._exec("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "incidents_v1" in tables and "alerts" in tables and "pages" in tables
