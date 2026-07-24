# MineManager

Self-hosted control plane for a small–medium Minecraft network running Paper/Vanilla
servers and Velocity proxies, across one or more Linux machines.

Notice: Being written with AI collaboration. In BETA.

> Architecture, scope, and decisions live in [`PLAN.md`](PLAN.md). Read it first.

## What it does (v1, manage-only)

From a web UI, per server/proxy: **power** (start/stop/restart), a live
**console** (real pty, RCON secondary), **file** management (jailed to the
instance root), **log** tailing, and config editing. Works whether the node is
local or remote — every node runs an agent; the hub talks to them over a
persistent WebSocket.

## Layout

| Path       | What                                                              |
|------------|------------------------------------------------------------------|
| `shared/`  | Wire-protocol models shared by hub and agent (single source).    |
| `hub/`     | FastAPI control plane: state (SQLite), agent registry, secrets.  |
| `agent/`   | Per-node daemon: tmux supervisor, console, files, RCON.          |
| `web/`     | The web UI — static HTML/CSS/JS, served same-origin by the hub.  |
| `deploy/`  | systemd unit + install docs (the agent is the only daemon).      |

## Process model in one line

**systemd supervises the agent; the agent supervises the servers** (each in its
own tmux session). The hub is the source of truth for *declared* state; agents
own *runtime* state. See PLAN.md §4.

## Dev quickstart

```bash
python -m pip install -e shared -e hub -e agent   # editable installs

# terminal 1 — hub (the launcher honors MM_HOST/MM_PORT; bare uvicorn does not)
MM_DATA_DIR=./_devdata MM_PORT=8730 python -m minemanager_hub

# create a node in the API to get an enrollment token
curl -sX POST localhost:8730/api/nodes -H 'content-type: application/json' \
  -d '{"name":"box-a"}'

# terminal 2 — agent (needs tmux for real server control; file ops work anywhere)
MM_HUB_URL=ws://127.0.0.1:8730/ws/agent \
MM_ENROLL_TOKEN=<token-from-above> \
MM_AGENT_DATA_DIR=./_agentdata \
  python -m minemanager_agent.main
```

Then open <http://127.0.0.1:8730/> — the hub serves the UI from `web/` at the
root, so there is no separate frontend server, build step, or `node_modules`.

### Working on the UI

`web/` is plain ES modules; edit and reload, nothing to compile. Two knobs:

- `MM_WEB_DIR=/path/to/web` — serve the UI from elsewhere (or point it at a
  missing path for an API-only hub; the mount is skipped when absent).
- To run the UI off a different origin than the hub, start the hub with
  `MM_CORS_ORIGINS=http://localhost:5173` and open the page with
  `?hub=http://localhost:8730` (remembered in `localStorage`).

The API it consumes is documented in [`docs/UI_CONTEXT.md`](docs/UI_CONTEXT.md);
what the UI does and does not yet cover is in
[`docs/UI_STATUS.md`](docs/UI_STATUS.md).

## Status

v1 scaffold: shared protocol, hub (REST + agent WebSocket + secret vault), and
agent (connection, tmux supervisor with crash-loop protection, jailed files,
RCON) are implemented and covered by integration tests. The web UI covers the
manage-only v1 surface — nodes and enrollment, instances, power, live console,
files, settings and secrets. Roadmap and v2 (provisioning) are in PLAN.md.
