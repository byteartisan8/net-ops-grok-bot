// The NOC's big wall screen: Zabbix's L2VPN services, the topology and the open problems, drawn in pixels from
// the bot's live view of the lab (polled every few seconds through lab-api). Click it in the room to zoom.
import { drawText, textWidth, truncate } from './font.js';
import { P, SEV, SEV_RANK, TIER, rect, statusColor } from './sprites.js';

const HUB_POS = {
  'hub-a': [0.25, 0], 'hub-b': [0.75, 0], 'hub-c': [0.25, 0], 'hub-d': [0.75, 0],
};
const SEV_SHORT = { Disaster: 'D', High: 'H', Average: 'A', Warning: 'W', Information: 'I', 'Not classified': 'N' };

export function drawWallScreen(ctx, R, t, model) {
  const lab = model.lab || {};
  rect(ctx, R.x - 2, R.y - 1, R.w + 4, R.h + 3, '#0b0e16');
  rect(ctx, R.x, R.y, R.w, R.h, '#151b29');
  const S = { x: R.x + 3, y: R.y + 3, w: R.w - 6, h: R.h - 6 };
  rect(ctx, S.x, S.y, S.w, S.h, '#06101c');
  rect(ctx, R.x + R.w / 2 - 6, R.y + R.h, 12, 2, '#2a3142');

  if (!lab.ts) {
    drawText(ctx, 'CONNECTING TO LAB-API…', S.x + S.w / 2, S.y + 28, P.dim, 'center');
    return;
  }
  if (!lab.ok) {
    if (Math.floor(t * 2) % 2) rect(ctx, S.x, S.y, S.w, S.h, '#2a0710');
    drawText(ctx, 'LAB-API UNREACHABLE', S.x + S.w / 2, S.y + 24, P.red, 'center');
    drawText(ctx, truncate(lab.error || '', 56), S.x + S.w / 2, S.y + 34, '#ff9aa5', 'center');
    return;
  }
  const problems = lab.problems || [];
  const severe = problems.some((p) => SEV_RANK[p.severity] >= SEV_RANK.High);
  const pages = ['services'];
  if (problems.length) pages.push('problems');
  pages.push('crew');
  const period = severe ? 6 : 9;
  const page = pages[Math.floor(t / period) % pages.length];

  // header
  const title = { services: 'ZABBIX · L2VPN SERVICES', problems: `ZABBIX · PROBLEMS (${problems.length})`, crew: 'NOC · GROK CREW' }[page];
  drawText(ctx, title, S.x + 3, S.y + 2, '#7fdcff');
  let hx = S.x + S.w - 3;
  const counts = lab.counts || {};
  for (const sev of ['Warning', 'Average', 'High', 'Disaster']) {
    const n = counts[sev] || 0;
    const label = SEV_SHORT[sev] + n;
    const w = textWidth(label) + 4;
    hx -= w + 2;
    rect(ctx, hx, S.y + 1, w, 7, n ? SEV[sev] : '#1c2536');
    drawText(ctx, label, hx + 2, S.y + 2, n ? '#10131c' : '#56627c');
  }
  pages.forEach((p, i) => rect(ctx, S.x + S.w / 2 + 40 + i * 4, S.y + 4, 2, 2, p === page ? '#7fdcff' : '#2a3550'));
  rect(ctx, S.x, S.y + 9, S.w, 1, '#13233a');

  if (page === 'services') drawServices(ctx, S, t, lab);
  else if (page === 'problems') drawProblems(ctx, S, t, problems);
  else drawCrew(ctx, S, t, model);

  // ticker
  const ty = S.y + S.h - 8;
  rect(ctx, S.x, ty, S.w, 8, '#0b1626');
  let text;
  if (!problems.length) text = `ALL ${lab.services?.length || 0} SERVICES OK · NO OPEN PROBLEMS · POLLING IS TRUTH · ROOT ${lab.root || '?'} · `;
  else text = problems.map((p) => `${SEV_SHORT[p.severity] || '?'} ${p.name}`).join('  ·  ') + '  ·  ';
  const tw = textWidth(text) + 4;
  const off = (t * 18) % tw;
  ctx.save();
  ctx.beginPath();
  ctx.rect(S.x + 1, ty, S.w - 2, 8);
  ctx.clip();
  const color = problems.length ? (severe ? '#ff8f98' : '#ffd84d') : '#5fe3a1';
  for (let x = S.x + 2 - off; x < S.x + S.w; x += tw) drawText(ctx, text, x, ty + 2, color);
  ctx.restore();

  // scanlines
  ctx.fillStyle = 'rgba(0,0,0,0.18)';
  for (let y = S.y; y < S.y + S.h; y += 2) ctx.fillRect(S.x, y, S.w, 1);
}

function drawServices(ctx, S, t, lab) {
  const services = lab.services || [];
  const north = services.filter((s) => s.region !== 'South');
  const south = services.filter((s) => s.region === 'South');
  const tile = (s, x, y) => {
    const st = s.zabbix || 'OK';
    const c = statusColor(st === 'OK' ? 'OK' : st);
    const bad = st !== 'OK';
    const blink = st === 'Disaster' && Math.floor(t * 3) % 2;
    rect(ctx, x, y, 25, 10, blink ? '#5d0c16' : bad ? shadeBg(st) : '#0d2a1d');
    rect(ctx, x, y, 25, 1, c);
    rect(ctx, x, y + 9, 25, 1, c);
    rect(ctx, x, y, 2, 10, TIER[s.tier] || '#666');
    drawText(ctx, (s.service || '').replace('SVC-', ''), x + 4, y + 3, bad ? '#ffffff' : '#b9f5d2');
    if (s.rules === 'unprotected') rect(ctx, x + 22, y + 3, 2, 2, '#ffb648');
  };
  drawText(ctx, 'NORTH', S.x + 3, S.y + 12, '#6f86b3');
  north.slice(0, 8).forEach((s, i) => tile(s, S.x + 3 + (i % 4) * 27, S.y + 19 + Math.floor(i / 4) * 12));
  drawText(ctx, 'SOUTH', S.x + 3, S.y + 44, '#6f86b3');
  south.slice(0, 4).forEach((s, i) => tile(s, S.x + 3 + i * 27, S.y + 51 - 1));
  // legend
  let lx = S.x + 84;
  for (const [tier, c] of Object.entries(TIER)) {
    rect(ctx, lx, S.y + 45, 3, 3, c);
    drawText(ctx, tier.toUpperCase().slice(0, 1), lx + 4, S.y + 44, '#7b8bab');
    lx += 9;
  }
  drawTopology(ctx, { x: S.x + 116, y: S.y + 11, w: S.w - 119, h: S.h - 21 }, t, lab);
}

function shadeBg(status) {
  return { Warning: '#3a3006', Average: '#3d2508', High: '#3d1010', Disaster: '#4a0b12' }[status] || '#222a3a';
}

function drawTopology(ctx, B, t, lab) {
  const devices = lab.devices || {};
  const regions = [
    { name: 'NORTH', hubs: ['hub-a', 'hub-b'], spokes: ['spoke-1', 'spoke-2', 'spoke-3', 'spoke-4'] },
    { name: 'SOUTH', hubs: ['hub-c', 'hub-d'], spokes: ['spoke-5', 'spoke-6'] },
  ];
  const gap = 4;
  const rw = [Math.round((B.w - gap) * 0.6), 0];
  rw[1] = B.w - gap - rw[0];
  const pos = {};
  let x0 = B.x;
  regions.forEach((r, ri) => {
    const w = rw[ri];
    rect(ctx, x0, B.y, w, B.h, '#0a1828');
    rect(ctx, x0, B.y, w, 1, '#1d3354');
    r.hubs.forEach((h) => { pos[h] = [x0 + Math.round(w * HUB_POS[h][0]), B.y + 6]; });
    r.spokes.forEach((s, i) => { pos[s] = [x0 + Math.round(((i + 0.5) * w) / r.spokes.length), B.y + B.h - 9]; });
    x0 += w + gap;
  });
  // PW lines: one per (spoke, hub) pair, colored by the worst PW on it
  const pairs = {};
  for (const s of lab.services || []) {
    for (const pw of s.pws || []) {
      const key = `${s.spoke}|${pw.hub}`;
      const state = pw.up ? 0 : 2;
      pairs[key] = Math.max(pairs[key] ?? 0, state);
    }
  }
  for (const [key, state] of Object.entries(pairs)) {
    const [s, h] = key.split('|');
    if (!pos[s] || !pos[h]) continue;
    ctx.strokeStyle = state ? (Math.floor(t * 3) % 2 ? '#ff4d5e' : '#8c1f2b') : '#2fbf71';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(pos[s][0] + 0.5, pos[s][1] - 1);
    ctx.lineTo(pos[h][0] + 0.5, pos[h][1] + 3);
    ctx.stroke();
  }
  const devColor = (h) => {
    const d = devices[h];
    if (!d) return '#56627c';
    if (d.powered_off) return '#3a4152';
    if (d.uplink && d.uplink !== 'up') return '#ff4d5e';
    if (d.faults && Object.keys(d.faults).length) return '#ff4d5e';
    if (d.pws && d.pws_up < d.pws) return '#ffb648';
    return '#39d98a';
  };
  for (const [h, [x, y]] of Object.entries(pos)) {
    const d = devices[h];
    const hub = h.startsWith('hub');
    const c = devColor(h);
    const sz = hub ? 7 : 5;
    rect(ctx, x - Math.floor(sz / 2), y - (hub ? 3 : 2), sz, sz, c);
    if (d?.powered_off) {
      rect(ctx, x - 2, y - 2 + (hub ? -1 : 0), 1, 1, '#ff4d5e');
      rect(ctx, x + 2, y - 2 + (hub ? -1 : 0), 1, 1, '#ff4d5e');
      rect(ctx, x, y + (hub ? -1 : 0), 1, 1, '#ff4d5e');
    }
    const label = hub ? h.slice(4).toUpperCase() : h.slice(6);
    if (hub) drawText(ctx, label, x + 5, y - 2, '#a9b8d6');
    else drawText(ctx, label, x, y + 4, '#a9b8d6', 'center');
  }
}

function drawProblems(ctx, S, t, problems) {
  const rows = [...problems].sort((a, b) => (SEV_RANK[b.severity] || 0) - (SEV_RANK[a.severity] || 0) || a.clock - b.clock);
  const now = Date.now() / 1000;
  rows.slice(0, 6).forEach((p, i) => {
    const y = S.y + 12 + i * 7;
    const c = SEV[p.severity] || '#888';
    rect(ctx, S.x + 3, y, 4, 5, c);
    drawText(ctx, truncate(p.name || '', 49), S.x + 10, y, '#dfe7f6');
    const age = Math.max(0, Math.round(now - (p.clock || now)));
    drawText(ctx, age < 120 ? `${age}S` : `${Math.round(age / 60)}M`, S.x + S.w - 3, y, '#8c9bbb', 'right');
  });
  if (rows.length > 6) drawText(ctx, `+${rows.length - 6} MORE`, S.x + S.w - 3, S.y + 12 + 6 * 7, '#8c9bbb', 'right');
  void t;
}

function drawCrew(ctx, S, t, model) {
  // the visitor (an outside agent) is listed only while it is in the room
  const crew = (model.crew || []).filter((m) => m.role !== 'visitor' || m.status !== 'away');
  crew.slice(0, 5).forEach((m, i) => {
    const y = S.y + 12 + i * 7;
    rect(ctx, S.x + 3, y + 1, 3, 3, m.color);
    drawText(ctx, m.name, S.x + 8, y, m.color);
    const st = (m.status || '').toUpperCase();
    drawText(ctx, st, S.x + 34, y, st === 'WORKING' ? '#7fdcff' : st === 'AWAY' ? '#ffb648' : '#6c7a99');
    const busy = m.role === 'watcher' || (st !== 'IDLE' && st !== 'PAUSED');
    drawText(ctx, truncate(busy ? m.activity || '' : st === 'PAUSED' ? 'ON THE COUCH' : '-', 41), S.x + 68, y, busy ? '#c3cde2' : '#56627c');
  });
  const eff = model.effective || {};
  const usage = model.usage || {};
  const incidents = model.incidents || [];
  const open = incidents.filter((i) => i.status !== 'resolved').length;
  const today = new Date().toISOString().slice(0, 10);
  const solved = incidents.filter((i) => i.status === 'resolved' && new Date((i.resolved || 0) * 1000).toISOString().slice(0, 10) === today);
  const mttr = solved.length ? Math.round(solved.reduce((a, i) => a + (i.resolved - i.created), 0) / solved.length) : null;
  const brain = eff.brain === 'grok' ? (eff.model || 'GROK').toUpperCase() : 'RULES';
  const line = `BRAIN ${brain} · ${String(eff.autonomy || '').toUpperCase()} · OPEN ${open} · SOLVED TODAY ${solved.length}` +
    (mttr !== null ? ` · MTTR ${mttr}S` : '') + ` · $${(usage.usd_day || 0).toFixed(3)}`;
  drawText(ctx, truncate(line, 57), S.x + 3, S.y + 48, '#9fb3d9');
  void t;
}
