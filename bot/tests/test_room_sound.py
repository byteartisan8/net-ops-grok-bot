"""The NOC room's sound (web/js/sound.js): when the alarm rings and the recovery jingle plays, the music's notes, and
the page's sound controls. The module is imported in node (it touches no browser API until it plays)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "netsim_bot" / "web"
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def node(tmp_path: Path, body: str) -> object:
    """Run ``body`` (it prints one JSON value) in node with the page's modules imported as ES modules."""
    for js in (WEB / "js").glob("*.js"):
        shutil.copy(js, tmp_path / js.name)
    (tmp_path / "package.json").write_text('{"type": "module"}', encoding="utf-8")
    script = tmp_path / "check.js"
    script.write_text("import * as S from './sound.js';\n" + body, encoding="utf-8")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def inc(id_: str, status: str, severity: str) -> dict[str, str]:
    return {"id": id_, "status": status, "severity": severity}


@needs_node
def test_the_alarm_rings_while_an_incident_is_open_and_the_jingle_plays_when_it_recovers(tmp_path: Path) -> None:
    steps = [
        [],
        [inc("INC-1", "new", "Warning")],  # opened: ring
        [inc("INC-1", "working", "Warning")],  # taken: nothing new, still open
        [inc("INC-1", "working", "Disaster")],  # worse (degraded -> down): ring
        [inc("INC-1", "awaiting-approval", "Disaster"), inc("INC-2", "new", "Warning")],  # a second one: ring
        [inc("INC-1", "recovered", "OK"), inc("INC-2", "working", "Warning")],  # INC-1 recovered: jingle
        [inc("INC-1", "working", "Warning"), inc("INC-2", "working", "Warning")],  # INC-1 came back: ring
        [inc("INC-1", "closed", "OK"), inc("INC-2", "closed", "OK")],  # closed straight from the alarm: jingle
        [inc("INC-1", "closed", "OK"), inc("INC-2", "closed", "OK")],  # nothing changed
    ]
    out = node(
        tmp_path,
        f"""
let prev = null;
const out = [];
for (const list of {json.dumps(steps)}) {{
  const tr = S.transitions(prev, list);
  prev = tr.state;
  out.push([tr.alarm, tr.recovered, tr.open]);
}}
// a page opened while an incident is open sounds nothing: it plays the incident loop and its reminders
const first = S.transitions(null, [{{ id: 'INC-9', status: 'working', severity: 'High' }}]);
out.push([first.alarm, first.recovered, first.open]);
console.log(JSON.stringify(out));
""",
    )
    assert out == [
        [[], [], 0],
        [["INC-1"], [], 1],
        [[], [], 1],
        [["INC-1"], [], 1],
        [["INC-2"], [], 2],
        [[], ["INC-1"], 1],
        [["INC-1"], [], 2],
        [[], ["INC-1", "INC-2"], 0],
        [[], [], 0],
        [[], [], 1],
    ]


@needs_node
def test_the_music_is_well_formed_and_in_tune(tmp_path: Path) -> None:
    out = node(
        tmp_path,
        """
const problems = [];
const scale = new Set([9, 11, 0, 2, 4, 5, 7]); // A natural minor
for (const piece of [S.CALM, S.INCIDENT]) {
  for (const [i, bar] of piece.bars.entries()) {
    const ch = S.CHORDS[bar.chord];
    if (!ch) { problems.push(`${piece.name} bar ${i}: no chord ${bar.chord}`); continue; }
    const part = piece.parts[bar.part];
    if (!part) problems.push(`${piece.name} bar ${i}: no part ${bar.part}`);
    if (part.arp.length * part.arpEvery !== 16) problems.push(`${piece.name} ${bar.part}: arpeggio is not a bar`);
    if (part.arp.some((k) => k >= ch.arp.length)) problems.push(`${piece.name} bar ${i}: arpeggio past ${bar.chord}`);
    for (const drum of ['kick', 'snare', 'hat']) {
      if (!/^[xo.]{16}$/.test(part[drum])) problems.push(`${piece.name} ${bar.part}: ${drum} is not 16 steps`);
    }
    for (const [step, len] of part.bass) {
      if (step + len > 16) problems.push(`${piece.name} ${bar.part}: bass past the bar`);
    }
    for (const n of [ch.bass, ...ch.arp, ...(ch.pad || [])]) {
      if (piece === S.CALM && !scale.has(S.midi(n) % 12)) problems.push(`${bar.chord}: ${n} is not in A minor`);
    }
  }
}
const tuneBars = S.CALM.bars.filter((b) => b.tune !== null).length;
for (const [k, tune] of S.TUNES.entries()) {
  if (tune.length !== tuneBars) problems.push(`tune ${k}: ${tune.length} bars for ${tuneBars}`);
  for (const [b, bar] of tune.entries()) {
    for (const [step, n, len] of bar) {
      if (step + len > 16) problems.push(`tune ${k} bar ${b}: ${n} past the bar`);
      if (!scale.has(S.midi(n) % 12)) problems.push(`tune ${k} bar ${b}: ${n} is not in A minor`);
    }
  }
}
for (const n of S.BLIPS) if (!scale.has(S.midi(n) % 12)) problems.push(`blip ${n} is not in A minor`);
console.log(JSON.stringify({ problems, a4: S.hz('A4'), c4: S.midi('C4'), bar: S.BAR }));
""",
    )
    assert out["problems"] == []
    assert out["a4"] == 440 and out["c4"] == 60
    # the ring (4 alarm cycles) lasts about as long as the room's red flash on an alarm (room.js: 6 s)
    assert 4 * 1.46 < 6 and 2.5 < out["bar"] < 2.7


def test_the_page_has_the_sound_controls_and_feeds_the_incidents() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    main = (WEB / "js" / "main.js").read_text(encoding="utf-8")
    sound = (WEB / "js" / "sound.js").read_text(encoding="utf-8")
    assert 'id="btn-sound"' in html and 'id="btn-music"' in html
    assert "import { Sound } from './sound.js';" in main
    # on the first state and on every incident change, so the alarm and the jingle follow the board
    assert main.count("sound.incidents([...S.incidents.values()])") == 2
    assert "AudioContext" not in main and "new AudioContext" in sound
    assert "ALARM_CYCLE" in sound and "'pointerdown', 'keydown'" in main
    # the sound button is a plain switch for all of it (a click never starts the music instead of stopping it), and
    # turning sound or music off in one tab of the room silences the others
    assert "sound.start()" not in main and "sound.setOn(!sound.on);" in main
    assert "window.addEventListener('storage'" in main and "e.key === 'noc.sound' && sound.on" in main
