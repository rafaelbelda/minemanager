# MineManager — Architecture & Build Plan

> A local, self-hosted control plane for managing a small–medium Minecraft
> network (servers + proxies) across one or more Linux machines.

Status: **v1 planning locked.** This document is the source of truth for the
decisions we've made and the structure we're building against.

---

## 1. What we're building

A Python application with a web interface (HTML/CSS/JS — built later by Claude
Design) that lets our team manage and control a Minecraft network: Paper/Vanilla
servers and Velocity proxies, whether they run on the same machine as the app or
on remote machines.

From the web UI an operator can, per node:

- **Power:** start / stop / restart, see live running state.
- **Console:** live output stream + type commands (real console, RCON secondary).
- **Files:** browse, read, edit, upload, delete — jailed to the node's root dir.
- **Logs:** live tail + historical.
- **Config:** edit `server.properties`, `velocity.toml`, etc. (they're just files).

---

## 2. Scope & constraints (decided)

- **OS:** all-Linux ecosystem. No Windows/macOS target for nodes.
- **Scale:** built for **5–10 servers**. No hard limits in code — this is a
  *recommendation* to users, not an enforced cap.
- **Tenancy:** **single-tenant.** One team, one trust domain.
- **Software supported:** **Paper** and **Vanilla** servers; **Velocity** proxies.
  Nothing else in v1.
- **Process scope:** **manage-only** in v1 (control servers that already exist on
  disk). Full **provisioning** (download jars, create servers from scratch) is
  **v2**.
- **Network / auth posture:** the app sits behind our existing **Authelia** and is
  reachable only over **WireGuard**. It is not meant to be exposed to the public
  internet. Therefore MineManager does **not** implement its own user login — it
  trusts the authenticated reverse-proxy in front of it.
- **Security concern for us:** in-app **secrets and tokens** (agent enrollment
  tokens, RCON passwords, Velocity forwarding secrets). Everything else is handled
  by the perimeter (Authelia + WireGuard).

---

## 3. Topology

MineManager is a **hub + agents** system. Every managed machine runs an **agent**;
one machine additionally runs the **hub** (the web control plane). "Local" is just
"an agent that happens to be on the same box as the hub" — there is no special case
for it. This uniformly supports every mix the user asked for:

- local servers + remote proxy
- local proxy + remote servers
- everything local / everything remote / any mix

```
                         ┌──────────────────────────────┐
   Browser (Authelia +   │            HUB               │
   WireGuard) ────────►  │  FastAPI web + control plane │
                         │  SQLite state, secrets vault │
                         └───────────┬──────────────────┘
                                     │  (agents dial OUT to hub,
                                     │   persistent WebSocket, mTLS/token)
             ┌───────────────────────┼───────────────────────┐
             │                       │                       │
      ┌──────┴──────┐         ┌──────┴──────┐         ┌──────┴──────┐
      │   AGENT     │         │   AGENT     │         │   AGENT     │
      │ (box A)     │         │ (box B)     │         │ (box C)     │
      │ Paper srvs  │         │ Velocity    │         │ Paper srvs  │
      └─────────────┘         └─────────────┘         └─────────────┘
```

### Connection direction: agents dial out to the hub

Agents open and maintain a persistent **WebSocket to the hub** (agent → hub),
not the other way around. Rationale:

- Works cleanly with WireGuard and NAT — only the hub needs a stable reachable
  address; agents can be anywhere on the WG mesh.
- One consistent auth handshake per agent.
- The hub never has to "reach into" a node; it sends commands over the already-open
  channel and streams results back.

---

## 4. Process & supervision model (decided: **Model B — agent owns the processes**)

The agent is the **only** systemd-managed daemon on a node. Each Minecraft
server/proxy runs in **its own detached pty session (tmux) owned by the agent**.

**Why this over per-server systemd units:** the live console is the core feature.
A real pty gives clean bidirectional I/O (type any command, not just RCON) and a
single source of truth for process lifecycle. Per-server systemd units would still
need a pty wrapper for console input anyway, and would split lifecycle state
between systemd and the agent.

### Responsibilities

- **systemd supervises the _agent_** (auto-restart, start-on-boot). The one thing
  that must always be up keeps systemd's guarantee.
- **The agent supervises the _servers_:**
  - spawns each server into its own **tmux** session as the non-root mc user
    (direct child processes — no sudo/polkit needed);
  - implements restart policy: graceful `stop` → timeout → kill; per-server
    auto-restart toggle; **crash-loop detection** (e.g. stop auto-restarting after
    N crashes in a window);
  - on agent boot, restores servers marked "should be running."

### Console I/O

- **Input:** `tmux send-keys` into the session (real stdin).
- **Output stream:** tail `logs/latest.log` (clean, no ANSI — what we display);
  raw pane pipe available if we ever need it.
- **RCON:** secondary command channel / fallback, not the primary console.

### Resource limits (optional, v2)

Launch each server into a transient cgroup via
`systemd-run --user --scope -p MemoryMax=…`. Agent still owns the pty; the kernel
enforces limits. Not in v1.

---

## 5. Security model

Perimeter (Authelia + WireGuard) handles user authentication and network exposure.
MineManager owns **machine-to-machine** trust and **secret storage**.

- **Agent enrollment:** hub issues a one-time enrollment token; agent enrolls,
  receives a long-lived per-agent credential. Each agent has its own identity and
  can be revoked independently.
- **Transport:** TLS on the agent↔hub WebSocket, plus the per-agent token. (mTLS
  is an option since it's a closed WG network; token-over-TLS is the v1 baseline.)
- **Secret storage:** RCON passwords, Velocity forwarding secrets, agent
  credentials are encrypted at rest in the hub with a key from the environment /
  a key file outside the DB. Never logged, never returned in plaintext to the UI
  once set.
- **No agent runs as root.** Agent runs as a dedicated non-root user (the mc user
  or a service user in its group); all file/process operations happen as that user.
- **Path jailing:** every file operation is confined to the node's configured root
  directory; path traversal is rejected server-side in the agent.

---

## 6. State & source of truth

- **Hub owns declared/desired state** (which nodes exist, their config, "should be
  running") in **SQLite**.
- **Agent owns observed/runtime state** (is the process actually up, pid, uptime,
  live logs) and reports it up the WebSocket.
- The UI shows both; where they disagree (e.g. desired=running, observed=stopped),
  that's surfaced as a reconciliation signal, not hidden.

---

## 7. Communication protocol

- **Channel:** one persistent WebSocket per agent (agent dials out).
- **Encoding:** JSON message envelopes validated by **Pydantic models** shared
  between hub and agent (the `shared` package — single definition, no drift).
- **Shape:** request/response with correlation IDs for commands
  (`power.start`, `files.list`, `console.send`, …), plus unsolicited **event**
  frames for streams (`console.output`, `log.line`, `state.changed`, `heartbeat`).
- **Streaming:** console/log output flows as event frames the hub fans out to any
  UI clients currently watching that node.

---

## 8. Data model (hub, first cut)

- **Node** — a managed machine's agent: id, name, enrollment/credential, address,
  last-seen, status.
- **Instance** — a server or proxy on a node: id, node_id, type
  (`paper` | `vanilla` | `velocity`), name, root dir, start command, jar/launch
  info, auto-restart flag, desired state.
- **Secret** — encrypted blobs (RCON password, Velocity forwarding secret),
  scoped to an instance or node.
- **AuditLog** (nice-to-have v1): who did what, when — power actions, file writes,
  deletes.

---

## 9. Tech stack

- **Language:** Python (3.11+).
- **Hub:** **FastAPI** + **uvicorn** (native WebSocket support), **SQLite** via
  **SQLAlchemy** (perfect for single-tenant, 5–10 nodes — no Postgres needed),
  **Pydantic** for schemas. Serves the static web UI (built later).
- **Agent:** asyncio Python daemon; **websockets**/httpx client (dials out); wraps
  `tmux`, `systemctl --user` where relevant, and RCON. No inbound web server.
- **Shared:** `minemanager_shared` package — Pydantic protocol models + version,
  imported by both hub and agent so message contracts never drift.
- **Console session backend:** **tmux** (bonus: a human can `tmux attach` to debug
  a server directly).
- **RCON:** a small Source-RCON client in the agent.

---

## 10. Repository structure

```
minemanager/
├── PLAN.md                     # this document
├── README.md
├── hub/                        # control plane (web backend + serves UI)
│   ├── pyproject.toml
│   ├── minemanager_hub/
│   │   ├── main.py             # FastAPI app entrypoint
│   │   ├── config.py           # settings (env-driven)
│   │   ├── db/                 # SQLAlchemy models + session
│   │   ├── api/                # REST + WebSocket routes
│   │   ├── agents/             # agent connection registry / fan-out
│   │   ├── services/           # business logic
│   │   └── security/           # secret vault, token issuance
│   └── tests/
├── agent/                      # node daemon
│   ├── pyproject.toml
│   ├── minemanager_agent/
│   │   ├── main.py             # dials hub, holds WS, dispatch loop
│   │   ├── config.py
│   │   ├── connection.py       # WebSocket client + reconnect
│   │   ├── supervisor.py       # tmux sessions, restart policy, crash-loop
│   │   ├── console.py          # log tail (output) + send-keys (input)
│   │   ├── files.py            # jailed file operations
│   │   ├── rcon.py             # secondary command channel
│   │   └── handlers.py         # map protocol commands → actions
│   └── tests/
├── shared/                     # protocol library (imported by hub + agent)
│   ├── pyproject.toml
│   └── minemanager_shared/
│       ├── protocol.py         # message envelopes + payload models
│       └── version.py
├── web/                        # the UI — static ES modules, no build step
│   ├── index.html              # shell: topbar, tree sidebar, three views
│   ├── styles.css              # design tokens + components
│   ├── api.js                  # REST client + error → wording mapping
│   ├── events.js               # per-node event sockets w/ backoff
│   ├── dom.js                  # element builder, toasts, dialogs
│   └── app.js                  # state, rendering, action handlers
└── deploy/
    ├── minemanager-agent.service   # systemd unit for the AGENT (the only daemon)
    └── README.md                   # install/enroll steps
```

---

## 11. Roadmap

**v1 (now) — manage-only**
- Hub: agent registry, node/instance CRUD, WebSocket hub, secret vault, serves UI.
- Agent: enroll + persistent connection, tmux supervisor with restart/crash-loop
  policy, power actions, live console (out via log tail, in via send-keys), jailed
  file ops, log tail, RCON secondary.
- **Version & build updater** — provider-based (Vanilla via Mojang, Paper &
  Velocity via the PaperMC v3 Fill API), software-agnostic UI. Transactional
  jar swap on the agent (download → checksum verify → backup → atomic replace →
  rollback) that touches only the server executable. Adding a provider (Purpur,
  Fabric, Forge…) is a new module + one registry line, no UI change.
- **File explorer** — jailed browse/edit plus fullscreen editor, upload (button
  + drag&drop, folders recurse), download (file, or folder→zip), archive
  extraction (ZIP/TAR.GZ/TGZ/GZ, RAR if available; zip-slip jailed; overwrite
  prompt), rename, right-click context menu, auto-refresh, and configurable
  large-file + binary-file guards. Simple upload/download is capped; multi-GB
  streaming is a separate next pass.
- Protocol: shared Pydantic contracts.

**v2 — provisioning & polish**
- Create servers/proxies from scratch (download Paper/Velocity, scaffold configs).
- **Version detector** — read the *actual* installed version/build from disk
  (updater currently records what it installed; there is no independent
  detection yet — step 6 of the update workflow is a deliberate placeholder).
- Per-server cgroup resource limits via `systemd-run --scope`.
- Backups/snapshots, scheduled tasks, richer audit log.

---

## 11b. Known v1 / BETA gaps (tracked)

Deliberately deferred; the UI degrades gracefully around these — upload is a
disabled "coming soon" control (see [`docs/UI_CONTEXT.md`](docs/UI_CONTEXT.md)
§6, and [`docs/UI_STATUS.md`](docs/UI_STATUS.md) for how each one presents):

- **Multi-GB streamed transfers** — file upload/download use a simple base64
  path capped at `transfer_cap_bytes` (~8 MB); a dedicated streaming channel
  (binary frames multiplexed over the agent WS with credit-based flow control,
  bridged to the browser via HTTP streaming, with progress + cancel) for whole
  worlds is the next pass.
- **Full log viewer** — `console/history` tails `logs/latest.log` and `files`
  reads it, but there is no rotation-aware log browser (gzip logs can't be
  opened).
- **Live console after an agent reconnect** — the agent tails a server's log
  only from the moment it *starts* it; a server already running when the agent
  (re)connects streams no new lines until restarted. The UI backfills recent
  output via `console/history` on open, so the console isn't blank — but live
  updates for that case still wait on sync-on-connect.
- **Desired-state reconciliation** — `desired_running` is recorded but not
  pushed back to an agent on reconnect (no auto-restore yet).
- **RCON REST surface** — agent supports `rcon.command`; not exposed via REST.
- **Audit log** — model exists but is not populated or exposed.

## 12. Open items to confirm as we build

- Exact secret-encryption key delivery (env var vs. key file) — pick during hub
  security module.
- mTLS vs. token-over-TLS for the agent channel — baseline token-over-TLS; revisit
  if we want client certs on the WG mesh.
- Whether Velocity forwarding-secret sync is automated by the hub in v1 or left as
  a managed file edit. (Leaning: managed file edit in v1, automation in v2.)
