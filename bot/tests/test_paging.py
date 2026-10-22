"""Paging the roles' routines: delivery, retries, the hourly cap, skips, the test sink."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from netsim_bot.paging import Paging, sink_allowed
from netsim_bot.store import Store


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def paging(tmp_path: Path, settings_factory, routines):
    def make(**env):
        s = settings_factory(**env)
        store = Store(tmp_path / f"p{len(env)}.db")
        recorded: list = []
        pg = Paging(lambda: s, store, lambda p: recorded.append((p.id, p.status)), transport=routines.transport())
        pg.delays = (0.0, 0.0, 0.0)
        return pg, store, recorded

    return make


def test_a_page_is_one_post_with_the_routines_key(paging, routines) -> None:
    async def scenario():
        pg, store, recorded = paging()
        p = pg.page("noc-engineer", "incident.opened", "INC-0001 opened", {"incident": {"id": "INC-0001"}})
        assert p.status == "queued"
        await pg.drain()
        return store, recorded, p

    store, recorded, p = run(scenario())
    [sent] = routines.pages
    assert sent["url"] == "https://routines.test/noceng" and sent["auth"] == "Bearer crsr_noceng"
    body = sent["body"]
    assert (body["event"], body["role"], body["page_id"], body["summary"]) == (
        "incident.opened",
        "noc-engineer",
        p.id,
        "INC-0001 opened",
    )
    assert body["incident"] == {"id": "INC-0001"} and body["source"] == "netsim-noc"
    final = store.page(p.id)
    assert (final.status, final.http, final.attempts, final.sink) == ("sent", 200, 1, False)
    assert recorded == [(p.id, "queued"), (p.id, "sent")]


def test_a_pasted_bearer_prefix_is_stripped(paging, routines) -> None:
    async def scenario():
        pg, _, _ = paging()
        pg.page("network-engineer", "incident.escalated", "s", {})
        await pg.drain()

    run(scenario())
    assert routines.pages[0]["auth"] == "Bearer crsr_neteng"  # BOT_ROUTINE_NETENG_KEY was "Bearer crsr_neteng"


def test_retries_on_5xx_and_429_but_not_on_4xx(paging, routines) -> None:
    async def scenario():
        pg, store, _ = paging()
        routines.status["/noceng"] = [503, 429, 200]
        a = pg.page("noc-engineer", "incident.opened", "a", {})
        await pg.drain()
        routines.status["/nocmgr"] = [401]
        b = pg.page("noc-manager", "question", "b", {})
        await pg.drain()
        return store.page(a.id), store.page(b.id)

    a, b = run(scenario())
    assert (a.status, a.attempts, a.http) == ("sent", 3, 200)
    assert (b.status, b.attempts, b.http) == ("failed", 1, 401)  # the URL or the key is wrong: no retry


def test_skips_are_logged_with_the_reason(paging, routines) -> None:
    pg, store, _ = paging(BOT_ROUTINE_NETENG_URL="", BOT_PAGE_MAX_PER_HOUR="2")

    async def scenario():
        out = [
            pg.page("network-engineer", "incident.escalated", "no url", {}),
            pg.page("noc-engineer", "incident.opened", "muted", {}, muted=True),
            pg.page("noc-engineer", "incident.opened", "maintenance", {}, paused=True),
            pg.page("noc-manager", "x", "1", {}),
            pg.page("noc-manager", "x", "2", {}),
            pg.page("noc-manager", "x", "3", {}),
        ]
        await pg.drain()
        return out

    pages = run(scenario())
    whys = [p.why for p in pages]
    assert whys[:3] == ["BOT_ROUTINE_NETENG_URL is not set: no routine to page", "pages muted", "maintenance mode"]
    assert whys[3:5] == [None, None] and whys[5] == "hourly cap: BOT_PAGE_MAX_PER_HOUR=2"
    assert [p.status for p in store.pages(role="noc-manager")] == ["skipped", "sent", "sent"]
    assert len(routines.pages) == 2


def test_the_test_sink_never_gets_a_real_key_and_has_no_cap(paging, routines) -> None:
    pg, store, _ = paging(BOT_PAGE_MAX_PER_HOUR="1", BOT_ROUTINE_NOCMGR_URL="")

    async def scenario():
        for _ in range(3):
            pg.page("noc-manager", "question", "q", {}, sink_url="http://172.30.10.61:8096")
        await pg.drain()

    run(scenario())
    assert [p["url"] for p in routines.pages] == ["http://172.30.10.61:8096/noc-manager"] * 3
    assert {p["auth"] for p in routines.pages} == {"Bearer test-sink"}
    assert all(p.sink and p.status == "sent" for p in store.pages())


def test_forced_test_pages_ignore_mute_and_the_cap_but_need_a_url(paging, routines) -> None:
    pg, store, _ = paging(BOT_PAGE_MAX_PER_HOUR="0", BOT_ROUTINE_NETENG_URL="")

    async def scenario():
        a = pg.page("noc-engineer", "test", "t", {}, muted=True, paused=True, force=True)
        b = pg.page("network-engineer", "test", "t", {}, force=True)
        await pg.drain()
        return store.page(a.id), store.page(b.id)

    a, b = run(scenario())
    assert a.status == "sent" and b.status == "skipped" and "BOT_ROUTINE_NETENG_URL" in b.why


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://172.30.10.61:8096", True),
        ("http://127.0.0.1:8096/x", True),
        ("http://localhost:9", True),
        ("https://example.com/hook", False),
        ("http://10.0.0.5:80", False),
        ("ftp://172.30.10.61", False),
        ("not a url", False),
    ],
)
def test_the_sink_must_be_a_lab_address(url: str, ok: bool) -> None:
    assert sink_allowed(url) is ok
