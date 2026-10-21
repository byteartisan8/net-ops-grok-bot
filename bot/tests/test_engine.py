"""The engine on a fake lab-api (recorded states): watcher -> incident -> investigation -> proposals -> approval ->
executor (make before break, exact plan_id, approver in the audit actor) -> resolution; 409 re-plan, auto mode,
maintenance mode, chat, restart recovery and the Grok fallbacks."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from netsim_bot.engine import Engine, change_order
from netsim_bot.grok import GrokError
from netsim_bot.models import Incident, Proposal
from netsim_bot.store import Store

APPLIED = {
    "applied": True,
    "elapsed_s": 11.2,
    "verification": {
        "verdict": "ok",
        "problems": [],
        "simd": {"pseudowires": {}},
        "zabbix": {"services": {"SVC-0002": "OK"}, "settled": True},
        "drift_after": [],
    },
}


def intent_route(dry: dict[str, Any], confirm_status: int = 200, confirm_body: dict[str, Any] | None = None):
    def route(request: httpx.Request) -> tuple[int, dict[str, Any]]:
        body = json.loads(request.content or b"{}")
        if body.get("confirm"):
            return confirm_status, confirm_body or {**APPLIED, "plan_id": dry["plan_id"], "host": dry["host"]}
        return 200, dry

    return route


def lab_for(fake_lab, state: str):
    lab = fake_lab("healthy", state)
    for key in list(lab.routes):
        if key.startswith("POST /devices/") and key.endswith("/apply-intent"):
            lab.set(key, intent_route(lab.routes[key]))
    return lab


def engine(settings_factory, tmp_path, lab, **env) -> Engine:
    s = settings_factory(**env)
    return Engine(settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client())


async def until(cond, timeout=5.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


async def drain(eng: Engine) -> list[str]:
    done = []
    while not eng._exec_queue.empty():
        pid = eng._exec_queue.get_nowait()
        await eng._execute(pid)
        done.append(pid)
    return done


def test_full_loop_fake_redundancy(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "fake-redundancy")

    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, lab, BOT_BRAIN="rules")
        await eng.tick()
        assert len(eng.tracker.incidents) == 1
        inc = next(iter(eng.tracker.incidents.values()))
        assert inc.assignee == "PING" and inc.status == "investigating"
        await until(lambda: inc.status == "awaiting-approval")
        pending = eng.store.proposals(incident_id=inc.id, status="pending")
        assert sorted(p.host for p in pending) == ["hub-a", "hub-b", "spoke-2"]
        assert inc.report["category"] == "config-drift" and inc.brain == "rules"
        # a second tick while busy changes nothing
        await eng.tick()
        approved = await eng.approve_incident(inc.id, "tester", "looks right")
        # make before break: hub-a only adds, spoke-2 changes a peer, hub-b only removes
        assert [p.host for p in approved] == ["hub-a", "spoke-2", "hub-b"]
        assert inc.status == "remediating"
        executed = await drain(eng)
        assert executed == [p.id for p in approved]
        confirms = [(path, body, h) for path, body, h in lab.posts("/devices/") if body.get("confirm")]
        assert [c[0] for c in confirms] == [
            "/devices/hub-a/apply-intent",
            "/devices/spoke-2/apply-intent",
            "/devices/hub-b/apply-intent",
        ]
        for (_path, body, headers), p in zip(confirms, approved, strict=True):
            assert body["plan_id"] == p.plan["plan_id"] and headers["x-lab-token"] == "lab-token"
            assert headers["x-lab-actor"] == "grok-bot/PING approved-by:tester"
        assert all(
            p.status == "applied" and p.result["verdict"] == "ok" for p in eng.store.proposals(incident_id=inc.id)
        )
        assert inc.status == "monitoring"
        # Zabbix clears: resolved
        lab.load("healthy")
        await eng.tick()
        assert inc.status == "resolved" and not eng.tracker.incidents
        kinds = [a.kind for a in eng.store.activities(incident_id=inc.id)]
        for k in ("alarm", "assign", "tool", "propose", "report", "await", "approve", "apply", "verify", "resolve"):
            assert k in kinds, k
        await eng.stop()

    asyncio.run(scenario())


def test_stale_plan_goes_back_to_pending_with_the_new_plan(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "fake-redundancy")
    dry = lab.routes  # keep the recorded dry run of hub-a

    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, lab, BOT_BRAIN="rules")
        await eng.tick()
        inc = next(iter(eng.tracker.incidents.values()))
        await until(lambda: inc.status == "awaiting-approval")
        p = next(x for x in eng.store.proposals(status="pending") if x.host == "hub-a")
        new_plan = {
            **dry["POST /devices/hub-a/apply-intent"](httpx.Request("POST", "http://x", json={}))[1],
            "plan_id": "feedface",
        }
        lab.set(
            "POST /devices/hub-a/apply-intent",
            intent_route(new_plan, 409, {"detail": {"message": "the plan changed", "plan": new_plan}}),
        )
        await eng.approve(p.id, "tester")
        await drain(eng)
        again = eng.store.proposal(p.id)
        assert again.status == "pending" and again.plan["plan_id"] == "feedface" and again.decided_by is None
        assert inc.status == "awaiting-approval"
        await eng.stop()

    asyncio.run(scenario())


def test_auto_mode_approves_by_policy_and_field_repair_restores(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "hub-down")
    lab.set("POST /faults", lambda r: (200, {"done": "hub-b powered on", "faults": [], "stopped": []}))

    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, lab, BOT_BRAIN="rules", BOT_AUTONOMY="auto")
        await eng.tick()
        inc = next(iter(eng.tracker.incidents.values()))
        await until(lambda: not eng._exec_queue.empty())
        await until(lambda: inc.status in ("remediating", "awaiting-approval", "diagnosed"))
        p = eng.store.proposals(incident_id=inc.id)[0]
        assert p.kind == "field-repair" and p.decided_by == "policy:auto"
        await drain(eng)
        _path, body, headers = lab.posts("/faults")[0]
        assert body == {"kind": "device", "host": "hub-b", "action": "restore"}
        assert headers["x-lab-actor"] == "grok-bot/PING approved-by:policy:auto"
        assert eng.store.proposal(p.id).status == "applied" and eng.crew["PATCH"].status == "idle"
        assert any(a.kind == "travel" and a.bot == "PATCH" for a in eng.store.activities())
        await eng.stop()

    asyncio.run(scenario())


def test_maintenance_mode_opens_no_incident(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "hub-down")

    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, lab, BOT_BRAIN="rules")
        await eng.set_control("tester", paused=True, reason="e2e")
        await eng.tick()
        assert not eng.tracker.incidents and eng.crew["PING"].status == "paused"
        assert eng.state()["effective"]["paused"] is True
        await eng.set_control("tester", paused=False)
        await eng.tick()
        assert len(eng.tracker.incidents) == 1
        await eng.stop()

    asyncio.run(scenario())


def test_cleared_before_a_decision_makes_proposals_obsolete(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "fake-redundancy")

    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, lab, BOT_BRAIN="rules")
        await eng.tick()
        inc = next(iter(eng.tracker.incidents.values()))
        await until(lambda: inc.status == "awaiting-approval")
        lab.load("healthy")
        await eng.tick()
        assert inc.status == "resolved"
        assert {p.status for p in eng.store.proposals(incident_id=inc.id)} == {"obsolete"}
        await eng.stop()

    asyncio.run(scenario())


def test_chat_with_the_rules_brain(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "hub-down")

    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, lab, BOT_BRAIN="rules")
        await eng.set_control("tester", paused=True)  # no incident takes the responders
        await eng.tick()
        msg = await eng.ask("what happens if hub-b is lost?")
        await until(lambda: len(eng.store.chat(msg.chat_id)) == 2)
        answer = eng.store.chat(msg.chat_id)[-1]
        assert answer.role == "bot" and answer.text.startswith("Losing hub-b now")
        await eng.stop()

    asyncio.run(scenario())


def test_grok_failure_falls_back_to_rules_and_marks_grok_unavailable(fake_lab, settings_factory, tmp_path) -> None:
    lab = lab_for(fake_lab, "hub-down")

    class BrokenGrok:
        model = "grok-4.7"

        async def respond(self, **_kw):
            raise GrokError("xAI API HTTP 401: bad key", "auth", 401)

        async def aclose(self):
            pass

    async def scenario() -> None:
        s = settings_factory(XAI_API_KEY="xai-bad")
        eng = Engine(
            settings_loader=lambda: s,
            store=Store(tmp_path / "bot.db"),
            lab=lab.client(),
            grok_factory=lambda _s: BrokenGrok(),
        )
        assert eng.effective()["brain"] == "grok"
        await eng.tick()
        inc = next(iter(eng.tracker.incidents.values()))
        await until(lambda: inc.status in ("awaiting-approval", "diagnosed"))
        assert inc.brain == "rules" and any("Grok failed" in n for n in inc.notes)
        eff = eng.effective()
        assert eff["brain"] == "rules" and "Grok unavailable" in eff["why"]
        await eng.stop()

    asyncio.run(scenario())


def test_grok_investigation_is_accounted(fake_lab, settings_factory, tmp_path, grok_kit) -> None:
    lab = lab_for(fake_lab, "hub-down")
    k = grok_kit
    script = [
        k.response("r1", [k.call("list_devices", {})], text="Who is down?"),
        k.response(
            "r2",
            [k.call("propose_field_repair", {"kind": "device", "host": "hub-b", "reason": "hub-b powered off"})],
        ),
        k.response(
            "r3",
            [
                k.call(
                    "finish_investigation",
                    {
                        "title": "hub-b powered off",
                        "summary": "s",
                        "root_cause": "hub-b off",
                        "category": "physical-fault",
                        "confidence": "high",
                    },
                )
            ],
        ),
    ]
    fake = k.FakeGrok(script)

    async def scenario() -> None:
        s = settings_factory(XAI_API_KEY="xai-ok")
        eng = Engine(
            settings_loader=lambda: s, store=Store(tmp_path / "bot.db"), lab=lab.client(), grok_factory=lambda _s: fake
        )
        await eng.tick()
        inc = next(iter(eng.tracker.incidents.values()))
        await until(lambda: inc.status == "awaiting-approval")
        assert inc.brain == "grok" and inc.model == "grok-4.7" and inc.title == "hub-b powered off"
        assert inc.usage["calls"] == 3 and inc.usage["usd"] > 0
        assert eng.usage["usd_day"] == inc.usage["usd"] and eng.usage["input_tokens"] == 3000
        assert "Incident INC-0001" in fake.requests[0]["input"][1]["content"]
        await eng.stop()

    asyncio.run(scenario())


def test_budget_exhausted_means_rules(fake_lab, settings_factory, tmp_path) -> None:
    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, fake_lab("healthy"), XAI_API_KEY="xai-ok", BOT_DAILY_BUDGET_USD="0.5")
        eng.usage["usd_day"] = 0.75
        eff = eng.effective()
        assert eff["brain"] == "rules" and "budget" in eff["why"]
        await eng.stop()

    asyncio.run(scenario())


def test_restart_recovery(fake_lab, settings_factory, tmp_path) -> None:
    store = Store(tmp_path / "bot.db")
    store.save_incident(Incident(id="INC-0001", status="investigating", assignee="PING", problems={}))
    store.save_proposal(
        Proposal(
            id="CHG-0001",
            incident_id="INC-0001",
            kind="apply-intent",
            host="hub-a",
            reason="r",
            proposed_by="PING",
            brain="rules",
            status="applying",
        )
    )
    store.close()

    async def scenario() -> None:
        eng = engine(settings_factory, tmp_path, fake_lab("healthy"))
        assert eng.store.incident("INC-0001").status == "open"
        p = eng.store.proposal("CHG-0001")
        assert p.status == "failed" and "restart" in p.error
        await eng.stop()

    asyncio.run(scenario())


def test_change_order() -> None:
    def prop(changes, kind="apply-intent"):
        return Proposal(
            id="x",
            incident_id=None,
            kind=kind,
            host="h",
            reason="r",
            proposed_by="P",
            brain="rules",
            plan={"diff": [{"change": c} for c in changes]},
        )

    ranks = [change_order(prop(c))[0] for c in (["added"], ["changed"], ["removed"], ["added", "removed"])]
    assert ranks == [1, 2, 3, 2]
    assert change_order(prop([], kind="field-repair"))[0] == 0
