/* MineManager web UI.
 *
 * Two-layer rule from docs/UI_CONTEXT.md: REST for actions and declared state,
 * WebSocket for live streams. Declared state (nodes, instances) is fetched and
 * refreshed on a slow poll; runtime state (run states, console output) is owned
 * by the per-node event sockets.
 */

import { api, ApiError, describe, originLabel, wsOrigin } from './api.js';
import { $, clear, confirmDialog, copyText, dialog, el, fill, show, toast } from './dom.js';
import { NodeEvents } from './events.js';

/* --- constants ----------------------------------------------------------- */

const META = {
  running: { label: 'RUNNING', color: '#47d18a', anim: 'none' },
  stopped: { label: 'STOPPED', color: '#6b6b67', anim: 'none' },
  starting: { label: 'STARTING', color: '#f0b429', anim: 'mm-pulse 1.2s infinite' },
  stopping: { label: 'STOPPING', color: '#f0b429', anim: 'mm-pulse 1.2s infinite' },
  crashed: { label: 'CRASHED', color: '#ff5c54', anim: 'mm-pulse 1.6s infinite' },
  updating: { label: 'UPDATING', color: '#5ac8fa', anim: 'mm-pulse 1.2s infinite' },
  unknown: { label: 'UNKNOWN', color: '#6b6b67', anim: 'none' },
};
const metaOf = (s) => META[s] || META.unknown;

const CONSOLE_CAP = 1500;
// Files at/under this size use the simple base64 path (fast, no pill); larger
// ones (and all directories) use the streaming transfer with a progress pill.
const STREAM_THRESHOLD = 4 * 1024 * 1024;
const REFRESH_MS = 10000;
const REFRESH_FAST_MS = 3000;
const HEALTH_MS = 15000;

/* --- state --------------------------------------------------------------- */

const state = {
  nodes: [],
  instances: new Map(),   // nodeId -> InstanceOut[]
  runState: new Map(),    // instanceId -> RunState
  console: new Map(),     // instanceId -> line[]
  consoleLoaded: new Set(), // instanceIds we've backfilled history for
  consoleFailed: new Set(), // instanceIds whose history backfill failed
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
  // Version tab (catalog is provider-driven; software-agnostic).
  version: {
    instId: null, loading: false, error: null,
    software: null, label: '', hasBuilds: false,
    versions: [], builds: [], buildsLoading: false,
    selVersion: null, selBuild: null, updating: false,
  },
  autoscroll: true,
  scanlines: true,
  loaded: false,
};

const events = new NodeEvents(onNodeEvent, () => renderWsStatus());

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

/** Runtime state of an instance — unknowable while its node is offline. */
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
  if (sel.type === 'instance' && changedInstance) loadTab();
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

/** Ask each online node's agent for the real run-state of its instances, so the
 *  UI shows stopped/running immediately instead of waiting for a heartbeat.
 *  Non-blocking: updates and re-renders as answers arrive. */
function primeStates(nodes) {
  for (const node of nodes.filter((n) => n.online)) {
    api.nodeInstanceStates(node.id).then((res) => {
      let changed = false;
      for (const [iid, st] of Object.entries(res?.states || {})) {
        if (state.runState.get(iid) !== st) { state.runState.set(iid, st); changed = true; }
      }
      if (changed) render();
      // A now-known "running" instance we're viewing should backfill its console.
      if (state.sel.type === 'instance') maybeLoadConsole(state.sel.instId);
    }).catch(() => { /* offline / transient — keep unknown */ });
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

async function refreshHealth() {
  const dot = $('health-dot');
  const text = $('health-text');
  try {
    const h = await api.health();
    dot.style.background = '#47d18a';
    dot.style.boxShadow = '0 0 8px #47d18a88';
    text.textContent = 'hub healthy';
    $('brand-ver').textContent = `v${h.version} · BETA`;
  } catch {
    dot.style.background = '#ff5c54';
    dot.style.boxShadow = '0 0 8px #ff5c5488';
    text.textContent = 'hub unreachable';
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
      // Visually separate each server run in the console.
      if (st === 'starting' && prev !== 'starting') {
        pushConsole(id, '', 'sys');
        pushConsole(id, `session start · ${new Date().toLocaleString()}`, 'sep');
      }
      const detail = frame.data?.detail;
      pushConsole(id, detail ? `${st} — ${detail}` : st, 'sys');
      if (st === 'stopped' || st === 'crashed') pushConsole(id, '', 'sys');  // trailing gap
      if (st === 'running') maybeLoadConsole(id);
      render();
      break;
    }
    case 'heartbeat': {
      // Node-level: refreshes every instance state at once and proves liveness.
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
    $('term-count').textContent = `${buf.length} line${buf.length === 1 ? '' : 's'}`;
    show($('console-hint'), false);
    if (state.autoscroll) body.scrollTop = body.scrollHeight;
  }
}

/**
 * Backfill the console with recent lines from the instance's log, so a fresh
 * web session sees what happened before it connected. Loaded once per instance;
 * the log is the source of truth, so we replace the buffer with its snapshot
 * and let the live stream append from there. Best-effort: any failure is
 * surfaced as a soft banner and never breaks the live console.
 */
async function loadConsoleHistory(inst) {
  if (!inst || state.consoleLoaded.has(inst.id)) return;
  if (!isOnline(inst)) return; // retry when the console is reopened online
  // A stopped instance shows an empty console rather than replaying the last
  // run's log. Not marked loaded, so it backfills if/when it starts running.
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
    const body = $('term-body');
    body.dataset.inst = ''; // force renderConsole to repaint from the new buffer
    if (state.tab === 'console' && state.sel.instId === inst.id) render();
  } catch {
    // Never break the console over history — just flag it.
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
  renderWsStatus();

  show($('view-hub'), state.sel.type === 'hub');
  show($('view-node'), state.sel.type === 'node');
  show($('view-inst'), state.sel.type === 'instance');

  if (state.sel.type === 'hub') renderHub();
  else if (state.sel.type === 'node') renderNode();
  else renderInstance();
}

function renderWsStatus() {
  const dot = $('ws-dot');
  const text = $('ws-text');
  const total = state.nodes.length;
  if (!total) {
    dot.style.background = '#6b6b67';
    text.textContent = 'events ws · no nodes';
    return;
  }
  const open = state.nodes.filter((n) => events.isOpen(n.id)).length;
  const ok = open > 0;
  dot.style.background = ok ? '#47d18a' : '#6b6b67';
  text.textContent = `events ws · ${open}/${total} connected`;
}

function matchesFilter(node) {
  const q = state.filter.trim().toLowerCase();
  if (!q) return { node: true, instances: instancesOf(node.id) };
  const nodeHit = node.name.toLowerCase().includes(q) || (node.hostname || '').toLowerCase().includes(q);
  const insts = instancesOf(node.id).filter((i) => i.name.toLowerCase().includes(q));
  if (nodeHit) return { node: true, instances: instancesOf(node.id) };
  return { node: insts.length > 0, instances: insts };
}

function renderSidebar() {
  const online = state.nodes.filter((n) => n.online).length;
  const instCount = state.nodes.reduce((sum, n) => sum + instancesOf(n.id).length, 0);
  $('side-count').textContent = `${online}/${state.nodes.length} online`;
  $('row-hub-count').textContent = `${instCount} inst`;
  $('row-hub').classList.toggle('sel', state.sel.type === 'hub');

  const tree = clear($('tree'));
  let shown = 0;

  for (const node of state.nodes) {
    const hit = matchesFilter(node);
    if (!hit.node) continue;
    shown++;

    const nodeSelected = state.sel.nodeId === node.id;
    const group = el('div.tree-group');

    group.append(el(`div.row.row-node${nodeSelected && state.sel.type === 'node' ? '.sel-soft' : ''}`, {
      onclick: () => selectNode(node.id),
      title: `${node.name} · ${node.hostname || 'unknown host'}`,
    },
      el('span.row-caret', { text: '▾' }),
      el('span.dot', { style: `background:${node.online ? '#47d18a' : '#6b6b67'};box-shadow:${node.online ? '0 0 8px #47d18a88' : 'none'}` }),
      el('span.row-title', { text: node.name }),
      el('span.row-sub', { text: node.hostname || '' }),
      el('span.spacer'),
      el('span.row-badge', {
        style: `color:${node.online ? '#47d18a' : '#8b8b86'}`,
        text: node.online ? 'ONLINE' : 'OFFLINE',
      })));

    const kids = el('div.tree-kids');
    for (const inst of hit.instances) {
      const m = metaOf(runStateOf(inst));
      const sel = state.sel.type === 'instance' && state.sel.instId === inst.id;
      kids.append(el(`div.row.row-inst${sel ? '.sel' : ''}`, {
        onclick: () => selectInstance(node.id, inst.id),
        title: inst.root_dir,
      },
        el('span.dot.dot-sm', { style: `background:${m.color};animation:${m.anim}` }),
        el('span.row-title-inst', { text: inst.name }),
        el('span.spacer'),
        el('span.row-state', { style: `color:${m.color}`, text: m.label })));
    }
    if (!hit.instances.length) {
      kids.append(el('div.row.row-inst', { style: 'cursor:default' },
        el('span.row-state', { style: 'color:var(--dim-2)', text: 'no instances' })));
    }
    group.append(kids);
    tree.append(group);
  }

  if (!shown) {
    tree.append(el('div.empty', {
      text: !state.loaded ? 'Loading…'
        : state.filter ? 'Nothing matches that filter.'
        : 'No nodes yet — add one to get started.',
    }));
  }
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

  const grid = clear($('node-grid'));
  if (!nodes.length) {
    grid.append(el('div.empty', {
      style: 'grid-column:1/-1;border:1px dashed var(--line);border-radius:6px',
      text: state.loaded ? 'No nodes registered yet.' : 'Loading nodes…',
    }));
  }
  for (const node of nodes) {
    const insts = instancesOf(node.id);
    grid.append(el('div.node-card', { onclick: () => selectNode(node.id) },
      el('div.node-card-head', {},
        el('span.dot', {
          style: `width:9px;height:9px;background:${node.online ? '#47d18a' : '#6b6b67'};box-shadow:${node.online ? '0 0 8px #47d18a88' : 'none'}`,
        }),
        el('span.node-card-name', { text: node.name }),
        el('span.node-card-host', { text: node.hostname || '—' }),
        el('span.spacer'),
        nodeBadge(node)),
      el('div.node-card-meta', {},
        el('div', {}, el('div.k', { text: 'AGENT' }), node.agent_version || '—'),
        el('div', {}, el('div.k', { text: 'LAST SEEN' }), relTime(node.last_seen)),
        el('div', {}, el('div.k', { text: 'INSTANCES' }), String(insts.length))),
      el('div.node-card-insts', {},
        insts.length
          ? insts.map((inst) => el('span.inst-chip', {},
              el('span.dot.dot-sm', { style: `background:${metaOf(runStateOf(inst)).color}` }),
              inst.name))
          : el('span.inst-chip', { style: 'color:var(--dim-2)' }, 'no instances'))));
  }

  fill($('enroll-section'), enrollSection());
}

function nodeBadge(node) {
  const on = node.online;
  const label = on ? 'ONLINE' : node.enrolled ? 'OFFLINE' : 'PENDING';
  const color = on ? '#47d18a' : node.enrolled ? '#8b8b86' : '#f0b429';
  const border = on ? '#47d18a44' : node.enrolled ? '#2d2e33' : '#f0b42955';
  const bg = on ? '#47d18a12' : node.enrolled ? '#1a1b1f' : '#f0b4291a';
  return el('span.pill', { style: `color:${color};border-color:${border};background:${bg}`, text: label });
}

/** The one-time enrollment token card — the token can never be fetched again. */
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
          'and cannot be retrieved again — only re-minted.')),
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
          connected ? 'agent connected — node is online' : 'waiting for agent to connect…',
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

  $('node-dot').style.background = node.online ? '#47d18a' : '#6b6b67';
  $('node-dot').style.boxShadow = node.online ? '0 0 8px #47d18a88' : 'none';
  $('node-name').textContent = node.name;

  const badge = nodeBadge(node);
  $('node-badge').className = badge.className;
  $('node-badge').style.cssText = badge.style.cssText;
  $('node-badge').textContent = badge.textContent;

  fill($('node-meta'),
    el('span', { text: node.hostname || 'hostname unknown' }), el('span.sep', {}, '·'),
    el('span', { text: `agent ${node.agent_version || '—'}` }), el('span.sep', {}, '·'),
    el('span', { text: `last seen ${relTime(node.last_seen)}` }));

  const insts = instancesOf(node.id);
  $('node-inst-label').textContent = `INSTANCES · ${insts.length}`;

  const rows = clear($('node-inst-rows'));
  if (!insts.length) {
    rows.append(el('div.empty', { text: 'No instances on this node yet.' }));
  }
  for (const inst of insts) {
    const run = runStateOf(inst);
    const m = metaOf(run);
    const live = node.online;
    rows.append(el('div.table-row', { onclick: () => selectInstance(node.id, inst.id) },
      el('span.cell-name', { text: inst.name }),
      el('span.cell-type', { text: inst.type }),
      el('span.cell-state', { style: `color:${m.color}` },
        el('span.dot', { style: `width:7px;height:7px;background:${m.color};animation:${m.anim}` }),
        m.label),
      el('span.cell-root', { text: inst.root_dir, title: inst.root_dir }),
      el('span.cell-ctl', {},
        powerButton(inst, 'start', '▶', 'Start', !live || ['running', 'starting'].includes(run), 'go'),
        powerButton(inst, 'stop', '■', 'Stop', !live || run === 'stopped'),
        powerButton(inst, 'restart', '⟳', 'Restart', !live))));
  }

  fill($('node-enroll-section'),
    state.enrollment && state.enrollment.nodeId === node.id ? enrollSection() : null);
}

function powerButton(inst, op, glyph, title, disabled, extra = '') {
  return el(`button.btn-icon${extra ? '.' + extra : ''}`, {
    title, disabled,
    onclick: (ev) => { ev.stopPropagation(); doPower(inst, op, ev.currentTarget); },
  }, glyph);
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
    ['console', 'files', 'settings'].forEach((t) => show($(`pane-${t}`), false));
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
  st.style.cssText = `color:${m.color};border:1px solid ${m.color}55;background:${m.color}14`;
  fill(st, el('span.dot', { style: `width:7px;height:7px;background:${m.color};animation:${m.anim}` }), m.label);

  // Used to show the RCON endpoint; RCON is gone, so surface the installed
  // build instead — the one other piece of per-instance metadata worth a glance.
  $('inst-meta').textContent = inst.version
    ? `${inst.version}${inst.build ? ` build ${inst.build}` : ''}`
    : '';

  // Power controls — gated on reachability, and fully locked during an update.
  const updating = run === 'updating';
  const start = $('act-start');
  start.classList.toggle('btn-start', live && !updating && ['stopped', 'crashed', 'unknown'].includes(run));
  start.disabled = !live || updating || ['running', 'starting'].includes(run);
  $('act-stop').disabled = !live || updating || run === 'stopped';
  $('act-restart').disabled = !live || updating;
  $('act-kill').disabled = !live || updating || run === 'stopped';

  document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('on', t.dataset.tab === state.tab));
  show($('inst-offline'), !live);
  show($('pane-console'), state.tab === 'console');
  show($('pane-files'), state.tab === 'files');
  show($('pane-version'), state.tab === 'version');
  show($('pane-settings'), state.tab === 'settings');

  if (state.tab === 'console') renderConsole(inst, m, live);
  if (state.tab === 'files') renderFiles(inst, live);
  if (state.tab === 'version') renderVersion(inst, live);
}

function renderConsole(inst, m, live) {
  $('term-dot').style.cssText = `width:8px;height:8px;background:${m.color};animation:${m.anim}`;
  $('term-title').textContent = `pty · ${inst.name}`;
  $('console-prompt').textContent = `${inst.name} ›`;

  const buf = state.console.get(inst.id) || [];
  $('term-count').textContent = `${buf.length} line${buf.length === 1 ? '' : 's'}`;
  $('term-autoscroll').classList.toggle('off', !state.autoscroll);
  $('term-scanlines').classList.toggle('off', !state.scanlines);
  show($('term-scan'), state.scanlines);

  const input = $('console-line');
  input.disabled = !live;
  input.placeholder = live ? 'send a console command…' : 'node offline';

  // Only surfaced when the history backfill failed — the live console still works.
  show($('console-hint'), state.consoleFailed.has(inst.id));

  const body = $('term-body');
  if (body.dataset.inst !== inst.id || body.childElementCount !== buf.length) {
    body.dataset.inst = inst.id;
    fill(body, buf.map(lineNode));
    if (state.autoscroll) body.scrollTop = body.scrollHeight;
  }
}

/* --- power / console actions --------------------------------------------- */

async function doPower(inst, op, button) {
  if (op === 'kill') {
    const ok = await confirmDialog({
      title: `Kill ${inst.name}?`,
      description: 'This terminates the process immediately — the world may not be saved. Prefer Stop for a graceful shutdown.',
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
  body.dataset.inst = '';
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
    state.files = { path, entries: [], loading: false, error: "Node offline — can't list files." };
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
  $('editor-fullscreen').title = state.editorFullscreen ? 'Exit fullscreen' : 'Toggle fullscreen editor';

  const list = clear($('files-list'));
  if (state.files.loading) {
    list.append(el('div.empty', {}, 'Loading…'));
  } else if (state.files.error) {
    list.append(el('div.empty', { style: 'color:var(--err-fg)', text: state.files.error }));
  } else {
    if (path !== '.') {
      const parent = segs.slice(0, -1).join('/') || '.';
      list.append(el('div.file-row.dir', { onclick: () => loadFiles(parent) },
        el('span.file-icon', {}, '↰'),
        el('span.file-name', {}, '..')));
    }
    const entries = [...state.files.entries].sort(
      (a, b) => (b.is_dir - a.is_dir) || a.name.localeCompare(b.name));
    if (!entries.length && path === '.') list.append(el('div.empty', {}, 'Empty directory.'));

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
      list.append(el(`div.file-row${f.is_dir ? '.dir' : ''}${selected ? '.sel' : ''}`, attrs,
        el('span.file-icon', {}, f.is_dir ? '▸' : '·'),
        el('span.file-name', { text: f.name }),
        el('span.spacer'),
        el('span.file-size', { text: f.is_dir ? '—' : formatSize(f.size) }),
        el('button.file-act', {
          title: f.is_dir ? 'Download as zip' : 'Download', disabled: !live,
          onclick: (ev) => { ev.stopPropagation(); downloadEntry(f); },
        }, '⇩'),
        el('button.file-del', {
          title: 'Delete', disabled: !live,
          onclick: (ev) => { ev.stopPropagation(); deleteEntry(f); },
        }, '×')));
    }
  }

  // editor header
  $('editor-name').textContent = state.editor.path || '—';
  const dirty = state.editor.path !== null && $('editor-area').value !== state.editor.original;
  show($('editor-dirty'), dirty);
  $('editor-save').disabled = !dirty || !live;
  $('editor-info').textContent = state.editor.path
    ? `${formatSize(new Blob([$('editor-area').value]).size)} · utf-8`
    : '';
  $('editor-area').readOnly = state.editor.path === null || !live;
}

async function openFile(entry) {
  const inst = curInst();
  if (!inst) return;

  // Binary guard by extension — never even try to open these as text.
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
    // Server-side content detection (NUL bytes) — belt and suspenders.
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
    description: `Created under ${state.files.path === '.' ? inst.name + '/' : state.files.path + '/'} — parent directories are created as needed.`,
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

// Extensions we refuse to open as text (the editor is for text). .jar included.
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

/** Download a file, or a directory as a zip. Confirms first (name + size) unless
 *  the caller already asked (binary/large-file "download instead?" flows). */
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
  // Large files and directories stream (with a progress pill); small files take
  // the simple, instant path.
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

/** Upload {file, relPath}[] into destPath. Large files stream (with a progress
 *  pill); small files take the quick base64 path. Refreshes on completion. */
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
  if (!isOnline(curInst())) { toast("Node offline — can't upload.", 'error'); return; }
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
    if (!live) { toast("Node offline — can't reach the agent.", 'error'); return; }
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

  // The mousedown/contextmenu that opened this already fired, so registering now
  // won't self-close; outside interaction dismisses it.
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

function fillSettings(inst) {
  $('set-name').value = inst.name;
  $('set-type').value = inst.type;
  $('set-root').value = inst.root_dir;
  $('set-cmd').value = inst.start_command;
  $('set-jar').value = inst.jar_path || '';
  $('set-java').value = inst.java_home || '';
  $('set-auto').classList.toggle('on', !!inst.auto_restart);
}

async function saveInstance() {
  const inst = curInst();
  if (!inst) return;
  const changes = {
    name: $('set-name').value.trim(),
    type: $('set-type').value,
    root_dir: $('set-root').value.trim(),
    start_command: $('set-cmd').value.trim(),
    // Empty means "derive it from the start command" (see hub serverjar.py).
    jar_path: $('set-jar').value.trim() || null,
    // Empty means "use whatever java the node's PATH resolves to".
    java_home: $('set-java').value.trim() || null,
    auto_restart: $('set-auto').classList.contains('on'),
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

/** Fill a <select> only when its option set actually changed (so heartbeat
 *  re-renders don't disrupt an open dropdown). */
function syncSelect(sel, sig, build) {
  if (sel.dataset.sig !== sig) {
    build();
    sel.dataset.sig = sig;
  }
}

function renderVersion(inst, live) {
  const v = state.version;
  const run = runStateOf(inst);
  const updating = run === 'updating' || v.updating;

  show($('ver-updating'), updating);

  // Current
  $('ver-cur-soft').textContent = v.label || cap(inst.type);
  $('ver-cur-ver').textContent = inst.version || 'not detected yet';
  const curBuild = inst.build ? `${v.label || cap(inst.type)} Build ${inst.build}` : '';
  $('ver-cur-build').textContent = curBuild;
  show($('ver-cur-build'), !!curBuild);

  // Adaptive labels
  $('ver-version-label').textContent = inst.type === 'paper' ? 'Minecraft Version' : 'Version';
  $('ver-build-label').textContent = `${v.label || cap(inst.type)} Build`;

  // Version select
  const vsel = $('ver-version');
  if (v.loading) {
    syncSelect(vsel, '__loading__', () => fill(vsel, el('option', {}, 'Loading…')));
    vsel.disabled = true;
  } else if (v.error && !v.versions.length) {
    syncSelect(vsel, '__error__', () => fill(vsel, el('option', {}, 'Failed to load')));
    vsel.disabled = true;
  } else {
    syncSelect(vsel, v.versions.map((x) => x.id).join('|'), () =>
      fill(vsel, v.versions.map((x) => el('option', { value: x.id }, x.label))));
    if (v.selVersion) vsel.value = v.selVersion;
    vsel.disabled = updating;
  }

  // Build select (only when the software exposes builds)
  show($('ver-build-field'), !!v.hasBuilds);
  if (v.hasBuilds) {
    const bsel = $('ver-build');
    if (v.buildsLoading) {
      syncSelect(bsel, '__loading__', () => fill(bsel, el('option', {}, 'Loading…')));
      bsel.disabled = true;
    } else if (!v.builds.length) {
      syncSelect(bsel, '__empty__', () => fill(bsel, el('option', {}, '—')));
      bsel.disabled = true;
    } else {
      syncSelect(bsel, v.builds.map((b) => b.id).join('|'), () =>
        fill(bsel, v.builds.map((b) => el('option', { value: b.id },
          b.channel && b.channel !== 'STABLE' ? `${b.label} · ${b.channel.toLowerCase()}` : b.label))));
      if (v.selBuild) bsel.value = v.selBuild;
      bsel.disabled = updating;
    }
  }

  // Update button + reason
  const canPick = !!v.selVersion && (!v.hasBuilds || !!v.selBuild);
  let reason = '';
  if (!live) reason = "Node offline — can't reach the agent.";
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

/* --- start-command helpers (jar + memory) -------------------------------- */

// Each server type is launched from a conventionally-named jar.
const JAR_FOR_TYPE = { paper: 'paper.jar', vanilla: 'server.jar', velocity: 'velocity.jar' };
const jarForType = (type) => JAR_FOR_TYPE[type] || 'server.jar';

const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));

/**
 * Build the launch command for a new instance from its type and max memory.
 * The command field is hidden in the add dialog (beginners only pick memory);
 * it can be fine-tuned later in the instance's Settings. Servers take `nogui`;
 * Velocity does not.
 */
function buildStartCommand(type, ram) {
  const xmx = (ram || '').trim() ? `-Xmx${ram.trim()} ` : '';
  const gui = type === 'velocity' ? '' : ' nogui';
  return `java ${xmx}-jar ${jarForType(type)}${gui}`;
}

/** Interpret a memory string ("6G", "6144M", "8") as a GB number for the slider. */
function ramToGB(val) {
  const m = String(val || '').trim().match(/^(\d+(?:\.\d+)?)\s*([gGmMkK])?$/);
  if (!m) return null;
  let n = parseFloat(m[1]);
  const unit = (m[2] || 'G').toUpperCase();
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

async function addInstance(node) {
  // Beginner-friendly: pick a type and memory; the launch command is built from
  // those and can be fine-tuned later in Settings.
  const values = await dialog({
    title: `Add instance on ${node.name}`,
    description: 'Declares a server the agent should manage. The directory must already exist on the node. The launch command is built from the type and memory — fine-tune it later in the instance’s Settings.',
    fields: [
      { name: 'name', label: 'Name', placeholder: 'survival' },
      { name: 'type', label: 'Type', type: 'select', options: ['paper', 'vanilla', 'velocity'], value: 'paper' },
      { name: 'root_dir', label: 'Root directory', placeholder: '/srv/minecraft/survival', mono: true },
      { name: 'ram', label: 'Max memory (-Xmx)', value: '4G', mono: true, hint: 'Drag the slider or type an exact value (e.g. 4G or 6144M).' },
      { name: 'auto_restart', label: 'Auto-restart', type: 'toggle', value: true, hint: 'Restart the server automatically after a crash' },
    ],
    confirmText: 'Create instance',
    onMount: ({ set, input }) => {
      // Pair the memory text field with a beginner-friendly slider (1–32 GB).
      // Both edit the same value and stay in sync with each other.
      const ramEl = input('ram');
      ramEl.classList.add('ram-value');
      const slider = el('input.ram-slider', { type: 'range', min: '1', max: '32', step: '1' });
      const row = el('div.ram-row');
      ramEl.parentNode.insertBefore(row, ramEl);
      row.append(slider, ramEl);

      const syncSliderFromText = () => {
        const gb = ramToGB(ramEl.value);
        if (gb != null) slider.value = String(clamp(Math.round(gb), 1, 32));
      };
      syncSliderFromText();
      slider.addEventListener('input', () => set('ram', `${slider.value}G`));
      ramEl.addEventListener('input', syncSliderFromText);
    },
  });
  if (!values) return;
  if (!values.name.trim() || !values.root_dir.trim()) {
    toast('Name and root directory are both required.', 'error');
    return;
  }
  try {
    const created = await api.createInstance(node.id, {
      name: values.name.trim(),
      type: values.type,
      root_dir: values.root_dir.trim(),
      start_command: buildStartCommand(values.type, values.ram),
      auto_restart: values.auto_restart,
    });
    await refreshNodes({ quiet: true });
    selectInstance(node.id, created.id);
    toast(`${created.name} created`, 'ok');
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
  if (bytes === null || bytes === undefined) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = bytes / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

function relTime(iso) {
  if (!iso) return 'never';
  // The hub stores UTC but serialises naive timestamps ("…T23:49:12.231535"),
  // which Date.parse would read as local time. Pin those to UTC.
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

function bind() {
  $('health-origin').textContent = originLabel;

  $('btn-add-node').onclick = addNode;
  $('hub-add-node').onclick = addNode;
  $('row-hub').onclick = selectHub;

  $('filter').oninput = (ev) => { state.filter = ev.target.value; renderSidebar(); };

  $('node-reenroll').onclick = () => { const n = curNode(); if (n) reenrollNode(n); };
  $('node-delete').onclick = () => { const n = curNode(); if (n) deleteNode(n); };
  $('node-add-inst').onclick = () => { const n = curNode(); if (n) addInstance(n); };

  $('act-start').onclick = () => { const i = curInst(); if (i) doPower(i, 'start'); };
  $('act-stop').onclick = () => { const i = curInst(); if (i) doPower(i, 'stop'); };
  $('act-restart').onclick = () => { const i = curInst(); if (i) doPower(i, 'restart'); };
  $('act-kill').onclick = () => { const i = curInst(); if (i) doPower(i, 'kill'); };

  document.querySelectorAll('.tab').forEach((tab) => {
    tab.onclick = async () => {
      if (state.tab === tab.dataset.tab) return;
      if (state.tab === 'files' && await maybeDiscard()) return;
      state.tab = tab.dataset.tab;
      render();
      loadTab();
    };
  });

  // Console
  $('console-line').onkeydown = (ev) => { if (ev.key === 'Enter') sendConsole(); };
  $('term-autoscroll').onclick = () => {
    state.autoscroll = !state.autoscroll;
    if (state.autoscroll) $('term-body').scrollTop = $('term-body').scrollHeight;
    render();
  };
  $('term-scanlines').onclick = () => { state.scanlines = !state.scanlines; render(); };
  $('term-clear').onclick = () => {
    const inst = curInst();
    if (!inst) return;
    state.console.set(inst.id, []);
    $('term-body').dataset.inst = '';
    render();
  };
  // Turning autoscroll off implicitly when the user scrolls up keeps the log
  // readable while it is still streaming.
  $('term-body').onscroll = (ev) => {
    const b = ev.target;
    const atBottom = b.scrollHeight - b.scrollTop - b.clientHeight < 24;
    if (state.autoscroll !== atBottom) {
      state.autoscroll = atBottom;
      $('term-autoscroll').classList.toggle('off', !state.autoscroll);
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

  // Drag & drop onto the list background uploads into the current folder
  // (folder rows handle their own drops and stop propagation).
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
  $('set-auto').onclick = () => $('set-auto').classList.toggle('on');
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
  // Load configurable file-explorer thresholds (best-effort; defaults otherwise).
  try { state.config = { ...state.config, ...(await api.config()) }; } catch { /* keep defaults */ }
  setInterval(refreshHealth, HEALTH_MS);
  // Keep "last seen" strings honest without hammering the API.
  setInterval(() => { if (state.sel.type !== 'instance') render(); }, 20000);
  await refreshNodes();
  if (state.sel.type === 'instance') loadTab();
}

boot();
