// The controller terminal: a CRT on its own small desk, the Clixon controller's CLI as the NOC roles use it
// (controller_cli, read only). Each session types `<user>@controller> <command>` and then shows the first lines of
// the output; a refusal (lab-api's allow-list) shows in red, an unavailable controller in amber. The whole
// transcript (who ran what, when, the full output) is in the side panel. Drawn in the room's 3x5 font and colours.
import { ADV, drawText } from './font.js';
import { P, rect, drawDesk } from './sprites.js';

const COLS = 17; // characters per screen line (4 px each)
const ROWS = 5; // screen lines (6 px each)
const PITCH = 6;
const TYPE_CPS = 32; // the prompt and the command are typed at this many characters a second
const LINES_PS = 7; // then the output scrolls in at this many lines a second
const SCROLLBACK = 120;

export const TERMINAL = { x: 110, y: 222, w: 80 }; // the desk; the CRT stands on it
const CRT = { x: TERMINAL.x + 2, y: TERMINAL.y - 44, w: 76, h: 42 };

// a session's lines as the screen shows them: wrapped at COLS, each with its colour
export function sessionLines(s) {
  const prompt = `${s.user || s.role || '?'}@controller> ${s.command || ''}`;
  const out = [{ text: prompt, color: P.cyan, typed: true }];
  if (!s.allowed) out.push({ text: 'REFUSED: NOT ON THE ALLOW-LIST', color: P.red });
  else if (s.available === false) out.push({ text: 'CONTROLLER UNAVAILABLE', color: P.amber });
  // a refusal: its reason (the allowed commands are in the transcript)
  const head = s.allowed ? s.head || [] : (s.head || []).slice(0, 1).map((l) => l.split('. Allowed commands')[0]);
  for (const line of head) {
    const color = !s.allowed ? P.red : /error|denied/i.test(line) ? P.amber : P.green;
    out.push({ text: line, color });
  }
  if (s.allowed && (s.lines || 0) > head.length) out.push({ text: `... ${s.lines - head.length} MORE`, color: P.dim });
  if (s.allowed && s.available !== false && !head.length) out.push({ text: '(NO OUTPUT)', color: P.dim });
  return out;
}

// what fits 17 columns: indentation and runs of spaces (diffs, tables) shrink to one space; the transcript keeps them
export function squeeze(text) {
  return String(text).replace(/\s+$/, '').replace(/^([-+]?)\s+/, (m, sign) => (sign ? sign + ' ' : '')).replace(/\s{2,}/g, ' ');
}

export function wrap(line) {
  const text = line.typed ? String(line.text).replace(/\s+$/, '') : squeeze(line.text);
  if (!text) return [{ ...line, text: '' }];
  const parts = [];
  for (let i = 0; i < text.length; i += COLS) parts.push({ ...line, text: text.slice(i, i + COLS) });
  return parts;
}

export class Terminal {
  constructor() {
    this.buffer = []; // finished screen lines
    this.queue = [];
    this.play = null; // { lines, i, chars, t }
    this.last = null; // the last session shown (for the tooltip)
  }

  // a new session: animated (the robot has arrived) or, at page load, shown at once
  show(session, animate = true) {
    this.last = session;
    if (!animate) {
      for (const line of sessionLines(session).flatMap(wrap)) this.push(line);
      return;
    }
    this.queue.push(session);
  }

  push(line) {
    this.buffer.push(line);
    if (this.buffer.length > SCROLLBACK) this.buffer.splice(0, this.buffer.length - SCROLLBACK);
  }

  get busy() { return !!this.play || this.queue.length > 0; }

  update(t) {
    if (!this.play && this.queue.length) {
      const session = this.queue.shift();
      this.play = { lines: sessionLines(session), i: 0, t0: t, typed: 0 };
    }
    const p = this.play;
    if (!p) return;
    const line = p.lines[p.i];
    if (!line) { this.play = null; return; }
    if (line.typed) {
      const n = Math.floor((t - p.t0) * TYPE_CPS);
      if (n < line.text.length) { p.typed = n; return; }
      for (const w of wrap(line)) this.push(w);
      p.i += 1;
      p.t0 = t;
      p.typed = 0;
      return;
    }
    if (t - p.t0 >= 1 / LINES_PS) {
      for (const w of wrap(line)) this.push(w);
      p.i += 1;
      p.t0 = t;
    }
  }

  // the screen's lines right now: the end of the scrollback plus the line being typed
  screen() {
    const lines = [...this.buffer];
    const p = this.play;
    const typing = p && p.lines[p.i]?.typed ? p.lines[p.i] : null;
    if (typing) lines.push(...wrap({ ...typing, text: typing.text.slice(0, p.typed) || ' ' }));
    return { lines: lines.slice(-ROWS), typing: !!typing };
  }

  draw(ctx, t) {
    const { x, y, w, h } = CRT;
    // the CRT: case, bezel, screen with scan lines; its stand behind the keyboard
    rect(ctx, x + w / 2 - 6, y + h, 12, 2, P.metalDark);
    rect(ctx, x, y, w, h, '#2b3245');
    rect(ctx, x + 1, y + 1, w - 2, h - 2, '#1a2133');
    rect(ctx, x + 3, y + 3, w - 6, h - 7, P.screen);
    for (let r = y + 4; r < y + h - 4; r += 2) rect(ctx, x + 3, r, w - 6, 1, '#0c172a');
    rect(ctx, x + w - 8, y + h - 3, 3, 1, this.busy ? (Math.floor(t * 6) % 2 ? P.green : '#1e6b44') : '#1e6b44');
    const { lines, typing } = this.screen();
    const left = x + 5, top = y + 5;
    lines.forEach((line, i) => drawText(ctx, line.text, left, top + i * PITCH, line.color));
    // the cursor: after what is being typed, or on a fresh line when idle
    const blink = Math.floor(t * 2.5) % 2 === 0;
    if (typing || blink) {
      const last = lines[lines.length - 1];
      const cx = typing && last ? left + last.text.length * ADV : left;
      const cy = typing ? top + (lines.length - 1) * PITCH : top + Math.min(lines.length, ROWS - 1) * PITCH;
      if (typing || lines.length < ROWS) rect(ctx, Math.min(cx, x + w - 8), cy, 3, 5, typing ? P.cyan : P.green);
    }
    // the desk: keyboard in front of the CRT, the label on the front
    drawDesk(ctx, TERMINAL.x, TERMINAL.y, TERMINAL.w, {});
    rect(ctx, TERMINAL.x + 22, TERMINAL.y + 1, 36, 2, '#2b3245');
    for (let k = 0; k < 8; k++) rect(ctx, TERMINAL.x + 24 + k * 4, TERMINAL.y + 1, 3, 1, '#566075');
    drawText(ctx, 'CONTROLLER', TERMINAL.x + TERMINAL.w / 2, TERMINAL.y + 5, '#9fb3d9', 'center');
  }

  hit(p) {
    return p.x >= TERMINAL.x && p.x <= TERMINAL.x + TERMINAL.w && p.y >= CRT.y && p.y <= TERMINAL.y + 18;
  }
}
