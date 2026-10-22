// The NOC room: three colleagues (the NOC roles, each a Grok Bot app routine) at their desks, the furniture, and
// the director that turns the office's real activity (pages, tool calls, tickets, approvals) into movement.
//
// NOCENG (NOC Engineer) sits at the Zabbix console, NETENG (Network Engineer) between the NetBox cabinet and the
// server racks, NOCMGR (NOC Manager) at a desk by the incident board. Each desk has a phone that rings while its
// role is paged. They stay at their desks between runs, get up on their first tool call, walk to each tool's
// station, and sit down again when the run is over. Field repairs happen off-screen: the strip under the board.
import { drawText, textWidth, wrap, truncate, LINE } from './font.js';
import {
  P, SEV, SEV_RANK, TIER, rect, shade, statusColor, drawBot, drawEmote, drawDesk, drawMonitor, drawChair, drawPlant,
  drawPhone,
} from './sprites.js';
import { drawWallScreen } from './screen.js';

export const W = 480;
export const H = 270;
const FLOOR_Y = 82;
const CELL = 4;
const SPEED = 38; // px per second

// ------------------------------------------------------------------------------------------------ layout
const L = {
  board: { x: 8, y: 10, w: 104, h: 56 },
  field: { x: 8, y: 68, w: 104, h: 11 },
  screen: { x: 120, y: 4, w: 238, h: 72 },
  window: { x: 372, y: 12, w: 58, h: 46 },
  clock: { x: 452, y: 22 },
  beacon: { x: 452, y: 50 },
  netbox: { x: 296, y: 86, w: 42, h: 50 },
  whiteboard: { x: 50, y: 150, w: 50, h: 30 },
  console: { x: 194, y: 104, w: 92 },
  racks: [
    { x: 402, y: 82, w: 34, h: 78, title: 'HUBS' },
    { x: 438, y: 82, w: 34, h: 78, title: 'SPOKES' },
  ],
  desks: {
    NOCMGR: { x: 18, y: 118, w: 76, label: 'NOC MANAGER' },
    NETENG: { x: 342, y: 118, w: 56, label: 'NETWORK ENG' },
  },
  operator: { x: 194, y: 224, w: 92 },
  printer: { x: 300, y: 212, w: 26, h: 26 },
  link: { x: 384, y: 210, w: 56, h: 18 },
  coffee: { x: 12, y: 214, w: 16, h: 36 },
  cooler: { x: 32, y: 224, w: 11, h: 26 },
  rug: { x: 150, y: 140, w: 180, h: 26 },
};

const HOMES = {
  NOCENG: { x: 240, y: 134, face: 'up', sit: true },
  NOCMGR: { x: L.desks.NOCMGR.x + 22, y: L.desks.NOCMGR.y + 9, face: 'down', sit: true },
  NETENG: { x: L.desks.NETENG.x + 20, y: L.desks.NETENG.y + 9, face: 'down', sit: true },
};
const STATIONS = {
  screen: [{ x: 150, y: 94, face: 'up' }, { x: 330, y: 94, face: 'up' }, { x: 176, y: 96, face: 'up' }],
  board: [{ x: 36, y: 94, face: 'up' }, { x: 62, y: 94, face: 'up' }, { x: 88, y: 94, face: 'up' }],
  netbox: [{ x: 312, y: 146, face: 'up' }, { x: 326, y: 147, face: 'up' }],
  whiteboard: [{ x: 70, y: 194, face: 'up' }, { x: 84, y: 195, face: 'up' }],
  printer: [{ x: 313, y: 206, face: 'down' }, { x: 336, y: 222, face: 'left' }],
  operator: [{ x: 228, y: 216, face: 'down' }, { x: 252, y: 216, face: 'down' }, { x: 212, y: 218, face: 'down' }],
  link: [{ x: 404, y: 238, face: 'up' }, { x: 420, y: 238, face: 'up' }],
  visit: {
    NOCENG: { x: 266, y: 136, face: 'left' },
    NOCMGR: { x: L.desks.NOCMGR.x + L.desks.NOCMGR.w + 6, y: 130, face: 'left' },
    NETENG: { x: L.desks.NETENG.x - 6, y: 130, face: 'right' },
  },
};
const HUBS = ['hub-a', 'hub-b', 'hub-c', 'hub-d'];
const RACK_PITCH = 11;
const ROLE_ORDER = ['NOCENG', 'NETENG', 'NOCMGR'];

function rackSlot(host) {
  const hub = HUBS.indexOf(host);
  if (hub >= 0) return { rack: 0, i: hub };
  const m = /^spoke-(\d+)$/.exec(host || '');
  if (m) return { rack: 1, i: Math.min(5, Number(m[1]) - 1) };
  return null;
}

function unitRect(host) {
  const s = rackSlot(host);
  if (!s) return null;
  const r = L.racks[s.rack];
  return { x: r.x + 4, y: r.y + 8 + s.i * RACK_PITCH, w: r.w - 8, h: 9 };
}

// ------------------------------------------------------------------------------------------------ walking
class Grid {
  constructor() {
    this.cols = Math.ceil(W / CELL);
    this.rows = Math.ceil(H / CELL);
    this.block = new Uint8Array(this.cols * this.rows);
    this.blockRect(0, 0, W, FLOOR_Y + 4);
    this.blockRect(0, 0, 6, H);
    this.blockRect(W - 6, 0, 6, H);
    this.blockRect(0, H - 4, W, 4);
    const fp = (x, y, w, h) => this.blockRect(x - 2, y, w + 4, h);
    fp(L.netbox.x, 116, L.netbox.w, 22);
    fp(L.whiteboard.x, 176, L.whiteboard.w, 10);
    fp(L.console.x, 108, L.console.w, 16);
    fp(402, 136, 70, 26);
    for (const d of Object.values(L.desks)) fp(d.x, d.y, d.w, 18);
    fp(L.operator.x, 226, L.operator.w, 18);
    fp(L.printer.x, 222, L.printer.w, 16);
    fp(L.link.x, L.link.y + 4, L.link.w, 14);
    fp(L.coffee.x, 238, L.coffee.w, 12);
    fp(L.cooler.x, 240, L.cooler.w, 10);
  }
  idx(c, r) { return r * this.cols + c; }
  setRect(x, y, w, h, v) {
    const c0 = Math.max(0, Math.floor(x / CELL)), c1 = Math.min(this.cols - 1, Math.floor((x + w - 1) / CELL));
    const r0 = Math.max(0, Math.floor(y / CELL)), r1 = Math.min(this.rows - 1, Math.floor((y + h - 1) / CELL));
    for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) this.block[this.idx(c, r)] = v;
  }
  blockRect(x, y, w, h) { this.setRect(x, y, w, h, 1); }
  free(c, r) { return c >= 0 && r >= 0 && c < this.cols && r < this.rows && !this.block[this.idx(c, r)]; }
  cellOf(p) { return [Math.max(0, Math.min(this.cols - 1, Math.floor(p.x / CELL))), Math.max(0, Math.min(this.rows - 1, Math.floor(p.y / CELL)))]; }
  nearestFree(c, r) {
    if (this.free(c, r)) return [c, r];
    for (let d = 1; d < 30; d++) {
      for (let dc = -d; dc <= d; dc++) for (const dr of [-d, d]) if (this.free(c + dc, r + dr)) return [c + dc, r + dr];
      for (let dr = -d; dr <= d; dr++) for (const dc of [-d, d]) if (this.free(c + dc, r + dr)) return [c + dc, r + dr];
    }
    return [c, r];
  }
  los(a, b) {
    let [c0, r0] = a; const [c1, r1] = b;
    const dc = Math.abs(c1 - c0), dr = Math.abs(r1 - r0), sc = c0 < c1 ? 1 : -1, sr = r0 < r1 ? 1 : -1;
    let err = dc - dr;
    for (;;) {
      if (!this.free(c0, r0)) return false;
      if (c0 === c1 && r0 === r1) return true;
      const e2 = 2 * err;
      if (e2 > -dr) { err -= dr; c0 += sc; }
      if (e2 < dc) { err += dc; r0 += sr; }
    }
  }
  path(from, to) {
    const s = this.nearestFree(...this.cellOf(from));
    const g = this.nearestFree(...this.cellOf(to));
    const key = (c, r) => r * this.cols + c;
    const open = [[s[0], s[1]]];
    const came = new Map(), cost = new Map([[key(...s), 0]]);
    const h = (c, r) => Math.hypot(c - g[0], r - g[1]);
    const fscore = new Map([[key(...s), h(...s)]]);
    let found = false, guard = 0;
    while (open.length && guard++ < 6000) {
      let bi = 0;
      for (let i = 1; i < open.length; i++) if (fscore.get(key(...open[i])) < fscore.get(key(...open[bi]))) bi = i;
      const [c, r] = open.splice(bi, 1)[0];
      if (c === g[0] && r === g[1]) { found = true; break; }
      for (const [dc, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]]) {
        const nc = c + dc, nr = r + dr;
        if (!this.free(nc, nr)) continue;
        if (dc && dr && (!this.free(c + dc, r) || !this.free(c, r + dr))) continue;
        const nk = key(nc, nr), ng = cost.get(key(c, r)) + (dc && dr ? 1.414 : 1);
        if (!cost.has(nk) || ng < cost.get(nk)) {
          cost.set(nk, ng);
          fscore.set(nk, ng + h(nc, nr));
          came.set(nk, [c, r]);
          if (!open.some(([oc, or]) => oc === nc && or === nr)) open.push([nc, nr]);
        }
      }
    }
    if (!found) return [{ x: to.x, y: to.y }];
    const cells = [g];
    let cur = g;
    while (key(...cur) !== key(...s)) { cur = came.get(key(...cur)); if (!cur) break; cells.unshift(cur); }
    // string pulling: keep only the turns that line of sight cannot skip
    const pts = [cells[0]];
    let anchor = 0;
    for (let i = 2; i < cells.length; i++) {
      if (!this.los(cells[anchor], cells[i])) { pts.push(cells[i - 1]); anchor = i - 1; }
    }
    pts.push(cells[cells.length - 1]);
    const out = pts.slice(1).map(([c, r]) => ({ x: c * CELL + CELL / 2, y: r * CELL + CELL / 2 }));
    out.push({ x: to.x, y: to.y });
    return out;
  }
}

// ------------------------------------------------------------------------------------------------ the robots
class Bot {
  constructor(member) {
    this.name = member.name;
    this.color = member.color;
    this.hat = member.hat;
    this.member = member;
    this.home = HOMES[member.name] || HOMES.NOCENG;
    this.x = this.home.x;
    this.y = this.home.y;
    this.dir = this.home.face;
    this.pose = 'sit';
    this.queue = [];
    this.cur = null;
    this.path = null;
    this.bubble = null;
    this.emote = null;
    this.frame = 0;
    this.ft = 0;
    this.blinkAt = Math.random() * 4 + 2;
    this.target = null;
    this.carry = false;
    this.cheerUntil = 0;
  }

  get busy() { return this.cur !== null || this.queue.length > 0; }

  push(...actions) {
    this.queue.push(...actions);
    // a long backlog plays faster and drops chatter, so the room never lags far behind the office
    if (this.queue.length > 14) this.queue = this.queue.filter((a, i) => a.type !== 'say' || i > this.queue.length - 4);
  }

  get rush() { return this.queue.length > 6 ? 2.2 : this.queue.length > 3 ? 1.5 : 1; }

  say(text, kind = 'say', secs = 3.2) {
    this.bubble = { text, kind, until: performance.now() / 1000 + secs };
  }

  update(dt, room, t) {
    this.ft += dt;
    if (this.ft > 0.18) { this.ft = 0; this.frame++; }
    if (this.bubble && t > this.bubble.until) this.bubble = null;
    if (this.emote && t > this.emote.until) this.emote = null;
    if (!this.cur && this.queue.length) this.cur = this.queue.shift();
    const a = this.cur;
    if (!a) { this.idle(); return; }
    const speed = SPEED * this.rush;
    switch (a.type) {
      case 'go': {
        if (!this.path) {
          this.pose = 'walk';
          this.carry = !!a.carry;
          this.path = room.grid.path({ x: this.x, y: this.y }, a.to);
        }
        const wp = this.path[0];
        if (!wp) { this.arrive(a); break; }
        const dx = wp.x - this.x, dy = wp.y - this.y, d = Math.hypot(dx, dy);
        const step = speed * dt;
        if (Math.abs(dx) > Math.abs(dy)) this.dir = dx > 0 ? 'right' : 'left';
        else if (d > 0.01) this.dir = dy > 0 ? 'down' : 'up';
        if (d <= step) { this.x = wp.x; this.y = wp.y; this.path.shift(); if (!this.path.length) this.arrive(a); }
        else { this.x += (dx / d) * step; this.y += (dy / d) * step; }
        break;
      }
      case 'work':
        if (a.until === undefined) {
          a.until = t + a.secs / this.rush;
          this.pose = a.pose || (this.atHome() && this.home.sit ? 'type' : 'stand');
          if (a.label) this.say(a.label, 'label', a.secs + 0.6);
          this.target = a.target || null;
        }
        if (t >= a.until) { this.target = null; this.cur = null; if (this.pose === 'type') this.pose = 'sit'; }
        break;
      case 'say':
        this.say(a.text, a.kind || 'say', a.secs || 3);
        this.cur = null;
        break;
      case 'emote':
        this.emote = { icon: a.icon, until: t + (a.secs || 1.8) };
        if (a.cheer) this.cheerUntil = t + 1.2;
        this.cur = null;
        break;
      case 'call':
        a.fn();
        this.cur = null;
        break;
      default:
        this.cur = null;
    }
  }

  arrive(a) {
    this.path = null;
    this.carry = !!a.keepCarry && this.carry;
    if (a.face) this.dir = a.face;
    this.pose = a.sit ? 'sit' : 'stand';
    this.x = a.to.x; this.y = a.to.y;
    this.cur = null;
  }

  atHome() { return Math.hypot(this.x - this.home.x, this.y - this.home.y) < 1; }

  goHome() { return { type: 'go', to: this.home, face: this.home.face, sit: this.home.sit }; }

  // between runs a role sits at its desk; while a run is on (status working) it waits at its last station
  idle() {
    if (this.member.status === 'working' || this.atHome()) return;
    this.push(this.goHome());
  }

  draw(ctx, t) {
    if (t > this.blinkAt) this.blinkAt = t + 2.5 + Math.random() * 3;
    const blinking = this.blinkAt - t < 0.12;
    let eyes = blinking ? 'blink' : 'normal';
    if (this.emote?.icon === '!') eyes = 'alert';
    else if (this.emote?.icon === '★' || this.emote?.icon === '✓') eyes = 'happy';
    else if (this.bubble?.kind === 'think') eyes = 'look';
    let pose = this.pose;
    if (t < this.cheerUntil) pose = 'cheer';
    else if (this.carry && pose !== 'sit') pose = 'carry';
    drawBot(ctx, this.x, this.y, {
      color: this.color, dir: this.dir, frame: this.frame, pose, eyes, hat: this.hat,
      blink: Math.floor(t * 2) % 2 === 0, light: this.member.status === 'working' && Math.floor(t * 4) % 2 === 0,
    });
  }
}

// ------------------------------------------------------------------------------------------------ the room
export class Room {
  constructor(canvas, hooks = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.ctx.imageSmoothingEnabled = false;
    this.hooks = hooks;
    this.grid = new Grid();
    this.bots = new Map();
    this.model = { lab: null, incidents: [], proposals: [], roles: [], effective: {}, control: {}, pages: [] };
    this.tray = 0;
    this.stamp = null;
    this.alarmUntil = 0;
    this.screenFlashUntil = 0;
    this.banner = null;
    this.hover = null;
    this.last = performance.now() / 1000;
    this.particles = [];
    this.linkBlink = {};
    this.rackGlow = {};
    this.verifyUntil = 0;
    this.pins = [];
    this._raf = null;
    canvas.addEventListener('mousemove', (e) => this.onMove(e));
    canvas.addEventListener('mouseleave', () => { this.hover = null; this.hooks.onHover?.(null); });
    canvas.addEventListener('click', (e) => this.onClick(e));
  }

  start() {
    const loop = () => { this.frame(); this._raf = requestAnimationFrame(loop); };
    loop();
  }

  // ---------------------------------------------------------------- model
  setModel(m) {
    Object.assign(this.model, m);
    if (m.roles) this.syncRoles(m.roles);
    this.tray = (this.model.proposals || []).filter((p) => p.status === 'pending').length;
  }

  syncRoles(roles) {
    for (const member of roles) {
      let bot = this.bots.get(member.name);
      if (!bot) {
        bot = new Bot(member);
        this.bots.set(member.name, bot);
      }
      bot.member = member;
    }
  }

  member(name) { return this.bots.get(name)?.member || {}; }

  stationFor(station, target, bot) {
    const pick = (s) => {
      if (!Array.isArray(s)) return s;
      return s[Math.max(0, ROLE_ORDER.indexOf(bot.name)) % s.length];
    };
    if (station && station.startsWith('desk:')) return STATIONS.visit[station.slice(5)] || bot.home;
    switch (station) {
      case 'rack': {
        const r = unitRect(target) ? L.racks[rackSlot(target).rack] : L.racks[0];
        return { x: r.x + r.w / 2 + (ROLE_ORDER.indexOf(bot.name) - 1) * 5, y: 168, face: 'up' };
      }
      case 'console':
        return bot.name === 'NOCENG' ? bot.home : pick(STATIONS.screen);
      case 'desk':
      case 'phone':
        return bot.home;
      case 'screen':
      case 'netbox':
      case 'whiteboard':
      case 'printer':
      case 'operator':
      case 'board':
      case 'link':
        return pick(STATIONS[station]);
      default:
        return bot.home;
    }
  }

  go(bot, station, target, extra = {}) {
    const s = this.stationFor(station, target, bot);
    return { type: 'go', to: s, face: s.face, sit: s === bot.home && bot.home.sit, ...extra };
  }

  // ---------------------------------------------------------------- the director
  activity(a) {
    const bot = this.bots.get(a.bot);
    const short = (s, n = 34) => truncate(s || '', n);
    const t = this.now();
    if (!bot) {
      switch (a.kind) {
        case 'alert':
          this.screenFlashUntil = t + 1.6;
          if (!a.ok) this.alarmUntil = t + 4;
          break;
        case 'alarm':
          this.alarmUntil = t + 6;
          this.pins.push({ until: t + 2.5, x: L.board.x + 12, y: L.board.y + 16 });
          this.banner = { text: a.text, until: t + 5 };
          break;
        case 'approve':
          this.stamp = { ok: true, until: t + 2.2, text: 'APPROVED' };
          this.burst(240, 226, P.green);
          break;
        case 'reject':
          this.stamp = { ok: false, until: t + 2.2, text: 'REJECTED' };
          break;
        case 'apply':
          if (a.target) this.rackGlow[a.target] = t + 12;
          this.banner = { text: a.text, until: t + 4 };
          break;
        case 'verify':
          this.verifyUntil = t + 3;
          if (a.target) this.rackGlow[a.target] = 0;
          break;
        case 'field':
          this.banner = { text: a.text, until: t + 5 };
          break;
        case 'control':
        case 'question':
          this.banner = { text: a.text, until: t + 5 };
          break;
        default:
          break;
      }
      return;
    }
    switch (a.kind) {
      case 'page':
        if (a.ok) bot.push({ type: 'emote', icon: '!', secs: 2 }, { type: 'say', text: 'RING RING! ' + (a.incident_id || ''), kind: 'alert', secs: 3 });
        else bot.push({ type: 'say', text: short(a.text, 60), kind: 'err', secs: 3.5 });
        break;
      case 'tool':
        this.linkBlink[bot.name] = t + 0.6;
        if (a.tool === 'escalate' && a.args?.to) {
          const to = { 'network-engineer': 'NETENG', 'noc-manager': 'NOCMGR', 'noc-engineer': 'NOCENG' }[a.args.to];
          bot.push(this.go(bot, 'desk:' + to), { type: 'work', secs: 1.3, label: short(a.text, 40), pose: 'stand' });
        } else {
          bot.push(this.go(bot, a.station, a.target), { type: 'work', secs: 1.3, label: short(a.text, 40), target: a.target });
        }
        break;
      case 'result':
        bot.push({ type: 'say', text: a.text, kind: a.ok ? 'ok' : 'err', secs: 3.2 });
        break;
      case 'take':
        bot.push({ type: 'emote', icon: '!', secs: 1.2 }, { type: 'say', text: short('ON IT: ' + a.text, 60), kind: 'say' });
        break;
      case 'propose':
        bot.push(
          this.go(bot, 'printer'),
          { type: 'work', secs: 0.9, label: 'PRINTING TICKET', pose: 'stand' },
          { type: 'call', fn: () => this.printPaper() },
          this.go(bot, 'operator', null, { carry: true }),
          { type: 'work', secs: 0.6, label: short(a.text, 40) },
        );
        break;
      case 'close':
        bot.push(
          { type: 'work', secs: 0.7, label: 'CLOSED: ' + (a.incident_id || '') },
          { type: 'emote', icon: '★', cheer: true, secs: 2 },
          { type: 'call', fn: () => this.burst(bot.x, bot.y - 20, P.yellow) },
        );
        break;
      case 'ack':
      case 'triage':
      case 'report':
      case 'escalate':
      case 'priority':
      case 'recommend':
      case 'update':
      case 'answer':
      case 'say':
        bot.push({ type: 'say', text: a.text, kind: 'say', secs: Math.min(6, 2.2 + a.text.length / 22) });
        if (a.kind === 'report' || a.kind === 'answer') bot.push({ type: 'emote', icon: '✓', secs: 1.4 });
        break;
      case 'error':
        bot.push({ type: 'emote', icon: '×' }, { type: 'say', text: short(a.text, 60), kind: 'err', secs: 3.5 });
        break;
      default:
        break;
    }
  }

  now() { return performance.now() / 1000; }

  printPaper() {
    this.particles.push({ kind: 'paper', x: L.printer.x + 8, y: L.printer.y + 4, vy: -6, life: 1.2, t: 0 });
  }

  burst(x, y, color) {
    for (let i = 0; i < 14; i++) {
      const a = (Math.PI * 2 * i) / 14;
      this.particles.push({ kind: 'spark', x, y, vx: Math.cos(a) * 28, vy: Math.sin(a) * 28 - 10, life: 0.9, t: 0, color });
    }
  }

  // ---------------------------------------------------------------- input
  toLocal(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: ((e.clientX - r.left) * W) / r.width, y: ((e.clientY - r.top) * H) / r.height };
  }

  hit(p) {
    for (const bot of this.bots.values()) {
      if (Math.abs(p.x - bot.x) <= 7 && p.y >= bot.y - 19 && p.y <= bot.y + 2) return { kind: 'bot', bot };
    }
    for (const r of L.racks) {
      if (p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h) {
        for (const host of Object.keys(this.model.lab?.devices || {})) {
          const u = unitRect(host);
          if (u && p.x >= u.x && p.x <= u.x + u.w && p.y >= u.y && p.y <= u.y + u.h) return { kind: 'device', host };
        }
        return { kind: 'rack' };
      }
    }
    const inside = (b) => p.x >= b.x && p.x <= b.x + b.w && p.y >= b.y && p.y <= b.y + (b.h || 20);
    if (inside(L.screen)) return { kind: 'screen' };
    if (inside(L.board) || inside(L.field)) return { kind: 'board' };
    if (inside({ ...L.operator, y: L.operator.y - 8, h: 26 })) return { kind: 'operator' };
    if (inside(L.netbox)) return { kind: 'netbox' };
    if (inside(L.link)) return { kind: 'link' };
    return null;
  }

  onMove(e) {
    const p = this.toLocal(e);
    const h = this.hit(p);
    this.hover = h;
    this.canvas.style.cursor = h && h.kind !== 'rack' ? 'pointer' : 'default';
    this.hooks.onHover?.(h, e);
  }

  onClick(e) {
    const h = this.hit(this.toLocal(e));
    if (h) this.hooks.onClick?.(h, e);
  }

  // ---------------------------------------------------------------- render
  frame() {
    const t = this.now();
    const dt = Math.min(0.1, t - this.last);
    this.last = t;
    for (const bot of this.bots.values()) bot.update(dt, this, t);
    this.particles = this.particles.filter((p) => (p.t += dt) < p.life);
    for (const p of this.particles) {
      p.x += (p.vx || 0) * dt;
      p.y += (p.vy || 0) * dt;
      if (p.kind === 'spark') p.vy += 40 * dt;
    }
    this.draw(t);
  }

  draw(t) {
    const ctx = this.ctx;
    const lab = this.model.lab || {};
    const severe = (lab.problems || []).some((p) => SEV_RANK[p.severity] >= SEV_RANK.High);
    this.drawFloor(ctx, t);
    this.drawWall(ctx, t, severe);
    // depth-sorted things: furniture parts and robots, by their base line
    const items = [];
    const add = (y, fn) => items.push({ y, fn });
    add(136, () => this.drawNetbox(ctx, t));
    add(186, () => this.drawWhiteboard(ctx, t));
    add(122, () => this.drawConsole(ctx, t, severe));
    add(160, () => this.drawRacks(ctx, t));
    add(L.desks.NOCMGR.y + 18, () => this.drawRoleDesk(ctx, 'NOCMGR', t));
    add(L.desks.NETENG.y + 18, () => this.drawRoleDesk(ctx, 'NETENG', t));
    add(242, () => this.drawOperatorDesk(ctx, t));
    add(236, () => this.drawPrinter(ctx, t));
    add(L.link.y + L.link.h, () => this.drawLink(ctx, t));
    add(250, () => this.drawCoffee(ctx, t));
    add(262, () => drawPlant(ctx, 128, 262, t));
    add(150, () => drawPlant(ctx, 116, 150, t));
    for (const bot of this.bots.values()) add(bot.y + (bot.pose === 'sit' || bot.pose === 'type' ? -2 : 0), () => bot.draw(ctx, t));
    items.sort((a, b) => a.y - b.y).forEach((i) => i.fn());
    this.drawParticles(ctx);
    this.drawStamp(ctx, t);
    this.drawBubbles(ctx, t);
    this.drawNames(ctx);
    this.drawLighting(ctx, t, severe);
    this.drawBanner(ctx, t);
  }

  drawFloor(ctx) {
    rect(ctx, 0, FLOOR_Y, W, H - FLOOR_Y, P.floorA);
    for (let y = FLOOR_Y; y < H; y += 16) {
      for (let x = ((y / 16) % 2) * 16; x < W; x += 32) rect(ctx, x, y, 16, 16, P.floorB);
    }
    for (let y = FLOOR_Y; y < H; y += 16) rect(ctx, 0, y, W, 1, P.floorSeam);
    const r = L.rug;
    rect(ctx, r.x - 2, r.y - 2, r.w + 4, r.h + 4, P.rugEdge);
    rect(ctx, r.x, r.y, r.w, r.h, P.rug);
    for (let x = r.x + 4; x < r.x + r.w - 4; x += 8) { rect(ctx, x, r.y + 2, 4, 1, P.rugMark); rect(ctx, x + 4, r.y + r.h - 3, 4, 1, P.rugMark); }
    drawText(ctx, 'NETSIM NOC', r.x + r.w / 2, r.y + 11, P.rugMark, 'center');
    rect(ctx, 0, FLOOR_Y, 6, H - FLOOR_Y, P.wallTop);
    rect(ctx, W - 6, FLOOR_Y, 6, H - FLOOR_Y, P.wallTop);
    rect(ctx, 0, H - 4, W, 4, P.wallTop);
  }

  drawWall(ctx, t, severe) {
    rect(ctx, 0, 0, W, FLOOR_Y, P.wall);
    for (let x = 0; x < W; x += 40) rect(ctx, x, 0, 1, FLOOR_Y - 4, P.wallSeam);
    rect(ctx, 0, 0, W, 3, P.wallTop);
    rect(ctx, 0, FLOOR_Y - 4, W, 4, P.base);
    this.drawBoard(ctx, t);
    this.drawField(ctx, t);
    drawWallScreen(ctx, L.screen, t, { ...this.model, flash: t < this.screenFlashUntil, verifying: t < this.verifyUntil });
    this.drawWindow(ctx, t);
    this.drawClock(ctx);
    this.drawBeacon(ctx, t, severe);
  }

  drawBoard(ctx, t) {
    const b = L.board;
    rect(ctx, b.x - 2, b.y - 2, b.w + 4, b.h + 4, P.woodDark);
    rect(ctx, b.x, b.y, b.w, b.h, P.cork);
    for (let i = 0; i < 40; i++) rect(ctx, b.x + ((i * 37) % b.w), b.y + ((i * 23) % b.h), 1, 1, P.corkDot);
    const cols = [['NEW', ['new']], ['WORK', ['working']], ['YOU', ['awaiting-approval']], ['FIX', ['remediating']], ['OK', ['recovered']], ['DONE', ['closed']]];
    const cw = Math.floor(b.w / cols.length);
    const incidents = this.model.incidents || [];
    cols.forEach(([title, states], i) => {
      const x = b.x + i * cw;
      drawText(ctx, title, x + cw / 2, b.y + 2, '#f6e7c8', 'center');
      if (i) rect(ctx, x, b.y + 1, 1, b.h - 2, P.corkDark);
      let list = incidents.filter((inc) => states.includes(inc.status));
      if (title === 'DONE') list = list.filter((inc) => Date.now() / 1000 - (inc.closed || 0) < 1800).slice(0, 4);
      list.slice(0, 5).forEach((inc, j) => {
        const nx = x + 2, ny = b.y + 9 + j * 8;
        const c = title === 'DONE' ? '#b9e6c4' : title === 'OK' ? '#7fe0a8' : SEV[inc.severity] || '#ddd';
        const wob = title === 'YOU' && Math.floor(t * 2) % 2 ? 1 : 0;
        rect(ctx, nx, ny + wob, cw - 4, 7, c);
        rect(ctx, nx + Math.floor((cw - 4) / 2), ny + wob, 1, 1, inc.priority === 'P1' ? '#ffffff' : P.red);
        drawText(ctx, inc.id.replace('INC-', '').replace(/^0+/, ''), nx + 1, ny + 1 + wob, '#1b1f2a');
      });
      if (list.length > 5) drawText(ctx, '+' + (list.length - 5), x + cw / 2, b.y + b.h - 6, '#f6e7c8', 'center');
    });
    for (const pin of this.pins) {
      if (t > pin.until) continue;
      const k = Math.floor(t * 6) % 2;
      rect(ctx, pin.x - 2, pin.y - 2 - k, 5, 5, '#ff4d5e');
    }
    this.pins = this.pins.filter((p) => t <= p.until);
  }

  // Field repairs happen off-screen: a road from the office to the site, the van on its way, the stage written.
  drawField(ctx, t) {
    const f = L.field;
    rect(ctx, f.x - 2, f.y - 1, f.w + 4, f.h + 2, '#0b0e16');
    rect(ctx, f.x, f.y, f.w, f.h, '#141b2b');
    const jobs = (this.model.proposals || []).filter((p) => p.kind === 'field-repair' && (p.stage === 'dispatched' || p.stage === 'on-site' || (p.stage === 'repaired' && Date.now() / 1000 - (p.updated || 0) < 20)));
    if (!jobs.length) {
      drawText(ctx, 'FIELD: NOBODY OUT', f.x + 3, f.y + 3, '#56627c');
      return;
    }
    const job = jobs[0];
    const road = { x0: f.x + 30, x1: f.x + f.w - 10, y: f.y + 7 };
    rect(ctx, road.x0, road.y, road.x1 - road.x0, 1, '#3a4357');
    rect(ctx, road.x1, road.y - 4, 7, 5, '#5c6b8a');
    rect(ctx, road.x1 + 1, road.y - 6, 5, 2, '#8a3a2a');
    const progress = job.stage === 'dispatched' ? ((t * 0.18) % 1) * 0.85 : 1;
    const vx = Math.round(road.x0 + (road.x1 - road.x0 - 6) * progress);
    rect(ctx, vx, road.y - 4, 7, 4, job.stage === 'repaired' ? P.green : '#ff8a3d');
    rect(ctx, vx + 1, road.y, 2, 1, '#151a26');
    rect(ctx, vx + 4, road.y, 2, 1, '#151a26');
    const label = { dispatched: 'OUT', 'on-site': 'SITE', repaired: 'DONE' }[job.stage];
    drawText(ctx, label, f.x + 3, f.y + 3, job.stage === 'repaired' ? P.green : '#ffb648');
    if (jobs.length > 1) drawText(ctx, '+' + (jobs.length - 1), f.x + 20, f.y + 3, '#9fb3d9');
  }

  drawWindow(ctx, t) {
    const w = L.window;
    const hour = new Date().getHours() + new Date().getMinutes() / 60;
    const night = hour < 6 || hour >= 19.5;
    const dusk = (hour >= 17.5 && hour < 19.5) || (hour >= 6 && hour < 7.5);
    rect(ctx, w.x - 3, w.y - 3, w.w + 6, w.h + 6, P.metalDark);
    const top = night ? '#0b1430' : dusk ? '#f08a5d' : '#5fb6ff';
    const bottom = night ? '#1b2a52' : dusk ? '#ffcf7a' : '#a8dcff';
    for (let i = 0; i < w.h; i++) rect(ctx, w.x, w.y + i, w.w, 1, mix(top, bottom, i / w.h));
    if (night) {
      for (let i = 0; i < 14; i++) {
        const sx = w.x + ((i * 17) % w.w), sy = w.y + ((i * 11) % (w.h - 18));
        if ((Math.floor(t * 1.5) + i) % 5) rect(ctx, sx, sy, 1, 1, '#e8ecff');
      }
      rect(ctx, w.x + w.w - 14, w.y + 6, 5, 5, '#f4f1d0');
    } else {
      const cx = w.x + ((t * 3) % (w.w + 20)) - 10;
      rect(ctx, Math.round(cx), w.y + 8, 12, 3, '#ffffff');
      rect(ctx, Math.round(cx) + 3, w.y + 6, 6, 2, '#ffffff');
    }
    const sky = [8, 14, 10, 18, 12, 9, 16, 11];
    sky.forEach((h, i) => {
      const bx = w.x + i * 8;
      rect(ctx, bx, w.y + w.h - h, 7, h, night ? '#141b30' : '#4d6283');
      if (night) for (let k = 0; k < h - 3; k += 3) if ((i + k) % 3 === 0) rect(ctx, bx + 2, w.y + w.h - h + 2 + k, 1, 1, '#ffd77a');
    });
    rect(ctx, w.x + w.w / 2 - 1, w.y, 2, w.h, P.metalDark);
    rect(ctx, w.x, w.y + w.h / 2 - 1, w.w, 2, P.metalDark);
  }

  drawClock(ctx) {
    const { x, y } = L.clock;
    const d = new Date();
    ctx.fillStyle = P.metalLight;
    ctx.beginPath(); ctx.arc(x, y, 9, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = P.white;
    ctx.beginPath(); ctx.arc(x, y, 7.5, 0, Math.PI * 2); ctx.fill();
    const hand = (frac, len, w, c) => {
      const a = frac * Math.PI * 2 - Math.PI / 2;
      ctx.strokeStyle = c; ctx.lineWidth = w;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(a) * len, y + Math.sin(a) * len); ctx.stroke();
    };
    hand(((d.getHours() % 12) + d.getMinutes() / 60) / 12, 4, 1.4, P.ink);
    hand(d.getMinutes() / 60, 6, 1, P.ink);
    hand(d.getSeconds() / 60, 6.5, 0.6, P.red);
  }

  drawBeacon(ctx, t, severe) {
    const { x, y } = L.beacon;
    rect(ctx, x - 1, y - 8, 2, 6, P.metalDark);
    rect(ctx, x - 4, y - 2, 8, 2, P.metalDark);
    const on = severe && Math.floor(t * 4) % 2 === 0;
    rect(ctx, x - 3, y - 6, 6, 4, severe ? (on ? '#ff3344' : '#a01622') : '#3c2328');
    if (severe) {
      ctx.save();
      ctx.globalAlpha = 0.07;
      ctx.fillStyle = '#ff2a3a';
      const a = t * 3;
      ctx.beginPath();
      ctx.moveTo(x, y - 4);
      ctx.arc(x, y - 4, 520, a, a + 0.45);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
    }
  }

  drawNetbox(ctx) {
    const n = L.netbox;
    rect(ctx, n.x, n.y, n.w, n.h, '#3b4f73');
    rect(ctx, n.x, n.y, n.w, 3, '#4d6491');
    rect(ctx, n.x + 2, n.y + 5, n.w - 4, 9, '#13203a');
    drawText(ctx, 'NETBOX', n.x + n.w / 2, n.y + 7, '#9fd0ff', 'center');
    for (let i = 0; i < 4; i++) {
      const dy = n.y + 17 + i * 8;
      rect(ctx, n.x + 3, dy, n.w - 6, 6, '#466089');
      rect(ctx, n.x + n.w / 2 - 4, dy + 2, 8, 2, P.metalLight);
      rect(ctx, n.x + 5, dy + 1, 6, 3, P.paper);
    }
    rect(ctx, n.x, n.y + n.h - 2, n.w, 2, '#2b3a57');
  }

  drawWhiteboard(ctx) {
    const b = L.whiteboard;
    rect(ctx, b.x + 4, b.y + b.h, 2, 8, P.metalDark);
    rect(ctx, b.x + b.w - 6, b.y + b.h, 2, 8, P.metalDark);
    rect(ctx, b.x - 1, b.y - 1, b.w + 2, b.h + 2, P.metalLight);
    rect(ctx, b.x, b.y, b.w, b.h, '#f7f9fc');
    drawText(ctx, 'BLAST RADIUS', b.x + 2, b.y + 2, '#3d5afe');
    const devices = this.model.lab?.devices || {};
    const col = (h) => {
      const d = devices[h];
      if (!d) return '#9aa4b8';
      if (d.powered_off) return '#5a5f6b';
      if (d.uplink && d.uplink !== 'up') return P.red;
      if (d.pws && d.pws_up < d.pws) return P.amber;
      return P.green;
    };
    const node = (h, x, y, big) => { rect(ctx, x - (big ? 2 : 1), y - (big ? 2 : 1), big ? 5 : 3, big ? 5 : 3, col(h)); };
    const hubs = { 'hub-a': [b.x + 10, b.y + 12], 'hub-b': [b.x + 20, b.y + 12], 'hub-c': [b.x + 34, b.y + 12], 'hub-d': [b.x + 42, b.y + 12] };
    const spokes = { 'spoke-1': [b.x + 5, b.y + 24], 'spoke-2': [b.x + 12, b.y + 24], 'spoke-3': [b.x + 19, b.y + 24], 'spoke-4': [b.x + 26, b.y + 24], 'spoke-5': [b.x + 35, b.y + 24], 'spoke-6': [b.x + 43, b.y + 24] };
    ctx.strokeStyle = '#c4cbe0'; ctx.lineWidth = 0.6;
    for (const [s, [sx, sy]] of Object.entries(spokes)) {
      const hs = s === 'spoke-5' || s === 'spoke-6' ? ['hub-c', 'hub-d'] : ['hub-a', 'hub-b'];
      for (const h of hs) { ctx.beginPath(); ctx.moveTo(sx + 0.5, sy); ctx.lineTo(hubs[h][0] + 0.5, hubs[h][1]); ctx.stroke(); }
    }
    for (const [h, [x, y]] of Object.entries(hubs)) node(h, x, y, true);
    for (const [h, [x, y]] of Object.entries(spokes)) node(h, x, y, false);
    rect(ctx, b.x + 30, b.y + 8, 1, 20, '#d7dcea');
  }

  drawConsole(ctx, t, severe) {
    const c = L.console;
    for (let i = 0; i < 3; i++) {
      const mx = c.x + 10 + i * 26;
      rect(ctx, mx, c.y - 12, 22, 12, '#1a2133');
      rect(ctx, mx + 1, c.y - 11, 20, 1, severe && Math.floor(t * 3) % 2 ? '#ff5c5c' : '#38b6ff');
      rect(ctx, mx + 9, c.y, 4, 2, P.metalDark);
    }
    drawDesk(ctx, c.x, c.y + 2, c.w, { drawer: true });
    drawText(ctx, 'ZABBIX CONSOLE', c.x + c.w / 2 - 6, c.y + 6, '#9fb3d9', 'center');
    const m = this.member('NOCENG');
    drawPhone(ctx, c.x + c.w - 13, c.y - 4, t, { ringing: m.status === 'paged', missed: !m.routine });
    drawChair(ctx, HOMES.NOCENG.x, HOMES.NOCENG.y + 1, '#3a4664', 'up');
  }

  drawRoleDesk(ctx, name, t) {
    const d = L.desks[name];
    const m = this.member(name);
    const working = m.status === 'working';
    drawMonitor(ctx, d.x + d.w - 26, d.y - 14, 22, 15, t, { mode: working ? 'code' : m.status === 'paged' ? 'alert' : 'graph', color: m.color || P.glow });
    drawDesk(ctx, d.x, d.y, d.w, {});
    drawPhone(ctx, d.x + 4, d.y - 4, t, { ringing: m.status === 'paged', missed: !m.routine });
    rect(ctx, d.x + 16, d.y + 1, 14, 2, '#2b3245');
    drawText(ctx, d.label, d.x + d.w / 2, d.y + 6, m.color || P.dim, 'center');
    if (working && Math.floor(t * 2) % 2) rect(ctx, d.x + d.w - 30, d.y - 3, 1, 2, '#d7dce6');
  }

  drawRacks(ctx, t) {
    const devices = this.model.lab?.devices || {};
    const now = this.now();
    L.racks.forEach((r, ri) => {
      rect(ctx, r.x, r.y, r.w, r.h, '#151a26');
      rect(ctx, r.x, r.y, r.w, 3, '#2a3245');
      rect(ctx, r.x + 1, r.y + 3, 2, r.h - 5, '#3a4357');
      rect(ctx, r.x + r.w - 3, r.y + 3, 2, r.h - 5, '#3a4357');
      drawText(ctx, r.title, r.x + r.w / 2, r.y + r.h + 2, P.dim, 'center');
      const hosts = ri === 0 ? HUBS : ['spoke-1', 'spoke-2', 'spoke-3', 'spoke-4', 'spoke-5', 'spoke-6'];
      hosts.forEach((host) => {
        const u = unitRect(host);
        const d = devices[host];
        const inspected = [...this.bots.values()].some((b) => b.target === host) || (this.rackGlow[host] || 0) > now;
        rect(ctx, u.x, u.y, u.w, u.h, d?.powered_off ? '#1d2230' : '#2c3446');
        if (inspected) {
          ctx.strokeStyle = Math.floor(t * 6) % 2 ? '#7fd8ff' : '#2f8fd8';
          ctx.lineWidth = 1;
          ctx.strokeRect(u.x - 0.5, u.y - 0.5, u.w + 1, u.h + 1);
        }
        drawText(ctx, host.replace('spoke-', 'SPK').replace('hub-', 'HUB'), u.x + 2, u.y + 2, d?.powered_off ? '#4a5266' : '#c9d3e6');
        const power = d && !d.powered_off;
        const uplinkOk = d && d.uplink === 'up' && !(d.faults && Object.keys(d.faults).length);
        const pwOk = d && d.pws && d.pws_up === d.pws;
        const pwSome = d && d.pws_up > 0;
        const blink = (Math.floor(t * 5) + host.length) % 3 === 0;
        rect(ctx, u.x + u.w - 9, u.y + 2, 2, 2, power ? (blink ? '#9dffb6' : P.green) : '#2a2f3a');
        rect(ctx, u.x + u.w - 6, u.y + 2, 2, 2, !power ? '#2a2f3a' : uplinkOk ? P.green : Math.floor(t * 4) % 2 ? P.red : '#5a1018');
        rect(ctx, u.x + u.w - 3, u.y + 2, 2, 2, !power ? '#2a2f3a' : pwOk ? P.green : pwSome ? P.amber : P.red);
        rect(ctx, u.x + 2, u.y + 7, u.w - 4, 1, '#232a39');
      });
      if (ri === 0) {
        const y = r.y + 8 + 4 * RACK_PITCH;
        rect(ctx, r.x + 4, y, r.w - 8, 9, '#232b3c');
        drawText(ctx, 'CORE', r.x + 6, y + 2, '#8ea0c2');
        for (let k = 0; k < 4; k++) rect(ctx, r.x + 20 + k * 3, y + 3, 2, 2, (Math.floor(t * 8) + k) % 3 ? P.green : '#1f5f3a');
        const y2 = y + RACK_PITCH;
        rect(ctx, r.x + 4, y2, r.w - 8, 9, '#1f2635');
        for (let k = 0; k < 8; k++) rect(ctx, r.x + 6 + k * 3, y2 + 3, 2, 3, ['#4aa3ff', '#ffd84d', '#39d98a', '#ff6b5e'][k % 4]);
      }
    });
  }

  // The cloud link: /mcp through the tunnel to the Grok Bot app's cloud PC; one light per role, on each tool call.
  drawLink(ctx, t) {
    const k = L.link;
    rect(ctx, k.x, k.y, k.w, k.h, '#1b2232');
    rect(ctx, k.x, k.y, k.w, 2, '#2c3650');
    rect(ctx, k.x + k.w - 8, k.y - 10, 1, 10, P.metalLight);
    const any = ROLE_ORDER.some((n) => (this.linkBlink[n] || 0) > t);
    rect(ctx, k.x + k.w - 9, k.y - 12, 3, 3, any && Math.floor(t * 8) % 2 ? '#7fdcff' : '#2a3550');
    drawText(ctx, 'CLOUD /MCP', k.x + 3, k.y + 4, '#9fb3d9');
    ROLE_ORDER.forEach((n, i) => {
      const m = this.member(n);
      const lit = (this.linkBlink[n] || 0) > t;
      rect(ctx, k.x + 4 + i * 8, k.y + 12, 5, 3, lit ? '#ffffff' : m.status === 'working' ? m.color : '#2a3142');
    });
    drawText(ctx, 'APP', k.x + k.w - 15, k.y + 11, '#56627c');
  }

  drawOperatorDesk(ctx, t) {
    const o = L.operator;
    const pending = this.tray;
    drawDesk(ctx, o.x, o.y, o.w, { drawer: true });
    rect(ctx, o.x + 6, o.y - 3, 18, 5, '#3c4458');
    for (let i = 0; i < Math.min(pending, 5); i++) rect(ctx, o.x + 7, o.y - 4 - i * 2, 16, 2, i % 2 ? P.paper : '#e4e0d4');
    const on = pending > 0 && Math.floor(t * 2) % 2 === 0;
    rect(ctx, o.x + o.w - 14, o.y - 8, 7, 5, pending ? (on ? '#ffb648' : '#7a5420') : '#39404f');
    rect(ctx, o.x + o.w - 12, o.y - 3, 3, 3, P.metalDark);
    drawText(ctx, 'OPERATOR', o.x + o.w / 2, o.y + 5, '#c7d1e6', 'center');
    if (pending) drawText(ctx, pending + ' TO APPROVE', o.x + o.w / 2, o.y - 13, on ? P.amber : '#c89a52', 'center');
    drawChair(ctx, o.x + o.w / 2, o.y + 30, '#4a3a5e', 'down');
    drawText(ctx, 'YOU', o.x + o.w / 2, o.y + 35, '#8d7fae', 'center');
  }

  drawPrinter(ctx, t) {
    const p = L.printer;
    rect(ctx, p.x, p.y + 6, p.w, p.h - 6, '#cfd5e0');
    rect(ctx, p.x, p.y + 6, p.w, 3, '#e6eaf1');
    rect(ctx, p.x + 3, p.y + 2, p.w - 6, 5, P.paper);
    rect(ctx, p.x + 4, p.y + 12, p.w - 8, 2, '#3b4255');
    rect(ctx, p.x + p.w - 6, p.y + 16, 2, 2, Math.floor(t * 2) % 2 ? P.green : '#1e6b44');
    drawText(ctx, 'PRINT', p.x + p.w / 2, p.y + p.h + 1, P.dim, 'center');
  }

  drawCoffee(ctx) {
    const c = L.coffee;
    rect(ctx, c.x, c.y, c.w, c.h, '#3d3532');
    rect(ctx, c.x + 2, c.y + 3, c.w - 4, 8, '#1e1a19');
    rect(ctx, c.x + 3, c.y + 5, 2, 2, P.red);
    rect(ctx, c.x + 4, c.y + 18, c.w - 8, 6, '#24201f');
    rect(ctx, c.x + 6, c.y + 21, 4, 3, P.white);
    const k = L.cooler;
    rect(ctx, k.x, k.y + 8, k.w, k.h - 8, '#d9dee8');
    rect(ctx, k.x + 1, k.y, k.w - 2, 9, '#7cc7ff');
    rect(ctx, k.x + 2, k.y + 1, 2, 6, '#b6e2ff');
    drawText(ctx, 'COFFEE', c.x + 14, c.y + c.h + 2, P.dim, 'center');
  }

  drawParticles(ctx) {
    for (const p of this.particles) {
      if (p.kind === 'paper') rect(ctx, p.x, p.y, 8, 5, P.paper);
      else rect(ctx, p.x, p.y, 2, 2, p.color || P.yellow);
    }
  }

  drawStamp(ctx, t) {
    if (!this.stamp || t > this.stamp.until) return;
    const o = L.operator;
    const c = this.stamp.ok ? P.green : P.red;
    const w = textWidth(this.stamp.text) + 8;
    const x = o.x + o.w / 2 - w / 2, y = o.y - 30;
    rect(ctx, x, y, w, 11, c);
    rect(ctx, x + 1, y + 1, w - 2, 9, shade(c, -0.3));
    drawText(ctx, this.stamp.text, o.x + o.w / 2, y + 3, '#ffffff', 'center');
  }

  // Speech bubbles and emotes for every robot; bubbles that would overlap are stacked upwards.
  drawBubbles(ctx, t) {
    const styles = {
      say: [P.white, '#1b2330', '#9aa4b8'],
      ok: [P.white, '#1b2330', P.green],
      err: ['#fff1f2', '#5a1018', P.red],
      alert: ['#fff7e6', '#3a2400', P.amber],
      think: ['#e8eef9', '#55627a', '#b5c0d6'],
    };
    const boxes = [];
    for (const bot of this.bots.values()) {
      const em = bot.emote;
      if (em) {
        const color = drawEmote(ctx, bot.x, bot.y, em.icon, t);
        drawText(ctx, em.icon, bot.x, bot.y - 27 + Math.round(Math.sin(t * 6)), color, 'center');
        continue;
      }
      const b = bot.bubble;
      if (!b) continue;
      const kind = b.kind;
      const lines = kind === 'think' ? ['…'] : wrap(b.text, kind === 'label' ? 26 : 30, kind === 'label' ? 1 : 3);
      const w = Math.max(...lines.map(textWidth)) + 6;
      const h = lines.length * LINE + 3;
      const x = Math.max(2, Math.min(W - w - 2, Math.round(bot.x - w / 2)));
      const style = kind === 'label' ? ['rgba(10,16,30,0.86)', '#8fe9ff', bot.color] : styles[kind] || styles.say;
      boxes.push({ bot, lines, x, y: Math.round(bot.y - 22 - h), w, h, style });
    }
    boxes.sort((a, b) => b.y - a.y);
    const placed = [];
    for (const box of boxes) {
      for (let guard = 0; guard < 8; guard++) {
        const hit = placed.find((o) => box.x < o.x + o.w + 2 && o.x < box.x + box.w + 2 && box.y < o.y + o.h + 3 && o.y < box.y + box.h + 3);
        if (!hit) break;
        box.y = hit.y - box.h - 4;
      }
      box.y = Math.max(2, box.y);
      placed.push(box);
    }
    for (const { bot, lines, x, y, w, h, style } of placed) {
      const [bg, fg, border] = style;
      rect(ctx, x - 1, y, w + 2, h, border);
      rect(ctx, x, y - 1, w, h + 2, border);
      rect(ctx, x, y, w, h, bg);
      const tx = Math.max(x + 3, Math.min(x + w - 4, Math.round(bot.x)));
      if (bot.y - 22 - (y + h) < 6) {
        rect(ctx, tx - 1, y + h, 3, 1, border);
        rect(ctx, tx, y + h + 1, 1, 1, border);
      } else {
        ctx.fillStyle = border;
        for (let yy = y + h; yy < bot.y - 21; yy += 2) ctx.fillRect(tx, yy, 1, 1);
      }
      lines.forEach((line, i) => drawText(ctx, line, x + 3, y + 2 + i * LINE, fg));
    }
  }

  drawNames(ctx) {
    for (const bot of this.bots.values()) {
      const hovered = this.hover?.bot === bot;
      if (!hovered && bot.pose !== 'walk' && bot.pose !== 'stand') continue;
      drawText(ctx, bot.name, bot.x, bot.y + 3, hovered ? '#ffffff' : shade(bot.color, -0.1), 'center');
    }
  }

  drawLighting(ctx, t, severe) {
    const hour = new Date().getHours();
    if (hour < 6 || hour >= 20) {
      ctx.fillStyle = 'rgba(8,10,30,0.18)';
      ctx.fillRect(0, 0, W, H);
    }
    if (severe || t < this.alarmUntil) {
      const a = 0.05 + 0.05 * (Math.sin(t * 5) * 0.5 + 0.5);
      const g = ctx.createRadialGradient(W / 2, H / 2, 80, W / 2, H / 2, 300);
      g.addColorStop(0, 'rgba(255,40,60,0)');
      g.addColorStop(1, `rgba(255,40,60,${a * 2})`);
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, W, H);
    }
    const c = this.model.control || {};
    const msg = c.paused ? 'MAINTENANCE MODE: ALERTS RECORDED, NOBODY PAGED' : c.pages_muted ? 'PAGES MUTED: ALERTS RECORDED ONLY' : c.sink_url ? 'TEST SINK: PAGES GO TO THE ROUTINE SINK' : null;
    if (msg) {
      if (c.paused) { ctx.fillStyle = 'rgba(30,40,80,0.18)'; ctx.fillRect(0, 0, W, H); }
      const w = textWidth(msg) + 10;
      rect(ctx, W / 2 - w / 2, H - 16, w, 10, c.paused ? '#2f3b73' : '#3a2f12');
      drawText(ctx, msg, W / 2, H - 13, '#d4dcff', 'center');
    }
  }

  drawBanner(ctx, t) {
    if (!this.banner || t > this.banner.until) return;
    const text = truncate(this.banner.text, 80);
    const w = textWidth(text) + 10;
    rect(ctx, W / 2 - w / 2, FLOOR_Y + 2, w, 10, 'rgba(10,14,26,0.85)');
    drawText(ctx, text, W / 2, FLOOR_Y + 5, '#d8e2f5', 'center');
  }

  unitAt(host) { return unitRect(host); }
}

function mix(a, b, f) {
  const pa = parseInt(a.slice(1), 16), pb = parseInt(b.slice(1), 16);
  const ch = (s) => Math.round(((pa >> s) & 255) * (1 - f) + ((pb >> s) & 255) * f);
  return '#' + ((1 << 24) | (ch(16) << 16) | (ch(8) << 8) | ch(0)).toString(16).slice(1);
}

export { statusColor, TIER, SEV };
