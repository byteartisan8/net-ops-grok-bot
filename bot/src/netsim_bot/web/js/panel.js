// The side panel: incidents (with the bot's report and its change tickets), approvals, live activity and the chat.

const SEVC = {
  Disaster: 'var(--dis)', High: 'var(--high)', Average: 'var(--avg)', Warning: 'var(--warn)',
  Information: 'var(--info)', 'Not classified': '#97a3b6', OK: 'var(--ok)',
};
const STATUSC = {
  open: '#ffd84d', investigating: '#5cc8ff', diagnosed: '#c792ea', 'awaiting-approval': '#ffa64d',
  remediating: '#7aa2ff', monitoring: '#7aa2ff', resolved: '#39d98a', pending: '#ffa64d', approved: '#7aa2ff',
  applying: '#7aa2ff', applied: '#39d98a', failed: '#ff6b5e', rejected: '#8b97b1', obsolete: '#8b97b1',
};
const TIERC = { gold: 'var(--gold)', silver: 'var(--silver)', bronze: 'var(--bronze)' };
const KIND_ICON = {
  alarm: '🚨', assign: '🙋', think: '💭', say: '💬', tool: '🔧', result: '↳', propose: '📝', report: '📋', await: '⏳',
  approve: '✅', reject: '⛔', apply: '⚙️', verify: '🔍', travel: '🚐', return: '🏠', resolve: '🎉', chat: '❓',
  error: '⚠️', control: '🎛️', idle: '·',
};

export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

export function age(ts) {
  if (!ts) return '-';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const dur = (s) => (s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);

function sev(s) { return `<span class="sev" style="--c:${SEVC[s] || '#888'}">${esc(s === 'Not classified' ? 'N/C' : s)}</span>`; }
function pill(s) { return `<span class="pill" style="--c:${STATUSC[s] || 'var(--muted)'}">${esc(s)}</span>`; }

function tierOf(state, service) {
  const s = (state.lab?.services || []).find((x) => x.service === service);
  return s?.tier;
}
function svcChip(state, service) {
  const t = tierOf(state, service);
  return `<span class="svc" style="--t:${TIERC[t] || '#555'}" title="${esc(t || '')}">${esc(service)}</span>`;
}

// ------------------------------------------------------------------------------------------------ incidents
export function renderIncidents(state) {
  const incidents = [...state.incidents.values()].sort((a, b) => {
    const act = (i) => (i.status === 'resolved' ? 1 : 0);
    return act(a) - act(b) || b.created - a.created;
  });
  const parts = [];
  parts.push(brainNote(state));
  if (!incidents.length) {
    parts.push(`<div class="empty"><span class="big">🟢</span>No incidents yet. POLL is watching Zabbix every ${esc(state.settings?.poll_s ?? 2)} s.<br>Try <code>uv run labctl scenario apply hub-down</code> and watch the crew.</div>`);
    return parts.join('');
  }
  const settling = state.settling ? `<div class="note">⏱ ${state.settling} new problem(s) settling before an incident opens…</div>` : '';
  parts.push(settling);
  for (const i of incidents.slice(0, 60)) {
    const services = (i.services || []).slice(0, 6).map((s) => svcChip(state, s)).join(' ');
    const pending = [...state.proposals.values()].filter((p) => p.incident_id === i.id && p.status === 'pending').length;
    parts.push(`<div class="card click" data-incident="${esc(i.id)}">
      <div class="row">${sev(i.severity)}<span class="title">${esc(i.id)} · ${esc(i.title)}</span>${pill(i.status)}</div>
      <div class="meta"><span>${services}${(i.services || []).length > 6 ? ' …' : ''}</span></div>
      <div class="meta"><span>${esc(i.assignee || 'unassigned')}${i.model ? ' · ' + esc(i.model) : ''}</span>
        <span>${i.active_problem_count}/${i.problem_count} problems open</span>
        ${pending ? `<span style="color:var(--avg)">⏳ ${pending} to approve</span>` : ''}
        <span>${i.status === 'resolved' ? 'resolved in ' + dur((i.resolved || 0) - i.created) : 'opened ' + age(i.created)}</span>
        ${i.usage?.usd ? `<span>$${i.usage.usd.toFixed(4)}</span>` : ''}</div>
    </div>`);
  }
  return parts.join('');
}

function brainNote(state) {
  const eff = state.effective || {};
  if (eff.brain === 'grok') return '';
  if (eff.why && eff.why.includes('XAI_API_KEY')) {
    return `<div class="warnbox">🤖 The responders run the <b>rules</b> autopilot: no Grok key yet. Add <code>XAI_API_KEY=xai-…</code> to <code>.env</code> (console.x.ai → API Keys); the bot picks it up within seconds, no restart.</div>`;
  }
  if (eff.why === 'rules brain selected') return '<div class="warnbox">🤖 Responders use the <b>rules</b> brain (chosen in the header or with <code>BOT_BRAIN=rules</code>).</div>';
  if (eff.why) return `<div class="warnbox">🤖 Responders use the <b>rules</b> brain: ${esc(eff.why)}.</div>`;
  return '';
}

export function renderIncidentDetail(state, inc) {
  if (!inc) return '<div class="empty">Loading…</div>';
  const r = inc.report;
  const out = [`<button class="back" data-back>← all incidents</button>`];
  out.push(`<div class="card"><div class="row">${sev(inc.severity)}<span class="title">${esc(inc.id)} · ${esc(inc.title)}</span>${pill(inc.status)}</div>
    <div class="meta"><span>opened ${clock(inc.created)}</span>${inc.resolved ? `<span>resolved ${clock(inc.resolved)} (${dur(inc.resolved - inc.created)})</span>` : ''}
    <span>${esc(inc.assignee || '-')} · ${esc(inc.model || '-')}</span>
    ${inc.usage?.calls ? `<span>Grok ${inc.usage.calls} calls · ${inc.usage.input_tokens} in / ${inc.usage.output_tokens} out · $${(inc.usage.usd || 0).toFixed(4)}</span>` : ''}</div>
    ${inc.status !== 'resolved' ? `<div class="actions" style="margin-top:8px"><button class="btn small" data-reinvestigate="${esc(inc.id)}">Investigate again</button></div>` : ''}
  </div>`);
  if (r) {
    const tiers = r.impact_by_tier && Object.keys(r.impact_by_tier).length
      ? `<table class="tier-table"><tr><th>tier</th><th>down</th><th>degraded</th></tr>${Object.entries(r.impact_by_tier).map(([t, v]) => `<tr><td><span class="svc" style="--t:${TIERC[t] || '#555'}">${esc(t)}</span></td><td>${esc(v?.down ?? 0)}</td><td>${esc(v?.degraded ?? 0)}</td></tr>`).join('')}</table>` : '';
    out.push(`<div class="section">Diagnosis · ${esc(r.category)} · confidence ${esc(r.confidence)}</div>
      <div class="card"><div>${esc(r.summary)}</div>
      <dl class="kv" style="margin-top:8px"><dt>root cause</dt><dd>${esc(r.root_cause)}</dd>
      <dt>recommendation</dt><dd>${esc(r.recommendation || '-')}</dd>
      ${(r.affected || []).length ? `<dt>affected</dt><dd>${r.affected.map((a) => `${svcChip(state, a.service)} ${esc(a.state)}`).join(' · ')}</dd>` : ''}</dl>
      ${tiers}
      ${(r.evidence || []).length ? `<div class="section">Evidence</div><ul class="ev">${r.evidence.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}
      </div>`);
  } else if (inc.status !== 'resolved') {
    out.push(`<div class="note">🔎 ${esc(inc.assignee || 'A responder')} is investigating…</div>`);
  }
  const proposals = inc.proposals || [];
  if (proposals.length) {
    const pending = proposals.filter((p) => p.status === 'pending');
    out.push(`<div class="section">Change tickets</div>`);
    if (pending.length > 1) out.push(`<button class="btn primary small" data-approve-all="${esc(inc.id)}" style="margin-bottom:8px">Approve all ${pending.length} (make before break)</button>`);
    for (const p of proposals) out.push(renderProposal(state, p));
  }
  if (inc.notes?.length) out.push(`<div class="section">Notes</div><ul class="ev">${inc.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`);
  const probs = Object.values(inc.problems || {}).sort((a, b) => a.clock - b.clock);
  if (probs.length) {
    out.push(`<div class="section">Zabbix problems (${probs.length})</div><div class="card">${probs.map((p) => `<div class="row" style="margin:3px 0;${p.cleared ? 'opacity:.55' : ''}">${sev(p.severity)}<span style="flex:1;font-size:12.5px">${esc(p.name)}</span><span class="note">${p.cleared ? 'cleared' : esc(p.check || p.scope || '')}</span></div>`).join('')}</div>`);
  }
  const acts = (inc.activities || []).filter((a) => a.kind !== 'think');
  if (acts.length) out.push(`<div class="section">Timeline</div>${timeline(state, acts)}`);
  return out.join('');
}

export function renderProposal(state, p) {
  const diff = (p.plan?.diff || []).map((d) => {
    const cls = d.change === 'added' ? 'add' : d.change === 'removed' ? 'del' : 'chg';
    const sign = d.change === 'added' ? '+' : d.change === 'removed' ? '-' : '~';
    return `<span class="${cls}">${sign} ${esc(d.message || d.path)}</span>`;
  }).join('\n');
  const field = p.kind === 'field-repair';
  const verdict = p.result?.verdict;
  const vc = verdict === 'ok' ? 'var(--ok)' : verdict === 'degraded' ? 'var(--warn)' : 'var(--high)';
  return `<div class="card proposal" style="--c:${STATUSC[p.status] || 'var(--line)'}">
    <div class="row"><span class="title">${esc(p.id)} · ${field ? '🚐 field repair' : '⚙️ ' + esc(p.kind)} · ${esc(p.host)}</span>${pill(p.status)}</div>
    <div class="meta"><span>by ${esc(p.proposed_by)} (${esc(p.brain)})</span><span>${age(p.created)}</span>${p.incident_id ? `<span>${esc(p.incident_id)}</span>` : '<span>from chat</span>'}</div>
    <div style="margin-top:6px;font-size:13px">${esc(p.reason)}</div>
    ${field ? `<div class="diff">${esc(fieldText(p))}</div>` : diff ? `<div class="diff">${diff}</div>` : ''}
    ${p.plan?.affected_services?.length ? `<div class="meta"><span>touches ${p.plan.affected_services.map((s) => svcChip(state, s)).join(' ')}</span><span>hosts ${esc(p.plan.affected_hosts.join(', '))}</span></div>` : ''}
    ${p.plan?.drift_after_predicted?.length ? `<div class="meta"><span>drift after (predicted): ${esc(p.plan.drift_after_predicted.join('; '))}</span></div>` : p.plan ? '<div class="meta"><span>drift after (predicted): none</span></div>' : ''}
    ${p.decided_by ? `<div class="meta"><span>${esc(p.status === 'rejected' ? 'rejected' : 'approved')} by ${esc(p.decided_by)}${p.comment ? ': ' + esc(p.comment) : ''}</span></div>` : p.comment ? `<div class="meta"><span>${esc(p.comment)}</span></div>` : ''}
    ${verdict ? `<div class="meta"><span class="verdict" style="--c:${vc}">verdict ${esc(verdict)}</span>${p.result.elapsed_s ? `<span>${esc(p.result.elapsed_s)} s</span>` : ''}${p.result.drift_after?.length ? `<span>drift after: ${esc(p.result.drift_after.join('; '))}</span>` : ''}</div>` : ''}
    ${p.result?.done ? `<div class="meta"><span class="verdict" style="--c:var(--ok)">${esc(p.result.done)}</span></div>` : ''}
    ${p.error ? `<div class="meta"><span style="color:var(--high)">${esc(p.error)}</span></div>` : ''}
    ${p.status === 'pending' ? `<div class="actions"><button class="btn primary small" data-approve="${esc(p.id)}">Approve</button><button class="btn danger small" data-reject="${esc(p.id)}">Reject</button><span class="note">plan ${esc(p.plan?.plan_id || '-')}</span></div>` : ''}
  </div>`;
}

function fieldText(p) {
  const k = p.params?.kind;
  if (k === 'device') return `power on ${p.host} (technician on site)`;
  if (k === 'uplink') return `repair the core uplink of ${p.host}`;
  if (k === 'cut') return `repair the core path ${p.host} <-> ${p.params?.peer}`;
  if (k === 'ac') return `repair attachment circuit ${p.params?.interface} on ${p.host}`;
  return JSON.stringify(p.params);
}

// ------------------------------------------------------------------------------------------------ approvals
export function renderApprovals(state) {
  const all = [...state.proposals.values()].sort((a, b) => b.created - a.created);
  const pending = all.filter((p) => p.status === 'pending');
  const out = [];
  if (!pending.length) out.push('<div class="empty"><span class="big">✅</span>Nothing waits for your approval.</div>');
  else {
    const byInc = new Map();
    for (const p of pending) { const k = p.incident_id || 'chat'; if (!byInc.has(k)) byInc.set(k, []); byInc.get(k).push(p); }
    for (const [inc, list] of byInc) {
      const i = state.incidents.get(inc);
      out.push(`<div class="section">${esc(inc)}${i ? ' · ' + esc(i.title) : ''}</div>`);
      if (list.length > 1 && inc !== 'chat') out.push(`<button class="btn primary small" data-approve-all="${esc(inc)}" style="margin-bottom:8px">Approve all ${list.length} (make before break)</button>`);
      for (const p of list) out.push(renderProposal(state, p));
    }
  }
  const decided = all.filter((p) => p.status !== 'pending').slice(0, 12);
  if (decided.length) {
    out.push('<div class="section">Recent</div>');
    for (const p of decided) {
      const v = p.result?.verdict || p.result?.done || p.error || '';
      out.push(`<div class="card"><div class="row"><span class="title">${esc(p.id)} · ${esc(p.kind)} · ${esc(p.host)}</span>${pill(p.status)}</div><div class="meta"><span>${esc(p.decided_by || '-')}</span><span>${age(p.updated)}</span><span>${esc(v)}</span></div></div>`);
    }
  }
  return out.join('');
}

// ------------------------------------------------------------------------------------------------ activity
export function timeline(state, acts) {
  const colors = Object.fromEntries((state.crew || []).map((m) => [m.name, m.color]));
  return `<ul class="timeline">${acts.map((a) => `<li class="${a.ok === false ? 'err' : ''}"><span class="t">${clock(a.ts)}</span><span class="b" style="--c:${colors[a.bot] || 'var(--text)'}">${esc(a.bot)}</span><span class="x">${KIND_ICON[a.kind] || '·'} ${esc(a.text)}${a.duration_s ? ` <span class="k">${a.duration_s}s</span>` : ''}</span></li>`).join('')}</ul>`;
}

export function renderActivity(state, filter) {
  const bots = [...new Set(['POLL', ...(state.crew || []).map((m) => m.name), 'operator'])];
  const colors = Object.fromEntries((state.crew || []).map((m) => [m.name, m.color]));
  const chips = `<div class="filters">${['all', ...bots].map((b) => `<button data-filter="${esc(b)}" class="${(filter || 'all') === b ? 'on' : ''}" style="--c:${colors[b] || 'var(--accent)'}">${esc(b)}</button>`).join('')}</div>`;
  const acts = state.activities.filter((a) => a.kind !== 'think' && (!filter || filter === 'all' || a.bot === filter)).slice(-250);
  if (!acts.length) return chips + '<div class="empty">Quiet. Activity shows up here live.</div>';
  return chips + timeline(state, acts);
}

// ------------------------------------------------------------------------------------------------ chat
// The chat is two parts: the thread (re-rendered on every event) and the form (rendered once per visit of the tab,
// so typing and clicks are never interrupted by a live update).
export function renderAskThread(state, chatId) {
  const msgs = state.chat.filter((m) => !chatId || m.chat_id === chatId);
  const colors = Object.fromEntries((state.crew || []).map((m) => [m.name, m.color]));
  const out = [brainNote(state), '<div class="chat">'];
  if (!msgs.length) out.push('<div class="empty"><span class="big">💬</span>Ask the crew about the lab. They look things up with the same tools they use on incidents.</div>');
  for (const m of msgs) {
    if (m.role === 'user') out.push(`<div class="msg user"><span class="who">you</span>${esc(m.text)}</div>`);
    else out.push(`<div class="msg bot"><span class="who" style="--c:${colors[m.bot] || 'var(--accent)'}">${esc(m.bot)}</span>${esc(m.text)}${(m.proposals || []).length ? `<div class="note" style="margin-top:6px">proposed ${m.proposals.map(esc).join(', ')}: see Approvals</div>` : ''}</div>`);
  }
  const last = msgs[msgs.length - 1];
  if (last && last.role === 'user') {
    const live = state.activities.filter((a) => a.chat_id === last.chat_id && a.ts >= last.ts && (a.kind === 'tool' || a.kind === 'say')).slice(-3);
    out.push(`<div class="msg bot live">${live.length ? live.map((a) => `${esc(a.bot)}: ${esc(a.text)}`).join('<br>') : 'a bot is on its way to the operator desk…'}</div>`);
  }
  out.push('</div>');
  return out.join('');
}

export function renderAskForm() {
  return `<form class="ask-form" id="ask-form">
    <div class="examples">
      <button type="button" data-example="What happens if hub-c goes down for maintenance? Rank by SLA tier.">hub-c maintenance?</button>
      <button type="button" data-example="Is there any drift between NetBox and the devices right now?">any drift?</button>
      <button type="button" data-example="Which gold services have no working backup path right now?">gold at risk?</button>
      <button type="button" data-example="Give me a one-paragraph status of the network.">status</button>
    </div>
    <textarea id="ask-text" placeholder="e.g. what if hub-a fails? · which customers are unprotected? · why is SVC-0002 degraded?" maxlength="4000"></textarea>
    <div class="row"><span class="note">Ctrl+Enter sends</span><span><button type="button" class="btn small" data-newchat>New conversation</button> <button type="submit" class="btn primary">Ask</button></span></div>
  </form>`;
}

// ------------------------------------------------------------------------------------------------ the wall screen, zoomed
export function renderWall(state) {
  const lab = state.lab || {};
  const services = lab.services || [];
  const problems = [...(lab.problems || [])].sort((a, b) => b.clock - a.clock);
  const bad = services.filter((s) => s.zabbix && s.zabbix !== 'OK');
  const byTier = {};
  for (const s of bad) { const t = s.tier || '?'; byTier[t] = byTier[t] || { down: 0, degraded: 0 }; byTier[t][s.zabbix === 'Disaster' ? 'down' : 'degraded']++; }
  const incidents = [...state.incidents.values()];
  const open = incidents.filter((i) => i.status !== 'resolved');
  const tiles = (region) => services.filter((s) => (region === 'South') === (s.region === 'South')).map((s) => `
    <div class="svc-tile ${esc(s.zabbix || 'OK')}" style="--t:${TIERC[s.tier] || '#555'}">
      <div class="row"><span class="id">${esc(s.service)}</span><span style="margin-left:auto">${esc(s.zabbix || '?')}</span></div>
      <div class="sub">${esc(s.customer)} · ${esc(s.tier)} · ${esc(s.spoke)}${s.rules === 'unprotected' ? ' · <b style="color:var(--avg)">unprotected</b>' : ''}</div>
      <div class="pws">${(s.pws || []).map((p) => `${p.up ? '🟢' : '🔴'} ${esc(p.role)} → ${esc(p.hub)}`).join('<br>')}</div>
    </div>`).join('');
  return `
    <div class="kpis">
      <div class="kpi"><div class="v" style="color:${bad.length ? 'var(--avg)' : 'var(--ok)'}">${services.length - bad.length}/${services.length}</div><div class="l">services OK (Zabbix BSM)</div></div>
      <div class="kpi"><div class="v" style="color:${problems.length ? 'var(--high)' : 'var(--ok)'}">${problems.length}</div><div class="l">open problems</div></div>
      <div class="kpi"><div class="v">${open.length}</div><div class="l">open incidents</div></div>
      <div class="kpi"><div class="v">$${(state.usage?.usd_day || 0).toFixed(3)}</div><div class="l">Grok spend today</div></div>
    </div>
    <div class="wall">
      <div class="box"><h3>L2VPN services · North</h3><div class="svc-grid">${tiles('North')}</div>
        <h3 style="margin-top:12px">L2VPN services · South</h3><div class="svc-grid">${tiles('South')}</div>
        ${Object.keys(byTier).length ? `<h3 style="margin-top:12px">Impact by SLA tier</h3><table class="tier-table"><tr><th>tier</th><th>down</th><th>degraded</th></tr>${Object.entries(byTier).map(([t, v]) => `<tr><td>${esc(t)}</td><td>${v.down}</td><td>${v.degraded}</td></tr>`).join('')}</table>` : ''}
      </div>
      <div class="box"><h3>Topology</h3><div class="topo">${topoSvg(lab)}</div>
        <h3 style="margin-top:12px">Open problems</h3>
        ${problems.length ? `<table class="ptable"><tr><th>sev</th><th>problem</th><th>age</th></tr>${problems.slice(0, 30).map((p) => `<tr><td>${sev(p.severity)}</td><td>${esc(p.name)}</td><td>${age(p.clock)}</td></tr>`).join('')}</table>` : '<div class="note">none: every PW end matches NetBox and is up</div>'}
      </div>
    </div>`;
}

function topoSvg(lab) {
  const devices = lab.devices || {};
  const pos = {
    'hub-a': [90, 40], 'hub-b': [210, 40], 'hub-c': [400, 40], 'hub-d': [500, 40],
    'spoke-1': [40, 160], 'spoke-2': [113, 160], 'spoke-3': [186, 160], 'spoke-4': [260, 160], 'spoke-5': [400, 160], 'spoke-6': [500, 160],
  };
  const pairs = {};
  for (const s of lab.services || []) for (const pw of s.pws || []) {
    const k = `${s.spoke}|${pw.hub}`;
    pairs[k] = pairs[k] || { up: true, n: 0 };
    pairs[k].n++;
    if (!pw.up) pairs[k].up = false;
  }
  const color = (h) => {
    const d = devices[h];
    if (!d) return '#56627c';
    if (d.powered_off) return '#3a4152';
    if (d.uplink !== 'up' || (d.faults && Object.keys(d.faults).length)) return '#ff4d5e';
    if (d.pws && d.pws_up < d.pws) return '#ffb648';
    return '#39d98a';
  };
  const lines = Object.entries(pairs).map(([k, v]) => {
    const [s, h] = k.split('|');
    if (!pos[s] || !pos[h]) return '';
    return `<line x1="${pos[s][0]}" y1="${pos[s][1] - 12}" x2="${pos[h][0]}" y2="${pos[h][1] + 12}" stroke="${v.up ? '#2fbf71' : '#ff4d5e'}" stroke-width="${1 + v.n * 0.7}" ${v.up ? '' : 'stroke-dasharray="6 4"'}/>`;
  }).join('');
  const nodes = Object.entries(pos).map(([h, [x, y]]) => {
    const d = devices[h] || {};
    const hub = h.startsWith('hub');
    const c = color(h);
    return `<g><rect x="${x - (hub ? 26 : 22)}" y="${y - 12}" width="${hub ? 52 : 44}" height="24" rx="6" fill="#0c1424" stroke="${c}" stroke-width="2"/>
      <text x="${x}" y="${y + 4}" fill="#dfe5f2" font-size="12" text-anchor="middle" font-family="ui-monospace,monospace">${h}</text>
      ${d.powered_off ? `<text x="${x}" y="${y + 28}" fill="#ff6b5e" font-size="10" text-anchor="middle">powered off</text>` : d.pws ? `<text x="${x}" y="${y + (hub ? -18 : 28)}" fill="#8b97b1" font-size="10" text-anchor="middle">${d.pws_up}/${d.pws} PWs</text>` : ''}</g>`;
  }).join('');
  return `<svg viewBox="0 0 560 200" role="img" aria-label="topology"><text x="150" y="196" fill="#56709f" font-size="11" text-anchor="middle">NORTH</text><text x="450" y="196" fill="#56709f" font-size="11" text-anchor="middle">SOUTH</text><line x1="330" y1="10" x2="330" y2="190" stroke="#1d2a44" stroke-dasharray="3 5"/>${lines}${nodes}</svg>`;
}
