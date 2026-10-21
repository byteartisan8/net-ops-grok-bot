"""OPT-IN (``make bot-eval``, costs money): Grok diagnoses real faults on the live lab, graded against the scenarios.

For each case: reset, apply the scenario, let the bot (brain forced to Grok) open and investigate the incident, then
grade the report and the proposals against what the scenario file says happened. Nothing is applied: every proposal
is rejected and the lab reset afterwards. Prints a scorecard with time to diagnosis and cost, and writes it to
``build/grok-bot-eval/results.json``.

Hard requirements per case (the test fails): the right category and every expected fix proposed. Extra proposals,
root-cause wording and the tier counts are scored, not required.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from netsim_bot.settings import find_repo_root, load_settings

pytestmark = pytest.mark.skipif(
    os.environ.get("BOT_EVAL") != "1" or not load_settings().xai_api_key,
    reason="opt-in: BOT_EVAL=1 and XAI_API_KEY in .env (make bot-eval)",
)

CASES: list[dict[str, Any]] = [
    {
        "scenario": "vcid-typo",
        "category": "config-drift",
        "fixes": {("apply-intent", "spoke-1")},
        "mentions": ["spoke-1", "2010"],
    },
    {
        "scenario": "mtu-mismatch",
        "category": "config-drift",
        "fixes": {("apply-intent", "hub-b")},
        "mentions": ["hub-b", "mtu"],
    },
    {
        "scenario": "fake-redundancy",
        "category": "config-drift",
        "fixes": {("apply-intent", "hub-a"), ("apply-intent", "hub-b"), ("apply-intent", "spoke-2")},
        "mentions": ["hub-b", "SVC-0002"],
    },
    {
        "scenario": "stale-backup",
        "category": "config-drift",
        "fixes": {("apply-intent", "spoke-1"), ("apply-intent", "hub-b")},
        "mentions": ["SVC-0001", "SVC-0005"],
    },
    {
        "scenario": "hub-down",
        "category": "physical-fault",
        "fixes": {("field-repair", "hub-b")},
        "mentions": ["hub-b"],
        "tiers": {"gold": {"degraded": 5}, "silver": {"degraded": 2}},
    },
    {
        "scenario": "region-down",
        "category": "physical-fault",
        "fixes": {("field-repair", "hub-c"), ("field-repair", "hub-d")},
        "mentions": ["hub-c", "hub-d"],
        "tiers": {"gold": {"down": 1}, "silver": {"down": 1}, "bronze": {"down": 1}},
    },
]
RESULTS: list[dict[str, Any]] = []


@pytest.fixture(scope="module")
def grok_bot(bot):
    bot.control(brain="grok", autonomy="propose", paused=False)
    yield bot
    bot.control(brain="rules")
    if RESULTS:
        out = (find_repo_root() or Path.cwd()) / "build" / "grok-bot-eval"
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(json.dumps(RESULTS, indent=2) + "\n", encoding="utf-8")
        total = sum(r["score"] for r in RESULTS)
        best = sum(r["max"] for r in RESULTS)
        cost = sum(r["usd"] for r in RESULTS)
        print(f"\n{'scenario':<18}{'score':>7}{'diag s':>8}{'USD':>9}  notes")
        for r in RESULTS:
            notes = "; ".join(r["notes"])
            print(f"{r['scenario']:<18}{r['score']:>4}/{r['max']:<2}{r['seconds']:>8.1f}{r['usd']:>9.4f}  {notes}")
        print(f"{'total':<18}{total:>4}/{best:<2}{'':>8}{cost:>9.4f}  (results in {out / 'results.json'})")


@pytest.mark.parametrize("case", CASES, ids=[c["scenario"] for c in CASES])
def test_grok_diagnoses(grok_bot, lab_api, green, scenario, case) -> None:
    bot = grok_bot
    green()
    t0 = scenario(case["scenario"])
    inc = bot.new_incident(t0, timeout=150)
    inc = bot.until_status(inc["id"], ("awaiting-approval", "diagnosed"), timeout=600)
    seconds = (inc.get("updated") or time.time()) - t0
    report = inc["report"] or {}
    proposals = [p for p in inc["proposals"] if p["status"] == "pending"]
    got = {(p["kind"], p["host"]) for p in proposals}
    notes, score, best = [], 0, 6
    if inc.get("brain") != "grok":
        notes.append(f"brain was {inc.get('brain')}: {inc.get('notes')}")
    if report.get("category") == case["category"]:
        score += 1
    else:
        notes.append(f"category {report.get('category')}")
    missing, extra = case["fixes"] - got, got - case["fixes"]
    score += 2 if not missing and not extra else 1 if not missing else 0
    if missing:
        notes.append(f"missing fixes {sorted(missing)}")
    if extra:
        notes.append(f"extra fixes {sorted(extra)}")
    text = json.dumps(report).lower()
    hits = [m for m in case["mentions"] if m.lower() in text]
    score += 1 if len(hits) == len(case["mentions"]) else 0
    if len(hits) != len(case["mentions"]):
        notes.append(f"report does not mention {sorted(set(case['mentions']) - set(hits))}")
    tiers = case.get("tiers")
    tiers_ok = True
    if tiers:
        have = report.get("impact_by_tier") or {}
        for tier, counts in tiers.items():
            for k, v in counts.items():
                if int((have.get(tier) or {}).get(k, 0)) != v:
                    tiers_ok = False
        if not tiers_ok:
            notes.append(f"impact_by_tier {json.dumps(have)}")
    score += 1 if tiers_ok else 0
    score += 1 if report.get("confidence") in ("medium", "high") and not missing else 0
    usd = float((inc.get("usage") or {}).get("usd") or 0.0)
    RESULTS.append(
        {
            "scenario": case["scenario"],
            "incident": inc["id"],
            "score": score,
            "max": best,
            "seconds": round(seconds, 1),
            "usd": round(usd, 5),
            "calls": (inc.get("usage") or {}).get("calls"),
            "category": report.get("category"),
            "fixes": sorted(got),
            "title": report.get("title"),
            "root_cause": report.get("root_cause"),
            "notes": notes or ["ok"],
        }
    )
    for p in proposals:  # grade only: nothing is applied
        bot.post(f"/api/proposals/{p['id']}/reject", {"comment": "bot-eval: graded, not applied"})
    green()
    assert inc.get("brain") == "grok", f"the incident was not investigated by Grok: {inc.get('notes')}"
    assert report.get("category") == case["category"], f"{case['scenario']}: category {report.get('category')}"
    assert not missing, f"{case['scenario']}: missing fixes {sorted(missing)} (got {sorted(got)})"
