# MineManager — UI Build Context (Web Frontend)

Everything the web UI needs to talk to the MineManager hub. The UI is a separate
HTML/CSS/JS app; this backend exposes a REST + WebSocket API. Read
[`../PLAN.md`](../PLAN.md) for the architecture; this doc is the API contract.

**Status: BETA.** Some endpoints listed under [Known gaps](#known-gaps--not-yet-built)
are intentionally not built yet — build the UI so those areas degrade gracefully.

---

## 1. Mental model (build the UI around this)

- **Hub** = the server this UI talks to. It holds *declared* state (nodes,
  instances) in a DB.
- **Node** = one managed machine (runs an "agent"). A node is **online** only
  while its agent holds a live WebSocket to the hub.
- **Instance** = one Minecraft server or proxy living on a node
  (`paper` | `vanilla` | `velocity`).
- Commands (power, console, files) are **proxied through the hub to the agent**
  in real time. If the node's agent is offline, these fail with `409`.
- **Live output** (console lines, state changes, heartbeats) arrives on a
  **per-node WebSocket**, not via polling.

So the two-layer rule: **REST for actions and declared state; WebSocket for live
streams.**

---

## 2. Base URL, auth, CORS

- All REST paths are under **`/api`**. The agent transport lives under `/ws`
  (not for the UI).
- **Authentication:** none at the app layer. The hub runs behind **Authelia +
  WireGuard**; the reverse proxy authenticates the user. The UI does **not**
  implement login, send tokens, or manage sessions. Assume the user is already
  authenticated by the proxy.
- **CORS:** off by default (production serves the UI same-origin). For local dev
  against a separate dev server, start the hub with
  `MM_CORS_ORIGINS=http://localhost:5173` (comma-separated for multiple).

---

## 3. Data shapes

### NodeOut
```json
{
  "id": "5170525f9c...",          // 32-char hex
  "name": "box-a",
  "hostname": "mc-host-1",
  "agent_version": "0.1.0",
  "online": true,                  // live agent socket present
  "last_seen": "2026-07-23T10:11:12Z",  // ISO 8601 or null
  "enrolled": true                 // has completed first enrollment
}
```

### EnrollmentOut (returned once, on node create / re-enroll)
```json
{ "node_id": "5170525f...", "enrollment_token": "6eXswrB1...", "expires_in_s": 900 }
```
The **enrollment_token is shown exactly once** — display it prominently with a
copy button and the install hint; it cannot be retrieved again (only re-minted).

### InstanceOut
```json
{
  "id": "9455de9c...",
  "node_id": "5170525f...",
  "name": "survival",
  "type": "paper",                 // paper | vanilla | velocity
  "root_dir": "/srv/minecraft/survival",
  "start_command": "java -Xmx4G -jar paper.jar nogui",
  "desired_running": true,         // operator intent (see gap note in §6)
  "auto_restart": true,
  "rcon_host": "127.0.0.1",
  "rcon_port": 25575               // or null
}
```

### FileEntry (from files list)
```json
{ "name": "server.properties", "path": "server.properties",
  "is_dir": false, "size": 1234, "modified": 1753267872.5 }
```
`path` is **relative to the instance root** and is what you pass back to
read/write/delete. `modified` is a unix timestamp (float seconds).

### RunState enum (instance runtime state)
`stopped` · `starting` · `running` · `stopping` · `crashed` · `unknown`

---

## 4. REST endpoints

Base: `/api`. Request/response bodies are JSON. FastAPI errors are
`{"detail": "..."}`.

### Health
| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","version":"0.1.0"}` |

### Nodes
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/nodes` | — | `NodeOut[]` |
| POST | `/api/nodes` | `{"name": "box-a"}` | `201` `EnrollmentOut` |
| POST | `/api/nodes/{node_id}/reenroll` | — | `EnrollmentOut` (new token; marks node un-enrolled until agent reconnects) |
| DELETE | `/api/nodes/{node_id}` | — | `204` (cascades to its instances) |

### Instances
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/api/nodes/{node_id}/instances` | — | `InstanceOut[]` |
| POST | `/api/nodes/{node_id}/instances` | `InstanceCreate` | `201` `InstanceOut` |
| DELETE | `/api/instances/{instance_id}` | — | `204` |

`InstanceCreate`:
```json
{
  "name": "survival",
  "type": "paper",
  "root_dir": "/srv/minecraft/survival",
  "start_command": "java -Xmx4G -jar paper.jar nogui",
  "auto_restart": true,          // optional, default true
  "rcon_host": "127.0.0.1",      // optional
  "rcon_port": 25575             // optional, null if RCON unused
}
```

### Power
| Method | Path | Returns |
|---|---|---|
| POST | `/api/instances/{instance_id}/power/start` | `{"state":"running"}` (agent result) |
| POST | `/api/instances/{instance_id}/power/stop` | `{"state":"stopped", ...}` |
| POST | `/api/instances/{instance_id}/power/restart` | `{"state":"running"}` |
| POST | `/api/instances/{instance_id}/power/kill` | `{"state":"stopped", ...}` |

`stop` is graceful (sends the console stop command, waits up to ~45s, then
kills). `kill` is immediate. The authoritative live state still comes over the
**events WebSocket** (`state.changed`) — treat the POST result as the initial
value and let events drive the UI thereafter.

### Console (send only; output is via WebSocket)
| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/instances/{instance_id}/console` | `{"line":"say hello"}` | `{"sent": true}` |

### Files (jailed to the instance root; paths are relative)
| Method | Path | Body / Query | Returns |
|---|---|---|---|
| GET | `/api/instances/{instance_id}/files?path=.` | query `path` | `{"entries": FileEntry[]}` |
| GET | `/api/instances/{instance_id}/files/read?path=logs/latest.log` | query `path` | `{"content": "..."}` (utf-8, max 5 MB) |
| POST | `/api/instances/{instance_id}/files/write` | `{"path","content"}` | `{"path","size"}` (creates parent dirs) |
| DELETE | `/api/instances/{instance_id}/files?path=x&recursive=false` | query | `{"path","deleted":true}` |

Path traversal (`..`, absolute paths, symlinks out of root) is rejected by the
agent (`502` with a "path rejected" detail). The instance root itself cannot be
deleted.

**Log viewing tip:** there is no dedicated historical-log endpoint yet. To show
past logs, `files/read` on `logs/latest.log` (and rotated `logs/*.log.gz` are
listable but not decompressed by the API). Live console tailing is via the WS.

### Secrets (write-only values)
| Method | Path | Body | Returns |
|---|---|---|---|
| PUT | `/api/instances/{instance_id}/secrets` | `{"key":"rcon_password","value":"..."}` | `204` |
| GET | `/api/instances/{instance_id}/secrets` | — | `{"keys": ["rcon_password"]}` |

Values are encrypted at rest and **never returned**. The UI can show *which*
keys are set and let the user overwrite them, but cannot display current values.
Common keys: `rcon_password`, `forwarding_secret` (Velocity).

---

## 5. WebSocket: live node events

Connect per node:

```
ws://<hub>/api/nodes/{node_id}/events
```

The socket pushes **all events for that node**. Each message is one JSON *event
frame*; filter by `instance_id` client-side. The UI does not need to send
anything (the socket is push-only; sending is ignored).

Event frame shape:
```json
{ "kind": "event", "action": "...", "instance_id": "9455de9c...", "data": { }, "ts": 1753267872.5 }
```

Event actions and their `data`:

| action | data | meaning |
|---|---|---|
| `console.output` | `{"line": "...", "source": "log"}` | one console/log line (append to terminal view) |
| `state.changed` | `{"state": "running", "detail": null}` | instance runtime state transition (drive power UI + status badges) |
| `heartbeat` | `{"uptime_s": 123.4, "instances": {"<id>": "running"}}` | ~every 15s; `instance_id` is null (node-level). Use to refresh all instance states + liveness at once |

Recommended UI wiring:
- Keep one events socket open per node the user is viewing.
- Maintain an instance→state map, seeded by `heartbeat.instances` and updated by
  `state.changed`.
- Route `console.output` to the matching instance's console pane by
  `instance_id`.
- If the socket closes, reconnect with backoff (the node may just be offline).

---

## 6. Known gaps / not yet built

Build the UI so these degrade gracefully (hide, disable, or mark "coming soon"):

1. **Editing an instance** — no `PATCH`. To change `start_command`, `root_dir`,
   `auto_restart`, or RCON settings, the current path is delete + recreate.
   (A `PATCH /api/instances/{id}` is planned.)
2. **Binary file upload** — the agent supports it, but there is **no REST
   endpoint** yet. Text create/edit works via `files/write`. Treat upload of
   binary (jars, zips) as "coming soon".
3. **Historical logs** — no dedicated endpoint. Use `files/read` on
   `logs/latest.log`. Live tailing is WS-only.
4. **Console output before a start** — the agent starts tailing when it *starts*
   an instance this session. If a server was already running before the agent
   connected (or agent restarted), console output won't stream until the next
   `power/restart`. Show a hint like "restart to attach console" when a running
   instance has no recent output.
5. **`desired_running`** is currently operator-intent bookkeeping only; the hub
   does not yet auto-reconcile it to the agent on reconnect. Don't present it as
   a guarantee the server will be restored automatically.
6. **RCON tab** — RCON is secondary. There is no REST endpoint exposed for
   ad-hoc RCON yet (the agent supports `rcon.command`). Primary console is the
   `console` POST + events WS. Treat an RCON console as optional/future.
7. **Audit log** — model exists but isn't populated or exposed. No history view yet.
8. **No pagination** anywhere (small-scale by design — fine to render full lists).

---

## 7. Error conventions

| Status | Meaning | Suggested UI |
|---|---|---|
| `400` | bad request (e.g. unknown power op) | dev error toast |
| `404` | node/instance not found | "not found" state |
| `409` | **agent offline** (or dropped mid-command) | "Node offline — can't reach agent." Disable live actions when `node.online` is false to avoid hitting this |
| `502` | agent reported an error (bad path, tmux/RCON failure) | show `detail` — it's the agent's message |
| `504` | agent didn't answer in time | "Timed out talking to the agent" + retry |

Since `409` is the common "offline" case, gate power/console/file controls on the
node's `online` flag (from `GET /api/nodes`) and/or recent heartbeats.

---

## 8. Suggested screens (not prescriptive)

1. **Nodes overview** — cards/table of `NodeOut` with online badge, version,
   last-seen, instance count. "Add node" → shows the one-time enrollment token +
   install command; then poll `GET /api/nodes` until `online`.
2. **Node detail** — its instances, each with a state badge (from heartbeats),
   power buttons, and quick links to console/files.
3. **Instance console** — terminal view fed by `console.output`; input box →
   `POST console`. Show current `state` from `state.changed`.
4. **File manager** — breadcrumb + `files list`; text editor via `read`/`write`
   (great for `server.properties`, `velocity.toml`, `*.yml`); delete with confirm.
5. **Settings/secrets** — set `rcon_password` / `forwarding_secret` (write-only),
   toggle `auto_restart` (needs the PATCH endpoint — see gaps).

---

## 9. Quick reference: full endpoint list

```
GET    /api/health
GET    /api/nodes
POST   /api/nodes                                  {name} -> EnrollmentOut (201)
POST   /api/nodes/{node_id}/reenroll               -> EnrollmentOut
DELETE /api/nodes/{node_id}                         (204)
GET    /api/nodes/{node_id}/instances
POST   /api/nodes/{node_id}/instances              InstanceCreate -> InstanceOut (201)
DELETE /api/instances/{instance_id}                 (204)
POST   /api/instances/{instance_id}/power/{start|stop|restart|kill}
POST   /api/instances/{instance_id}/console        {line}
GET    /api/instances/{instance_id}/files?path=.
GET    /api/instances/{instance_id}/files/read?path=...
POST   /api/instances/{instance_id}/files/write    {path, content}
DELETE /api/instances/{instance_id}/files?path=...&recursive=false
PUT    /api/instances/{instance_id}/secrets        {key, value}   (204)
GET    /api/instances/{instance_id}/secrets        -> {keys:[...]}
WS     /api/nodes/{node_id}/events                 (push-only event stream)
```

Interactive API docs are always available at **`/docs`** (Swagger) when the hub
is running — the source of truth if this file and the code ever diverge.
