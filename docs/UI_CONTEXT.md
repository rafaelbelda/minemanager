# MineManager — UI Build Context (Web Frontend)

Everything the web UI needs to talk to the MineManager hub. The UI is a plain
HTML/CSS/JS app in [`../web/`](../web/), served same-origin by the hub; this
backend exposes a REST + WebSocket API. Read [`../PLAN.md`](../PLAN.md) for the
architecture; this doc is the API contract.

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
  (not for the UI). Everything else at `/` is the static UI, mounted last so
  `/api`, `/ws` and `/docs` always win.
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
  "jar_path": "paper.jar",         // or null — derived from start_command when unset
  "java_home": "/opt/jdk-21",      // or null — the node's default java
  "desired_running": true,         // operator intent (see gap note in §6)
  "auto_restart": true,
  "version": "1.21.8",            // installed server version, or null if unknown
  "build": "60"                   // installed build (paper/velocity), else null
}
```

`version`/`build` are set by the updater (and, later, a version detector). They
are `null` on a freshly declared instance — show "not detected yet".

`java_home` is a **plain field, not a secret** — it round-trips through the API
like any other. It is the JDK *directory* (the one containing `bin/java`), which
lets one node run instances on different Java versions. When set, the agent
launches with `JAVA_HOME`/`PATH` pointed at it, so the start command itself is
unchanged and wrapper scripts inherit the choice. The hub rejects a relative
path or one containing shell metacharacters with `422`; the agent refuses to
start (`502`) if `<java_home>/bin/java` is missing or not executable. Empty
string and `null` both mean "use the node's default java".

### FileEntry (from files list)
```json
{ "name": "server.properties", "path": "server.properties",
  "is_dir": false, "size": 1234, "modified": 1753267872.5 }
```
`path` is **relative to the instance root** and is what you pass back to
read/write/delete. `modified` is a unix timestamp (float seconds).

### RunState enum (instance runtime state)
`stopped` · `starting` · `running` · `stopping` · `crashed` · `updating` · `unknown`

`updating` is emitted while a server-binary update is in progress: the agent
refuses to start the server, so the UI should lock power controls until it
clears (it transitions back to `stopped`).

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
| PATCH | `/api/instances/{instance_id}` | `InstanceUpdate` (partial) | `InstanceOut` |
| DELETE | `/api/instances/{instance_id}` | — | `204` |

`InstanceUpdate` is a partial update — send only the fields you want to change
(`name`, `type`, `root_dir`, `start_command`, `jar_path`, `java_home`,
`auto_restart`). Changes to `root_dir`,
`start_command` and `java_home` apply on the instance's **next start**; a
running session keeps the spec it launched with.

`InstanceCreate`:
```json
{
  "name": "survival",
  "type": "paper",
  "root_dir": "/srv/minecraft/survival",
  "start_command": "java -Xmx4G -jar paper.jar nogui",
  "jar_path": "paper.jar",       // optional, derived from start_command if unset
  "java_home": "/opt/jdk-21",    // optional, null = the node's default java
  "auto_restart": true           // optional, default true
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

### Console (send + history; live output is via WebSocket)
| Method | Path | Body / Query | Returns |
|---|---|---|---|
| POST | `/api/instances/{instance_id}/console` | `{"line":"say hello"}` | `{"sent": true}` |
| GET | `/api/instances/{instance_id}/console/history?lines=200` | query `lines` (1–1000, default 200) | `{"lines": ["..."], "path", "missing"?}` |

`console/history` returns the tail of `logs/latest.log` so a freshly-opened UI
session can backfill what happened before it connected, then let the live
WebSocket take over. It is best-effort: a missing log yields `{"lines": [],
"missing": true}` (not an error), and any failure should be non-fatal to the
console.

### Files (jailed to the instance root; paths are relative)
| Method | Path | Body / Query | Returns |
|---|---|---|---|
| GET | `/api/instances/{instance_id}/files?path=.` | query `path` | `{"entries": FileEntry[]}` |
| GET | `/api/instances/{instance_id}/files/read?path=x` | query `path` | `{"binary": bool, "content"?, "size"}` |
| POST | `/api/instances/{instance_id}/files/write` | `{"path","content"}` | `{"path","size"}` (creates parent dirs) |
| POST | `/api/instances/{instance_id}/files/upload` | `{"path","content_b64"}` | `{"path","size"}` (simple/small; see cap) |
| GET | `/api/instances/{instance_id}/files/download?path=x` | query `path` | raw file bytes, or a **zip** if `path` is a dir (`Content-Disposition: attachment`) |
| POST | `/api/instances/{instance_id}/files/rename` | `{"path","new_name"}` | `{"path","renamed":true}` |
| POST | `/api/instances/{instance_id}/files/extract` | `{"path","overwrite"}` | see below |
| DELETE | `/api/instances/{instance_id}/files?path=x&recursive=false` | query | `{"path","deleted":true}` |

- **`read`** now flags binary files: `{"binary": true, "size"}` with no content
  (the UI must not render these as text — offer download instead). Text files
  return `{"binary": false, "content", "size"}`. Files above `editor_max_bytes`
  are refused (`502`).
- **`upload`/`download`** are the simple (non-streaming) path, bounded by
  `transfer_cap_bytes`. Over-cap uploads return `413`; over-cap files/dirs on
  download return `502`. Multi-GB streaming transfers are a separate feature
  (see gaps). `download` on a directory returns a zip.
- **`extract`** unpacks ZIP / TAR(.GZ/.BZ2/.XZ) / TGZ / single GZ (RAR only if
  the node has the tooling) into the archive's own directory. Returns
  `{"extracted": true, "count": n}`, or when files would be overwritten and
  `overwrite` is false: `{"extracted": false, "conflicts": [...],
  "conflict_count": n}` — prompt, then retry with `"overwrite": true`. Archive
  members are jailed (zip-slip is rejected).

Path traversal (`..`, absolute paths, symlinks out of root) is rejected by the
agent (`502` with a "path rejected" detail). The instance root itself cannot be
deleted or renamed.

### Config (UI thresholds — configurable, never hardcode in the UI)
`GET /api/config` → `{"editor_warn_bytes", "editor_max_bytes", "transfer_cap_bytes"}`.
Above `editor_warn_bytes` the editor should warn before opening; above
`editor_max_bytes` it must not open as text (offer download); `transfer_cap_bytes`
bounds the simple upload/download path.

### Secrets (write-only values)
| Method | Path | Body | Returns |
|---|---|---|---|
| PUT | `/api/instances/{instance_id}/secrets` | `{"key":"forwarding_secret","value":"..."}` | `204` |
| GET | `/api/instances/{instance_id}/secrets` | — | `{"keys": ["forwarding_secret"]}` |

Values are encrypted at rest and **never returned**. The UI can show *which*
keys are set and let the user overwrite them, but cannot display current values.
The only key in use today is `forwarding_secret` (Velocity).

### Version / build updater

The catalog is served by the hub from software providers; the install runs on
the agent. Endpoints are keyed by **software** (an instance's `type`) and are
provider-agnostic — the UI never hard-codes Paper/Vanilla/Velocity logic.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/providers/{software}` | `{software, label, has_builds}` |
| GET | `/api/providers/{software}/versions` | `{software, label, has_builds, versions:[{id,label,channel}]}` |
| GET | `/api/providers/{software}/versions/{version}/builds` | `{software, version, builds:[{id,label,channel}]}` |
| POST | `/api/instances/{instance_id}/update` | body `{version, build?}` → `{ok, version, build, jar, detail}` |

Rules for the UI:
- `has_builds` decides whether to show a build selector at all. Vanilla is
  `false` (version only); Paper and Velocity are `true`.
- When `has_builds`, changing the version must refetch builds (they're
  per-version). `versions` and `builds` are newest-first.
- `build` is required in the update body only when `has_builds`.
- The update **requires the instance to be stopped** (the agent enforces it and
  returns `502` otherwise). While it runs, the agent emits `state.changed` with
  `updating`; lock power controls and show progress until it clears.
- The update is transactional on the agent: only the server jar is replaced
  (worlds/plugins/mods/config/player-data are never touched), the previous jar
  is backed up, and a failed download/replace rolls back. On success the hub
  records the installed `version`/`build` on the instance.

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
| `console.output` | `{"line": "...", "source": "pty"}` | raw terminal output, replayed as a block when an instance crashes (see below) |
| `state.changed` | `{"state": "running", "detail": null}` | instance runtime state transition (drive power UI + status badges) |
| `heartbeat` | `{"uptime_s": 123.4, "instances": {"<id>": "running"}}` | ~every 15s; `instance_id` is null (node-level). Use to refresh all instance states + liveness at once |

Recommended UI wiring:
- Keep one events socket open per node the user is viewing.
- Maintain an instance→state map, seeded by `heartbeat.instances` and updated by
  `state.changed`.
- Route `console.output` to the matching instance's console pane by
  `instance_id`. `source` may be ignored — every source is a console line — but
  it is there if you want to style them differently.
- `source: "pty"` lines arrive as a short block (headed by
  `--- last terminal output before exit ---`) immediately **before** the
  `state.changed` → `crashed` event. They are what the process printed to its
  terminal, which is the only place the reason lives when a server dies before
  its logger starts — a JVM version mismatch, a port already bound, a bad
  `-Xmx`. Nothing reaches `logs/latest.log` in those cases, so without this the
  console shows a crash with no explanation. On a crash *after* startup these
  lines may repeat log output you already streamed.
- If the socket closes, reconnect with backoff (the node may just be offline).

---

## 6. Known gaps / not yet built

Build the UI so these degrade gracefully (hide, disable, or mark "coming soon"):

1. **Multi-GB streamed transfers** — upload/download currently use a simple
   base64 path bounded by `transfer_cap_bytes` (~8 MB by default). Files/dirs
   over the cap are refused (`413`/`502`); the UI skips over-cap uploads with a
   "large transfers coming soon" note. A dedicated streaming channel (progress +
   cancel, memory-bounded) for whole worlds is the next pass.
2. **Full log viewer** — `console/history` tails `logs/latest.log`, and
   `files/read` opens it directly, but there is no rotation-aware log browser
   (rotated `*.log.gz` list but can't be opened — `files/read` is utf-8 and 502s
   on gzip). A proper historical-log view is future.
3. **Live output after an agent reconnect** — the agent only *tails* a server it
   started this session. A server already running when the agent (re)connected
   streams no new lines until its next `power/restart`. The UI backfills recent
   output via `console/history` on open, so the console is not blank — but live
   updates for that case wait on sync-on-connect (see PLAN.md).
4. **`desired_running`** is currently operator-intent bookkeeping only; the hub
   does not yet auto-reconcile it to the agent on reconnect. Don't present it as
   a guarantee the server will be restored automatically.
5. **Audit log** — model exists but isn't populated or exposed. No history view yet.
6. **No pagination** anywhere (small-scale by design — fine to render full lists).

---

## 7. Error conventions

| Status | Meaning | Suggested UI |
|---|---|---|
| `400` | bad request (e.g. unknown power op) | dev error toast |
| `404` | node/instance not found | "not found" state |
| `409` | **agent offline** (or dropped mid-command) | "Node offline — can't reach agent." Disable live actions when `node.online` is false to avoid hitting this |
| `502` | agent reported an error (bad path, tmux failure) | show `detail` — it's the agent's message |
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
5. **Settings/secrets** — edit the instance spec via `PATCH` (name, start
   command, root dir, `jar_path`, `java_home`, `auto_restart`); set
   `forwarding_secret` (write-only).

---

## 9. Quick reference: full endpoint list

```
GET    /api/health
GET    /api/nodes
POST   /api/nodes                                  {name} -> EnrollmentOut (201)
POST   /api/nodes/{node_id}/reenroll               -> EnrollmentOut
DELETE /api/nodes/{node_id}                         (204)
GET    /api/nodes/{node_id}/instances
GET    /api/nodes/{node_id}/instance-states        -> {states:{id: RunState}}  (fast initial load)
POST   /api/nodes/{node_id}/instances              InstanceCreate -> InstanceOut (201)
PATCH  /api/instances/{instance_id}                InstanceUpdate -> InstanceOut
DELETE /api/instances/{instance_id}                 (204)
POST   /api/instances/{instance_id}/power/{start|stop|restart|kill}
POST   /api/instances/{instance_id}/console        {line}
GET    /api/instances/{instance_id}/console/history?lines=200
GET    /api/config                                 -> {editor_warn_bytes, editor_max_bytes, transfer_cap_bytes}
GET    /api/instances/{instance_id}/files?path=.
GET    /api/instances/{instance_id}/files/read?path=...   -> {binary, content?, size}
POST   /api/instances/{instance_id}/files/write    {path, content}
POST   /api/instances/{instance_id}/files/upload   {path, content_b64}
GET    /api/instances/{instance_id}/files/download?path=...   (bytes / zip)
POST   /api/instances/{instance_id}/files/rename   {path, new_name}
POST   /api/instances/{instance_id}/files/extract  {path, overwrite}
DELETE /api/instances/{instance_id}/files?path=...&recursive=false
PUT    /api/instances/{instance_id}/secrets        {key, value}   (204)
GET    /api/instances/{instance_id}/secrets        -> {keys:[...]}
GET    /api/providers/{software}                   -> {software,label,has_builds}
GET    /api/providers/{software}/versions          -> {versions:[...]}
GET    /api/providers/{software}/versions/{ver}/builds -> {builds:[...]}
POST   /api/instances/{instance_id}/update         {version, build?}
WS     /api/nodes/{node_id}/events                 (push-only event stream)
```

Interactive API docs are always available at **`/docs`** (Swagger) when the hub
is running — the source of truth if this file and the code ever diverge.
