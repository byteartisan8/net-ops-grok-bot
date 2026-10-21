"""Settings from env and .env, the SQLite store, pricing, and the static NOC page assets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from netsim_bot.grok import Usage
from netsim_bot.models import Activity, ChatMessage, Incident, Proposal
from netsim_bot.pricing import cost_usd, price_for
from netsim_bot.settings import SettingsError, load_settings, read_env_file
from netsim_bot.store import Store

WEB = Path(__file__).resolve().parents[1] / "src" / "netsim_bot" / "web"


def test_env_file_parsing(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("# c\nA=1\nexport B = two \nC='quoted value'\nD=\nE=x=y\n", encoding="utf-8")
    assert read_env_file(f) == {"A": "1", "B": "two", "C": "quoted value", "D": "", "E": "x=y"}
    assert read_env_file(tmp_path / "missing") == {}


def test_settings_defaults_env_over_file_and_live_key(tmp_path: Path) -> None:
    f = tmp_path / ".env"
    f.write_text("XAI_API_KEY=\nLAB_API_TOKEN=from-file\nGROK_MODEL=grok-4.3\n", encoding="utf-8")
    s = load_settings(environ={"GROK_MODEL": "grok-4.7"}, env_file=f)
    assert s.model == "grok-4.7" and s.lab_api_token == "from-file" and not s.grok_configured
    assert s.effective_brain() == "rules" and s.autonomy == "propose" and s.responders == 2
    f.write_text("XAI_API_KEY=xai-123\n", encoding="utf-8")  # the user adds the key while the bot runs
    s = load_settings(environ={}, env_file=f)
    assert s.grok_configured and s.effective_brain() == "grok" and s.effective_brain("rules") == "rules"
    assert "xai-123" not in str(s.public())


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"BOT_BRAIN": "gpt"}, "BOT_BRAIN"),
        ({"BOT_AUTONOMY": "yolo"}, "BOT_AUTONOMY"),
        ({"GROK_REASONING_EFFORT": "max"}, "GROK_REASONING_EFFORT"),
        ({"BOT_RESPONDERS": "9"}, "BOT_RESPONDERS"),
        ({"BOT_POLL_S": "fast"}, "BOT_POLL_S"),
    ],
)
def test_settings_validation(tmp_path: Path, env: dict[str, str], message: str) -> None:
    with pytest.raises(SettingsError, match=message):
        load_settings(environ=env, env_file=tmp_path / "none")


def test_store_roundtrip_and_counters(tmp_path: Path) -> None:
    st = Store(tmp_path / "x" / "bot.db")
    assert [st.next_id("INC") for _ in range(3)] == ["INC-0001", "INC-0002", "INC-0003"]
    assert st.next_id("MSG", 6) == "MSG-000001"
    inc = Incident(id="INC-0001", title="t", services=["SVC-0001"])
    st.save_incident(inc)
    inc.status = "resolved"
    st.save_incident(inc)
    assert st.incident("INC-0001").status == "resolved" and st.incidents(active_only=True) == []
    p = Proposal(
        id="CHG-0001",
        incident_id="INC-0001",
        kind="apply-intent",
        host="hub-a",
        reason="r",
        proposed_by="PING",
        brain="rules",
    )
    st.save_proposal(p)
    assert st.proposals(incident_id="INC-0001", status="pending")[0].host == "hub-a"
    a = st.add_activity(Activity(bot="PING", kind="tool", text="drift", incident_id="INC-0001"))
    st.add_activity(Activity(bot="POLL", kind="alarm", text="x"))
    assert a.seq == 1 and [x.seq for x in st.activities(after=1)] == [2]
    assert st.activities(incident_id="INC-0001")[0].text == "drift"
    st.save_chat(ChatMessage(id="MSG-000002", chat_id="CHAT-0001", role="user", text="q", ts=5.0))
    st.save_chat(ChatMessage(id="MSG-000003", chat_id="CHAT-0001", role="bot", text="a", ts=5.0))
    assert [m.role for m in st.chat("CHAT-0001")] == ["user", "bot"]
    st.set_meta("control", {"paused": True})
    st.close()
    again = Store(tmp_path / "x" / "bot.db")
    assert again.get_meta("control") == {"paused": True} and again.next_id("INC") == "INC-0004"


def test_pricing() -> None:
    u = Usage(input_tokens=100_000, cached_tokens=60_000, output_tokens=10_000)
    # 40k fresh * $2 + 60k cached * $0.50 + 10k out * $6 per million
    assert cost_usd("grok-4.7", u) == pytest.approx(0.08 + 0.03 + 0.06)
    assert price_for("grok-4.7-latest") == (price_for("grok-4.7")[0], True)
    assert price_for("grok-9")[1] is False
    long = Usage(input_tokens=250_000, output_tokens=0)
    assert cost_usd("grok-4.3", long) == pytest.approx(250_000 * 2.50 / 1e6)


def test_page_assets_exist_and_reference_each_other() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert "__BOOT__" in html and "/static/js/main.js" in html and "/static/css/noc.css" in html
    for name in ("main", "room", "screen", "sprites", "font", "panel"):
        assert (WEB / "js" / f"{name}.js").is_file()
    for js in (WEB / "js").glob("*.js"):
        for line in js.read_text(encoding="utf-8").splitlines():
            if line.startswith("import ") and " from './" in line:
                target = line.split(" from './")[1].split("'")[0]
                assert (WEB / "js" / target).is_file(), f"{js.name} imports missing {target}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_scripts_parse() -> None:
    for js in sorted((WEB / "js").glob("*.js")):
        result = subprocess.run(["node", "--check", str(js)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, f"{js.name}: {result.stderr}"
