// NOC page: live state from grok-bot's event stream, the room, the side panel, controls, toasts and sounds.
import { Room } from './room.js';
import { esc, age, renderIncidents, renderIncidentDetail, renderApprovals, renderActivity, renderAskThread, renderAskForm, renderWall } from './panel.js';

const boot = JSON.parse(document.getElementById('boot').textContent || '{}');
const $ = (sel) => document.querySelector(sel);

const S = {
  settings: {}, control: {}, effective: {}, usage: {}, lab: null, crew: [], settling: 0,
  incidents: new Map(), proposals: new Map(), activities: [], chat: [],
  tab: 'incidents', detail: null, detailData: null, filter: 'all', chatId: load('chatId'), connected: false,
};

function load(k, d = null) { try { return localStorage.getItem('noc.' + k) ?? d; } catch { return d; } }
function save(k, v) { try { if (v === null) localStorage.removeItem('noc.' + k); else localStorage.setItem('noc.' + k, v); } catch { /* storage off */ } }

// ------------------------------------------------------------------------------------------------ API
async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Operator-Token': boot.token || '', 'X-Operator': 'noc-page' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

// ------------------------------------------------------------------------------------------------ room
const room = new Room($('#room'), {
  onHover: (h, e) => tooltip(h, e),
  onClick: (h) => {
    if (h.kind === 'screen') openWall();
    else if (h.kind === 'board') switchTab('incidents');
    else if (h.kind === 'operator') switchTab('approvals');
    else if (h.kind === 'bot') { S.filter = h.bot.name; switchTab('activity'); }
    else if (h.kind === 'device') { openWall(); }
    else if (h.kind === 'netbox' && boot.links?.netbox) window.open(boot.links.netbox, '_blank', 'noopener');
  },
});
room.start();

function syncRoom() {
  room.setModel({
    lab: S.lab, crew: S.crew, effective: S.effective, control: S.control, usage: S.usage,
    incidents: [...S.incidents.values()], proposals: [...S.proposals.values()],
  });
}

function tooltip(h, e) {
  const tip = $('#tooltip');
  if (!h || h.kind === 'rack') { tip.hidden = true; return; }
  let html = '';
  if (h.kind === 'bot') {
    const m = h.bot.member || {};
    html = `<h4 style="color:${esc(m.color)}">${esc(m.name)}</h4><div class="muted">${esc(m.title)}</div><div>${esc(m.status)}${m.task ? ' · ' + esc(m.task) : ''}</div>${m.activity ? `<div>${esc(m.activity)}</div>` : ''}`;
  } else if (h.kind === 'device') {
    const d = S.lab?.devices?.[h.host] || {};
    const faults = Object.keys(d.faults || {});
    html = `<h4>${esc(h.host)}</h4><div class="muted">${esc(d.role)} · ${esc(d.region)} · ${esc(d.site)}</div>
      <div>${d.powered_off ? '⛔ powered off' : d.reachable ? '🟢 reachable' : '🔴 unreachable'} · uplink ${esc(d.uplink ?? '?')}</div>
      <div>PWs up ${esc(d.pws_up ?? '?')}/${esc(d.pws ?? '?')}${faults.length ? ' · faults: ' + esc(faults.join(', ')) : ''}</div>`;
  } else if (h.kind === 'screen') html = '<h4>Wall screen</h4><div class="muted">Zabbix L2VPN services, topology, problems. Click to zoom.</div>';
  else if (h.kind === 'board') html = '<h4>Incident board</h4><div class="muted">NEW · WORK · YOU (needs a human) · FIX · DONE</div>';
  else if (h.kind === 'operator') {
    const n = [...S.proposals.values()].filter((p) => p.status === 'pending').length;
    html = `<h4>Operator desk (you)</h4><div>${n ? `⏳ ${n} change ticket(s) wait for your approval` : 'nothing to approve'}</div>`;
  } else if (h.kind === 'netbox') html = '<h4>NetBox</h4><div class="muted">source of truth: intent, topology. Click to open.</div>';
  tip.innerHTML = html;
  tip.hidden = false;
  const wrap = $('.room-wrap').getBoundingClientRect();
  let x = e.clientX - wrap.left + 14, y = e.clientY - wrap.top + 14;
  if (x + 300 > wrap.width) x = Math.max(4, x - 320);
  if (y + 120 > wrap.height) y = Math.max(4, y - 130);
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}

// ------------------------------------------------------------------------------------------------ panel
// render(tabs): the header always; the open tab only when it shows what changed (null = every tab).
let renderQueued = false;
let renderTabs = new Set();
function render(tabs = null) {
  if (tabs === null) renderTabs.add('*');
  else tabs.forEach((t) => renderTabs.add(t));
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    const tabs = renderTabs;
    renderTabs = new Set();
    renderNow(tabs);
  });
}

function renderNow(tabs = new Set(['*'])) {
  renderChips();
  const pending = [...S.proposals.values()].filter((p) => p.status === 'pending').length;
  const badge = $('#badge-approvals');
  badge.hidden = !pending;
  badge.textContent = pending;
  document.querySelectorAll('.tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === S.tab));
  if (!$('#overlay').hidden) $('#overlay-body').innerHTML = renderWall(S);
  if (!tabs.has('*') && !tabs.has(S.tab) && !S._jump) return;
  const body = $('#tab-body');
  const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  const top = body.scrollTop;
  if (S.tab === 'ask') {
    let thread = $('#ask-thread');
    if (!thread || !$('#ask-form')) {
      body.innerHTML = `<div id="ask-thread"></div>${renderAskForm()}`;
      thread = $('#ask-thread');
    }
    thread.innerHTML = renderAskThread(S, S.chatId);
    body.scrollTop = atBottom || S._jump ? body.scrollHeight : top;
    S._jump = false;
    return;
  }
  let html = '';
  if (S.tab === 'incidents') html = S.detail ? renderIncidentDetail(S, S.detailData) : renderIncidents(S);
  else if (S.tab === 'approvals') html = renderApprovals(S);
  else html = renderActivity(S, S.filter);
  body.innerHTML = html;
  if (S.tab === 'activity') body.scrollTop = atBottom || S._jump ? body.scrollHeight : top;
  else body.scrollTop = S._jump ? 0 : top;
  S._jump = false;
}

function switchTab(tab) {
  S.tab = tab;
  if (tab !== 'incidents') S.detail = null;
  S._jump = true;
  render();
}

async function openIncident(id) {
  S.detail = id;
  S.detailData = null;
  S.tab = 'incidents';
  S._jump = true;
  render();
  await refreshDetail();
}

let detailTimer = null;
async function refreshDetail() {
  if (!S.detail) return;
  try {
    S.detailData = await api('GET', `/api/incidents/${encodeURIComponent(S.detail)}`);
  } catch (err) {
    S.detailData = null;
    toast(`could not load ${S.detail}: ${err.message}`, 'var(--high)');
  }
  render();
}
function refreshDetailSoon() {
  clearTimeout(detailTimer);
  detailTimer = setTimeout(refreshDetail, 350);
}

function renderChips() {
  const eff = S.effective || {};
  const lab = S.lab || {};
  const services = lab.services || [];
  const ok = services.filter((s) => s.zabbix === 'OK').length;
  const chips = [];
  const brainColor = eff.brain === 'grok' ? 'var(--ok)' : 'var(--warn)';
  chips.push(`<span class="chip ${eff.brain === 'grok' ? '' : 'warn'}" title="${esc(eff.why || '')}"><i class="led" style="--c:${brainColor}"></i>brain <b>${esc(eff.brain === 'grok' ? eff.model : 'rules')}</b></span>`);
  chips.push(`<span class="chip" title="observe: diagnose only · propose: fixes wait for you · auto: applied by policy">autonomy <b>${esc(eff.autonomy || '?')}</b></span>`);
  const labColor = !lab.ok ? 'var(--high)' : ok === services.length ? 'var(--ok)' : 'var(--avg)';
  chips.push(`<span class="chip"><i class="led" style="--c:${labColor}"></i>${lab.ok ? `services <b>${ok}/${services.length}</b> OK` : 'lab-api unreachable'}</span>`);
  chips.push(`<span class="chip" title="Grok spend today (UTC) and all time">Grok <b>$${(S.usage.usd_day || 0).toFixed(4)}</b> today</span>`);
  if (eff.paused) chips.push('<span class="chip warn"><i class="led" style="--c:var(--warn)"></i><b>maintenance mode</b></span>');
  if (!S.connected) chips.push('<span class="chip warn"><i class="led" style="--c:var(--high)"></i>reconnecting…</span>');
  $('#chips').innerHTML = chips.join('');
  $('#btn-maint').classList.toggle('on', !!S.control.paused);
  $('#btn-maint').textContent = S.control.paused ? 'Resume' : 'Maintenance';
  $('#sel-brain').value = S.control.brain || 'default';
  $('#sel-autonomy').value = S.control.autonomy || 'default';
}

// ------------------------------------------------------------------------------------------------ events from the page
document.querySelector('.tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-tab]');
  if (b) switchTab(b.dataset.tab);
});

$('#tab-body').addEventListener('click', async (e) => {
  const t = e.target.closest('[data-incident],[data-back],[data-approve],[data-reject],[data-approve-all],[data-reinvestigate],[data-filter],[data-example],[data-newchat]');
  if (!t) return;
  try {
    if (t.dataset.incident) await openIncident(t.dataset.incident);
    else if (t.hasAttribute('data-back')) { S.detail = null; S._jump = true; render(); }
    else if (t.dataset.approve) { t.disabled = true; await api('POST', `/api/proposals/${t.dataset.approve}/approve`, {}); toast(`${t.dataset.approve} approved: applying and verifying…`, 'var(--ok)'); }
    else if (t.dataset.reject) {
      const why = window.prompt(`Reject ${t.dataset.reject}? Optional reason:`, '');
      if (why === null) return;
      await api('POST', `/api/proposals/${t.dataset.reject}/reject`, { comment: why || null });
    } else if (t.dataset.approveAll) { t.disabled = true; const r = await api('POST', `/api/incidents/${t.dataset.approveAll}/approve-all`, {}); toast(`approved ${r.length} change(s)`, 'var(--ok)'); }
    else if (t.dataset.reinvestigate) { await api('POST', `/api/incidents/${t.dataset.reinvestigate}/reinvestigate`); toast('asked for a new investigation'); }
    else if (t.dataset.filter) { S.filter = t.dataset.filter; render(); }
    else if (t.dataset.example) { $('#ask-text').value = t.dataset.example; $('#ask-text').focus(); }
    else if (t.hasAttribute('data-newchat')) { S.chatId = null; save('chatId', null); render(); }
  } catch (err) {
    toast(err.message, 'var(--high)');
    t.disabled = false;
  }
});

$('#tab-body').addEventListener('submit', async (e) => {
  if (e.target.id !== 'ask-form') return;
  e.preventDefault();
  const text = $('#ask-text').value.trim();
  if (!text) return;
  try {
    const msg = await api('POST', '/api/chat', { message: text, chat_id: S.chatId });
    S.chatId = msg.chat_id;
    save('chatId', S.chatId);
    $('#ask-text').value = '';
    render(['ask']);
  } catch (err) { toast(err.message, 'var(--high)'); }
});
$('#tab-body').addEventListener('keydown', (e) => {
  if (e.target.id === 'ask-text' && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('#ask-form').requestSubmit(); }
});

$('#btn-maint').addEventListener('click', async () => {
  try {
    const paused = !S.control.paused;
    const reason = paused ? window.prompt('Maintenance mode: the bots watch but open no incidents. Reason?', 'planned work') : null;
    if (paused && reason === null) return;
    await api('POST', '/api/control', { paused, reason });
  } catch (err) { toast(err.message, 'var(--high)'); }
});
$('#sel-brain').addEventListener('change', async (e) => {
  try { await api('POST', '/api/control', { brain: e.target.value }); } catch (err) { toast(err.message, 'var(--high)'); }
});
$('#sel-autonomy').addEventListener('change', async (e) => {
  if (e.target.value === 'auto' && !window.confirm('auto: the bots apply their fixes without asking you (lab only). Continue?')) { renderChips(); return; }
  try { await api('POST', '/api/control', { autonomy: e.target.value }); } catch (err) { toast(err.message, 'var(--high)'); }
});

// ------------------------------------------------------------------------------------------------ wall screen zoom
function openWall() {
  $('#overlay').hidden = false;
  $('#overlay-body').innerHTML = renderWall(S);
}
$('#overlay-close').addEventListener('click', () => { $('#overlay').hidden = true; });
$('#overlay').addEventListener('click', (e) => { if (e.target.id === 'overlay') $('#overlay').hidden = true; });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') $('#overlay').hidden = true; });

function renderLinks() {
  const l = boot.links || {};
  const links = [
    l.zabbix && `<a href="${esc(l.zabbix)}/zabbix.php?action=dashboard.list" target="_blank" rel="noopener">Zabbix ↗</a>`,
    l.netbox && `<a href="${esc(l.netbox)}" target="_blank" rel="noopener">NetBox ↗</a>`,
    l.lab_api && `<a href="${esc(l.lab_api)}/docs" target="_blank" rel="noopener">lab-api ↗</a>`,
    '<a href="/api/docs" target="_blank" rel="noopener">bot API ↗</a>',
  ].filter(Boolean).join('');
  $('#links').innerHTML = links;
  $('#overlay-links').innerHTML = links;
}
renderLinks();

// ------------------------------------------------------------------------------------------------ toasts and sounds
function toast(text, color = 'var(--accent)') {
  const el = document.createElement('div');
  el.className = 'toast';
  el.style.setProperty('--c', color);
  el.textContent = text;
  $('#toasts').appendChild(el);
  setTimeout(() => el.remove(), 6000);
  while ($('#toasts').children.length > 4) $('#toasts').firstChild.remove();
}

let audio = null;
let soundOn = load('sound') === 'on';
function setSoundButton() { $('#btn-sound').textContent = soundOn ? '🔊' : '🔇'; }
setSoundButton();
$('#btn-sound').addEventListener('click', () => {
  soundOn = !soundOn;
  save('sound', soundOn ? 'on' : 'off');
  setSoundButton();
  if (soundOn) beep([660, 880], 0.08);
});
function beep(freqs, len = 0.12, type = 'square', gain = 0.035) {
  if (!soundOn) return;
  try {
    audio = audio || new AudioContext();
    let t = audio.currentTime;
    for (const f of freqs) {
      const o = audio.createOscillator(), g = audio.createGain();
      o.type = type; o.frequency.value = f;
      g.gain.setValueAtTime(gain, t); g.gain.exponentialRampToValueAtTime(0.0001, t + len);
      o.connect(g).connect(audio.destination);
      o.start(t); o.stop(t + len);
      t += len * 1.1;
    }
  } catch { /* no audio */ }
}

// ------------------------------------------------------------------------------------------------ the stream
function applyHello(st) {
  S.settings = st.settings || {};
  S.control = st.control || {};
  S.effective = st.effective || {};
  S.usage = st.usage || {};
  S.lab = st.lab || null;
  S.crew = st.crew || [];
  const visitor = document.getElementById('visitor-name');
  if (visitor && st.settings?.agent_name) visitor.textContent = st.settings.agent_name;
  S.settling = st.settling || 0;
  S.incidents = new Map((st.incidents || []).map((i) => [i.id, i]));
  S.proposals = new Map((st.proposals || []).map((p) => [p.id, p]));
  S.activities = st.activities || [];
  S.chat = st.chat || [];
  syncRoom();
  render();
}

function onActivity(a) {
  S.activities.push(a);
  if (S.activities.length > 600) S.activities.splice(0, S.activities.length - 600);
  room.activity(a);
  if (a.kind === 'alarm') { toast(`🚨 ${a.text}`, 'var(--high)'); beep([880, 660, 880], 0.11); }
  else if (a.kind === 'await') { toast(`⏳ ${a.text}`, 'var(--avg)'); beep([1320], 0.18, 'sine', 0.05); }
  else if (a.kind === 'resolve') { toast(`🎉 ${a.text}`, 'var(--ok)'); beep([523, 659, 784, 1047], 0.09, 'triangle', 0.05); }
  else if (a.kind === 'error') toast(`⚠️ ${a.bot}: ${a.text}`, 'var(--high)');
  if (S.detail && a.incident_id === S.detail) refreshDetailSoon();
}

let es = null;
function connect() {
  es = new EventSource('/api/stream');
  es.addEventListener('hello', (e) => { S.connected = true; applyHello(JSON.parse(e.data)); });
  const on = (type, tabs, fn) => es.addEventListener(type, (e) => { const ev = JSON.parse(e.data); fn(ev.data); render(tabs); });
  on('lab', [], (d) => { S.lab = d; syncRoom(); });
  on('incident', ['incidents', 'approvals'], (d) => { S.incidents.set(d.id, d); syncRoom(); if (S.detail === d.id) refreshDetailSoon(); });
  on('proposal', ['approvals', 'incidents'], (d) => { S.proposals.set(d.id, d); syncRoom(); if (S.detail && d.incident_id === S.detail) refreshDetailSoon(); });
  on('activity', ['activity', 'ask'], onActivity);
  on('crew', [], (d) => { S.crew = d; syncRoom(); });
  on('control', ['incidents', 'ask'], (d) => { S.control = d; if (d.effective) S.effective = d.effective; syncRoom(); });
  on('usage', [], (d) => { S.usage = d; });
  on('chat', ['ask'], (d) => { S.chat.push(d); if (d.role === 'bot' && d.chat_id === S.chatId && S.tab !== 'ask') toast(`💬 ${d.bot}: ${d.text.slice(0, 120)}`); });
  es.onopen = () => { S.connected = true; render(); };
  es.onerror = () => { S.connected = false; render(); };
}
connect();

// ages in the panel refresh even when nothing happens
setInterval(() => { if (S.tab === 'incidents' && !S.detail) render(['incidents']); }, 15000);
// the effective brain (a key added to .env, the budget) can change without an event
setInterval(async () => {
  try { const h = await api('GET', '/api/health'); S.effective = h.effective; renderChips(); } catch { /* the stream shows it */ }
}, 20000);
window.__noc = { S, room, age };
