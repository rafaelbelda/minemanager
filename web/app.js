/* MineManager web UI.
 * REST for actions and declared state (slow poll); per-node WebSockets for run
 * states and console output. See docs/UI_CONTEXT.md.
 */

import { api, ApiError, describe, originLabel, wsOrigin } from './api.js';
import { $, clear, clickable, confirmDialog, copyText, dialog, el, fill, repaint, show, toast } from './dom.js';
import { NodeEvents } from './events.js';

/* --- constants ----------------------------------------------------------- */

const META = {
  running: { label: 'Running', color: '#47d18a', anim: 'none', glow: true },
  stopped: { label: 'Stopped', color: '#6b6b67', anim: 'none' },
  starting: { label: 'Starting', color: '#f0b429', anim: 'mm-pulse 1.2s infinite' },
  stopping: { label: 'Stopping', color: '#f0b429', anim: 'mm-pulse 1.2s infinite' },
  crashed: { label: 'Crashed', color: '#ff5c54', anim: 'mm-pulse 1.6s infinite' },
  updating: { label: 'Updating', color: '#5ac8fa', anim: 'mm-pulse 1.2s infinite' },
  unknown: { label: 'Unknown', color: '#6b6b67', anim: 'none', hollow: true },
};
const metaOf = (s) => META[s] || META.unknown;

const NODE_META = {
  online: { label: 'Online', color: '#47d18a', glow: true },
  offline: { label: 'Offline', color: '#6b6b67' },
  pending: { label: 'Not enrolled yet', color: '#f0b429', hollow: true },
};
const nodeMetaOf = (node) =>
  NODE_META[node.online ? 'online' : node.enrolled ? 'offline' : 'pending'];

/** Status light. The name goes in the tooltip and to screen readers. */
function statusDot(meta, { size = 8, what = '' } = {}) {
  // filled = live, hollow ring = not reporting, glow = up.
  const paint = meta.hollow
    ? `background:transparent;box-shadow:inset 0 0 0 1.5px ${meta.color}`
    : `background:${meta.color}` + (meta.glow ? `;box-shadow:0 0 7px ${meta.color}99` : '');
  return el('span.dot', {
    style: `width:${size}px;height:${size}px;${paint};animation:${meta.anim || 'none'}`,
    title: what ? `${what}: ${meta.label}` : meta.label,
  }, el('span.sr-only', { text: meta.label }));
}

const CONSOLE_CAP = 1500;
// Above this, and for every directory, transfers stream with a progress pill.
const STREAM_THRESHOLD = 4 * 1024 * 1024;
const REFRESH_MS = 10000;
const REFRESH_FAST_MS = 3000;
const HEALTH_MS = 15000;

/* --- state --------------------------------------------------------------- */

const state = {
  nodes: [],
  instances: new Map(),   // nodeId -> InstanceOut[]
  runState: new Map(),    // instanceId -> RunState
  supervised: new Map(),  // instanceId -> is the agent still keeping it up?
  console: new Map(),     // instanceId -> line[]
  consoleLoaded: new Set(), // instanceIds we've backfilled history for
  consoleFailed: new Set(), // instanceIds whose history backfill failed
  // Instances the hub is still building: 202 received, no provision.done yet.
  // Live-only, like runState; a reload loses it.
  provisioning: new Set(),
  sel: { type: 'hub' },
  tab: 'console',
  filter: '',
  enrollment: null,       // { nodeId, name, token, expiresIn, mintedAt }
  files: { path: '.', entries: [], loading: false, error: null },
  editor: { path: null, original: '', size: 0 },
  editorFullscreen: false,
  // File-explorer thresholds from /api/config (overwritten at boot).
  config: { editor_warn_bytes: 2_000_000, editor_max_bytes: 5_000_000, transfer_cap_bytes: 8_388_608 },
  transfers: new Map(),   // tid -> live transfer (for the bottom-right pills)
  // Whoever the reverse proxy authenticated; null when it forwards nobody.
  account: null,
  // Version tab (catalog is provider-driven; software-agnostic).
  version: {
    instId: null, loading: false, error: null,
    software: null, label: '', hasBuilds: false,
    versions: [], builds: [], buildsLoading: false,
    selVersion: null, selBuild: null, updating: false,
  },
  autoscroll: true,
  loaded: false,
};

const events = new NodeEvents(onNodeEvent);

/* --- lookups ------------------------------------------------------------- */

const nodeById = (id) => state.nodes.find((n) => n.id === id) || null;
const instancesOf = (nodeId) => state.instances.get(nodeId) || [];

function instById(instId) {
  for (const list of state.instances.values()) {
    const found = list.find((i) => i.id === instId);
    if (found) return found;
  }
  return null;
}

/** Runtime state of an instance. Unknowable while its node is offline. */
function runStateOf(inst) {
  const node = nodeById(inst.node_id);
  if (!node || !node.online) return 'unknown';
  return state.runState.get(inst.id) || 'unknown';
}

const curNode = () => (state.sel.nodeId ? nodeById(state.sel.nodeId) : null);
const curInst = () => (state.sel.type === 'instance' ? instById(state.sel.instId) : null);

/** A node is reachable when its agent holds a live socket to the hub. */
const isOnline = (inst) => !!nodeById(inst?.node_id)?.online;

/* --- routing ------------------------------------------------------------- */

function selToHash(sel) {
  if (sel.type === 'node') return `#/node/${sel.nodeId}`;
  if (sel.type === 'instance') return `#/inst/${sel.instId}`;
  return '#/';
}

function hashToSel() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  if (parts[0] === 'node' && parts[1]) return { type: 'node', nodeId: parts[1] };
  if (parts[0] === 'inst' && parts[1]) {
    const inst = instById(parts[1]);
    return inst
      ? { type: 'instance', nodeId: inst.node_id, instId: inst.id }
      : { type: 'instance', nodeId: null, instId: parts[1] };
  }
  return { type: 'hub' };
}

function navigate(sel, { push = true } = {}) {
  const changedInstance = sel.instId !== state.sel.instId;
  state.sel = sel;
  if (changedInstance) {
    state.tab = 'console';
    resetInstancePanes();
  }
  if (push) {
    const hash = selToHash(sel);
    if (location.hash !== hash) history.replaceState(null, '', hash);
  }
  render();
  setNav(false);   // a choice was made; the drawer has done its job
  if (sel.type === 'instance' && changedInstance) loadTab();
  if (sel.type === 'node' && sel.nodeId) { loadNodeJava(sel.nodeId); loadJavaCompat(); }
}

const selectHub = () => navigate({ type: 'hub' });
const selectNode = (nodeId) => navigate({ type: 'node', nodeId });
const selectInstance = (nodeId, instId) => navigate({ type: 'instance', nodeId, instId });

/* --- data loading -------------------------------------------------------- */

let refreshTimer = null;

async function refreshNodes({ quiet = false } = {}) {
  let nodes;
  try {
    nodes = await api.listNodes();
  } catch (err) {
    if (!quiet) toast(describe(err), 'error');
    return;
  }
  state.nodes = nodes;

  const results = await Promise.allSettled(nodes.map((n) => api.listInstances(n.id)));
  const seen = new Set();
  results.forEach((res, i) => {
    if (res.status === 'fulfilled') {
      state.instances.set(nodes[i].id, res.value);
      res.value.forEach((inst) => seen.add(inst.id));
    }
  });
  // Drop cached state for instances (or nodes) that no longer exist.
  for (const id of [...state.instances.keys()]) {
    if (!nodes.some((n) => n.id === id)) state.instances.delete(id);
  }
  for (const id of [...state.runState.keys()]) if (!seen.has(id)) state.runState.delete(id);
  for (const id of [...state.console.keys()]) if (!seen.has(id)) state.console.delete(id);
  for (const id of [...state.consoleLoaded]) if (!seen.has(id)) state.consoleLoaded.delete(id);
  for (const id of [...state.consoleFailed]) if (!seen.has(id)) state.consoleFailed.delete(id);

  state.loaded = true;

  // Resolve a deep-linked instance now that instances are known.
  if (state.sel.type === 'instance' && !state.sel.nodeId) {
    const inst = instById(state.sel.instId);
    if (inst) state.sel.nodeId = inst.node_id;
  }

  events.sync(nodes.map((n) => n.id));
  render();
  primeStates(nodes);   // fill run-states from the agent's real view (no heartbeat wait)
  scheduleRefresh();
}

/** Pull real run-states from each online agent instead of waiting for a
 *  heartbeat. Non-blocking: re-renders as answers arrive. */
function primeStates(nodes) {
  for (const node of nodes.filter((n) => n.online)) {
    api.nodeInstanceStates(node.id).then((res) => {
      let changed = false;
      for (const [iid, st] of Object.entries(res?.states || {})) {
        if (state.runState.get(iid) !== st) { state.runState.set(iid, st); changed = true; }
      }
      if (changed) render();
      // Backfill the console of a now-running instance we are viewing.
      if (state.sel.type === 'instance') maybeLoadConsole(state.sel.instId);
    }).catch(() => { /* offline or transient; keep unknown */ });
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  const pending = state.enrollment && !nodeById(state.enrollment.nodeId)?.online;
  refreshTimer = setTimeout(
    () => refreshNodes({ quiet: true }),
    pending ? REFRESH_FAST_MS : REFRESH_MS,
  );
}

/** Name the signed-in user, or stay out of the way where there is no proxy. */
function setAccount(account) {
  state.account = account?.user ? account : null;
  const pill = $('account');
  const label = state.account ? (state.account.name || state.account.user) : '';
  $('account-name').textContent = label;
  pill.title = label ? `Signed in as ${label}` : '';
  show(pill, Boolean(label));
  if (!state.account) closeAccountMenu();
}

let _acctCleanup = null;
function closeAccountMenu() {
  $('acct-menu').classList.remove('open');
  $('account').setAttribute('aria-expanded', 'false');
  if (_acctCleanup) { _acctCleanup(); _acctCleanup = null; }
}

/** The pill's menu: who you are, then the things you can do about it. */
function toggleAccountMenu() {
  const menu = $('acct-menu');
  if (menu.classList.contains('open')) { closeAccountMenu(); return; }
  const acct = state.account;
  if (!acct) return;

  $('acct-id-name').textContent = acct.name || acct.user;
  // The username is a second line only when it is not already the heading.
  $('acct-id-user').textContent = acct.name ? acct.user : '';
  show($('acct-id-user'), Boolean(acct.name));
  const groups = acct.groups || [];
  fill($('acct-groups'), groups.map(
    (g) => el(`span.acct-group${g === 'admin' ? '.admin' : ''}`, {}, g)));
  show($('acct-groups'), groups.length > 0);
  // Only the proxy can end the session it issued, so this is a link to it.
  show($('acct-signout'), Boolean(acct.logout_url));

  const pill = $('account');
  menu.classList.add('open');
  pill.setAttribute('aria-expanded', 'true');
  const anchor = pill.getBoundingClientRect();
  const box = menu.getBoundingClientRect();
  menu.style.top = `${anchor.bottom + 6}px`;
  menu.style.left = `${Math.max(8, Math.min(anchor.right - box.width, innerWidth - box.width - 8))}px`;

  const onDown = (e) => { if (!menu.contains(e.target) && !pill.contains(e.target)) closeAccountMenu(); };
  const onKey = (e) => { if (e.key === 'Escape') closeAccountMenu(); };
  document.addEventListener('mousedown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', closeAccountMenu);
  _acctCleanup = () => {
    document.removeEventListener('mousedown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', closeAccountMenu);
  };
}

/** Hub poll. The page only says anything when the hub cannot be reached. */
async function refreshHealth() {
  try {
    const h = await api.health();
    $('brand-ver').textContent = `v${h.version}`;
    setAccount(h.account);
    show($('hub-down'), false);
  } catch {
    show($('hub-down'), true);
  }
}

/* --- live events --------------------------------------------------------- */

function onNodeEvent(frame, nodeId) {
  switch (frame.action) {
    case 'console.output': {
      const id = frame.instance_id;
      if (!id) break;
      pushConsole(id, frame.data?.line ?? '');
      break;
    }
    case 'state.changed': {
      const id = frame.instance_id;
      if (!id) break;
      const st = frame.data?.state || 'unknown';
      const prev = state.runState.get(id);
      state.runState.set(id, st);
      // Live-only: absent after a reload.
      state.supervised.set(id, frame.data?.supervised !== false);
      // Visually separate each server run in the console.
      if (st === 'starting' && prev !== 'starting') {
        pushConsole(id, '', 'sys');
        pushConsole(id, `session start · ${new Date().toLocaleString()}`, 'sep');
      }
      const detail = frame.data?.detail;
      pushConsole(id, detail ? `${st} · ${detail}` : st, 'sys');
      if (st === 'stopped' || st === 'crashed') pushConsole(id, '', 'sys');  // trailing gap
      if (st === 'running') maybeLoadConsole(id);
      render();
      break;
    }
    case 'provision.done': {
      const id = frame.instance_id;
      state.provisioning.delete(id);
      const { ok, name, error } = frame.data || {};
      if (ok) {
        toast(`${name} created and initialised`, 'ok');
        refreshNodes({ quiet: true });        // picks up version/build and the flag
      } else {
        // Un-declared now, so this is the last word on that id.
        refreshNodes({ quiet: true }).then(() => {
          if (state.sel.instId === id) selectNode(nodeId);
        });
        reportProvisionFailure(name, error);
      }
      render();
      break;
    }
    case 'heartbeat': {
      // Node-level: every instance state at once, and proof of liveness.
      const node = nodeById(nodeId);
      if (node && !node.online) node.online = true;
      const map = frame.data?.instances || {};
      for (const [instId, run] of Object.entries(map)) state.runState.set(instId, run);
      render();
      break;
    }
    default:
      break;
  }
}

/* --- console buffer ------------------------------------------------------ */

const LINE_RE_BRACKET = /^\[(\d{2}:\d{2}:\d{2})\]\s*\[([^\]]*)\]:?\s?(.*)$/s;
const LINE_RE_INLINE = /^\[(\d{2}:\d{2}:\d{2})\s+([A-Za-z]+)\]:?\s?(.*)$/s;

function parseLine(raw, kind) {
  if (kind === 'sep') return { cls: 'sep', time: '', lvl: '', text: raw };
  if (kind === 'sys') return { cls: 'sys', time: '', lvl: '', text: raw };
  let m = raw.match(LINE_RE_BRACKET);
  let time = '', lvl = '', text = raw;
  if (m) {
    time = `[${m[1]}`;
    lvl = `${m[2].split('/').pop()}]:`;
    text = m[3];
  } else if ((m = raw.match(LINE_RE_INLINE))) {
    time = `[${m[1]}`;
    lvl = `${m[2]}]:`;
    text = m[3];
  }
  const upper = lvl.toUpperCase();
  const cls = /ERROR|SEVERE|FATAL/.test(upper) ? 'err' : /WARN/.test(upper) ? 'warn' : '';
  return { cls, time, lvl, text };
}

function pushConsole(instId, raw, kind) {
  let buf = state.console.get(instId);
  if (!buf) state.console.set(instId, (buf = []));
  const line = parseLine(raw, kind);
  buf.push(line);
  if (buf.length > CONSOLE_CAP) buf.splice(0, buf.length - CONSOLE_CAP);

  if (state.sel.type === 'instance' && state.sel.instId === instId && state.tab === 'console') {
    const body = $('term-body');
    body.append(lineNode(line));
    while (body.childElementCount > CONSOLE_CAP) body.firstElementChild.remove();
    body.dataset.sig = consoleSig(instId, buf.length);
    $('term-count').textContent = `${buf.length} line${buf.length === 1 ? '' : 's'}`;
    $('term-empty').hidden = true;
    show($('console-hint'), false);
    if (state.autoscroll) body.scrollTop = body.scrollHeight;
  }
}

/**
 * Backfill the console from the instance's log, once per instance. The log
 * snapshot replaces the buffer and the live stream appends from there.
 * Best-effort: failure shows a soft banner and leaves the live console alone.
 */
async function loadConsoleHistory(inst) {
  if (!inst || state.consoleLoaded.has(inst.id)) return;
  if (!isOnline(inst)) return; // retry when the console is reopened online
  // A stopped instance shows an empty console. Not marked loaded, so it
  // backfills once it starts running.
  if (runStateOf(inst) !== 'running') {
    if (!state.console.has(inst.id)) state.console.set(inst.id, []);
    return;
  }
  try {
    const res = await api.consoleHistory(inst.id, 300);
    if (state.sel.instId !== inst.id) return; // selection moved on while awaiting
    const buf = (res?.lines || []).map((raw) => parseLine(raw));
    state.console.set(inst.id, buf);
    state.consoleLoaded.add(inst.id);
    state.consoleFailed.delete(inst.id);
    $('term-body').dataset.sig = '';   // force a repaint from the new buffer
    if (state.tab === 'console' && state.sel.instId === inst.id) render();
  } catch {
    // Flag it; the live console keeps working.
    if (state.sel.instId === inst.id) {
      state.consoleFailed.add(inst.id);
      if (state.tab === 'console') render();
    }
  }
}

/** Backfill console history for an instance once it's the viewed, running one. */
function maybeLoadConsole(instId) {
  if (!instId || state.sel.type !== 'instance' || state.sel.instId !== instId || state.tab !== 'console') return;
  const inst = instById(instId);
  if (inst && runStateOf(inst) === 'running') loadConsoleHistory(inst);
}

const consoleSig = (instId, lines) => `${instId}:${lines}`;

function lineNode(line) {
  return el(`div.term-line${line.cls ? '.' + line.cls : ''}`, {},
    line.time && el('span.t', { text: line.time }),
    line.time && ' ',
    line.lvl && el('span.l', { text: line.lvl }),
    line.lvl && ' ',
    line.text);
}

/* --- render: chrome ------------------------------------------------------ */

function render() {
  renderSidebar();

  show($('view-hub'), state.sel.type === 'hub');
  show($('view-node'), state.sel.type === 'node');
  show($('view-inst'), state.sel.type === 'instance');

  if (state.sel.type === 'hub') renderHub();
  else if (state.sel.type === 'node') renderNode();
  else renderInstance();
}

function matchesFilter(node) {
  const q = state.filter.trim().toLowerCase();
  if (!q) return { node: true, instances: instancesOf(node.id) };
  const nodeHit = node.name.toLowerCase().includes(q) || (node.hostname || '').toLowerCase().includes(q);
  const insts = instancesOf(node.id).filter((i) => i.name.toLowerCase().includes(q));
  if (nodeHit) return { node: true, instances: instancesOf(node.id) };
  return { node: insts.length > 0, instances: insts };
}

/** Everything the sidebar tree draws from, flattened. */
function sidebarSig() {
  const parts = [state.filter, state.sel.type, state.sel.nodeId || '', state.sel.instId || '',
                 state.loaded ? '1' : '0'];
  for (const node of state.nodes) {
    parts.push(node.id, node.name, node.hostname || '', node.online ? '1' : '0');
    for (const inst of instancesOf(node.id)) parts.push(inst.id, inst.name, runStateOf(inst));
  }
  return parts.join('');
}

function renderSidebar() {
  $('row-hub').classList.toggle('sel', state.sel.type === 'hub');

  repaint($('tree'), sidebarSig(), buildTree);
}

function buildTree() {
  const out = [];

  for (const node of state.nodes) {
    const hit = matchesFilter(node);
    if (!hit.node) continue;

    const nodeSelected = state.sel.nodeId === node.id;
    const group = el('div.tree-group');

    // One status light per row; the state name is in the row's title.
    const nm = nodeMetaOf(node);
    group.append(el(`div.row.row-node${nodeSelected && state.sel.type === 'node' ? '.sel-soft' : ''}`, {
      ...clickable(() => selectNode(node.id)),
      title: `${node.name} · ${node.hostname || 'unknown host'} · ${nm.label}`,
    },
      statusDot(nm, { what: node.name }),
      el('span.row-title', { text: node.name }),
      el('span.row-sub', { text: node.hostname || '' })));

    const kids = el('div.tree-kids');
    for (const inst of hit.instances) {
      const m = metaOf(runStateOf(inst));
      const sel = state.sel.type === 'instance' && state.sel.instId === inst.id;
      kids.append(el(`div.row.row-inst${sel ? '.sel' : ''}`, {
        ...clickable(() => selectInstance(node.id, inst.id)),
        title: `${inst.name} · ${m.label} · ${inst.root_dir}`,
      },
        statusDot(m, { size: 7, what: inst.name }),
        el('span.row-title-inst', { text: inst.name })));
    }
    if (!hit.instances.length) {
      kids.append(el('div.row.row-inst', { style: 'cursor:default' },
        el('span.row-empty', { text: 'no instances' })));
    }
    group.append(kids);
    out.push(group);
  }

  if (!out.length) {
    out.push(el('div.empty', {
      text: !state.loaded ? 'Loading…'
        : state.filter ? 'Nothing matches that filter.'
        : 'No nodes yet. Add one to get started.',
    }));
  }
  return out;
}

/* --- render: hub view ---------------------------------------------------- */

function renderHub() {
  const nodes = state.nodes;
  const online = nodes.filter((n) => n.online).length;
  let instCount = 0, running = 0, attention = 0;
  for (const node of nodes) {
    for (const inst of instancesOf(node.id)) {
      instCount++;
      const run = runStateOf(inst);
      if (run === 'running') running++;
      if (run === 'crashed' || !node.online) attention++;
    }
  }

  $('st-online').textContent = online;
  $('st-nodes').textContent = ` / ${nodes.length}`;
  $('st-instances').textContent = instCount;
  $('st-running').textContent = running;
  $('st-attention').textContent = attention;

  // Colour only where there is something to look at.
  $('st-online').parentElement.className = online && online === nodes.length ? 'ok' : '';
  $('st-running').className = running ? 'ok' : '';
  $('st-attention').className = attention ? 'alert' : '';

  repaint($('node-grid'), sidebarSig(), buildNodeCards);

  fill($('enroll-section'), enrollSection());
}

function buildNodeCards() {
  const nodes = state.nodes;
  if (!nodes.length) {
    return el('div.empty', { style: 'grid-column:1/-1' },
      state.loaded
        ? el('div', {},
            el('div', { style: 'margin-bottom:14px' }, 'No nodes yet. Add one, then run its agent.'),
            el('button.btn.btn-primary', { onclick: addNode }, '+ Add node'))
        : 'Loading nodes…');
  }
  const out = [];
  for (const node of nodes) {
    const insts = instancesOf(node.id);
    out.push(el('div.node-card', clickable(() => selectNode(node.id), `Node ${node.name}`),
      el('div.node-card-head', {},
        statusDot(nodeMetaOf(node), { size: 9, what: node.name }),
        el('span.node-card-name', { text: node.name }),
        el('span.node-card-host', { text: node.hostname || 'host unknown' })),
      el('div.node-card-meta', {},
        el('div', {}, el('div.k', { text: 'AGENT' }), node.agent_version || 'not enrolled'),
        el('div', {}, el('div.k', { text: 'LAST SEEN' }), relTime(node.last_seen)),
        el('div', {}, el('div.k', { text: 'INSTANCES' }), String(insts.length))),
      el('div.node-card-insts', {},
        insts.length
          ? insts.map((inst) => {
              const m = metaOf(runStateOf(inst));
              return el('span.inst-chip', { title: `${inst.name}: ${m.label}` },
                statusDot(m, { size: 6 }),
                inst.name);
            })
          : el('span.inst-chip.muted', {}, 'no instances'))));
  }
  return out;
}

/** Node state, spelled out on the node's own page. */
function fillNodeBadge(node) {
  const m = nodeMetaOf(node);
  const badge = $('node-badge');
  badge.style.cssText = `color:${m.color};border-color:${m.color}55;background:${m.color}14`;
  badge.textContent = m.label;
}

/** One-time enrollment token card. The token cannot be fetched again. */
function enrollSection() {
  const e = state.enrollment;
  if (!e) return null;
  const node = nodeById(e.nodeId);
  const connected = !!node?.online;

  return el('div', {},
    el('div.label', { style: 'margin-bottom:12px' }, `ADD A NODE · ${e.name}`),
    el('div.enroll', {},
      el('div.enroll-head', {},
        el('div.enroll-title', {}, 'Enrollment token ',
          el('span.enroll-once', { text: 'SHOWN ONCE' })),
        el('div.enroll-note', {},
          `Run the agent on the new machine with this token. It expires in ${Math.round(e.expiresIn / 60)} minutes `,
          'and cannot be retrieved again, only re-minted.')),
      el('div.enroll-body', {},
        el('div.enroll-token-row', {},
          el('code.enroll-token', { text: e.token, title: e.token }),
          el('button.btn', {
            style: 'height:40px',
            onclick: async (ev) => {
              const ok = await copyText(e.token);
              ev.currentTarget.textContent = ok ? 'Copied' : 'Copy failed';
              setTimeout(() => { ev.currentTarget.textContent = 'Copy'; }, 1600);
            },
          }, 'Copy')),
        el('div', { class: 'mono', style: 'font-size:9.5px;letter-spacing:.1em;color:var(--dim);margin-bottom:8px' }, 'INSTALL HINT'),
        el('pre.enroll-hint', {
          text: `MM_HUB_URL=${wsOrigin()}/ws/agent \\\nMM_ENROLL_TOKEN=${e.token} \\\n  python -m minemanager_agent.main`,
        }),
        el(`div.enroll-wait${connected ? '.done' : ''}`, {},
          el('span.dot', {
            style: `width:7px;height:7px;background:${connected ? '#47d18a' : '#f0b429'};${connected ? '' : 'animation:mm-pulse 1.4s infinite'}`,
          }),
          connected ? 'agent connected.' : 'waiting for agent to connect…',
          el('span.spacer'),
          el('button.btn.btn-sm', { onclick: () => { state.enrollment = null; render(); } },
            connected ? 'Dismiss' : 'Hide')))));
}

/* --- render: node view --------------------------------------------------- */

function renderNode() {
  const node = curNode();
  if (!node) {
    fill($('node-crumbs'), el('a', { onclick: selectHub }, 'Hub'), el('span.sep', {}, '/'), el('span.cur', {}, 'unknown'));
    $('node-name').textContent = 'Node not found';
    fill($('node-meta'), 'It may have been deleted.');
    clear($('node-inst-rows'));
    return;
  }

  fill($('node-crumbs'),
    el('a', { onclick: selectHub }, 'Hub'),
    el('span.sep', {}, '/'),
    el('span.cur', { text: node.name }));

  $('node-name').textContent = node.name;

  fillNodeBadge(node);

  fill($('node-meta'),
    el('span', { text: node.hostname || 'hostname unknown' }),
    el('span', { text: `agent ${node.agent_version || 'unknown'}` }),
    el('span', { text: `last seen ${relTime(node.last_seen)}` }));

  const insts = instancesOf(node.id);
  $('node-inst-label').textContent = `INSTANCES · ${insts.length}`;

  repaint($('node-inst-rows'), sidebarSig(), () => buildInstanceRows(node, insts));

  fill($('node-enroll-section'),
    state.enrollment && state.enrollment.nodeId === node.id ? enrollSection() : null);

  renderJava(node);
}

function buildInstanceRows(node, insts) {
  if (!insts.length) return el('div.empty', { text: 'No instances on this node yet.' });
  const out = [];
  for (const inst of insts) {
    const run = runStateOf(inst);
    const m = metaOf(run);
    const live = node.online;
    out.push(el('div.table-row', clickable(() => selectInstance(node.id, inst.id), inst.name),
      el('span.cell-name', {},
        statusDot(m, { what: inst.name }),
        el('span', { text: inst.name })),
      el('span.cell-type', { text: inst.type }),
      el('span.cell-root', { text: inst.root_dir, title: inst.root_dir }),
      el('span.cell-ctl', {},
        powerButton(inst, 'start', '▶', 'Start', !live || ['running', 'starting'].includes(run), 'go'),
        powerButton(inst, 'stop', '■', 'Stop', !live || run === 'stopped'),
        powerButton(inst, 'restart', '⟳', 'Restart', !live))));
  }
  return out;
}

function powerButton(inst, op, glyph, title, disabled, extra = '') {
  return el(`button.btn-icon${extra ? '.' + extra : ''}`, {
    title, disabled, type: 'button', 'aria-label': `${title} ${inst.name}`,
    onclick: (ev) => { ev.stopPropagation(); doPower(inst, op, ev.currentTarget); },
  }, glyph);
}

/* --- java runtimes -------------------------------------------------------- */

// node id -> { data, loading, error }. Live-only: the agent is the authority.
const javaByNode = new Map();
let javaCompat = null;          // /api/java/compat, fetched once
let javaInstalling = null;      // { nodeId, feature } while an install runs

const JAVA_SOURCE = { managed: 'Installed', system: 'Found', manual: 'Added' };

function shortVendor(v) {
  if (!v) return '';
  if (/adoptium/i.test(v)) return 'Temurin';
  return v.replace(/,?\s*(Inc\.?|Corporation|Ltd\.?)$/i, '').trim();
}

/** "Java 21.0.5+11 · Temurin", or why it cannot be named. */
function javaName(r) {
  if (r.probed === false) return 'Not checked (node offline)';
  if (!r.major) return 'Unknown runtime';    // the reason is shown beneath
  const vendor = shortVendor(r.vendor);
  return `Java ${r.version || r.major}${vendor ? ` · ${vendor}` : ''}`;
}

const javaTitle = (r) => javaName(r) + (r.default ? ' (Default)' : '');

async function loadNodeJava(nodeId) {
  const prev = javaByNode.get(nodeId);
  if (prev?.loading) return;
  javaByNode.set(nodeId, { data: prev?.data || null, loading: true, error: null });
  if (state.sel.nodeId === nodeId) render();
  let next;
  try {
    next = { data: await api.listJava(nodeId), loading: false, error: null };
  } catch (err) {
    next = { data: prev?.data || null, loading: false, error: describe(err) };
  }
  javaByNode.set(nodeId, next);
  if (state.sel.nodeId === nodeId) render();
}

async function loadJavaCompat() {
  if (javaCompat) return;
  try {
    javaCompat = await api.javaCompat();
    render();
  } catch { /* the table is a reference; the rest works without it */ }
}

function renderJava(node) {
  const entry = javaByNode.get(node.id);
  const data = entry?.data;
  // Listed while the node was offline (or before it came back): look again.
  if (data && data.online !== node.online && !entry.loading && !entry.error) loadNodeJava(node.id);

  const busy = !!entry?.loading;
  const installing = javaInstalling?.nodeId === node.id;
  $('node-java-rescan').disabled = !node.online || busy;
  $('node-java-add').disabled = !node.online;
  $('node-java-install').disabled = !node.online || installing || !data?.installable;
  $('node-java-install').textContent = installing ? `Installing Java ${javaInstalling.feature}…` : 'Install Java';
  $('node-java-install').title = data && !data.installable && node.online
    ? `Temurin has no runtime for this node's platform (${data.platform?.os}/${data.platform?.arch || '?'})`
    : '';

  const sig = JSON.stringify([node.online, busy, entry?.error, data]);
  repaint($('node-java-rows'), sig, () => buildJavaRows(node, entry));

  if (javaCompat) repaint($('node-java-compat'), 'compat', () => buildCompatTable(javaCompat));
}

function buildJavaRows(node, entry) {
  if (!entry || (entry.loading && !entry.data)) return el('div.empty', { text: 'Looking for Java on this node…' });
  if (entry.error && !entry.data) return el('div.empty', { text: entry.error });
  const data = entry.data;
  const rows = data.runtimes || [];
  const out = [];
  if (!data.online) {
    out.push(el('div.empty', { text: 'Node offline. Only runtimes added by path are listed, unchecked.' }));
  } else if (!rows.length) {
    out.push(el('div.empty', { text: 'No Java found on this node. Install one, or add its path.' }));
  }
  for (const r of rows) {
    const removable = r.source === 'managed' || r.source === 'manual';
    const used = r.used_by || [];
    // A runtime that cannot be used is greyed out, its reason in red.
    const dim = r.error || r.probed === false;
    out.push(el(`div.table-row.java-row${dim ? '.java-off' : ''}`, {},
      el('span.cell-name', {},
        el('span.java-name', {},
          el('span.java-title', {},
            el('span.java-text', { text: javaName(r), title: r.version || '' }),
            r.default && el('span.java-default', { text: '(Default)' })),
          r.error && el('span.java-error', { text: r.error }),
          used.length > 0 && el('span.java-used', { text: `used by ${used.join(', ')}` }))),
      el('span.cell-type', { text: JAVA_SOURCE[r.source] || r.source }),
      el('span.cell-root', { text: r.home, title: r.home }),
      el('span.cell-ctl', {},
        removable && el('button.btn-icon', {
          type: 'button',
          title: r.source === 'managed' ? 'Delete this runtime from the node' : 'Forget this path',
          'aria-label': `Remove ${r.home}`,
          disabled: r.source === 'managed' && !node.online,
          onclick: () => removeJava(node, r),
        }, '✕'))));
  }
  return out;
}

function buildCompatTable(compat) {
  const names = { vanilla: 'Minecraft (Vanilla, Paper)', velocity: 'Velocity' };
  return ['vanilla', 'velocity'].map((software) => {
    const rules = compat.rules[software] || [];
    return el('div.compat-group', {},
      el('div.compat-title', { text: names[software] }),
      rules.map((rule, i) => {
        const range = i === 0 ? `Older than ${rules[1]?.since}` : `From ${rule.since}`;
        const needs = rule.minimum === rule.recommended
          ? `Java ${rule.recommended}`
          : `Java ${rule.recommended} (needs ${rule.minimum}+)`;
        return el('div.compat-row', {}, el('span', { text: range }), el('span', { text: needs }));
      }));
  });
}

async function installJava(node) {
  const data = javaByNode.get(node.id)?.data;
  const managed = new Set((data?.runtimes || []).filter((r) => r.source === 'managed').map((r) => r.major));
  const features = data?.managed || [8, 17, 21, 25];
  const values = await dialog({
    title: `Install Java on ${node.name}`,
    description: 'Downloads an Eclipse Temurin JRE from Adoptium, checks its SHA-256 and '
               + "unpacks it into the agent's data directory. Reinstalling updates it in "
               + 'place; servers pick the update up on their next start.',
    confirmText: 'Install',
    fields: [{
      name: 'feature', label: 'Release', type: 'select', value: String(features[features.length - 1]),
      options: features.slice().reverse().map((f) => ({
        value: String(f), label: `Java ${f}${managed.has(f) ? ' (installed; update it)' : ''}`,
      })),
    }],
  });
  if (!values) return;
  const feature = Number(values.feature);
  javaInstalling = { nodeId: node.id, feature };
  render();
  toast(`Installing Java ${feature} on ${node.name}. This takes a minute.`, 'info');
  try {
    const res = await api.installJava(node.id, feature);
    toast(`Java ${res.runtime?.version || feature} installed on ${node.name}`, 'ok');
  } catch (err) {
    toast(describe(err), 'error');
  } finally {
    javaInstalling = null;
    loadNodeJava(node.id);
  }
}

async function addJavaPath(node) {
  const values = await dialog({
    title: `Add Java on ${node.name}`,
    description: 'The directory that contains bin/java. It is checked on the node before it is added.',
    confirmText: 'Add',
    fields: [{ name: 'path', label: 'Java directory', placeholder: '/opt/jdk-21', mono: true }],
    validate: (v) => (v.path.trim().startsWith('/') ? null : 'Use an absolute path, such as /opt/jdk-21.'),
  });
  if (!values) return;
  try {
    const res = await api.registerJava(node.id, values.path.trim());
    toast(`${javaName(res.runtime)} added`, 'ok');
  } catch (err) {
    toast(describe(err), 'error');
  }
  loadNodeJava(node.id);
}

async function removeJava(node, r) {
  const managed = r.source === 'managed';
  const ok = await confirmDialog({
    title: managed ? `Delete ${javaName(r)}?` : `Forget ${r.home}?`,
    description: managed
      ? `Deletes ${r.home} from the node. Refused while an instance is set to it.`
      : 'Removes it from this list. The directory on the node is left alone, and '
        + 'instances already set to it keep using it.',
    confirmText: managed ? 'Delete' : 'Forget',
  });
  if (!ok) return;
  try {
    await api.removeJava(node.id, r.home);
    toast(managed ? 'Runtime deleted' : 'Path forgotten', 'ok');
  } catch (err) {
    toast(describe(err), 'error');
  }
  loadNodeJava(node.id);
}

/**
 * Options for a "which Java" select, from a listJava() result. The node's
 * default runtime is the value '' (follow the node default) and is labelled
 * "(Default)". `preferred` is what a new server should start on: the default
 * when there is one, else the only or the recommended runtime.
 */
function javaChoices(res) {
  const usable = (res?.runtimes || []).filter((r) => r.major && !r.error);
  const options = usable.map((r) => {
    let label = javaTitle(r);
    const bad = r.compatible === false;
    if (bad) label += ` (too old, needs ${res.requirement.minimum}+)`;
    else if (r.home === res.recommended_home) label += ' (recommended)';
    return { value: r.default ? '' : r.home, label, bad };
  });
  const def = usable.find((r) => r.default);
  if (!def) {
    // A default that exists but cannot run says so, rather than "none".
    const broken = (res?.runtimes || []).find((r) => r.default);
    options.unshift(broken
      ? { value: '', label: `Node default (unusable: ${broken.error})`, bad: true }
      : { value: '', label: "Node default (no java on the agent's PATH)", bad: !!res?.online });
  }
  const preferred = def ? '' : usable.length ? (res.recommended_home || usable[0].home) : '';
  return { options, preferred };
}

/**
 * The line under a Java picker: what the server needs, and what is wrong.
 * `bad` marks a choice that will not start the server, shown in red. It
 * warns, never blocks: running a server on another Java is allowed.
 */
function javaNote(res, value, what, idle = 'Takes effect the next time this instance starts.') {
  if (!res) return { text: 'Looking for Java on the node…' };
  if (!res.online) return { text: "Node offline: its Java runtimes can't be listed." };
  const usable = (res.runtimes || []).filter((r) => r.major && !r.error);
  const picked = usable.find((r) => (value === '' ? r.default : r.home === value));
  const req = res.requirement;
  const parts = [];
  let bad = false;
  if (value === '' && !picked && usable.length) {
    parts.push("The node has no working default Java: pick one, or this server won't start.");
    bad = true;
  }
  if (req && what) {
    parts.push(`${what} needs Java ${req.minimum} or newer.`);
    if (picked && picked.major < req.minimum) {
      parts.push('The selected Java is too old: the server will not start.');
      bad = true;
    } else if (req.legacy && picked && picked.major > 8) {
      parts.push('Built for Java 8: newer Java can break old plugins and mods.');
    }
    if (usable.length && !usable.some((r) => r.major >= req.minimum)) {
      parts.push(`Install Java ${req.recommended} from the node page first.`);
    }
  }
  if (!usable.length) {
    parts.push('No Java on this node yet. Install one from the node page.');
    bad = true;
  }
  return { text: parts.join(' ') || idle, bad };
}

/** Show a javaNote() under its picker, and mark the picker when it is bad. */
function showJavaNote(noteEl, selectEl, note) {
  noteEl.textContent = note.text;
  noteEl.classList.toggle('field-warn', !!note.bad);
  selectEl.classList.toggle('input-warn', !!note.bad);
}

/** Paint the too-old options red in an open list. */
function markJavaOptions(selectEl, options) {
  const bad = new Set(options.filter((o) => o.bad).map((o) => o.value));
  for (const o of selectEl.options) o.classList.toggle('opt-bad', bad.has(o.value));
}

/** Instance settings: fill the Java picker for `inst` from its node. */
async function fillJavaSetting(inst) {
  const sel = $('set-java');
  const pathEl = $('set-java-path');
  const note = $('set-java-note');
  const current = inst.java_home || '';
  fill(sel, el('option', { value: current, text: current || 'Node default' }));
  sel.onchange = null;
  pathEl.value = '';
  show(pathEl, false);
  showJavaNote(note, sel, javaNote(null));

  let res;
  try {
    res = await api.listJava(inst.node_id, inst.type, inst.version);
  } catch (err) {
    // The current choice stays selected, so saving keeps it.
    showJavaNote(note, sel, { text: `Couldn't list this node's Java: ${describe(err)}` });
    return;
  }
  if (curInst()?.id !== inst.id) return;       // moved on while waiting
  const { options } = javaChoices(res);
  if (current && !options.some((o) => o.value === current)) {
    options.push({ value: current, label: `Custom: ${current}` });
  }
  options.push({ value: '__custom', label: 'Other path…' });
  fill(sel, options.map((o) => el('option', { value: o.value, selected: o.value === current }, o.label)));
  sel.value = current;
  markJavaOptions(sel, options);
  const what = inst.version ? `${cap(inst.type)} ${inst.version}` : null;
  const update = () => {
    show(pathEl, sel.value === '__custom');
    showJavaNote(note, sel, sel.value === '__custom'
      ? { text: 'The directory that contains bin/java, on the node.' }
      : javaNote(res, sel.value, what));
  };
  sel.onchange = update;
  update();
}

/** What the Java picker in Settings currently says, as a java_home. */
function javaSettingValue() {
  const v = $('set-java').value;
  if (v === '__custom') return $('set-java-path').value.trim() || null;
  return v || null;
}

/* --- render: instance view ----------------------------------------------- */

function renderInstance() {
  const inst = curInst();
  if (!inst) {
    $('inst-name').textContent = state.loaded ? 'Instance not found' : 'Loading…';
    $('inst-type').textContent = '';
    $('inst-state').textContent = '';
    $('inst-meta').textContent = '';
    fill($('inst-crumbs'), el('a', { onclick: selectHub }, 'Hub'));
    ['console', 'files', 'version', 'settings'].forEach((t) => show($(`pane-${t}`), false));
    show($('inst-stranded'), false);
    show($('inst-building'), false);
    return;
  }
  const node = nodeById(inst.node_id);
  const run = runStateOf(inst);
  const m = metaOf(run);
  const live = !!node?.online;

  fill($('inst-crumbs'),
    el('a', { onclick: selectHub }, 'Hub'),
    el('span.sep', {}, '/'),
    el('a', { onclick: () => selectNode(inst.node_id) }, node?.name || 'node'),
    el('span.sep', {}, '/'),
    el('span.cur', { text: inst.name }));

  $('inst-name').textContent = inst.name;
  $('inst-type').textContent = inst.type;

  const st = $('inst-state');
  st.style.cssText = `color:${m.color};border-color:${m.color}55;background:${m.color}14`;
  st.textContent = m.label;

  renderArmed(inst, run);

  $('inst-meta').textContent = inst.version
    ? `${inst.version}${inst.build ? ` build ${inst.build}` : ''}`
    : '';

  // Gated on reachability, and locked outright during an update.
  const updating = run === 'updating';
  const start = $('act-start');
  start.classList.toggle('btn-start', live && !updating && ['stopped', 'crashed', 'unknown'].includes(run));
  start.disabled = !live || updating || ['running', 'starting'].includes(run);
  $('act-stop').disabled = !live || updating || run === 'stopped';
  $('act-restart').disabled = !live || updating;
  $('act-kill').disabled = !live || updating || run === 'stopped';

  document.querySelectorAll('.tab').forEach((t) => {
    const on = t.dataset.tab === state.tab;
    t.classList.toggle('on', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
    // Roving tabindex: the strip is one tab stop; arrows move within it.
    t.tabIndex = on ? 0 : -1;
  });
  // Only once the first load resolves; before that, offline is not known.
  show($('inst-offline'), state.loaded && !live);
  renderStranded(inst);
  renderBuilding(inst);
  show($('pane-console'), state.tab === 'console');
  show($('pane-files'), state.tab === 'files');
  show($('pane-version'), state.tab === 'version');
  show($('pane-settings'), state.tab === 'settings');

  if (state.tab === 'console') renderConsole(inst, live);
  if (state.tab === 'files') renderFiles(inst, live);
  if (state.tab === 'version') renderVersion(inst, live);
}

/**
 * Why a creation failed. The agent appends the server's last terminal output to
 * its reason; a toast would mangle it, so anything multi-line gets a dialog.
 */
function reportProvisionFailure(name, detail) {
  const [reason, ...output] = String(detail || 'the agent gave no reason').split('\n');
  if (!output.length) {
    toast(`Could not create ${name}: ${reason}`, 'error');
    return;
  }
  dialog({
    title: `Could not create ${name}`,
    description: reason,
    detail: output.join('\n').replace(/^--- last terminal output ---\n?/, ''),
    confirmText: 'Close',
    cancelText: null,
  });
}

/** An instance the hub is still building, which otherwise reads as stopped. */
function renderBuilding(inst) {
  const banner = $('inst-building');
  const building = state.provisioning.has(inst.id);
  show(banner, building);
  if (building) {
    banner.textContent =
      'Setting this server up: downloading the server jar, then running it once to '
      + 'generate its configuration. Progress appears in the console below. This '
      + 'takes a few minutes.';
  }
}

/**
 * An instance the node's root does not cover. The agent refuses every command
 * for it, so without this the only symptom is each button failing in turn.
 */
function renderStranded(inst) {
  const banner = $('inst-stranded');
  show(banner, !!inst.outside_node_root);
  if (!inst.outside_node_root) return;
  banner.textContent =
    `This server's directory is outside its node's root directory, so the agent `
    + `refuses every action for it. Move it under the node's root and change `
    + `"Root directory" in Settings to match, or widen MM_ROOT_DIR on the agent.`;
}

/**
 * The declared "start with node" flag, and where it disagrees with reality.
 * Driven by `start_with_node`, not run state, so it still says something while
 * the node is offline.
 */
function renderArmed(inst, run) {
  const pill = $('inst-armed');
  show(pill, !!inst.start_with_node);
  if (!inst.start_with_node) return;

  // Armed but not up: it returns on the next agent start.
  const down = ['stopped', 'crashed', 'unknown'].includes(run);
  pill.style.cssText = down
    ? 'color:var(--warn);border:1px solid #4a3c20;background:#1a1712'
    : 'color:var(--muted);border:1px solid var(--line-2);background:none';
  pill.textContent = down ? 'RETURNS ON NODE START' : 'STARTS WITH NODE';
  pill.title = state.supervised.get(inst.id) === false
    ? `The agent has stopped trying to keep this up, but it is still armed and will `
      + `be started again when the node's agent next starts. `
      + `Turn off "Start with node" in Settings to keep it down.`
    : `Started whenever the node's agent starts. `
      + `Change it under "Start with node" in Settings.`;
}

function renderConsole(inst, live) {
  const buf = state.console.get(inst.id) || [];
  $('term-count').textContent = `${buf.length} line${buf.length === 1 ? '' : 's'}`;
  $('term-autoscroll').setAttribute('aria-pressed', state.autoscroll ? 'true' : 'false');

  const input = $('console-line');
  input.disabled = !live;
  $('console-send').disabled = !live;
  input.placeholder = live ? 'send a console command…' : 'node offline';

  const empty = $('term-empty');
  empty.hidden = buf.length > 0;
  const run = runStateOf(inst);
  empty.textContent = !live ? 'Node offline. No console output.'
    : run === 'running' ? 'Waiting for output…'
    : run === 'stopped' || run === 'crashed' ? 'Server is not running. Output appears here once it starts.'
    : 'No console output yet.';

  // Shown only when the history backfill failed.
  show($('console-hint'), state.consoleFailed.has(inst.id));

  const body = $('term-body');
  if (repaint(body, consoleSig(inst.id, buf.length), () => buf.map(lineNode))) {
    if (state.autoscroll) body.scrollTop = body.scrollHeight;
  }
}

/* --- power / console actions --------------------------------------------- */

async function doPower(inst, op, button) {
  if (op === 'kill') {
    const ok = await confirmDialog({
      title: `Kill ${inst.name}?`,
      description: 'This terminates the process immediately! The world may not be saved. Prefer Stop for a graceful shutdown.',
      confirmText: 'Kill it',
    });
    if (!ok) return;
  }
  const buttons = [...document.querySelectorAll('.inst-actions .btn')];
  if (button) button.disabled = true;
  buttons.forEach((b) => { b.disabled = true; });
  pushConsole(inst.id, `» ${op} requested by operator`, 'sys');   // log the action
  try {
    const res = await api.power(inst.id, op);
    if (res && res.state) state.runState.set(inst.id, res.state);
    // The hub writes start_with_node on every accepted power op; mirror it,
    // and sync the toggle so a later Save does not write back a stale value.
    inst.start_with_node = op === 'start' || op === 'restart';
    setToggle($('set-startwith'), inst.start_with_node);
    toast(`${inst.name}: ${op} → ${res?.state || 'ok'}`, 'ok');
  } catch (err) {
    toast(`${inst.name}: ${describe(err)}`, 'error');
  } finally {
    render();
  }
}

async function sendConsole() {
  const inst = curInst();
  const input = $('console-line');
  const line = input.value.trim();
  if (!inst || !line) return;
  input.value = '';
  pushConsole(inst.id, `> ${line}`, 'sys');
  try {
    await api.console(inst.id, line);
  } catch (err) {
    pushConsole(inst.id, `!! ${describe(err)}`, 'sys');
    toast(describe(err), 'error');
  }
}

/* --- files tab ----------------------------------------------------------- */

function resetInstancePanes() {
  state.files = { path: '.', entries: [], loading: false, error: null };
  state.editor = { path: null, original: '', size: 0 };
  state.version = {
    instId: null, loading: false, error: null,
    software: null, label: '', hasBuilds: false,
    versions: [], builds: [], buildsLoading: false,
    selVersion: null, selBuild: null, updating: false,
  };
  $('editor-area').value = '';
  paintEditor();          // clear the gutter + highlight overlay (close the open file)
  const body = $('term-body');
  body.dataset.sig = '';
  clear(body);
}

function loadTab() {
  const inst = curInst();
  if (!inst) return;
  if (state.tab === 'console') loadConsoleHistory(inst);
  if (state.tab === 'files' && !state.files.entries.length && !state.files.loading) loadFiles(state.files.path);
  if (state.tab === 'version' && state.version.instId !== inst.id) loadVersion(inst);
  if (state.tab === 'settings') fillSettings(inst);
}

async function loadFiles(path) {
  const inst = curInst();
  if (!inst) return;
  if (!isOnline(inst)) {
    state.files = { path, entries: [], loading: false, error: "Node offline. Can't list files." };
    render();
    return;
  }
  state.files = { path, entries: [], loading: true, error: null };
  render();
  try {
    const res = await api.listFiles(inst.id, path);
    if (state.sel.instId !== inst.id) return;
    state.files = { path, entries: res.entries || [], loading: false, error: null };
  } catch (err) {
    state.files = { path, entries: [], loading: false, error: describe(err) };
    toast(describe(err), 'error');
  }
  render();
}

/** Everything the file list draws from, flattened. */
function filesSig(inst, live) {
  const f = state.files;
  const parts = [inst.id, f.path, live ? '1' : '0', f.loading ? '1' : '0', f.error || '',
                 state.editor.path || ''];
  for (const e of f.entries) parts.push(e.path, e.is_dir ? 'd' : 'f', String(e.size));
  return parts.join('');
}

function buildFileRows(path, segs, live) {
  if (state.files.loading) return el('div.empty', {}, 'Loading…');
  if (state.files.error) {
    return el('div.empty', { style: 'color:var(--err-fg)', text: state.files.error });
  }

  const out = [];
  if (path !== '.') {
    const parent = segs.slice(0, -1).join('/') || '.';
    out.push(el('div.file-row.dir', clickable(() => loadFiles(parent), 'Parent folder'),
      el('span.file-name', {}, '../')));
  }
  const entries = [...state.files.entries].sort(
    (a, b) => (b.is_dir - a.is_dir) || a.name.localeCompare(b.name));
  if (!entries.length && path === '.') out.push(el('div.empty', {}, 'Empty directory.'));

  for (const f of entries) {
    const selected = !f.is_dir && state.editor.path === f.path;
    const attrs = {
      title: f.path,
      onclick: () => (f.is_dir ? loadFiles(f.path) : openFile(f)),
      oncontextmenu: (ev) => showContextMenu(ev, f),
    };
    if (f.is_dir) {   // drop files onto a folder to upload into it
      attrs.ondragover = (ev) => { ev.preventDefault(); ev.stopPropagation(); ev.currentTarget.classList.add('drop-hi'); };
      attrs.ondragleave = (ev) => ev.currentTarget.classList.remove('drop-hi');
      attrs.ondrop = (ev) => { ev.stopPropagation(); ev.currentTarget.classList.remove('drop-hi'); handleDrop(ev, f.path); };
    }
    Object.assign(attrs, clickable(attrs.onclick, f.is_dir ? `Open folder ${f.name}` : `Open ${f.name}`));
    out.push(el(`div.file-row${f.is_dir ? '.dir' : ''}${selected ? '.sel' : ''}`, attrs,
      el('span.file-name', { text: f.is_dir ? `${f.name}/` : f.name }),
      el('span.spacer'),
      el('span.file-size', { text: f.is_dir ? '' : formatSize(f.size) }),
      el('button.file-act', {
        title: f.is_dir ? 'Download as zip' : 'Download', disabled: !live,
        onclick: (ev) => { ev.stopPropagation(); downloadEntry(f); },
      }, '⇩'),
      el('button.file-del', {
        title: 'Delete', disabled: !live,
        onclick: (ev) => { ev.stopPropagation(); deleteEntry(f); },
      }, '×')));
  }
  return out;
}

function renderFiles(inst, live) {
  // breadcrumb
  const path = state.files.path;
  const segs = path === '.' ? [] : path.split('/').filter(Boolean);
  fill($('files-path'),
    el('span.seg', { onclick: () => loadFiles('.'), text: inst.name }),
    ' / ',
    segs.length
      ? segs.map((seg, i) => [
          el('span.seg', { onclick: () => loadFiles(segs.slice(0, i + 1).join('/')), text: seg }),
          i < segs.length - 1 ? ' / ' : '',
        ])
      : '.');

  $('files-refresh').disabled = !live;
  $('files-new').disabled = !live;
  $('files-upload').disabled = !live;
  $('files-download').disabled = !live;

  // Pure class toggle, so file/editor state is untouched.
  document.querySelector('.files-wrap')?.classList.toggle('editor-fullscreen', state.editorFullscreen);
  const fs = $('editor-fullscreen');
  fs.title = state.editorFullscreen ? 'Exit fullscreen' : 'Toggle fullscreen editor';
  fs.setAttribute('aria-label', fs.title);
  fs.setAttribute('aria-pressed', state.editorFullscreen ? 'true' : 'false');

  repaint($('files-list'), filesSig(inst, live), () => buildFileRows(path, segs, live));

  // editor header
  const open = state.editor.path !== null;
  $('editor-name').textContent = open ? state.editor.path : 'No file open';
  $('editor-name').classList.toggle('placeholder', !open);
  // Hide the editor itself until a file is open.
  document.querySelector('.editor-pane')?.classList.toggle('empty-state', !open);
  const dirty = open && $('editor-area').value !== state.editor.original;
  show($('editor-dirty'), dirty);
  $('editor-save').disabled = !dirty || !live;
  $('editor-info').textContent = open
    ? `${formatSize(new Blob([$('editor-area').value]).size)} · utf-8`
    : '';
  $('editor-area').readOnly = state.editor.path === null || !live;
}

async function openFile(entry) {
  const inst = curInst();
  if (!inst) return;

  // Binary guard by extension.
  if (isBinaryName(entry.name)) return binaryNotice(entry);

  // Large-file guard, using configurable thresholds from the hub.
  const { editor_warn_bytes: warn, editor_max_bytes: max } = state.config;
  if (entry.size > max) {
    const ok = await confirmDialog({
      title: 'File too large to edit',
      description: `${entry.name} is ${formatSize(entry.size)}, above the ${formatSize(max)} editor limit. Download it instead?`,
      confirmText: 'Download', danger: false,
    });
    if (ok) downloadEntry(entry, { confirm: false });
    return;
  }
  if (entry.size > warn) {
    const ok = await confirmDialog({
      title: 'Open a large file?',
      description: `${entry.name} is ${formatSize(entry.size)}. Opening large files in the editor can be slow. Open it anyway?`,
      confirmText: 'Open anyway', danger: false,
    });
    if (!ok) return;
  }

  if (await maybeDiscard()) return;
  try {
    const res = await api.readFile(inst.id, entry.path);
    if (state.sel.instId !== inst.id) return;
    // Server-side content detection (NUL bytes).
    if (res.binary) return binaryNotice(entry);
    state.editor = { path: entry.path, original: res.content ?? '', size: entry.size };
    $('editor-area').value = state.editor.original;
    paintEditor();
    render();
  } catch (err) {
    toast(describe(err), 'error');
  }
}

async function binaryNotice(entry) {
  const ok = await confirmDialog({
    title: 'Binary file',
    description: `${entry.name} is a binary file and can't be edited with the text editor. Download it instead?`,
    confirmText: 'Download', danger: false,
  });
  if (ok) downloadEntry(entry, { confirm: false });
}

/** Warn before dropping unsaved edits. Returns true when the user cancels. */
async function maybeDiscard() {
  if (state.editor.path === null) return false;
  if ($('editor-area').value === state.editor.original) return false;
  const ok = await confirmDialog({
    title: 'Discard unsaved changes?',
    description: `${state.editor.path} has edits that have not been written to the server.`,
    confirmText: 'Discard',
  });
  return !ok;
}

async function saveFile() {
  const inst = curInst();
  if (!inst || state.editor.path === null) return;
  const content = $('editor-area').value;
  const btn = $('editor-save');
  btn.disabled = true;
  try {
    const res = await api.writeFile(inst.id, state.editor.path, content);
    state.editor.original = content;
    state.editor.size = res?.size ?? content.length;
    toast(`Saved ${state.editor.path}`, 'ok');
    loadFiles(state.files.path);
  } catch (err) {
    toast(describe(err), 'error');
    render();
  }
}

async function newFile() {
  const inst = curInst();
  if (!inst) return;
  const values = await dialog({
    title: 'New text file',
    description: `Created under ${state.files.path === '.' ? inst.name + '/' : state.files.path + '/'}. Parent directories are created as needed.`,
    fields: [{ name: 'name', label: 'File name', placeholder: 'config/example.yml', mono: true }],
    confirmText: 'Create',
  });
  if (!values || !values.name.trim()) return;
  const rel = joinPath(state.files.path, values.name.trim());
  try {
    await api.writeFile(inst.id, rel, '');
    toast(`Created ${rel}`, 'ok');
    state.editor = { path: rel, original: '', size: 0 };
    $('editor-area').value = '';
    paintEditor();
    loadFiles(state.files.path);
  } catch (err) {
    toast(describe(err), 'error');
  }
}

async function deleteEntry(entry) {
  const inst = curInst();
  if (!inst) return;
  const ok = await confirmDialog({
    title: `Delete ${entry.name}?`,
    description: entry.is_dir
      ? `Recursively deletes ${entry.path} and everything inside it. This cannot be undone.`
      : `Deletes ${entry.path}. This cannot be undone.`,
    confirmText: 'Delete',
  });
  if (!ok) return;
  try {
    await api.deleteFile(inst.id, entry.path, entry.is_dir);
    if (state.editor.path === entry.path || (entry.is_dir && state.editor.path?.startsWith(entry.path + '/'))) {
      state.editor = { path: null, original: '', size: 0 };
      $('editor-area').value = '';
      paintEditor();
    }
    toast(`Deleted ${entry.path}`, 'ok');
    loadFiles(state.files.path);
  } catch (err) {
    toast(describe(err), 'error');
  }
}

/* --- files: type helpers ------------------------------------------------- */

// Extensions the text editor refuses to open.
const BINARY_EXT = new Set(
  ('jar zip gz tgz tar bz2 xz rar 7z png jpg jpeg gif webp bmp ico svgz pdf class '
    + 'dat mca mcr nbt db sqlite so dll dylib exe bin o ttf otf woff woff2 mp3 mp4 '
    + 'ogg wav flac mov mkv').split(' '));
const extOf = (name) => { const i = name.lastIndexOf('.'); return i >= 0 ? name.slice(i + 1).toLowerCase() : ''; };
const isBinaryName = (name) => BINARY_EXT.has(extOf(name));
const ARCHIVE_RE = /\.(zip|tar\.gz|tgz|tar\.bz2|tar\.xz|tar|gz|rar)$/i;
const isArchiveName = (name) => ARCHIVE_RE.test(name);

/* --- files: download ----------------------------------------------------- */

function triggerDownload(url) {
  const a = el('a', { href: url, rel: 'noopener' });
  document.body.append(a);
  a.click();
  a.remove();
}

/** Download a file, or a directory as a zip. Confirms unless the caller did. */
async function downloadEntry(entry, { confirm = true } = {}) {
  const inst = curInst();
  if (!inst) return;
  if (confirm) {
    const what = entry.is_dir
      ? 'zip archive'
      : formatSize(entry.size);
    const ok = await confirmDialog({
      title: 'Download file',
      description: `${entry.name} (${what}) will be downloaded to your computer.`,
      confirmText: 'Download', danger: false,
    });
    if (!ok) return;
  }
  // Directories and large files stream; small files download directly.
  if (entry.is_dir || (entry.size ?? 0) > STREAM_THRESHOLD) {
    startDownloadStream(inst, entry);
  } else {
    triggerDownload(api.downloadUrl(inst.id, entry.path));
  }
}

function downloadCurrentFolder() {
  const inst = curInst();
  if (!inst) return;
  const name = state.files.path === '.' ? inst.name : state.files.path.split('/').pop();
  downloadEntry({ path: state.files.path, name, is_dir: true });
}

/* --- files: upload (button + drag&drop) ---------------------------------- */

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',', 2)[1] || '');
    r.onerror = () => reject(r.error || new Error('read failed'));
    r.readAsDataURL(file);
  });
}

/** Upload {file, relPath}[] into destPath. Large files stream, small ones go
 *  base64. Refreshes on completion. */
async function uploadFiles(items, destPath) {
  const inst = curInst();
  if (!inst || !items.length) return;
  let ok = 0, failed = 0, streamed = 0;
  for (const { file, relPath } of items) {
    if (file.size > STREAM_THRESHOLD) {
      startUpload(inst, file, destPath, relPath);   // streaming + pill + own refresh
      streamed++;
    } else {
      try {
        await api.uploadFile(inst.id, joinPath(destPath, relPath), await fileToBase64(file));
        ok++;
      } catch { failed++; }
    }
  }
  if (ok) toast(`Uploaded ${ok} file${ok === 1 ? '' : 's'}`, 'ok');
  if (failed) toast(`${failed} file(s) failed to upload.`, 'error');
  if (ok || failed) loadFiles(state.files.path);   // streamed ones refresh on finish
}

/* --- files: streaming transfers + progress pills ------------------------- */

const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function randTid() {
  if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, '');
  const a = new Uint8Array(16); crypto.getRandomValues(a);
  return [...a].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function addTransfer(tid, info) {
  state.transfers.set(tid, {
    tid, name: '', direction: 'down', sent: 0, total: 0, state: 'starting',
    rate: 0, error: null, xhr: null, _lastSent: 0, _lastT: performance.now(), ...info,
  });
  renderTransfers();
}
function updateTransfer(tid, patch) {
  const t = state.transfers.get(tid);
  if (!t || ['done', 'error', 'cancelled'].includes(t.state)) return;
  Object.assign(t, patch);
  const now = performance.now();
  const dt = (now - t._lastT) / 1000;
  if (dt >= 0.4) {
    const inst = Math.max(0, (t.sent - t._lastSent) / dt);
    t.rate = t.rate ? t.rate * 0.6 + inst * 0.4 : inst;   // smoothed
    t._lastSent = t.sent; t._lastT = now;
  }
  renderTransfers();
}
function finishTransfer(tid, st, error) {
  const t = state.transfers.get(tid);
  if (!t) return;
  t.state = st; t.error = error || null; t.rate = 0;
  if (st === 'done' && t.total) t.sent = t.total;
  renderTransfers();
  setTimeout(() => { state.transfers.delete(tid); renderTransfers(); },
    st === 'done' ? 2500 : st === 'error' ? 7000 : 1200);
}
function cancelTransfer(t) {
  if (t.xhr) { try { t.xhr.abort(); } catch { /* already gone */ } }
  api.cancelTransfer(t.tid).catch(() => {});
  finishTransfer(t.tid, 'cancelled');
}

/** Build a pill's DOM once. Reconciled (not rebuilt) on updates to avoid flicker. */
function buildPill(t) {
  const pill = el('div.transfer-pill', { dataset: { tid: t.tid } },
    el('div.tp-head', {},
      el('span.tp-dir', {}, t.direction === 'up' ? '↑' : '↓'),
      el('span.tp-name', { title: t.name, text: t.name }),
      el('span.spacer'),
      el('span.tp-ctl')),
    el('div.tp-bar', {}, el('div.tp-fill')),
    el('div.tp-meta', {},
      el('span.tp-pct'), el('span.spacer'), el('span.tp-status'), el('span.tp-size')));
  return pill;
}

/** Update an existing pill's mutable fields in place (no DOM recreation). */
function updatePill(pill, t) {
  pill.className = `transfer-pill ${t.state}`;
  const pct = t.total ? Math.min(100, Math.round((t.sent / t.total) * 100)) : null;
  const active = t.state === 'starting' || t.state === 'active';
  const fill = pill.querySelector('.tp-fill');
  fill.classList.toggle('indet', pct === null && active);
  fill.style.width = pct !== null ? `${pct}%` : '';

  const ctl = pill.querySelector('.tp-ctl');
  const wantCancel = active;
  if (wantCancel && ctl.dataset.k !== 'cancel') {
    ctl.dataset.k = 'cancel';
    ctl.className = 'tp-ctl tp-cancel'; ctl.textContent = '×'; ctl.title = 'Cancel';
    ctl.onclick = () => cancelTransfer(t);
  } else if (!wantCancel && ctl.dataset.k !== 'icon') {
    ctl.dataset.k = 'icon';
    ctl.className = 'tp-ctl tp-icon'; ctl.onclick = null; ctl.title = '';
    ctl.textContent = t.state === 'done' ? '✓' : t.state === 'error' ? '!' : '–';
  } else if (!wantCancel) {
    ctl.textContent = t.state === 'done' ? '✓' : t.state === 'error' ? '!' : '–';
  }

  pill.querySelector('.tp-pct').textContent = pct !== null ? `${pct}%` : (t.sent ? formatSize(t.sent) : '');
  pill.querySelector('.tp-status').textContent = t.state === 'error' ? (t.error || 'failed')
    : t.state === 'done' ? 'complete'
    : t.state === 'cancelled' ? 'cancelled'
    : t.state === 'starting' ? 'starting…'
    : t.rate ? `${formatSize(t.rate)}/s` : '…';
  pill.querySelector('.tp-size').textContent = t.total ? ` · ${formatSize(t.total)}` : '';
}

function renderTransfers() {
  const box = $('transfers');
  const seen = new Set();
  for (const t of state.transfers.values()) {
    seen.add(t.tid);
    let pill = box.querySelector(`[data-tid="${t.tid}"]`);
    if (!pill) { pill = buildPill(t); box.append(pill); }
    updatePill(pill, t);
  }
  for (const pill of [...box.children]) {
    if (!seen.has(pill.dataset.tid)) pill.remove();
  }
}

function startUpload(inst, file, destPath, relPath) {
  const tid = randTid();
  const rel = joinPath(destPath, relPath || file.name);
  const xhr = new XMLHttpRequest();
  addTransfer(tid, { name: file.name, direction: 'up', total: file.size, xhr });
  xhr.open('POST', api.uploadStreamUrl(inst.id, rel, tid));
  xhr.upload.onprogress = (e) => updateTransfer(tid, { state: 'active', sent: e.loaded, total: e.total || file.size });
  xhr.onload = () => {
    finishTransfer(tid, xhr.status === 200 ? 'done' : 'error', xhr.status === 200 ? null : `upload failed (${xhr.status})`);
    loadFiles(state.files.path);
  };
  xhr.onerror = () => finishTransfer(tid, 'error', 'network error');
  xhr.onabort = () => finishTransfer(tid, 'cancelled');
  xhr.setRequestHeader('content-type', 'application/octet-stream');
  xhr.send(file);
}

function startDownloadStream(inst, entry) {
  const tid = randTid();
  addTransfer(tid, {
    name: entry.is_dir ? `${entry.name}.zip` : entry.name,
    direction: 'down', total: entry.is_dir ? 0 : (entry.size || 0),
  });
  triggerDownload(api.downloadStreamUrl(inst.id, entry.path, tid));  // native save
  pollDownload(tid);                                                 // pill progress
}
const POLL_MISS_LIMIT = 40;   // ~20s at 500ms

async function pollDownload(tid) {
  let sawActive = false, misses = 0;
  for (;;) {
    const t = state.transfers.get(tid);
    if (!t || ['done', 'error', 'cancelled'].includes(t.state)) return;
    let st = null;
    try { st = await api.transferStatus(tid); } catch { /* transient */ }
    if (st && (st.state === 'active' || st.state === 'pending')) {
      sawActive = true; misses = 0;
      updateTransfer(tid, { state: 'active', sent: st.sent, total: st.total || t.total });
    } else if (st && st.state === 'done') { finishTransfer(tid, 'done'); return; }
    else if (st && st.state === 'cancelled') { finishTransfer(tid, 'cancelled'); return; }
    else if (st && st.state === 'error') { finishTransfer(tid, 'error', st.error); return; }
    else if (sawActive) {
      finishTransfer(tid, 'done'); return;
    } else if (++misses > POLL_MISS_LIMIT) {
      finishTransfer(tid, 'error', 'the transfer never started');
      return;
    }
    await _sleep(500);
  }
}

let uploadTarget = null;

function pickUpload(destPath) {
  uploadTarget = destPath || state.files.path;
  const input = $('files-file-input');
  input.value = '';
  input.click();
}
function onFilePicked(ev) {
  const files = [...ev.target.files];
  const dest = uploadTarget || state.files.path;
  uploadTarget = null;
  ev.target.value = '';
  if (files.length) uploadFiles(files.map((f) => ({ file: f, relPath: f.name })), dest);
}

// Recursively walk dropped folders (where the browser supports it).
const entryToFile = (entry) => new Promise((res, rej) => entry.file(res, rej));
async function readAllEntries(reader) {
  const out = [];
  let batch;
  do {
    batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    out.push(...batch);
  } while (batch.length);
  return out;
}
async function collectEntry(entry, prefix, out) {
  if (entry.isFile) {
    out.push({ file: await entryToFile(entry), relPath: prefix + entry.name });
  } else if (entry.isDirectory) {
    for (const kid of await readAllEntries(entry.createReader())) {
      await collectEntry(kid, prefix + entry.name + '/', out);
    }
  }
}
async function itemsFromDataTransfer(dt) {
  // webkitGetAsEntry must be read synchronously before any await.
  const roots = [];
  for (const item of dt.items || []) {
    if (item.kind !== 'file') continue;
    const entry = item.webkitGetAsEntry && item.webkitGetAsEntry();
    if (entry) roots.push(entry);
  }
  const out = [];
  if (roots.length) {
    for (const r of roots) await collectEntry(r, '', out);
  } else {
    for (const f of dt.files) out.push({ file: f, relPath: f.name });  // fallback
  }
  return out;
}
async function handleDrop(ev, destPath) {
  ev.preventDefault();
  if (!isOnline(curInst())) { toast("Node offline. Can't upload.", 'error'); return; }
  const items = await itemsFromDataTransfer(ev.dataTransfer);
  if (items.length) uploadFiles(items, destPath);
}

/* --- files: rename / extract --------------------------------------------- */

async function renameEntry(entry) {
  const inst = curInst();
  if (!inst) return;
  const values = await dialog({
    title: `Rename ${entry.name}`,
    fields: [{ name: 'name', label: 'New name', value: entry.name, mono: true }],
    confirmText: 'Rename',
  });
  if (!values) return;
  const nn = values.name.trim();
  if (!nn || nn === entry.name) return;
  try {
    const res = await api.renameFile(inst.id, entry.path, nn);
    if (state.editor.path === entry.path) state.editor.path = res.path;   // keep editor valid
    toast(`Renamed to ${nn}`, 'ok');
    loadFiles(state.files.path);
  } catch (err) {
    toast(describe(err), 'error');
  }
}

async function extractEntry(entry) {
  const inst = curInst();
  if (!inst) return;
  try {
    let res = await api.extractFile(inst.id, entry.path, false);
    if (!res.extracted && res.conflicts) {
      const sample = res.conflicts.slice(0, 4).join(', ') + (res.conflict_count > 4 ? '…' : '');
      const ok = await confirmDialog({
        title: 'Overwrite existing files?',
        description: `Extracting ${entry.name} would overwrite ${res.conflict_count} existing file(s): ${sample}. Overwrite them?`,
        confirmText: 'Overwrite',
      });
      if (!ok) return;
      res = await api.extractFile(inst.id, entry.path, true);
    }
    toast(`Extracted ${res.count} file${res.count === 1 ? '' : 's'} from ${entry.name}`, 'ok');
    loadFiles(state.files.path);
  } catch (err) {
    toast(describe(err), 'error');
  }
}

/* --- files: right-click context menu ------------------------------------- */

let _ctxCleanup = null;
function closeContextMenu() {
  document.querySelector('.ctx-menu')?.remove();
  if (_ctxCleanup) { _ctxCleanup(); _ctxCleanup = null; }
}
function showContextMenu(ev, entry) {
  ev.preventDefault();
  closeContextMenu();                       // replaces any open menu + its listeners
  const live = isOnline(curInst());
  const gated = (fn) => () => {
    closeContextMenu();
    if (!live) { toast("Node offline. Can't reach the agent.", 'error'); return; }
    fn();
  };
  const items = [];
  if (!entry.is_dir) items.push(['Open', gated(() => openFile(entry))]);
  items.push(['Download', gated(() => downloadEntry(entry))]);
  if (entry.is_dir) items.push(['Upload here…', gated(() => pickUpload(entry.path))]);
  items.push(['Rename…', gated(() => renameEntry(entry))]);
  if (!entry.is_dir && isArchiveName(entry.name)) items.push(['Extract', gated(() => extractEntry(entry))]);
  items.push(['Delete', gated(() => deleteEntry(entry)), true]);

  const menu = el('div.ctx-menu', {},
    items.map(([label, fn, danger]) => el(`div.ctx-item${danger ? '.danger' : ''}`, { onclick: fn }, label)));
  menu.style.left = `${ev.clientX}px`;
  menu.style.top = `${ev.clientY}px`;
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  if (r.right > innerWidth) menu.style.left = `${innerWidth - r.width - 8}px`;
  if (r.bottom > innerHeight) menu.style.top = `${innerHeight - r.height - 8}px`;

  // Registered after the opening event, so it cannot self-close.
  const onDown = (e) => { if (!menu.contains(e.target)) closeContextMenu(); };
  const onKey = (e) => { if (e.key === 'Escape') closeContextMenu(); };
  const onScroll = () => closeContextMenu();
  document.addEventListener('mousedown', onDown, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('scroll', onScroll, true);
  _ctxCleanup = () => {
    document.removeEventListener('mousedown', onDown, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('scroll', onScroll, true);
  };
}

/* --- files: fullscreen editor -------------------------------------------- */

function toggleFullscreen() {
  state.editorFullscreen = !state.editorFullscreen;
  const inst = curInst();
  if (inst) renderFiles(inst, isOnline(inst));
}

/** Repaint the line-number gutter and the syntax overlay behind the textarea. */
function paintEditor() {
  const area = $('editor-area');
  const lines = area.value.split('\n');
  $('editor-gutter').textContent = lines.map((_, i) => i + 1).join('\n');

  const hl = clear($('editor-hl'));
  lines.forEach((line, i) => {
    const isComment = /^\s*[#;]|^\s*\/\//.test(line);
    hl.append(isComment ? el('span.cmt', { text: line }) : document.createTextNode(line));
    if (i < lines.length - 1) hl.append(document.createTextNode('\n'));
  });
  hl.append(document.createTextNode('\n​'));
  syncEditorScroll();
}

function syncEditorScroll() {
  const area = $('editor-area');
  $('editor-hl').style.transform = `translate(${-area.scrollLeft}px, ${-area.scrollTop}px)`;
  $('editor-gutter').scrollTop = area.scrollTop;
}

/* --- settings tab -------------------------------------------------------- */

/** Keep a switch's class and its announced state in step. */
function setToggle(node, on) {
  node.classList.toggle('on', !!on);
  node.setAttribute('aria-checked', on ? 'true' : 'false');
}

function fillSettings(inst) {
  $('set-name').value = inst.name;
  $('set-type').value = inst.type;
  $('set-root').value = inst.root_dir;
  $('set-cmd').value = inst.start_command;
  $('set-jar').value = inst.jar_path || '';
  fillJavaSetting(inst);
  $('set-restart').value = inst.restart_policy || 'on_crash';
  setToggle($('set-startwith'), inst.start_with_node);
}

async function saveInstance() {
  const inst = curInst();
  if (!inst) return;
  const changes = {
    name: $('set-name').value.trim(),
    type: $('set-type').value,
    root_dir: $('set-root').value.trim(),
    start_command: $('set-cmd').value.trim(),
    // Empty means: derive it from the start command (hub serverjar.py).
    jar_path: $('set-jar').value.trim() || null,
    // Empty means: use whatever java the node's PATH resolves to.
    java_home: javaSettingValue(),
    restart_policy: $('set-restart').value,
    start_with_node: $('set-startwith').classList.contains('on'),
  };
  if (!changes.name || !changes.root_dir || !changes.start_command) {
    toast('Name, root directory and start command are all required.', 'error');
    return;
  }
  const btn = $('set-save');
  btn.disabled = true;
  try {
    const updated = await api.updateInstance(inst.id, changes);
    const list = instancesOf(inst.node_id);
    const idx = list.findIndex((i) => i.id === inst.id);
    if (idx >= 0) list[idx] = updated;
    toast('Instance updated. Root/start-command changes apply on next start.', 'ok');
    render();
  } catch (err) {
    toast(describe(err), 'error');
  } finally {
    btn.disabled = false;
  }
}

/* --- version tab --------------------------------------------------------- */

const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s);

/** Load the available versions for the instance's software (from the hub). */
async function loadVersion(inst) {
  const v = state.version;
  Object.assign(v, {
    instId: inst.id, loading: true, error: null,
    versions: [], builds: [], selVersion: null, selBuild: null,
    software: inst.type, label: cap(inst.type), hasBuilds: inst.type !== 'vanilla',
  });
  render();
  try {
    const res = await api.listSoftwareVersions(inst.type);
    if (state.sel.instId !== inst.id) return;
    v.software = res.software;
    v.label = res.label;
    v.hasBuilds = res.has_builds;
    v.versions = res.versions || [];
    // Default to the installed version if it's still offered, else the newest.
    const cur = inst.version && v.versions.find((x) => x.id === inst.version);
    v.selVersion = (cur ? cur.id : v.versions[0]?.id) || null;
    v.loading = false;
    if (v.hasBuilds && v.selVersion) await loadBuilds(inst, v.selVersion, inst.build);
    else render();
  } catch (err) {
    if (state.sel.instId !== inst.id) return;
    v.loading = false;
    v.error = describe(err);
    render();
  }
}

/** Refresh the build list when the selected version changes. */
async function loadBuilds(inst, version, preferBuild = null) {
  const v = state.version;
  v.buildsLoading = true;
  v.builds = [];
  v.selBuild = null;
  render();
  try {
    const res = await api.listSoftwareBuilds(inst.type, version);
    if (state.sel.instId !== inst.id || v.selVersion !== version) return;
    v.builds = res.builds || [];
    const pref = preferBuild && v.builds.find((b) => b.id === String(preferBuild));
    v.selBuild = (pref ? pref.id : v.builds[0]?.id) || null;
  } catch (err) {
    if (state.sel.instId === inst.id) v.error = describe(err);
  } finally {
    v.buildsLoading = false;
    render();
  }
}

function renderVersion(inst, live) {
  const v = state.version;
  const run = runStateOf(inst);
  const updating = run === 'updating' || v.updating;

  show($('ver-updating'), updating);

  // Current
  $('ver-cur-ver').textContent = inst.version || 'not detected yet';
  const curBuild = inst.build ? `build ${inst.build}` : '';
  $('ver-cur-build').textContent = curBuild;
  show($('ver-cur-build'), !!curBuild);

  // Adaptive labels
  $('ver-version-label').textContent = inst.type === 'paper' ? 'Minecraft Version' : 'Version';
  $('ver-build-label').textContent = `${v.label || cap(inst.type)} Build`;

  // Version select
  const vsel = $('ver-version');
  if (v.loading) {
    repaint(vsel, '__loading__', () => el('option', {}, 'Loading…'));
    vsel.disabled = true;
  } else if (v.error && !v.versions.length) {
    repaint(vsel, '__error__', () => el('option', {}, 'Failed to load'));
    vsel.disabled = true;
  } else {
    repaint(vsel, v.versions.map((x) => x.id).join('|'),
      () => v.versions.map((x) => el('option', { value: x.id }, x.label)));
    if (v.selVersion) vsel.value = v.selVersion;
    vsel.disabled = updating;
  }

  // Build select (only when the software exposes builds)
  show($('ver-build-field'), !!v.hasBuilds);
  if (v.hasBuilds) {
    const bsel = $('ver-build');
    if (v.buildsLoading) {
      repaint(bsel, '__loading__', () => el('option', {}, 'Loading…'));
      bsel.disabled = true;
    } else if (!v.builds.length) {
      repaint(bsel, '__empty__', () => el('option', {}, 'no builds'));
      bsel.disabled = true;
    } else {
      repaint(bsel, v.builds.map((b) => b.id).join('|'),
        () => v.builds.map((b) => el('option', { value: b.id },
          b.channel && b.channel !== 'STABLE' ? `${b.label} · ${b.channel.toLowerCase()}` : b.label)));
      if (v.selBuild) bsel.value = v.selBuild;
      bsel.disabled = updating;
    }
  }

  // Update button + reason
  const canPick = !!v.selVersion && (!v.hasBuilds || !!v.selBuild);
  let reason = '';
  if (!live) reason = "Node offline. Can't reach the agent.";
  else if (updating) reason = 'Update in progress…';
  else if (['running', 'starting', 'stopping'].includes(run)) reason = 'Stop the server before updating.';
  else if (v.error && !v.versions.length) reason = v.error;
  else if (!canPick) reason = 'Select a version to update.';

  const btn = $('ver-update');
  btn.disabled = !!reason || !canPick;
  btn.textContent = updating ? 'Updating…' : 'Update';
  $('ver-status').textContent = reason;
}

async function doUpdateVersion() {
  const inst = curInst();
  const v = state.version;
  if (!inst || !v.selVersion || (v.hasBuilds && !v.selBuild)) return;
  v.updating = true;
  render();
  try {
    const res = await api.updateInstanceVersion(inst.id, v.selVersion, v.hasBuilds ? v.selBuild : null);
    inst.version = res.version;
    inst.build = res.build ?? null;
    toast(`Updated ${inst.name} to ${res.version}${res.build ? ' build ' + res.build : ''}`, 'ok');
  } catch (err) {
    toast(describe(err), 'error');
  } finally {
    v.updating = false;
    render();
  }
}

/* --- memory helpers ------------------------------------------------------- */

// The launch line is composed hub-side (serverjar.py) for both kinds of create;
// this side only sends `type` and `memory`.

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

/**
 * Interpret a memory string ("6G", "6144M") as a GB number for the slider.
 * The unit is required: a bare number means bytes to the JVM.
 */
function ramToGB(val) {
  const m = String(val || '').trim().match(/^(\d+)\s*([gGmMkK])$/);
  if (!m) return null;
  let n = parseFloat(m[1]);
  const unit = m[2].toUpperCase();
  if (unit === 'M') n /= 1024;
  else if (unit === 'K') n /= 1024 * 1024;
  return n;
}

/* --- create / delete ----------------------------------------------------- */

async function addNode() {
  const values = await dialog({
    title: 'Add a node',
    description: 'Creates the node record and mints a one-time enrollment token for its agent.',
    fields: [{ name: 'name', label: 'Node name', placeholder: 'box-a' }],
    confirmText: 'Create node',
  });
  if (!values || !values.name.trim()) return;
  try {
    const res = await api.createNode(values.name.trim());
    state.enrollment = {
      nodeId: res.node_id,
      name: values.name.trim(),
      token: res.enrollment_token,
      expiresIn: res.expires_in_s,
      mintedAt: Date.now(),
    };
    selectHub();
    await refreshNodes({ quiet: true });
  } catch (err) {
    toast(describe(err), 'error');
  }
}

async function reenrollNode(node) {
  const ok = await confirmDialog({
    title: `Re-enroll ${node.name}?`,
    description: 'Mints a fresh enrollment token and invalidates the current agent credential. The node stays offline until its agent re-enrolls.',
    confirmText: 'Mint new token',
  });
  if (!ok) return;
  try {
    const res = await api.reenrollNode(node.id);
    state.enrollment = {
      nodeId: node.id,
      name: node.name,
      token: res.enrollment_token,
      expiresIn: res.expires_in_s,
      mintedAt: Date.now(),
    };
    await refreshNodes({ quiet: true });
  } catch (err) {
    toast(describe(err), 'error');
  }
}

async function deleteNode(node) {
  const insts = instancesOf(node.id);
  const ok = await confirmDialog({
    title: `Delete ${node.name}?`,
    description: `Removes the node and its ${insts.length} instance record${insts.length === 1 ? '' : 's'} from the hub. Server files on the machine are left untouched.`,
    confirmText: 'Delete node',
  });
  if (!ok) return;
  try {
    await api.deleteNode(node.id);
    if (state.enrollment?.nodeId === node.id) state.enrollment = null;
    selectHub();
    await refreshNodes({ quiet: true });
    toast(`${node.name} deleted`, 'ok');
  } catch (err) {
    toast(describe(err), 'error');
  }
}

const TYPE_OPTIONS = [
  { value: 'paper', label: 'Paper' },
  { value: 'vanilla', label: 'Vanilla' },
  { value: 'velocity', label: 'Velocity' },
];

const RESTART_OPTIONS = [
  { value: 'never', label: 'Leave it down' },
  { value: 'on_crash', label: 'Restart it after a crash' },
  { value: 'always', label: 'Always restart it' },
];

const RESTART_HINT = 'Stop and Kill always work, whatever this is set to.';

/** Software governed by Mojang's EULA; a proxy holds no world and needs none. */
const EULA_TYPES = ['paper', 'vanilla'];

/** Mirrors the hub's rule: `-Xmx` takes a whole number and a unit. */
const MEMORY_RE = /^\d+[KMGkmg]$/;

/** Mirrors the hub's rule, so a bad name is caught next to the field. */
const SERVER_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$/;

/** Pair a memory text field with a 1-32 GB slider, both editing one value. */
function attachRamSlider(ramEl, set) {
  ramEl.classList.add('ram-value');
  const slider = el('input.ram-slider', { type: 'range', min: '1', max: '32', step: '1' });
  const row = el('div.ram-row');
  ramEl.parentNode.insertBefore(row, ramEl);
  row.append(slider, ramEl);

  const syncFromText = () => {
    const gb = ramToGB(ramEl.value);
    if (gb != null) slider.value = String(clamp(Math.round(gb), 1, 32));
  };
  syncFromText();
  slider.addEventListener('input', () => set('ram', `${slider.value}G`));
  ramEl.addEventListener('input', syncFromText);
}

/**
 * Add an instance: either provision a new server or declare one already on the
 * node. One dialog, one pill selector, the fields of the chosen mode shown.
 */
async function addInstance(node) {
  const hasRoot = !!node.root_dir;
  const values = await dialog({
    title: `Add instance on ${node.name}`,
    confirmText: 'Create',
    fields: [
      { name: 'mode', label: 'Mode', type: 'segmented', value: hasRoot ? 'new' : 'existing',
        options: [
          { value: 'new', label: 'Create new server' },
          { value: 'existing', label: 'Add existing server' },
        ] },
      { name: 'name', label: 'Name', placeholder: 'survival' },
      { name: 'type', label: 'Type', type: 'select', value: 'paper', options: TYPE_OPTIONS },
      { name: 'version', label: 'Version', type: 'select', options: [] },
      { name: 'build', label: 'Build', type: 'select', options: [] },
      { name: 'root_dir', label: 'Root directory', placeholder: '/srv/minecraft/survival',
        mono: true, hint: 'Must already exist on the node, with the server in it.' },
      { name: 'java', label: 'Java', type: 'select',
        options: [{ value: '', label: 'Looking for Java on the node…' }] },
      { name: 'ram', label: 'Max memory (-Xmx)', value: '4G', mono: true,
        hint: 'Drag the slider, or type an exact value such as 6144M.' },
      { name: 'restart_policy', label: 'If the server exits on its own', type: 'select',
        value: 'on_crash', options: RESTART_OPTIONS, hint: RESTART_HINT },
      { name: 'accept_eula', label: 'I accept the Minecraft EULA', type: 'toggle', value: false,
        hint: 'Required for a Minecraft server. Writes eula=true into the new '
            + 'server directory.' },
      { name: 'run_after', label: 'Run server immediately', type: 'toggle', value: false,
        hint: 'Off by default: the server is left with its generated configuration, '
            + 'before any world is created.' },
    ],
    validate: (v) => {
      if (!v.name.trim()) return 'A name is required.';
      if (!MEMORY_RE.test(v.ram.trim())) {
        return 'Max memory must be a whole number with a unit, such as 4G or 6144M.';
      }
      if (v.mode === 'existing') {
        return v.root_dir.trim() ? null : 'A root directory is required.';
      }
      if (!SERVER_NAME_RE.test(v.name.trim())) {
        return 'A name may use letters, digits, dot, dash and underscore only, and '
             + 'must start with a letter or digit.';
      }
      if (!hasRoot) return 'This node has no root directory configured, so it cannot host new servers.';
      if (!v.version) return 'Pick a version.';
      if (EULA_TYPES.includes(v.type) && !v.accept_eula) {
        return 'The Minecraft EULA must be accepted before a server can be created.';
      }
      return null;
    },
    onMount: ({ get, set, setOptions, input, row }) => {
      attachRamSlider(input('ram'), set);

      const target = el('div.field-note');
      row('name').append(target);
      const showTarget = () => {
        const name = get('name').trim();
        target.textContent = hasRoot && name
          ? `Created in ${node.root_dir}/${name}`
          : hasRoot ? `Created under ${node.root_dir}` : '';
      };
      input('name').addEventListener('input', showTarget);

      let hasBuilds = false;
      const newOnly = ['version', 'build', 'accept_eula', 'run_after'];

      const applyMode = () => {
        const isNew = get('mode') === 'new';
        for (const f of newOnly) show(row(f), isNew);
        show(row('build'), isNew && hasBuilds);
        show(row('accept_eula'), isNew && EULA_TYPES.includes(get('type')));
        show(row('root_dir'), !isNew);
        show(target, isNew);
      };

      // Two catalog fetches can be in flight after a quick type switch; each
      // takes a ticket and drops its result if a later one has started.
      let fetchSeq = 0;

      const loadBuilds = async (software, version, ticket) => {
        setOptions('build', [{ value: '', label: 'Loading…' }]);
        try {
          const res = await api.listSoftwareBuilds(software, version);
          if (ticket !== fetchSeq) return;
          setOptions('build', (res.builds || []).map((b) => ({ value: b.id, label: b.label })));
        } catch {
          if (ticket === fetchSeq) {
            setOptions('build', [{ value: '', label: 'Could not load builds' }]);
          }
        }
      };

      const loadVersions = async (software) => {
        const ticket = ++fetchSeq;
        setOptions('version', [{ value: '', label: 'Loading…' }]);
        setOptions('build', []);
        try {
          const res = await api.listSoftwareVersions(software);
          if (ticket !== fetchSeq) return;
          const versions = res.versions || [];
          hasBuilds = !!res.has_builds;
          setOptions('version', versions.map((v) => ({ value: v.id, label: v.label })));
          applyMode();
          if (hasBuilds && versions.length) await loadBuilds(software, versions[0].id, ticket);
        } catch (err) {
          if (ticket !== fetchSeq) return;
          setOptions('version', [{ value: '', label: 'Could not load versions' }]);
          toast(describe(err), 'error');
        }
      };

      // Java: listed against the chosen version so too-old runtimes say so.
      // The user's pick survives a re-list; until they pick, it follows the
      // node default (or the only / recommended runtime when there is none).
      const javaNoteEl = el('div.field-note');
      row('java').append(javaNoteEl);
      let javaSeq = 0;
      let javaRes = null;
      let javaPicked = false;
      const javaWhat = () => (get('mode') === 'new' && get('version')
        ? `${cap(get('type'))} ${get('version')}` : null);
      const paintJavaNote = () => showJavaNote(javaNoteEl, input('java'),
        javaNote(javaRes, get('java'), javaWhat(), 'Can be changed later in Settings.'));
      const loadJava = async () => {
        const ticket = ++javaSeq;
        const what = javaWhat();
        let res;
        try {
          res = await api.listJava(node.id, what ? get('type') : null, what ? get('version') : null);
        } catch (err) {
          if (ticket !== javaSeq) return;
          // Still creatable: it falls back to the node default.
          javaRes = null;
          setOptions('java', [{ value: '', label: 'Node default' }], '');
          showJavaNote(javaNoteEl, input('java'),
            { text: `Couldn't list this node's Java: ${describe(err)}` });
          return;
        }
        if (ticket !== javaSeq) return;
        javaRes = res;
        const { options, preferred } = javaChoices(res);
        const keep = javaPicked && options.some((o) => o.value === get('java'));
        setOptions('java', options, keep ? get('java') : preferred);
        markJavaOptions(input('java'), options);
        paintJavaNote();
      };
      input('java').addEventListener('change', () => { javaPicked = true; paintJavaNote(); });

      input('mode').addEventListener('change', () => { applyMode(); loadJava(); });
      input('type').addEventListener('change', async () => {
        applyMode();                 // the EULA question is per type; answer now
        await loadVersions(get('type'));
        loadJava();
      });
      input('version').addEventListener('change', () => {
        if (hasBuilds) loadBuilds(get('type'), get('version'), fetchSeq);
        loadJava();
      });

      showTarget();
      applyMode();
      if (hasRoot) loadVersions(get('type')).then(loadJava);
      else loadJava();
    },
  });
  if (!values) return;

  const name = values.name.trim();
  try {
    const created = values.mode === 'new'
      ? await api.createServer(node.id, {
          name,
          type: values.type,
          version: values.version,
          build: values.build || null,
          memory: values.ram,
          java_home: values.java || null,
          restart_policy: values.restart_policy,
          accept_eula: values.accept_eula,
          run_after: values.run_after,
        })
      : await api.createInstance(node.id, {
          name,
          type: values.type,
          root_dir: values.root_dir.trim(),
          memory: values.ram,
          java_home: values.java || null,
          restart_policy: values.restart_policy,
        });
    // 202: the row is declared and the build continues hub-side. The outcome
    // arrives as provision.done; until then the console carries the first run.
    if (values.mode === 'new') state.provisioning.add(created.id);
    await refreshNodes({ quiet: true });
    selectInstance(node.id, created.id);
    toast(values.mode === 'new' ? `Setting ${created.name} up…` : `${created.name} created`, 'ok');
  } catch (err) {
    toast(describe(err), 'error');
  }
}

async function deleteInstance(inst) {
  const ok = await confirmDialog({
    title: `Delete ${inst.name}?`,
    description: 'Removes the instance and its secrets from the hub. Files under its root directory are left untouched on the node.',
    confirmText: 'Delete instance',
  });
  if (!ok) return;
  try {
    await api.deleteInstance(inst.id);
    const nodeId = inst.node_id;
    await refreshNodes({ quiet: true });
    selectNode(nodeId);
    toast(`${inst.name} deleted`, 'ok');
  } catch (err) {
    toast(describe(err), 'error');
  }
}

/* --- formatting ---------------------------------------------------------- */

function formatSize(bytes) {
  if (bytes === null || bytes === undefined) return '';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = bytes / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

function relTime(iso) {
  if (!iso) return 'never';
  // The hub serialises naive UTC timestamps, which Date.parse would read as
  // local time. Pin those to UTC.
  const hasZone = /[Zz]$|[+-]\d{2}:?\d{2}$/.test(iso);
  const then = Date.parse(hasZone ? iso : `${iso}Z`);
  if (Number.isNaN(then)) return 'unknown';
  const s = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (s < 10) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function joinPath(dir, name) {
  const clean = name.replace(/^\.?\//, '');
  return dir === '.' ? clean : `${dir}/${clean}`;
}

/* --- wiring -------------------------------------------------------------- */

/** Open or close the small-screen navigation drawer. */
function setNav(open) {
  $('sidebar').classList.toggle('open', open);
  $('nav-scrim').hidden = !open;
  $('nav-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) $('filter').focus();
}

function bind() {
  $('hub-origin').textContent = originLabel;

  $('btn-add-node').onclick = addNode;
  $('account').onclick = toggleAccountMenu;
  $('acct-signout').onclick = () => {
    const url = state.account?.logout_url;
    closeAccountMenu();
    if (url) location.assign(url);
  };
  $('row-hub').onclick = selectHub;
  $('row-hub').onkeydown = (ev) => {
    if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectHub(); }
  };

  $('filter').oninput = (ev) => { state.filter = ev.target.value; renderSidebar(); };

  // Below the sidebar breakpoint the tree is a drawer: toggle, scrim, Escape.
  // navigate() closes it on any selection.
  $('nav-toggle').onclick = () => setNav(!$('sidebar').classList.contains('open'));
  $('nav-scrim').onclick = () => setNav(false);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && $('sidebar').classList.contains('open')) setNav(false);
  });

  $('node-reenroll').onclick = () => { const n = curNode(); if (n) reenrollNode(n); };
  $('node-delete').onclick = () => { const n = curNode(); if (n) deleteNode(n); };
  $('node-add-inst').onclick = () => { const n = curNode(); if (n) addInstance(n); };
  $('node-java-rescan').onclick = () => { const n = curNode(); if (n) loadNodeJava(n.id); };
  $('node-java-add').onclick = () => { const n = curNode(); if (n) addJavaPath(n); };
  $('node-java-install').onclick = () => { const n = curNode(); if (n) installJava(n); };

  $('act-start').onclick = () => { const i = curInst(); if (i) doPower(i, 'start'); };
  $('act-stop').onclick = () => { const i = curInst(); if (i) doPower(i, 'stop'); };
  $('act-restart').onclick = () => { const i = curInst(); if (i) doPower(i, 'restart'); };
  $('act-kill').onclick = () => { const i = curInst(); if (i) doPower(i, 'kill'); };

  $('inst-armed').onclick = () => {
    if (state.tab === 'settings') return;
    state.tab = 'settings';
    render();
    loadTab();
  };

  const tabs = [...document.querySelectorAll('.tab')];
  const openTab = async (tab) => {
    if (state.tab === tab.dataset.tab) return;
    if (state.tab === 'files' && await maybeDiscard()) return;
    state.tab = tab.dataset.tab;
    render();
    loadTab();
  };
  tabs.forEach((tab, i) => {
    tab.onclick = () => openTab(tab);
    tab.onkeydown = (ev) => {
      const step = ev.key === 'ArrowRight' ? 1 : ev.key === 'ArrowLeft' ? -1 : 0;
      if (!step) return;
      ev.preventDefault();
      const next = tabs[(i + step + tabs.length) % tabs.length];
      next.focus();
      openTab(next);
    };
  });

  // Console
  $('console-line').onkeydown = (ev) => { if (ev.key === 'Enter') sendConsole(); };
  $('console-send').onclick = sendConsole;
  $('term-autoscroll').onclick = () => {
    state.autoscroll = !state.autoscroll;
    if (state.autoscroll) $('term-body').scrollTop = $('term-body').scrollHeight;
    render();
  };
  $('term-clear').onclick = () => {
    const inst = curInst();
    if (!inst) return;
    state.console.set(inst.id, []);
    $('term-body').dataset.sig = '';
    render();
  };
  // Scrolling up turns autoscroll off so a streaming log stays readable.
  $('term-body').onscroll = (ev) => {
    const b = ev.target;
    const atBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 24;
    if (state.autoscroll !== atBottom) {
      state.autoscroll = atBottom;
      $('term-autoscroll').setAttribute('aria-pressed', atBottom ? 'true' : 'false');
    }
  };

  // Files
  $('files-refresh').onclick = () => loadFiles(state.files.path);
  $('files-new').onclick = newFile;
  $('files-upload').onclick = () => pickUpload(state.files.path);
  $('files-download').onclick = downloadCurrentFolder;
  $('files-file-input').onchange = onFilePicked;
  $('editor-fullscreen').onclick = toggleFullscreen;
  $('editor-save').onclick = saveFile;

  // Dropping on the list background uploads into the current folder; folder
  // rows handle their own drops and stop propagation.
  const flist = $('files-list');
  const hasFiles = (ev) => ev.dataTransfer && [...ev.dataTransfer.types].includes('Files');
  flist.addEventListener('dragover', (ev) => { if (hasFiles(ev)) { ev.preventDefault(); flist.classList.add('drop-hi'); } });
  flist.addEventListener('dragleave', (ev) => { if (ev.target === flist) flist.classList.remove('drop-hi'); });
  flist.addEventListener('drop', (ev) => {
    flist.classList.remove('drop-hi');
    if (hasFiles(ev)) handleDrop(ev, state.files.path);
  });
  const area = $('editor-area');
  area.oninput = () => {
    paintEditor();
    const inst = curInst();
    if (inst && state.tab === 'files') renderFiles(inst, isOnline(inst));
  };
  area.onscroll = syncEditorScroll;
  area.onkeydown = (ev) => {
    if (ev.key === 'Tab') {
      ev.preventDefault();
      const { selectionStart: s, selectionEnd: e } = area;
      area.value = area.value.slice(0, s) + '  ' + area.value.slice(e);
      area.selectionStart = area.selectionEnd = s + 2;
      area.dispatchEvent(new Event('input'));
    } else if ((ev.ctrlKey || ev.metaKey) && ev.key === 's') {
      ev.preventDefault();
      saveFile();
    }
  };

  // Version
  $('ver-version').onchange = (ev) => {
    const inst = curInst();
    if (!inst) return;
    state.version.selVersion = ev.target.value;
    if (state.version.hasBuilds) loadBuilds(inst, ev.target.value);
    else render();
  };
  $('ver-build').onchange = (ev) => { state.version.selBuild = ev.target.value; render(); };
  $('ver-update').onclick = doUpdateVersion;

  // Settings
  $('set-startwith').onclick = () =>
    setToggle($('set-startwith'), !$('set-startwith').classList.contains('on'));
  $('set-save').onclick = saveInstance;
  $('set-delete').onclick = () => { const i = curInst(); if (i) deleteInstance(i); };

  window.addEventListener('hashchange', () => {
    const sel = hashToSel();
    if (selToHash(sel) !== selToHash(state.sel)) navigate(sel, { push: false });
  });

  window.addEventListener('beforeunload', (ev) => {
    if (state.editor.path !== null && $('editor-area').value !== state.editor.original) {
      ev.preventDefault();
      ev.returnValue = '';
    }
  });
}

async function boot() {
  bind();
  state.sel = hashToSel();
  render();
  refreshHealth();
  // File-explorer thresholds; falls back to the defaults above.
  try { state.config = { ...state.config, ...(await api.config()) }; } catch { /* keep defaults */ }
  setInterval(refreshHealth, HEALTH_MS);
  // Keep "last seen" strings fresh without hammering the API.
  setInterval(() => { if (state.sel.type !== 'instance') render(); }, 20000);
  await refreshNodes();
  if (state.sel.type === 'instance') loadTab();
}

boot();
