/* REST client for the MineManager hub.
 *
 * No app-layer auth: requests are same-origin behind the reverse proxy. For dev
 * against a hub on another port, serve the UI from localhost, pass
 * `?hub=http://localhost:8730` (kept in localStorage) and start the hub with
 * MM_CORS_ORIGINS set. Ignored when the UI is not served from loopback.
 */

const STORE_KEY = 'mm.hubBase';

// A development aid only. Honoured on a deployed hub it would let one crafted
// link repoint the UI, persistently, at any server: every console command, file
// and enrollment token typed afterwards would go there.
const DEV_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]']);

function resolveBase() {
  if (!DEV_HOSTS.has(location.hostname)) {
    try { localStorage.removeItem(STORE_KEY); } catch { /* storage blocked */ }
    return '';
  }
  const q = new URLSearchParams(location.search).get('hub');
  if (q !== null) {
    const v = q.replace(/\/$/, '');
    if (v) localStorage.setItem(STORE_KEY, v);
    else localStorage.removeItem(STORE_KEY);
    return v;
  }
  return localStorage.getItem(STORE_KEY) || '';
}

/** Base origin of the hub; '' means same-origin. */
export const base = resolveBase();

/** Human-readable origin, shown when the hub is unreachable. */
export const originLabel = base || location.host;

/** ws:// or wss:// origin for the events sockets. */
export function wsOrigin() {
  const http = base || location.origin;
  return http.replace(/^http/, 'ws');
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** Map a failure onto the wording from UI_CONTEXT §7. */
export function describe(err) {
  if (!(err instanceof ApiError)) return err?.message || "Can't reach the hub.";
  switch (err.status) {
    case 400: return err.detail || 'The hub rejected that request.';
    case 404: return 'Not found. It may have been deleted elsewhere.';
    case 409: return "Node offline. Can't reach the agent.";
    case 502: return `Agent error: ${err.detail}`;
    case 504: return 'Timed out talking to the agent. Try again.';
    default: return err.detail || `Request failed (${err.status}).`;
  }
}

/* FastAPI reports a 422 as a list of per-field objects; pull out the messages. */
function errorList(detail) {
  if (!Array.isArray(detail)) return JSON.stringify(detail);
  const msgs = detail
    .map((e) => (e && typeof e.msg === 'string' ? e.msg.replace(/^Value error,\s*/, '') : null))
    .filter(Boolean);
  return msgs.length ? msgs.join(' · ') : JSON.stringify(detail);
}

async function request(method, path, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers['content-type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(base + path, init);
  } catch (cause) {
    throw new Error("Can't reach the hub.", { cause });
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const payload = await res.json();
      if (payload && payload.detail) {
        detail = typeof payload.detail === 'string' ? payload.detail : errorList(payload.detail);
      }
    } catch { /* non-JSON error body; keep the status text */ }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return null;
  const text = await res.text();
  return text ? JSON.parse(text) : null;
}

const get = (p) => request('GET', p);
const post = (p, b) => request('POST', p, b);
const patch = (p, b) => request('PATCH', p, b);
const del = (p) => request('DELETE', p);

const q = encodeURIComponent;

export const api = {
  health: () => get('/api/health'),
  config: () => get('/api/config'),

  // Nodes
  listNodes: () => get('/api/nodes'),
  createNode: (name) => post('/api/nodes', { name }),
  reenrollNode: (nodeId) => post(`/api/nodes/${nodeId}/reenroll`),
  deleteNode: (nodeId) => del(`/api/nodes/${nodeId}`),

  // Instances
  listInstances: (nodeId) => get(`/api/nodes/${nodeId}/instances`),
  nodeInstanceStates: (nodeId) => get(`/api/nodes/${nodeId}/instance-states`),
  createInstance: (nodeId, spec) => post(`/api/nodes/${nodeId}/instances`, spec),
  // Provision a new server: download, install and first run happen on the agent.
  createServer: (nodeId, spec) => post(`/api/nodes/${nodeId}/servers`, spec),
  updateInstance: (id, changes) => patch(`/api/instances/${id}`, changes),
  deleteInstance: (id) => del(`/api/instances/${id}`),

  // Power / console
  power: (id, op) => post(`/api/instances/${id}/power/${op}`),
  console: (id, line) => post(`/api/instances/${id}/console`, { line }),
  consoleHistory: (id, lines = 200) => get(`/api/instances/${id}/console/history?lines=${lines}`),

  // Files (paths are relative to the instance root)
  listFiles: (id, path = '.') => get(`/api/instances/${id}/files?path=${q(path)}`),
  readFile: (id, path) => get(`/api/instances/${id}/files/read?path=${q(path)}`),
  writeFile: (id, path, content) => post(`/api/instances/${id}/files/write`, { path, content }),
  deleteFile: (id, path, recursive = false) =>
    del(`/api/instances/${id}/files?path=${q(path)}&recursive=${recursive}`),
  uploadFile: (id, path, content_b64) =>
    post(`/api/instances/${id}/files/upload`, { path, content_b64 }),
  renameFile: (id, path, newName) =>
    post(`/api/instances/${id}/files/rename`, { path, new_name: newName }),
  extractFile: (id, path, overwrite = false) =>
    post(`/api/instances/${id}/files/extract`, { path, overwrite }),
  // Downloads are plain GETs the browser handles, so expose the URL rather
  // than fetching into memory.
  downloadUrl: (id, path) => `${base}/api/instances/${id}/files/download?path=${q(path)}`,

  // Large-file streaming transfers (multi-GB, memory-bounded).
  uploadStreamUrl: (id, path, tid) =>
    `${base}/api/instances/${id}/files/upload-stream?path=${q(path)}&tid=${tid}`,
  downloadStreamUrl: (id, path, tid) =>
    `${base}/api/instances/${id}/files/download-stream?path=${q(path)}&tid=${tid}`,
  transferStatus: (tid) => get(`/api/transfers/${tid}`),
  cancelTransfer: (tid) => post(`/api/transfers/${tid}/cancel`),

  // Version / build updater (catalog from the hub; install runs on the agent)
  listSoftwareVersions: (software) => get(`/api/providers/${q(software)}/versions`),
  listSoftwareBuilds: (software, version) =>
    get(`/api/providers/${q(software)}/versions/${q(version)}/builds`),
  updateInstanceVersion: (id, version, build) =>
    post(`/api/instances/${id}/update`, build == null ? { version } : { version, build }),
};
