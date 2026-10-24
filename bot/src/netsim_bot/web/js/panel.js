// The side panel: incidents (the customers Zabbix alerted, what each role did, the change tickets), approvals,
// live activity per robot, Ask the NOC (the NOC Manager answers), and the zoomed wall screen.

const SEVC = {
  Disaster: 'var(--dis)', High: 'var(--high)', Average: 'var(--avg)', Warning: 'var(--warn)',
  Information: 'var(--info)', 'Not classified': '#97a3b6', OK: 'var(--ok)',
};
const STATUSC = {
  new: '#ffd84d', working: '#5cc8ff', 'awaiting-approval': '#ffa64d', remediating: '#7aa2ff', recovered: '#39d98a',
  closed: '#8b97b1', review: '#f06292', pending: '#ffa64d', approved: '#7aa2ff', applying: '#7aa2ff', applied: '#39d98a',
  failed: '#ff6b5e', rejected: '#8b97b1', returned: '#8b97b1', obsolete: '#8b97b1', sent: '#39d98a', skipped: '#8b97b1',
  queued: '#7aa2ff', open: '#ffa64d', answered: '#39d98a', 'timed-out': '#ff6b5e',
};
const TIERC = { gold: 'var(--gold)', silver: 'var(--silver)', bronze: 'var(--bronze)' };
const STATEC = { down: 'var(--dis)', degraded: 'var(--warn)', ok: 'var(--ok)' };
const KIND_ICON = {
  alert: '📟', alarm: '🚨', page: '☎️', take: '🙋', tool: '🔧', result: '↳', propose: '📝', triage: '🩺', report: '📋',
  escalate: '⤴️', ack: '✔️', priority: '🏷️', recommend: '🗳️', review: '🗳️', return: '↩️', update: '✉️', answer: '💬',
  question: '❓', approve: '✅', reject: '⛔', apply: '⚙️', verify: '🔍', field: '🚐', close: '🏁', say: '💬', error: '⚠️',
  control: '🎛️',
};
const NOBOT = { zabbix: '#e8283c', executor: '#7aa2ff', field: '#ff8a3d', operator: '#b18cff', 'grok-bot': '#97a3b6' };

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
function colors(state) {
  return { ...NOBOT, ...Object.fromEntries((state.roles || []).map((m) => [m.name, m.color])) };
}

export function custChip(name, c) {
  return `<span class="cust" style="--t:${TIERC[c?.tier] || '#555'};--s:${STATEC[c?.state] || '#555'}" title="${esc(c?.tier || '')} · ${esc(c?.state || '')}">${esc(name)} <i>${esc(c?.state || '')}</i></span>`;
}

function svcChip(state, service) {
  const s = (state.lab?.services || []).find((x) => x.service === service);
  return `<span class="svc" style="--t:${TIERC[s?.tier] || '#555'}" title="${esc(s?.tier || '')} ${esc(s?.zabbix || '')}">${esc(service)}</span>`;
}

// ------------------------------------------------------------------------------------------------ incidents
export function renderIncidents(state) {
  const incidents = [...state.incidents.values()].sort((a, b) => {
    const act = (i) => (i.status === 'closed' ? 1 : 0);
    return act(a) - act(b) || b.created - a.created;
  });
  const parts = [setupNote(state), batchNote(state)];
  if (!incidents.length) {
    parts.push(`<div class="empty"><span class="big">🟢</span>No incidents yet. Zabbix pages the office when a customer is degraded or down.<br>Try <code>uv run labctl scenario apply hub-down</code>.</div>`);
    return parts.join('');
  }
  for (const i of incidents.slice(0, 60)) {
    const customers = Object.entries(i.customers || {}).map(([n, c]) => custChip(n, c)).join(' ');
    const tickets = [...state.proposals.values()].filter((p) => p.incident_id === i.id);
    const pending = tickets.filter((p) => p.status === 'pending').length;
    const review = tickets.filter((p) => p.status === 'review').length;
    parts.push(`<div class="card click" data-incident="${esc(i.id)}">
      <div class="row">${sev(i.severity)}${i.priority ? `<span class="prio p${esc(i.priority.slice(1))}">${esc(i.priority)}</span>` : ''}<span class="title">${esc(i.id)} · ${esc(i.title)}</span>${pill(i.status)}</div>
      <div class="meta"><span>${customers}</span></div>
      <div class="meta"><span>${esc(i.assignee || 'not taken')}</span>
        ${review ? `<span style="color:${STATUSC.review}">🗳️ ${review} in NOCMGR's review</span>` : ''}
        ${pending ? `<span style="color:var(--avg)">⏳ ${pending} to approve</span>` : ''}
        <span>${(i.pages || []).length} page(s)</span>
        <span>${i.status === 'closed' ? 'closed after ' + dur((i.closed || 0) - i.created) : 'opened ' + age(i.created)}</span></div>
    </div>`);
  }
  return parts.join('');
}

function setupNote(state) {
  const roles = state.settings?.roles || {};
  const missing = Object.values(roles).filter((r) => !r.routine).map((r) => r.bot);
  const eff = state.effective || {};
  const notes = [];
  if (!eff.hook) notes.push('The alert intake is off: put <code>BOT_ZABBIX_HOOK_TOKEN</code> in <code>.env</code> and run <code>make zabbix-bootstrap</code>.');
  if (missing.length === 3 && !eff.sink) notes.push('No routine is set yet (<code>BOT_ROUTINE_NOCENG_URL</code>, ...): incidents open, but nobody is paged. See <code>bot/grokbot-app/README.md</code>, or try it with <code>make bot-sink-up</code>.');
  else if (missing.length && !eff.sink) notes.push(`Not paged: ${esc(missing.join(', '))} (no routine URL in <code>.env</code>).`);
  for (const w of eff.warnings || []) notes.push(esc(w));
  return notes.map((n) => `<div class="warnbox">${n}</div>`).join('');
}

function batchNote(state) {
  return (state.batches || []).map((b) => `<div class="note">⏱ ${b.alerts} alert(s) for the ${esc(b.role)} batch up; it closes in ${Math.max(0, Math.round(b.closes_at - Date.now() / 1000))} s</div>`).join('');
}

export function renderIncidentDetail(state, inc) {
  if (!inc) return '<div class="empty">Loading…</div>';
  const col = colors(state);
  const out = [`<button class="back" data-back>← all incidents</button>`];
  const counts = inc.counts || {};
  out.push(`<div class="card"><div class="row">${sev(inc.severity)}${inc.priority ? `<span class="prio p${esc(inc.priority.slice(1))}">${esc(inc.priority)}</span>` : ''}<span class="title">${esc(inc.id)} · ${esc(inc.title)}</span>${pill(inc.status)}</div>
    <div class="meta"><span>opened ${clock(inc.created)}</span>${inc.closed ? `<span>closed ${clock(inc.closed)} (${dur(inc.closed - inc.created)})</span>` : inc.recovered ? `<span>recovered ${clock(inc.recovered)}</span>` : ''}
    <span>owner ${esc(inc.assignee || '-')}</span><span>${counts.down || 0} down · ${counts.degraded || 0} degraded · ${counts.ok || 0} OK</span></div>
    ${inc.priority_reason ? `<div class="meta"><span>priority: ${esc(inc.priority_reason)}</span></div>` : ''}
  </div>`);
  // the customers Zabbix alerted, by tier
  const order = { gold: 0, silver: 1, bronze: 2 };
  const custs = Object.entries(inc.customers || {}).sort((a, b) => (order[a[1].tier] ?? 9) - (order[b[1].tier] ?? 9) || a[0].localeCompare(b[0]));
  out.push(`<div class="section">Customers (Zabbix BSM)</div><table class="tier-table"><tr><th>customer</th><th>tier</th><th>state</th><th>services</th><th>event</th></tr>${custs.map(([n, c]) => `<tr><td>${esc(n)}</td><td><span class="svc" style="--t:${TIERC[c.tier] || '#555'}">${esc(c.tier || '?')}</span></td><td style="color:${STATEC[c.state] || 'inherit'}">${esc(c.state)}${c.worst && c.worst !== c.state ? ` <span class="note">(was ${esc(c.worst)})</span>` : ''}</td><td>${(c.services || []).map((s) => svcChip(state, s)).join(' ')}</td><td class="note">${esc(c.event_id || '')}</td></tr>`).join('')}</table>`);
  if (inc.triage) out.push(work('Triage', inc.triage.by, col, `${esc(inc.triage.summary)}${inc.triage.suspected_cause ? `<dl class="kv" style="margin-top:6px"><dt>suspected</dt><dd>${esc(inc.triage.suspected_cause)}</dd>${inc.triage.customer_impact ? `<dt>impact</dt><dd>${esc(inc.triage.customer_impact)}</dd>` : ''}${inc.triage.next_step ? `<dt>next</dt><dd>${esc(inc.triage.next_step)}</dd>` : ''}</dl>` : ''}`));
  if (inc.root_cause) {
    const r = inc.root_cause;
    const tiers = r.impact_by_tier && Object.keys(r.impact_by_tier).length ? `<table class="tier-table"><tr><th>tier</th><th>down</th><th>degraded</th></tr>${Object.entries(r.impact_by_tier).map(([t, v]) => `<tr><td>${esc(t)}</td><td>${esc(v?.down ?? 0)}</td><td>${esc(v?.degraded ?? 0)}</td></tr>`).join('')}</table>` : '';
    out.push(work(`Root cause · ${esc(r.category || '')}${r.confidence ? ' · confidence ' + esc(r.confidence) : ''}`, r.by, col, `<div>${esc(r.summary)}</div><dl class="kv" style="margin-top:6px"><dt>root cause</dt><dd>${esc(r.root_cause)}</dd>${r.recommendation ? `<dt>recommendation</dt><dd>${esc(r.recommendation)}</dd>` : ''}</dl>${tiers}${(r.evidence || []).length ? `<ul class="ev">${r.evidence.map((e) => `<li>${esc(e)}</li>`).join('')}</ul>` : ''}`));
  }
  for (const e of inc.escalations || []) out.push(work(`Escalated to the ${esc(roleTitle(state, e.to))}`, e.from, col, esc(e.note)));
  for (const a of inc.acks || []) out.push(work('Acknowledged in Zabbix', a.by, col, `${(a.acknowledged || []).length + (a.commented || []).length} trigger problem(s) as <b>${esc(a.zabbix_user || '?')}</b>: ${esc(a.message)}`));
  for (const u of inc.customer_updates || []) out.push(work(`Customer update${u.customer ? ' · ' + esc(u.customer) : ''}`, u.by, col, esc(u.text)));
  if (inc.closing) out.push(work('Closed', inc.closing.by, col, esc(inc.closing.summary)));
  const proposals = inc.proposals || [];
  if (proposals.length) {
    out.push(`<div class="section">Change tickets</div>`);
    out.push(approveIncident(inc.id, proposals));
    for (const p of proposals) out.push(renderProposal(state, p));
  }
  if (inc.notes?.length) out.push(`<div class="section">Notes</div><ul class="ev">${inc.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`);
  if ((inc.page_log || []).length) {
    out.push(`<div class="section">Pages</div><ul class="timeline">${inc.page_log.map((p) => `<li><span class="t">${clock(p.created)}</span><span class="b" style="--c:${col[roleBot(state, p.role)] || 'var(--text)'}">${esc(roleBot(state, p.role))}</span><span class="x">${esc(p.event)} ${pill(p.status)}${p.sink ? ' <span class="note">test sink</span>' : ''}${p.why ? ` <span class="note">${esc(p.why)}</span>` : ''}</span></li>`).join('')}</ul>`);
  }
  const probs = inc.problems || [];
  if (probs.length) out.push(`<div class="section">The trigger problems behind it (${probs.length})</div><div class="card">${probs.map((p) => `<div class="row" style="margin:3px 0">${sev(p.severity)}<span style="flex:1;font-size:12.5px">${esc(p.name)}</span><span class="note">${esc(p.customer || '')}</span></div>`).join('')}</div>`);
  const acts = (inc.activities || []).filter((a) => a.kind !== 'result');
  if (acts.length) out.push(`<div class="section">Timeline</div>${timeline(state, acts)}`);
  return out.join('');
}

function roleTitle(state, key) { return state.settings?.roles?.[key]?.title || key; }
function roleBot(state, key) { return state.settings?.roles?.[key]?.bot || key; }

function work(title, by, col, body) {
  return `<div class="card work" style="--c:${col[by] || 'var(--line)'}"><div class="row"><span class="title">${title}</span><span class="who" style="color:${col[by] || 'var(--muted)'}">${esc(by || '')}</span></div><div style="margin-top:4px;font-size:13px">${body}</div></div>`;
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
  const rev = (p.reviews || []).slice(-1)[0];
  const stages = field ? ['dispatched', 'on-site', 'repaired'].map((s) => {
    const done = (p.progress || []).find((x) => x.stage === s);
    return `<span class="fstage ${done ? 'done' : ''} ${p.stage === s ? 'now' : ''}">${s === 'on-site' ? 'on site' : s}${done ? ' ' + clock(done.ts) : ''}</span>`;
  }).join('<span class="arrow">→</span>') : '';
  return `<div class="card proposal" style="--c:${STATUSC[p.status] || 'var(--line)'}">
    <div class="row"><span class="title">${esc(p.id)} · ${field ? '🚐 field repair' : '⚙️ ' + esc(p.kind)} · ${esc(p.host)}</span>${pill(p.status)}</div>
    <div class="meta"><span>by ${esc(p.proposed_by)}</span><span>${age(p.created)}</span><span>${esc(p.incident_id || '')}</span></div>
    <div style="margin-top:6px;font-size:13px">${esc(p.reason)}</div>
    ${field ? `<div class="diff">${esc(fieldText(p))}</div><div class="fstages">${stages}</div>` : diff ? `<div class="diff">${diff}</div>` : ''}
    ${p.plan?.affected_services?.length ? `<div class="meta"><span>touches ${p.plan.affected_services.map((s) => svcChip(state, s)).join(' ')}</span><span>hosts ${esc((p.plan.affected_hosts || []).join(', '))}</span></div>` : ''}
    ${p.plan?.drift_after_predicted?.length ? `<div class="meta"><span>drift after (predicted): ${esc(p.plan.drift_after_predicted.join('; '))}</span></div>` : p.plan ? '<div class="meta"><span>drift after (predicted): none</span></div>' : ''}
    ${p.status === 'review' ? `<div class="rec">🗳️ In NOCMGR's review: it comes to you once proposed, or goes back to ${esc(p.proposed_by)}</div>` : rev ? reviewLine(rev, p) : ''}
    ${p.decided_by ? `<div class="meta"><span>${esc(p.status === 'rejected' ? 'rejected' : 'approved')} by ${esc(p.decided_by)}${p.comment ? ': ' + esc(p.comment) : ''}</span></div>` : p.comment ? `<div class="meta"><span>${esc(p.comment)}</span></div>` : ''}
    ${verdict ? `<div class="meta"><span class="verdict" style="--c:${vc}">verdict ${esc(verdict)}</span>${p.result.elapsed_s ? `<span>${esc(p.result.elapsed_s)} s</span>` : ''}${p.result.drift_after?.length ? `<span>drift after: ${esc(p.result.drift_after.join('; '))}</span>` : ''}</div>` : ''}
    ${p.result?.done ? `<div class="meta"><span class="verdict" style="--c:var(--ok)">${esc(p.result.done)}</span></div>` : ''}
    ${p.error ? `<div class="meta"><span style="color:var(--high)">${esc(p.error)}</span></div>` : ''}
    ${p.status === 'pending' ? `<div class="actions"><button class="btn primary small" data-approve="${esc(p.id)}">Approve</button><button class="btn danger small" data-reject="${esc(p.id)}">Reject</button><span class="note">plan ${esc(p.plan?.plan_id || '-')}</span></div>` : ''}
  </div>`;
}

function reviewLine(rev, p) {
  const late = rev.late ? ` <span class="late">arrived after the ticket was ${esc(rev.late)}</span>` : '';
  if (rev.recommendation) { // a ticket from before the review step: the NOC Manager's advice
    return `<div class="rec ${esc(rev.recommendation)}">🗳️ ${esc(rev.by)} recommended to <b>${esc(rev.recommendation)}</b>: ${esc(rev.reason)}${late}</div>`;
  }
  if (rev.decision === 'skip') return `<div class="rec skip">⏭️ <b>Not reviewed</b> by NOCMGR: ${esc(rev.reason)}</div>`;
  if (rev.decision === 'return') return `<div class="rec return">↩️ ${esc(rev.by)} <b>returned</b> it to ${esc(p.proposed_by)}: ${esc(rev.reason)}${late}</div>`;
  return `<div class="rec propose">🗳️ ${esc(rev.by)} <b>proposes</b> it to you: ${esc(rev.reason)}${late}</div>`;
}

// Approve a whole incident: every ticket of it NOCMGR proposed, in one click, make before break. The button carries
// the tickets and plans on screen, so a ticket proposed since stops it (HTTP 409) instead of slipping in unseen.
function approveIncident(incId, tickets) {
  const pending = tickets.filter((p) => p.status === 'pending');
  const review = tickets.filter((p) => p.status === 'review').length;
  if (!pending.length) return review ? `<div class="note" style="margin-bottom:8px">🗳️ NOCMGR is reviewing ${review} ticket(s): each comes to you once proposed.</div>` : '';
  if (review) return `<div class="note" style="margin-bottom:8px">🗳️ NOCMGR is still reviewing ${review} more ticket(s) of ${esc(incId)}: approve these one by one, or the whole incident once they are in.</div>`;
  const expect = JSON.stringify(Object.fromEntries(pending.map((p) => [p.id, p.plan?.plan_id ?? null])));
  const what = pending.length > 1 ? `${pending.length} tickets, make before break` : esc(pending[0].id);
  return `<button class="btn primary small" data-approve-all="${esc(incId)}" data-expect="${esc(expect)}" style="margin-bottom:8px">✅ Approve ${esc(incId)} (${what})</button>`;
}

function fieldText(p) {
  const k = p.params?.kind;
  if (k === 'device') return `power on ${p.host} (the field technician goes on site)`;
  if (k === 'uplink') return `repair the core uplink of ${p.host}`;
  if (k === 'cut') return `repair the core path ${p.host} <-> ${p.params?.peer}`;
  if (k === 'ac') return `repair attachment circuit ${p.params?.interface} on ${p.host}`;
  return JSON.stringify(p.params);
}

// ------------------------------------------------------------------------------------------------ approvals
export function renderApprovals(state) {
  const all = [...state.proposals.values()].sort((a, b) => b.created - a.created);
  const pending = all.filter((p) => p.status === 'pending');
  const review = all.filter((p) => p.status === 'review');
  const out = [];
  if (!pending.length) out.push('<div class="empty"><span class="big">✅</span>Nothing waits for your approval.</div>');
  else {
    out.push('<div class="note" style="margin-bottom:8px">The engineers file tickets; NOCMGR reviews them and proposes them to you. Only you approve.</div>');
    const byInc = new Map();
    for (const p of pending) { const k = p.incident_id || '-'; if (!byInc.has(k)) byInc.set(k, []); byInc.get(k).push(p); }
    for (const [inc, list] of byInc) {
      const i = state.incidents.get(inc);
      out.push(`<div class="section">${esc(inc)}${i ? ' · ' + esc(i.title) : ''}</div>`);
      if (inc !== '-') out.push(approveIncident(inc, all.filter((p) => p.incident_id === inc)));
      for (const p of list) out.push(renderProposal(state, p));
    }
  }
  if (review.length) {
    out.push('<div class="section">In NOCMGR\'s review</div>');
    for (const p of review) out.push(`<div class="card"><div class="row"><span class="title">${esc(p.id)} · ${esc(p.kind)} · ${esc(p.host)}</span>${pill(p.status)}</div><div class="meta"><span>by ${esc(p.proposed_by)}</span><span>${age(p.review_since || p.created)}</span><span>${esc(p.incident_id || '')}</span></div></div>`);
  }
  const decided = all.filter((p) => p.status !== 'pending' && p.status !== 'review').slice(0, 12);
  if (decided.length) {
    out.push('<div class="section">Recent</div>');
    for (const p of decided) {
      const v = p.result?.verdict || p.result?.done || p.error || (p.stage ? p.stage : '');
      const rev = (p.reviews || []).slice(-1)[0];
      out.push(`<div class="card"><div class="row"><span class="title">${esc(p.id)} · ${esc(p.kind)} · ${esc(p.host)}</span>${pill(p.status)}</div><div class="meta"><span>${esc(p.decided_by || '-')}</span><span>${age(p.updated)}</span><span>${esc(v)}</span></div>${rev ? reviewLine(rev, p) : ''}</div>`);
    }
  }
  return out.join('');
}

// ------------------------------------------------------------------------------------------------ activity
export function timeline(state, acts) {
  const col = colors(state);
  return `<ul class="timeline">${acts.map((a) => `<li class="${a.ok === false ? 'err' : ''}"><span class="t">${clock(a.ts)}</span><span class="b" style="--c:${col[a.bot] || 'var(--text)'}">${esc(a.bot)}</span><span class="x">${KIND_ICON[a.kind] || '·'} ${esc(a.text)}${a.duration_s ? ` <span class="k">${a.duration_s}s</span>` : ''}</span></li>`).join('')}</ul>`;
}

export function renderActivity(state, filter) {
  const who = ['NOCENG', 'NETENG', 'NOCMGR', 'zabbix', 'executor', 'field', 'operator'];
  const col = colors(state);
  const chips = `<div class="filters">${['all', ...who].map((b) => `<button data-filter="${esc(b)}" class="${(filter || 'all') === b ? 'on' : ''}" style="--c:${col[b] || 'var(--accent)'}">${esc(b)}</button>`).join('')}</div>`;
  const acts = state.activities.filter((a) => a.kind !== 'result' && (!filter || filter === 'all' || a.bot === filter)).slice(-250);
  const pages = (state.pages || []).slice(0, 8);
  const pageList = pages.length ? `<div class="section">Last pages</div><ul class="timeline">${pages.map((p) => `<li><span class="t">${clock(p.created)}</span><span class="b" style="--c:${col[roleBot(state, p.role)] || 'var(--text)'}">${esc(roleBot(state, p.role))}</span><span class="x">${esc(p.event)} ${esc(p.incident_id || '')} ${pill(p.status)}${p.why ? ` <span class="note">${esc(p.why)}</span>` : ''}</span></li>`).join('')}</ul><div class="section">Activity</div>` : '';
  if (!acts.length) return chips + pageList + '<div class="empty">Quiet. Activity shows up here live.</div>';
  return chips + pageList + timeline(state, acts);
}

// ------------------------------------------------------------------------------------------------ Ask the NOC
// The chat is two parts: the thread (re-rendered on every event) and the form (rendered once per visit of the tab,
// so typing is never interrupted by a live update).
export function renderAskThread(state, chatId) {
  const msgs = state.chat.filter((m) => !chatId || m.chat_id === chatId);
  const col = colors(state);
  const out = ['<div class="note" style="margin-bottom:8px">Questions go to the NOC Manager: each one pages its Grok Bot routine, which counts as <b>one routine run</b>.</div>', '<div class="chat">'];
  if (!msgs.length) out.push('<div class="empty"><span class="big">💬</span>Ask the NOC Manager about the lab: customers, priorities, what-ifs.</div>');
  for (const m of msgs) {
    if (m.role === 'user') out.push(`<div class="msg user"><span class="who">you${m.status ? ' · ' + esc(m.status) : ''}</span>${esc(m.text)}</div>`);
    else if (m.role === 'system') out.push(`<div class="msg sys">${esc(m.text)}</div>`);
    else out.push(`<div class="msg bot"><span class="who" style="--c:${col[m.bot] || 'var(--accent)'}">${esc(m.bot)}</span>${esc(m.text)}</div>`);
  }
  const open = msgs.filter((m) => m.role === 'user' && m.status === 'open').slice(-1)[0];
  if (open) {
    const live = state.activities.filter((a) => a.bot === 'NOCMGR' && a.ts >= open.ts && (a.kind === 'tool' || a.kind === 'page')).slice(-3);
    out.push(`<div class="msg bot live">${live.length ? live.map((a) => `NOCMGR: ${esc(a.text)}`).join('<br>') : 'paging the NOC Manager…'}</div>`);
  }
  out.push('</div>');
  return out.join('');
}

export function renderAskForm() {
  return `<form class="ask-form" id="ask-form">
    <div class="examples">
      <button type="button" data-example="What happens if hub-c goes down for maintenance? Rank the customers by SLA tier.">hub-c maintenance?</button>
      <button type="button" data-example="Which gold customers have no working backup path right now?">gold at risk?</button>
      <button type="button" data-example="Give me a one-paragraph status of the network and the open incidents.">status</button>
    </div>
    <textarea id="ask-text" placeholder="e.g. which customers would hub-a's maintenance hurt? · what is INC-0007 waiting for?" maxlength="4000"></textarea>
    <div class="row"><span class="note">Ctrl+Enter sends · one routine run per question</span><span><button type="button" class="btn small" data-newchat>New conversation</button> <button type="submit" class="btn primary">Ask the NOC Manager</button></span></div>
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
  const open = [...state.incidents.values()].filter((i) => i.status !== 'closed');
  const tiles = (region) => services.filter((s) => (region === 'South') === (s.region === 'South')).map((s) => `
    <div class="svc-tile ${esc(s.zabbix || 'OK')}" style="--t:${TIERC[s.tier] || '#555'}">
      <div class="row"><span class="id">${esc(s.service)}</span><span style="margin-left:auto">${esc(s.zabbix || '?')}</span></div>
      <div class="sub">${esc(s.customer)} · ${esc(s.tier)} · ${esc(s.spoke)}${s.rules === 'unprotected' ? ' · <b style="color:var(--avg)">unprotected</b>' : ''}</div>
      <div class="pws">${(s.pws || []).map((p) => `${p.up ? '🟢' : '🔴'} ${esc(p.role)} → ${esc(p.hub)}`).join('<br>')}</div>
    </div>`).join('');
  const sentToday = (state.pages || []).filter((p) => p.status === 'sent').length;
  return `
    <div class="kpis">
      <div class="kpi"><div class="v" style="color:${bad.length ? 'var(--avg)' : 'var(--ok)'}">${services.length - bad.length}/${services.length}</div><div class="l">services OK (Zabbix BSM)</div></div>
      <div class="kpi"><div class="v" style="color:${problems.length ? 'var(--high)' : 'var(--ok)'}">${problems.length}</div><div class="l">open problems</div></div>
      <div class="kpi"><div class="v">${open.length}</div><div class="l">open incidents</div></div>
      <div class="kpi"><div class="v">${sentToday}</div><div class="l">recent pages sent</div></div>
    </div>
    <div class="wall">
      <div class="box"><h3>L2VPN services · North</h3><div class="svc-grid">${tiles('North')}</div>
        <h3 style="margin-top:12px">L2VPN services · South</h3><div class="svc-grid">${tiles('South')}</div>
        ${Object.keys(byTier).length ? `<h3 style="margin-top:12px">Impact by SLA tier</h3><table class="tier-table"><tr><th>tier</th><th>down</th><th>degraded</th></tr>${Object.entries(byTier).map(([t, v]) => `<tr><td>${esc(t)}</td><td>${v.down}</td><td>${v.degraded}</td></tr>`).join('')}</table>` : ''}
      </div>
      <div class="box"><h3>Topology</h3><div class="topo">${topoSvg(lab)}</div>
        <h3 style="margin-top:12px">Open problems</h3>
        ${problems.length ? `<table class="ptable"><tr><th>sev</th><th>problem</th><th>ack</th><th>age</th></tr>${problems.slice(0, 30).map((p) => `<tr><td>${sev(p.severity)}</td><td>${esc(p.name)}</td><td>${p.acknowledged ? '✔️' : ''}</td><td>${age(p.clock)}</td></tr>`).join('')}</table>` : '<div class="note">none: every PW end matches NetBox and is up</div>'}
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
