// NOC page: live state from grok-bot's event stream, the room, the side panel, controls, toasts and sounds.
import { Room } from './room.js';
import { Sound } from './sound.js';
import { esc, age, renderIncidents, renderIncidentDetail, renderApprovals, renderActivity, renderAskThread, renderAskForm, renderWall, renderTerminal } from './panel.js';

const boot = JSON.parse(document.getElementById('boot').textContent || '{}');
const $ = (sel) => document.querySelector(sel);

const S = {
  settings: {}, control: {}, effective: {}, lab: null, roles: [], batches: [], pages: [], alerts: [],
  incidents: new Map(), proposals: new Map(), activities: [], chat: [], terminal: [], transcript: new Map(),
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
const room = new Room($('#room'), $('#speech'), {
  onHover: (h, e) => tooltip(h, e),
  onClick: (h) => {
    if (h.kind === 'screen' || h.kind === 'device') openWall();
    else if (h.kind === 'board') switchTab('incidents');
    else if (h.kind === 'operator') switchTab('approvals');
    else if (h.kind === 'bot') { S.filter = h.bot.name; switchTab('activity'); }
    else if (h.kind === 'link') { S.filter = 'all'; switchTab('activity'); }
    else if (h.kind === 'terminal') switchTab('terminal');
    else if (h.kind === 'netbox' && boot.links?.netbox) window.open(boot.links.netbox, '_blank', 'noopener');
  },
});
room.start();

function syncRoom() {
  room.setModel({
    lab: S.lab, roles: S.roles, effective: S.effective, control: S.control, pages: S.pages,
    incidents: [...S.incidents.values()], proposals: [...S.proposals.values()],
  });
}

function tooltip(h, e) {
  const tip = $('#tooltip');
  if (!h || h.kind === 'rack') { tip.hidden = true; return; }
  let html = '';
  if (h.kind === 'bot') {
    const m = h.bot.member || {};
    html = `<h4 style="color:${esc(m.color)}">${esc(m.name)} · ${esc(m.title)}</h4><div class="muted">a Grok Bot app routine · ${m.routine ? 'routine set' : 'no routine set (BOT_ROUTINE_' + esc(m.name) + '_URL)'}</div><div>${esc(m.status)}${m.task ? ' · ' + esc(m.task) : ''}</div>${m.activity ? `<div>${esc(m.activity)}</div>` : ''}`;
  } else if (h.kind === 'device') {
    const d = S.lab?.devices?.[h.host] || {};
    const faults = Object.keys(d.faults || {});
    html = `<h4>${esc(h.host)}</h4><div class="muted">${esc(d.role)} · ${esc(d.region)} · ${esc(d.site)}</div>
      <div>${d.powered_off ? '⛔ powered off' : d.reachable ? '🟢 reachable' : '🔴 unreachable'} · uplink ${esc(d.uplink ?? '?')}</div>
      <div>PWs up ${esc(d.pws_up ?? '?')}/${esc(d.pws ?? '?')}${faults.length ? ' · faults: ' + esc(faults.join(', ')) : ''}</div>`;
  } else if (h.kind === 'screen') html = '<h4>Wall screen</h4><div class="muted">Zabbix L2VPN services, topology, problems, the three roles. Click to zoom.</div>';
  else if (h.kind === 'board') html = '<h4>Incident board</h4><div class="muted">NEW · WORK · YOU (needs your approval) · FIX · OK (recovered) · DONE. Below: field repairs (off-screen).</div>';
  else if (h.kind === 'operator') {
    const n = [...S.proposals.values()].filter((p) => p.status === 'pending').length;
    const r = [...S.proposals.values()].filter((p) => p.status === 'review').length;
    html = `<h4>Operator desk (you)</h4><div>${n ? `⏳ ${n} change ticket(s) NOCMGR proposed wait for your approval` : 'nothing to approve'}</div>${r ? `<div class="muted">🗳️ ${r} more in NOCMGR's review</div>` : ''}`;
  } else if (h.kind === 'netbox') html = '<h4>NetBox</h4><div class="muted">source of truth: intent, topology. Click to open.</div>';
  else if (h.kind === 'link') html = '<h4>Cloud link</h4><div class="muted">/mcp through the tunnel to the Grok Bot app. A light blinks on every tool call of a role.</div>';
  else if (h.kind === 'terminal') {
    const s = S.terminal[S.terminal.length - 1];
    html = `<h4>Controller terminal</h4><div class="muted">the Clixon controller's CLI, read only: what the roles ask it (controller_cli). Click for the transcript.</div>${s ? `<div><code>${esc(s.user || s.role)}@controller&gt; ${esc(s.command)}</code> · ${esc(s.bot)}${s.via === 'terminal' ? ' from its computer' : ''} · ${age(s.ts)}${s.allowed ? '' : ' · <b style="color:var(--high)">refused</b>'}</div>` : ''}`;
  }
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
  else if (S.tab === 'terminal') html = renderTerminal(S);
  else html = renderActivity(S, S.filter);
  body.innerHTML = html;
  if (S.tab === 'activity' || S.tab === 'terminal') body.scrollTop = atBottom || S._jump ? body.scrollHeight : top;
  else body.scrollTop = S._jump ? 0 : top;
  S._jump = false;
}

function switchTab(tab) {
  S.tab = tab;
  if (tab !== 'incidents') S.detail = null;
  S._jump = true;
  render();
  if (tab === 'terminal') loadTranscript();
}

// the controller terminal's transcript with the full output of each session (events carry its first lines only)
let transcriptTimer = null;
async function loadTranscript() {
  try {
    for (const s of await api('GET', '/api/terminal?limit=100')) S.transcript.set(s.seq, s);
    render(['terminal']);
  } catch (err) { toast(`transcript: ${err.message}`, 'var(--high)'); }
}
function loadTranscriptSoon() {
  clearTimeout(transcriptTimer);
  transcriptTimer = setTimeout(loadTranscript, 300);
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
  const routines = Object.values(eff.routines || {}).filter(Boolean).length;
  const chips = [];
  const labColor = !lab.ok ? 'var(--high)' : ok === services.length ? 'var(--ok)' : 'var(--avg)';
  chips.push(`<span class="chip"><i class="led" style="--c:${labColor}"></i>${lab.ok ? `services <b>${ok}/${services.length}</b> OK` : 'lab-api unreachable'}</span>`);
  chips.push(`<span class="chip ${routines < 3 && !eff.sink ? 'warn' : ''}" title="Grok Bot routines with a webhook URL in .env"><i class="led" style="--c:${routines === 3 || eff.sink ? 'var(--ok)' : 'var(--warn)'}"></i>routines <b>${routines}/3</b></span>`);
  chips.push(`<span class="chip" title="observe: the roles report only · propose: they file tickets you approve">autonomy <b>${esc(eff.autonomy || '?')}</b></span>`);
  if (!eff.hook) chips.push('<span class="chip warn"><i class="led" style="--c:var(--high)"></i>alert intake <b>off</b></span>');
  if (eff.paused) chips.push('<span class="chip warn"><i class="led" style="--c:var(--warn)"></i><b>maintenance mode</b></span>');
  if (eff.pages_muted) chips.push('<span class="chip warn"><i class="led" style="--c:var(--warn)"></i><b>pages muted</b></span>');
  if (eff.sink) chips.push('<span class="chip warn" title="pages go to the routine sink, not the routines"><i class="led" style="--c:var(--info)"></i><b>test sink</b></span>');
  if (!S.connected) chips.push('<span class="chip warn"><i class="led" style="--c:var(--high)"></i>reconnecting…</span>');
  $('#chips').innerHTML = chips.join('');
  $('#btn-maint').classList.toggle('on', !!S.control.paused);
  $('#btn-maint').textContent = S.control.paused ? 'Resume' : 'Maintenance';
  $('#btn-mute').classList.toggle('on', !!S.control.pages_muted);
  $('#btn-mute').textContent = S.control.pages_muted ? 'Unmute pages' : 'Mute pages';
  $('#sel-autonomy').value = S.control.autonomy || 'default';
}

// ------------------------------------------------------------------------------------------------ events from the page
document.querySelector('.tabs').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-tab]');
  if (b) switchTab(b.dataset.tab);
});

$('#tab-body').addEventListener('click', async (e) => {
  const t = e.target.closest('[data-incident],[data-back],[data-approve],[data-reject],[data-approve-all],[data-filter],[data-example],[data-newchat]');
  if (!t) return;
  try {
    if (t.dataset.incident) await openIncident(t.dataset.incident);
    else if (t.hasAttribute('data-back')) { S.detail = null; S._jump = true; render(); }
    else if (t.dataset.approve) { t.disabled = true; await api('POST', `/api/proposals/${t.dataset.approve}/approve`, {}); toast(`${t.dataset.approve} approved: applying and verifying…`, 'var(--ok)'); }
    else if (t.dataset.reject) {
      const why = window.prompt(`Reject ${t.dataset.reject}? Optional reason:`, '');
      if (why === null) return;
      await api('POST', `/api/proposals/${t.dataset.reject}/reject`, { comment: why || null });
    } else if (t.dataset.approveAll) {
      t.disabled = true;
      // exactly the tickets on screen: one proposed since makes the server refuse (409), and the panel shows it
      const r = await api('POST', `/api/incidents/${t.dataset.approveAll}/approve-all`, { tickets: JSON.parse(t.dataset.expect || 'null') });
      toast(`${t.dataset.approveAll}: approved ${r.length} change(s), applying and verifying…`, 'var(--ok)');
    }
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
    const reason = paused ? window.prompt('Maintenance mode: alerts are recorded, no incident opens, nobody is paged. Reason?', 'planned work') : null;
    if (paused && reason === null) return;
    await api('POST', '/api/control', { paused, reason });
  } catch (err) { toast(err.message, 'var(--high)'); }
});
$('#btn-mute').addEventListener('click', async () => {
  try { await api('POST', '/api/control', { pages_muted: !S.control.pages_muted }); } catch (err) { toast(err.message, 'var(--high)'); }
});
$('#sel-autonomy').addEventListener('change', async (e) => {
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

// the room's music, the alarm while an incident is open and the recovery jingle (sound.js); on unless turned off
const sound = new Sound({ on: load('sound', 'on') === 'on', music: load('music', 'on') === 'on', onchange: setSoundButtons });
function setSoundButtons() {
  const b = $('#btn-sound'), m = $('#btn-music');
  const st = sound.state;
  b.textContent = st === 'off' ? '🔇' : st === 'waiting' ? '🔈' : '🔊';
  b.classList.toggle('wait', st === 'waiting');
  b.title = st === 'off' ? 'Sound off: click for the music, the alarm while an incident is open and the recovery jingle'
    : st === 'waiting' ? 'Sound on: it starts at your first click or key press in the page (the browser\'s rule). Click here to turn all of it off'
    : 'Sound on: music, the alarm while an incident is open, a jingle when the network recovers. Click to turn all of it off';
  m.classList.toggle('off', !sound.musicOn || st === 'off');
  m.title = st === 'off' ? 'Sound off: click for the sound with the background music'
    : sound.musicOn ? 'Background music on: click to turn it off (the alarm and the jingle stay)' : 'Background music off: click to turn it on';
}
setSoundButtons();
// one click turns all of it off (music, alarm, jingle, beeps), in every tab of the room (the storage event below)
$('#btn-sound').addEventListener('click', () => {
  sound.setOn(!sound.on);
  save('sound', sound.on ? 'on' : 'off');
  setSoundButtons();
});
$('#btn-music').addEventListener('click', () => {
  if (sound.on) sound.setMusic(!sound.musicOn);
  else { // music asked for while the sound is off: the sound comes on with it
    sound.setMusic(true);
    sound.setOn(true);
    save('sound', 'on');
  }
  save('music', sound.musicOn ? 'on' : 'off');
  setSoundButtons();
});
// browsers let a page make sound only after a click or a key press in it (not the sound button's: it turns sound off)
for (const type of ['pointerdown', 'keydown']) {
  window.addEventListener(type, (e) => { if (!e.target.closest?.('#btn-sound')) sound.unlock(); }, { capture: true });
}
// sound or music turned off in another tab of the room: this one goes quiet too (turned on there, this one stays as
// it is, so two tabs do not play the music at once)
window.addEventListener('storage', (e) => {
  if (e.newValue !== 'off') return;
  if (e.key === 'noc.sound' && sound.on) sound.setOn(false);
  else if (e.key === 'noc.music' && sound.musicOn) sound.setMusic(false);
  else return;
  setSoundButtons();
});

// ------------------------------------------------------------------------------------------------ the stream
function applyHello(st) {
  S.settings = st.settings || {};
  S.control = st.control || {};
  S.effective = st.effective || {};
  S.lab = st.lab || null;
  S.roles = st.roles || [];
  S.batches = st.batches || [];
  S.pages = st.pages || [];
  S.alerts = st.alerts || [];
  S.incidents = new Map((st.incidents || []).map((i) => [i.id, i]));
  sound.incidents([...S.incidents.values()]);
  S.proposals = new Map((st.proposals || []).map((p) => [p.id, p]));
  S.activities = st.activities || [];
  S.chat = st.chat || [];
  S.terminal = st.terminal || [];
  room.loadTerminal(S.terminal);
  syncRoom();
  render();
}

function onActivity(a) {
  S.activities.push(a);
  if (S.activities.length > 600) S.activities.splice(0, S.activities.length - 600);
  room.activity(a);
  if (a.kind === 'alarm') toast(`🚨 ${a.text}`, 'var(--high)'); // the alarm sound follows the incident (sound.incidents)
  else if (a.kind === 'page' && a.ok) { toast(`☎️ ${a.bot}: ${a.text}`, 'var(--avg)'); sound.effect('page'); }
  else if (a.kind === 'close') { toast(`🏁 ${a.text}`, 'var(--ok)'); sound.effect('close'); }
  else if (a.kind === 'review' && (a.text.startsWith('proposes') || a.bot === 'grok-bot')) { toast(`🗳️ ${a.bot}: ${a.text}`, 'var(--avg)'); sound.effect('review'); }
  else if (a.kind === 'error') toast(`⚠️ ${a.bot}: ${a.text}`, 'var(--high)');
  if (S.detail && a.incident_id === S.detail) refreshDetailSoon();
}

let es = null;
function connect() {
  es = new EventSource('/api/stream');
  es.addEventListener('hello', (e) => { S.connected = true; applyHello(JSON.parse(e.data)); });
  const on = (type, tabs, fn) => es.addEventListener(type, (e) => { const ev = JSON.parse(e.data); fn(ev.data); render(tabs); });
  on('lab', [], (d) => { S.lab = d; syncRoom(); });
  on('incident', ['incidents', 'approvals'], (d) => {
    S.incidents.set(d.id, d);
    sound.incidents([...S.incidents.values()]);
    syncRoom();
    if (S.detail === d.id) refreshDetailSoon();
  });
  on('proposal', ['approvals', 'incidents'], (d) => { S.proposals.set(d.id, d); syncRoom(); if (S.detail && d.incident_id === S.detail) refreshDetailSoon(); });
  on('activity', ['activity', 'ask'], onActivity);
  on('roles', [], (d) => { S.roles = d; syncRoom(); });
  on('terminal', ['terminal'], (d) => {
    S.terminal = [...S.terminal, d].slice(-100);
    room.terminal(d);
    if (S.tab === 'terminal') loadTranscriptSoon();
  });
  on('batches', ['incidents'], (d) => { S.batches = d; });
  on('alert', ['incidents'], (d) => { S.alerts = [d, ...S.alerts].slice(0, 60); });
  on('page', ['activity'], (d) => { const i = S.pages.findIndex((p) => p.id === d.id); if (i >= 0) S.pages[i] = d; else S.pages = [d, ...S.pages].slice(0, 60); syncRoom(); });
  on('control', ['incidents', 'ask'], (d) => { S.control = d; if (d.effective) S.effective = d.effective; syncRoom(); });
  on('chat', ['ask'], (d) => {
    const i = S.chat.findIndex((m) => m.id === d.id);
    if (i >= 0) S.chat[i] = d; else S.chat.push(d);
    if (d.role === 'bot' && d.chat_id === S.chatId && S.tab !== 'ask') toast(`💬 ${d.bot}: ${d.text.slice(0, 120)}`);
  });
  es.onopen = () => { S.connected = true; render(); };
  es.onerror = () => { S.connected = false; render(); };
}
connect();

// ages and batch countdowns refresh even when nothing happens
setInterval(() => { if (S.tab === 'incidents' && !S.detail) render(['incidents']); }, 5000);
setInterval(async () => {
  try { const st = await api('GET', '/api/health'); S.effective = st.effective; renderChips(); } catch { /* the stream shows it */ }
}, 20000);
window.__noc = { S, room, age, sound };
