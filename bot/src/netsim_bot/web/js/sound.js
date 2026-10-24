// The NOC room's sound, made in the page with Web Audio (no audio files): chiptune background music in the room's
// pixel-art style, a simple two-tone alarm while an incident is open and a jingle when the network recovers.
//
// The music follows the room: a calm night-shift loop while no incident is open, a tense one (same tempo, minor key)
// while one is. The alarm rings when an incident opens, gets worse or comes back (about 6 s, the room's red flash),
// then once every 8 bars (about 21 s) until the incident recovers. Recovered (Zabbix shows each of its customers OK
// again, or it was closed): the jingle, then the calm loop from its top. Browsers start sound only after a click or a
// key press in the page (their autoplay rule); until then it waits.
import { SEV_RANK } from './sprites.js';

const BPM = 92;
export const BEAT = 60 / BPM;
export const STEP = BEAT / 4; // a 16th
export const BAR = BEAT * 4;
const MUSIC_LEVEL = 0.4; // the music, under the alarm and the jingle
const DUCK = 0.3; // the music while the alarm rings
const REMIND = { level: 0.8, duck: 0.55 }; // the reminder every REMIND_BARS: a softer cycle, the music a little down
export const ALARM_CYCLE = 1.46; // high, low, high, low, a pause
const RING_S = 4 * ALARM_CYCLE; // four cycles when an incident opens
const REMIND_BARS = 8; // then one cycle every 8 bars while it stays open
const ALARM_STATUSES = new Set(['new', 'working', 'awaiting-approval', 'remediating']);

// ------------------------------------------------------------------------------------------------ notes
const SEMI = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 };
export function midi(note) {
  const m = /^([A-G])([#b]?)(\d)$/.exec(note);
  if (!m) throw new Error(`not a note: ${note}`);
  return 12 * (Number(m[3]) + 1) + SEMI[m[1]] + (m[2] === '#' ? 1 : m[2] === 'b' ? -1 : 0);
}
export const hz = (note, semis = 0) => 440 * 2 ** ((midi(note) + semis - 69) / 12);

// bass: the root; arp: the arpeggio, low to high; pad: the chord held under it (the calm loop only)
export const CHORDS = {
  Am9: { bass: 'A2', arp: ['A4', 'C5', 'E5', 'G5', 'B5'], pad: ['C4', 'E4', 'G4', 'B4'] },
  Fmaj7: { bass: 'F2', arp: ['F4', 'A4', 'C5', 'E5', 'G5'], pad: ['A3', 'C4', 'E4', 'G4'] },
  Cmaj7: { bass: 'C3', arp: ['G4', 'C5', 'E5', 'G5', 'B5'], pad: ['G3', 'B3', 'D4', 'E4'] },
  G6: { bass: 'G2', arp: ['G4', 'B4', 'D5', 'E5', 'G5'], pad: ['B3', 'D4', 'E4', 'G4'] },
  Em7: { bass: 'E2', arp: ['E4', 'G4', 'B4', 'D5', 'E5'], pad: ['G3', 'B3', 'D4', 'E4'] },
  Am7: { bass: 'A2', arp: ['A4', 'C5', 'E5', 'G5', 'A5'], pad: ['G3', 'C4', 'E4', 'A4'] },
  Dm7: { bass: 'D3', arp: ['D4', 'F4', 'A4', 'C5', 'E5'], pad: ['F3', 'A3', 'C4', 'E4'] },
  E7sus4: { bass: 'E2', arp: ['E4', 'A4', 'B4', 'D5', 'E5'], pad: ['D4', 'E4', 'A4', 'B4'] },
  Am: { bass: 'A2', arp: ['A4', 'C5', 'E5'] },
  F: { bass: 'F2', arp: ['F4', 'A4', 'C5'] },
  Dm: { bass: 'D3', arp: ['D4', 'F4', 'A4'] },
  E: { bass: 'E2', arp: ['E4', 'G#4', 'B4'] },
};

// A part's bar: the arpeggio (indexes into the chord's arp, one every `arpEvery` 16ths), the bass ([16th, length in
// 16ths, semitones over the root]), drums as 16 steps ('x' accent, 'o' soft, '.' rest), the chance of a computer
// blip. The calm loop is 16 bars: A (a chord every 2 bars, no tune), then B (a chord a bar, with a tune; two tunes
// take turns, one per pass). The incident loop is 4 bars.
const CALM_CHORDS = [
  'Am9', 'Am9', 'Fmaj7', 'Fmaj7', 'Cmaj7', 'Cmaj7', 'G6', 'G6',
  'Fmaj7', 'G6', 'Em7', 'Am7', 'Fmaj7', 'G6', 'Dm7', 'E7sus4',
];
export const CALM = {
  name: 'calm',
  pad: true,
  bars: CALM_CHORDS.map((chord, i) => ({ chord, part: i < 8 ? 'A' : 'B', tune: i < 8 ? null : i - 8 })),
  parts: {
    A: {
      arp: [0, 1, 2, 3, 4, 3, 2, 1], arpEvery: 2, arpLen: 0.3, arpGain: 0.06, arpWave: 0.25,
      bass: [[0, 10, 0], [10, 2, 0], [12, 4, 7]],
      kick: 'x...............', snare: '................', hat: '..o...o...o...o.', blips: 0.35,
    },
    B: {
      arp: [0, 2, 1, 3, 2, 4, 3, 1], arpEvery: 2, arpLen: 0.26, arpGain: 0.05, arpWave: 0.25,
      bass: [[0, 6, 0], [6, 2, 12], [8, 6, 0], [14, 2, 7]],
      kick: 'x.........x.....', snare: '....o.......o...', hat: 'o.x.o.x.o.x.o.x.', blips: 0.2,
    },
  },
};
export const INCIDENT = {
  name: 'incident',
  pad: false,
  bars: ['Am', 'F', 'Dm', 'E'].map((chord) => ({ chord, part: 'X', tune: null })),
  parts: {
    X: {
      arp: [0, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2, 0, 1, 2, 1], arpEvery: 1, arpLen: 0.14, arpGain: 0.045, arpWave: 0.125,
      bass: [[0, 2, 0], [2, 2, 0], [4, 2, 12], [6, 2, 0], [8, 2, 0], [10, 2, 0], [12, 2, 12], [14, 2, 7]],
      kick: 'x...x...x...x...', snare: '....x.......x...', hat: 'o.x.o.x.o.x.oox.', blips: 0,
    },
  },
};
// the tunes over part B: per bar, [16th, note, length in 16ths]
export const TUNES = [
  [
    [[0, 'E5', 6], [6, 'D5', 2], [8, 'C5', 4], [12, 'A4', 4]],
    [[0, 'B4', 4], [4, 'D5', 4], [8, 'G5', 6], [14, 'E5', 2]],
    [[0, 'G5', 6], [6, 'E5', 2], [8, 'D5', 4], [12, 'B4', 4]],
    [[0, 'C5', 4], [4, 'E5', 4], [8, 'A4', 8]],
    [[0, 'A5', 4], [4, 'G5', 2], [6, 'E5', 2], [8, 'G5', 4], [12, 'E5', 4]],
    [[0, 'D5', 6], [6, 'E5', 2], [8, 'G5', 4], [12, 'B5', 4]],
    [[0, 'A5', 4], [4, 'F5', 4], [8, 'E5', 4], [12, 'D5', 4]],
    [[0, 'E5', 8], [12, 'B4', 2], [14, 'D5', 2]],
  ],
  [
    [[0, 'C5', 2], [2, 'E5', 2], [4, 'A5', 6], [10, 'G5', 2], [12, 'E5', 4]],
    [[0, 'D5', 8], [8, 'B4', 4], [12, 'D5', 4]],
    [[0, 'E5', 4], [4, 'G5', 4], [8, 'B5', 6], [14, 'A5', 2]],
    [[0, 'G5', 4], [4, 'E5', 4], [8, 'C5', 4], [12, 'E5', 4]],
    [[0, 'F5', 4], [4, 'E5', 2], [6, 'C5', 2], [8, 'A4', 8]],
    [[0, 'B4', 4], [4, 'D5', 4], [8, 'E5', 4], [12, 'G5', 4]],
    [[0, 'F5', 6], [6, 'E5', 2], [8, 'D5', 4], [12, 'C5', 4]],
    [[0, 'B4', 8], [8, 'A4', 4]],
  ],
];
export const BLIPS = ['C6', 'D6', 'E6', 'G6', 'A6'];
// the short sounds the page had before the music: [frequencies one after another, length of each, wave, gain]
const EFFECTS = {
  on: [[660, 880], 0.08, 'square', 0.07],
  page: [[1320, 1320, 1320], 0.07, 'sine', 0.12],
  close: [[523, 659, 784, 1047], 0.09, 'triangle', 0.12],
  review: [[660, 880], 0.08, 'triangle', 0.12],
};

// ------------------------------------------------------------------------------------------------ what sounds when
// What the incidents' new states mean for the sound: those that went into alarm (opened, got worse or came back
// from recovered), those that recovered, and how many are open. prev is the last call's state; null (the first
// call) sounds nothing.
export function transitions(prev, incidents) {
  const state = new Map();
  for (const inc of incidents) {
    state.set(inc.id, { alarm: ALARM_STATUSES.has(inc.status), rank: SEV_RANK[inc.severity] ?? 0, status: inc.status });
  }
  const alarm = [];
  const recovered = [];
  if (prev) {
    for (const [id, s] of state) {
      const p = prev.get(id);
      if (s.alarm && (!p || !p.alarm || s.rank > p.rank)) alarm.push(id);
      else if (!s.alarm && p?.alarm && (s.status === 'recovered' || s.status === 'closed')) recovered.push(id);
    }
  }
  const open = [...state.values()].filter((s) => s.alarm).length;
  return { state, alarm, recovered, open };
}

// ------------------------------------------------------------------------------------------------ the synth
function amp(ctx, value, to) {
  const g = ctx.createGain();
  g.gain.value = value;
  if (to) g.connect(to);
  return g;
}

// a pulse wave of the given duty cycle, the 8-bit consoles' sound (12.5 % thin, 25 % hollow, 50 % square)
function pulseWave(ctx, duty) {
  const n = 64;
  const real = new Float32Array(n);
  const imag = new Float32Array(n);
  for (let k = 1; k < n; k++) real[k] = (2 / (k * Math.PI)) * Math.sin(k * Math.PI * duty);
  return ctx.createPeriodicWave(real, imag);
}

function noiseBuffer(ctx) {
  const b = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
  const d = b.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  return b;
}

// The mixing desk: music voices -> a soft low-pass (drums around it) -> duck (under the alarm) -> gate (on, off, the
// jingle's cut) -> master; the effects -> master; master -> a compressor -> the speakers. An echo (a dotted 8th) for
// the arpeggio, the tune and the blips.
export class Mixer {
  constructor(ctx) {
    this.ctx = ctx;
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -14;
    comp.knee.value = 10;
    comp.ratio.value = 4;
    comp.attack.value = 0.005;
    comp.release.value = 0.25;
    comp.connect(ctx.destination);
    this.master = amp(ctx, 0.9, comp);
    this.gate = amp(ctx, MUSIC_LEVEL, this.master);
    this.duck = amp(ctx, 1, this.gate);
    this.music = ctx.createBiquadFilter();
    this.music.type = 'lowpass';
    this.music.frequency.value = 5000;
    this.music.connect(this.duck);
    this.drums = amp(ctx, 1, this.duck);
    this.echo = ctx.createDelay(2);
    this.echo.delayTime.value = BEAT * 0.75;
    const damp = ctx.createBiquadFilter();
    damp.type = 'lowpass';
    damp.frequency.value = 2400;
    this.echo.connect(damp);
    damp.connect(amp(ctx, 0.35, this.echo));
    damp.connect(amp(ctx, 0.4, this.music));
    this.sfx = amp(ctx, 1, this.master);
    this.waves = { 0.125: pulseWave(ctx, 0.125), 0.25: pulseWave(ctx, 0.25), 0.5: pulseWave(ctx, 0.5) };
    this.noise = noiseBuffer(ctx);
  }
}

// One note: wave is a pulse duty (0.125, 0.25, 0.5) or an oscillator type; env 'pluck' decays over len, 'hold'
// holds it and releases at the end. vibrato: depth as a fraction of the pitch, after a short delay. send: to the echo.
function note(m, out, opts) {
  const { t, freq, len, gain, wave = 0.5, env = 'hold', attack = 0.005, release = 0.05, vibrato = 0, glide = 0, send = 0 } = opts;
  const { ctx } = m;
  const o = ctx.createOscillator();
  if (typeof wave === 'number') o.setPeriodicWave(m.waves[wave]);
  else o.type = wave;
  o.frequency.setValueAtTime(freq, t);
  if (glide) o.frequency.exponentialRampToValueAtTime(freq * glide, t + len);
  const a = ctx.createGain();
  a.gain.setValueAtTime(0, t);
  a.gain.linearRampToValueAtTime(gain, t + attack);
  let end;
  if (env === 'pluck') {
    end = t + Math.max(len, attack + 0.01);
    a.gain.exponentialRampToValueAtTime(0.0001, end);
  } else {
    const hold = Math.max(t + attack, t + len - release);
    a.gain.setValueAtTime(gain, hold);
    a.gain.linearRampToValueAtTime(0, hold + release);
    end = hold + release;
  }
  o.connect(a).connect(out);
  if (send) a.connect(amp(ctx, send, m.echo));
  if (vibrato) {
    const lfo = ctx.createOscillator();
    const depth = ctx.createGain();
    lfo.frequency.value = 5.2;
    depth.gain.setValueAtTime(0, t);
    depth.gain.setValueAtTime(0, t + Math.min(0.15, len / 2));
    depth.gain.linearRampToValueAtTime(freq * vibrato, t + Math.min(0.45, len));
    lfo.connect(depth).connect(o.frequency);
    lfo.start(t);
    lfo.stop(end + 0.05);
  }
  o.start(t);
  o.stop(end + 0.02);
  return { node: o, end: end + 0.02 };
}

// a burst of filtered noise: hats, snares, the jingle's whoosh
function hit(m, out, { t, len, gain, type, freq, q = 0.8, sweep = 0 }) {
  const { ctx } = m;
  const s = ctx.createBufferSource();
  s.buffer = m.noise;
  const f = ctx.createBiquadFilter();
  f.type = type;
  f.Q.value = q;
  f.frequency.setValueAtTime(freq, t);
  if (sweep) f.frequency.exponentialRampToValueAtTime(sweep, t + len);
  const a = ctx.createGain();
  a.gain.setValueAtTime(gain, t);
  a.gain.exponentialRampToValueAtTime(0.0001, t + len);
  s.connect(f).connect(a).connect(out);
  s.start(t, Math.random() * 0.5, len + 0.05);
  return { node: s, end: t + len + 0.05 };
}

function kick(m, out, t, gain) {
  return note(m, out, { t, freq: 150, glide: 0.3, len: 0.16, gain, wave: 'triangle', env: 'pluck', attack: 0.002 });
}

function snare(m, out, t, gain) {
  return [
    hit(m, out, { t, len: 0.12, gain, type: 'bandpass', freq: 1900, q: 0.9 }),
    note(m, out, { t, freq: 200, glide: 0.6, len: 0.07, gain: gain * 1.5, wave: 'triangle', env: 'pluck', attack: 0.002 }),
  ];
}

// one bar of a loop at time t; returns its sources (the jingle cuts them)
export function playBar(m, t, piece, index, pass, rnd = Math.random) {
  const bar = piece.bars[index];
  const part = piece.parts[bar.part];
  const ch = CHORDS[bar.chord];
  const out = m.music;
  const at = (step) => t + step * STEP;
  const live = [];
  for (const [step, len, semis] of part.bass) {
    live.push(note(m, out, { t: at(step), freq: hz(ch.bass, semis), len: len * STEP * 0.92, gain: 0.2, wave: 'triangle', release: 0.04 }));
  }
  part.arp.forEach((k, i) => {
    live.push(note(m, out, {
      t: at(i * part.arpEvery), freq: hz(ch.arp[k % ch.arp.length]), len: part.arpLen,
      gain: part.arpGain * (0.85 + 0.3 * rnd()), wave: part.arpWave, env: 'pluck', send: 0.45,
    }));
  });
  if (piece.pad && ch.pad && (index === 0 || piece.bars[index - 1].chord !== bar.chord)) {
    let bars = 1;
    while (index + bars < piece.bars.length && piece.bars[index + bars].chord === bar.chord) bars += 1;
    for (const p of ch.pad) live.push(note(m, out, { t, freq: hz(p), len: bars * BAR, gain: 0.018, wave: 'triangle', attack: 0.5, release: 0.9 }));
  }
  if (bar.tune !== null) {
    for (const [step, n, len] of TUNES[pass % TUNES.length][bar.tune]) {
      live.push(note(m, out, { t: at(step), freq: hz(n), len: len * STEP * 0.95, gain: 0.05, wave: 0.25, vibrato: 0.012, send: 0.35, release: 0.08 }));
    }
  }
  const drums = m.drums;
  [...part.kick].forEach((c, step) => { if (c !== '.') live.push(kick(m, drums, at(step), c === 'x' ? 0.3 : 0.18)); });
  [...part.snare].forEach((c, step) => { if (c !== '.') live.push(...snare(m, drums, at(step), c === 'x' ? 0.06 : 0.03)); });
  [...part.hat].forEach((c, step) => {
    if (c === '.') return;
    const gain = (c === 'x' ? 0.04 : 0.025) * (0.8 + 0.4 * rnd());
    live.push(hit(m, drums, { t: at(step), len: c === 'x' ? 0.05 : 0.03, gain, type: 'highpass', freq: 7000 }));
  });
  if (rnd() < part.blips) {
    const n = BLIPS[Math.floor(rnd() * BLIPS.length)];
    live.push(note(m, out, { t: at(Math.floor(rnd() * 16)), freq: hz(n), len: 0.06, gain: 0.02, wave: 0.125, env: 'pluck', send: 0.7 }));
  }
  return live;
}

// the alarm: high, low, high, low on a square wave, then a pause (ALARM_CYCLE); level 1 rings, less reminds
export function alarmCycle(m, t, level = 1) {
  const live = [];
  for (let i = 0; i < 4; i++) {
    live.push(note(m, m.sfx, { t: t + i * 0.24, freq: i % 2 ? hz('E5') : hz('A5'), len: 0.22, gain: 0.1 * level, wave: 0.5, attack: 0.004, release: 0.02 }));
  }
  return live;
}

// the network is back: a rising A-major fanfare (the incident loop is in A minor); returns its sources and length
export function jingle(m, t) {
  const s = 0.1; // a 16th at 150 BPM
  const at = (k) => t + k * s;
  const out = m.sfx;
  const live = [];
  for (const [k, n, len] of [[0, 'C#5', 1], [1, 'E5', 1], [2, 'A5', 1], [3, 'C#6', 1], [4, 'E6', 2], [7, 'C#6', 1], [8, 'A6', 7]]) {
    const long = len > 2;
    live.push(note(m, out, { t: at(k), freq: hz(n), len: len * s, gain: 0.085, wave: 0.25, vibrato: long ? 0.01 : 0, release: long ? 0.35 : 0.03 }));
  }
  for (const [k, n, len] of [[4, 'C#6', 2], [7, 'A5', 1], [8, 'C#6', 7], [8, 'E6', 7]]) {
    live.push(note(m, out, { t: at(k), freq: hz(n), len: len * s, gain: 0.04, wave: 0.5, release: len > 2 ? 0.35 : 0.03 }));
  }
  for (const [k, n, len] of [[0, 'A3', 4], [4, 'E3', 3], [8, 'A2', 7]]) {
    live.push(note(m, out, { t: at(k), freq: hz(n), len: len * s, gain: 0.22, wave: 'triangle', release: 0.2 }));
  }
  ['E6', 'A6', 'C#7', 'E7'].forEach((n, i) => {
    live.push(note(m, out, { t: at(9 + i * 0.5), freq: hz(n), len: 0.25, gain: 0.025, wave: 0.125, env: 'pluck' }));
  });
  live.push(hit(m, out, { t, len: 0.8, gain: 0.035, type: 'bandpass', freq: 600, sweep: 5000, q: 1.2 }));
  return { live, length: 15 * s + 0.4 };
}

function beep(m, t, [freqs, len, type, gain]) {
  const live = [];
  for (const f of freqs) {
    live.push(note(m, m.sfx, { t, freq: f, len, gain, wave: type, env: 'pluck', attack: 0.003 }));
    t += len * 1.1;
  }
  return live;
}

// ------------------------------------------------------------------------------------------------ the player
export class Sound {
  constructor({ on = true, music = true, onchange = null } = {}) {
    this.on = on;
    this.musicOn = music;
    this.onchange = onchange;
    this.ctx = null;
    this.m = null;
    this.timer = null;
    this.mood = 'calm'; // the loop playing
    this.want = 'calm'; // the loop the incidents ask for: it changes at a bar line
    this.bar = 0;
    this.pass = 0;
    this.nextBar = 0; // audio time of the next bar line
    this.ringUntil = 0;
    this.ringEnd = 0; // where the scheduled ring cycles end
    this.jingleUntil = 0;
    this.prev = null;
    this.open = 0;
    this.live = []; // scheduled sources: { node, end, kind: music, alarm or sfx }
    this.log = [];
  }

  // off, waiting (on, but the browser waits for a click or a key in the page) or playing
  get state() {
    if (!this.on) return 'off';
    return this.ctx?.state === 'running' ? 'playing' : 'waiting';
  }

  get playing() { return this.state === 'playing' && this.timer !== null; }

  // a click or a key press in the page: the browser lets the sound start now
  unlock() {
    if (this.on && this.ctx?.state !== 'running') this.start();
  }

  start() {
    if (!this.ctx) {
      try {
        this.ctx = new AudioContext({ latencyHint: 'playback' });
      } catch {
        return; // no Web Audio
      }
      this.m = new Mixer(this.ctx);
      this.ctx.onstatechange = () => this.onchange?.(this);
    }
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
    if (this.timer === null) {
      this.mood = this.want;
      this.bar = 0;
      this.pass = 0;
      this.nextBar = 0;
      this.timer = setInterval(() => this.tick(), 100);
    }
    this.onchange?.(this);
  }

  setOn(on) {
    this.on = on;
    if (on) {
      this.start();
      this.ctx?.resume().then(() => this.effect('on')).catch(() => {});
      return;
    }
    clearInterval(this.timer);
    this.timer = null;
    const ctx = this.ctx;
    this.ctx = null;
    this.m = null;
    this.live = [];
    this.ringUntil = this.ringEnd = this.jingleUntil = 0;
    ctx?.close().catch(() => {});
    this.onchange?.(this);
  }

  setMusic(on) {
    this.musicOn = on;
    if (!on && this.playing) this.cut(this.ctx.currentTime);
    this.onchange?.(this); // on: it comes in at the next bar line
  }

  // the incidents as the page has them (summaries with status and severity), after every change
  incidents(list) {
    const tr = transitions(this.prev, list);
    this.prev = tr.state;
    this.open = tr.open;
    this.want = tr.open ? 'incident' : 'calm';
    if (!this.playing) return;
    const t = this.ctx.currentTime + 0.05;
    if (tr.alarm.length) {
      this.ring(t);
      this.record('alarm', tr.alarm);
    } else if (tr.recovered.length) {
      this.recover(t);
      this.record('recovered', tr.recovered);
    }
  }

  // the page's short sounds: on, page, close, review (not over the jingle)
  effect(name) {
    if (!this.playing || !EFFECTS[name]) return;
    const t = this.ctx.currentTime + 0.02;
    if (t < this.jingleUntil) return;
    this.keep(beep(this.m, t, EFFECTS[name]), 'sfx');
  }

  // try a sound from the browser's console: __noc.sound.test('alarm' | 'recovered' | 'incident' | 'calm'); a loop
  // stays until the next change of an incident
  test(name) {
    if (!this.playing) return `sound is ${this.state}: turn it on and click in the page`;
    const t = this.ctx.currentTime + 0.05;
    if (name === 'alarm') this.ring(t);
    else if (name === 'recovered') this.recover(t);
    else if (name === 'incident' || name === 'calm') this.want = name;
    else return 'alarm, recovered, incident or calm';
    this.record('test', [name]);
    return 'ok';
  }

  debug() {
    const { state, mood, want, bar, open, musicOn } = this;
    const now = this.ctx?.currentTime ?? 0;
    const ringing = now < this.ringUntil;
    const jingle = now < this.jingleUntil;
    return { state, mood, want, bar, open, musicOn, ringing, jingle, sources: this.live.length, log: this.log.slice(-10) };
  }

  // ---------------------------------------------------------------- inside
  tick() {
    const ctx = this.ctx;
    if (!ctx || ctx.state !== 'running') return;
    const now = ctx.currentTime;
    const ahead = document.hidden ? 1.6 : 0.35; // a hidden page's timers run about once a second
    if (this.nextBar < now) this.nextBar = now + 0.06; // the start, or the page stalled: go on from now
    while (this.nextBar < now + ahead) {
      this.barLine(this.nextBar);
      this.nextBar += BAR;
    }
    this.live = this.live.filter((s) => s.end > now);
  }

  barLine(t) {
    if (this.want !== this.mood) {
      this.mood = this.want; // a loop starts at its top
      this.bar = 0;
      this.pass = 0;
    }
    const piece = this.mood === 'incident' ? INCIDENT : CALM;
    if (this.mood === 'incident' && this.bar % REMIND_BARS === 0 && t > this.ringUntil + 1.5 && t > this.jingleUntil) {
      this.keep(alarmCycle(this.m, t, REMIND.level), 'alarm');
      this.duckFor(t, t + 1, REMIND.duck);
    }
    if (this.musicOn) {
      this.m.gate.gain.setValueAtTime(MUSIC_LEVEL, t); // open again after a cut
      this.keep(playBar(this.m, t, piece, this.bar % piece.bars.length, this.pass), 'music');
    }
    this.bar += 1;
    if (this.bar % piece.bars.length === 0) this.pass += 1;
  }

  // the alarm's ring: whole cycles until RING_S from t (a ring under way gets longer), the music ducked under it
  ring(t) {
    const until = t + RING_S;
    let s = Math.max(t, this.ringEnd);
    while (s + ALARM_CYCLE <= until + 0.5) {
      this.keep(alarmCycle(this.m, s, 1), 'alarm');
      s += ALARM_CYCLE;
    }
    this.ringEnd = s;
    this.ringUntil = Math.max(this.ringUntil, s);
    this.duckFor(t, this.ringUntil, DUCK);
  }

  // the music down to `level` from t to `until`, then back up
  duckFor(t, until, level) {
    const d = this.m.duck.gain;
    d.cancelScheduledValues(t);
    d.setValueAtTime(d.value, t);
    d.linearRampToValueAtTime(level, t + 0.1);
    d.setValueAtTime(level, until);
    d.linearRampToValueAtTime(1, until + 1);
  }

  // recovered: the ring and the music stop, the jingle plays, the music starts again after it from its top
  recover(t) {
    for (const s of this.live) if (s.kind === 'alarm' && s.end > t) this.stop(s, t);
    this.ringUntil = this.ringEnd = 0;
    const d = this.m.duck.gain;
    d.cancelScheduledValues(t);
    d.setValueAtTime(1, t);
    this.cut(t);
    const j = jingle(this.m, t);
    this.keep(j.live, 'sfx');
    this.jingleUntil = t + j.length;
    this.nextBar = this.jingleUntil + 0.15;
    this.mood = this.want;
    this.bar = 0;
    this.pass = 0;
  }

  // the music fades out at t (its sources stop); a later bar line opens it again
  cut(t) {
    const g = this.m.gate.gain;
    g.cancelScheduledValues(t);
    g.setValueAtTime(g.value, t);
    g.linearRampToValueAtTime(0, t + 0.08);
    for (const s of this.live) if (s.kind === 'music' && s.end > t) this.stop(s, t + 0.1);
  }

  stop(s, t) {
    try {
      s.node.stop(t);
    } catch {
      /* it ended */
    }
  }

  keep(sources, kind) {
    for (const s of sources) this.live.push({ ...s, kind });
  }

  record(what, ids) {
    this.log.push(`${new Date().toLocaleTimeString()} ${what} ${ids.join(', ')}`);
    if (this.log.length > 30) this.log.shift();
  }
}
