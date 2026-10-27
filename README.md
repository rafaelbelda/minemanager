# MineManager

Self-hosted control plane for a Minecraft network — Paper/Vanilla servers and
Velocity proxies — across one or more Linux machines.

Version 26.10.26. MIT.

## What it does

- Power: start, stop, restart, kill. Live console, per instance.
- Files: browse, edit, upload, download, rename, delete, zip/unzip. Jailed to
  the instance directory.
- Versions: Vanilla from Mojang, Paper and Velocity from the PaperMC Fill API.
  The jar swap is transactional — download, checksum, backup, atomic replace,
  rollback on failure.
- Provisioning: create a new server under a node's root from the UI.
- Reboot survival: instances marked *start with node* come back when the
  machine does. Crashed ones restart per their `restart_policy`.

What it does not do: authenticate anyone. Put Authelia, or anything
equivalent, in front of it, have that proxy send `MM_PROXY_SECRET` so the hub
can tell it was there, and expose it over WireGuard. See
[`deploy/README.md`](deploy/README.md#the-proxy-is-the-authentication-so-the-hub-checks-it-was-there).

## How it is built

Three Python packages and a static frontend:

| | |
|---|---|
| `shared/` | the hub↔agent wire protocol. Both sides import it, so contracts cannot drift. |
| `hub/`    | FastAPI control plane. SQLite, REST API, serves the UI. |
| `agent/`  | one asyncio daemon per machine. Owns the processes and the files. |
| `web/`    | plain ES modules. No build step, no `node_modules`. |

One rule runs through all of it: **agents dial out to the hub; the hub never
reaches into a node.** Power, console and files are all proxied over the
agent's own outbound WebSocket. A node with no agent connected answers `409`.

The second rule follows from the first: the hub owns *declared* state ("should
be running"), the agent owns *runtime* state ("is it up?"). Where the two
disagree the UI says so rather than papering over it.

systemd supervises the agent. The agent supervises the servers — one `tmux`
session each, no per-server units. Don't add any.

## Deploy

Needs Python 3.11+, and `tmux` on any machine that runs servers.

```bash
git clone https://github.com/rafaelbelda/minemanager.git && cd minemanager
sudo ./deploy/install.sh --hub --agent --dry-run   # preview
sudo ./deploy/install.sh --hub --agent
```

Then edit `/etc/minemanager/hub.env` — at minimum `MM_ALLOWED_HOSTS`, or every
request 400s — enable the unit, and point an agent at the hub with a node token
from the UI.

[`deploy/README.md`](deploy/README.md) is the full procedure: reverse proxy,
the two service users, server roots, WireGuard MTU. Every option is documented
in [`deploy/hub.env.example`](deploy/hub.env.example) and
[`deploy/agent.env.example`](deploy/agent.env.example).

There is no upgrade path. Fresh installs only; back up
`/var/lib/minemanager/minemanager.db` before you replace a release.

## Hack on it

```bash
python -m pip install -e shared -e hub -e agent
MM_DATA_DIR=./_devdata MM_PORT=8730 python -m minemanager_hub
```

Open <http://127.0.0.1:8730/>. The hub serves `web/` at the root — edit and
reload, there is nothing to rebuild. Create a node for a token:

```bash
curl -sX POST localhost:8730/api/nodes -H 'content-type: application/json' -d '{"name":"box-a"}'
```

```bash
MM_HUB_URL=ws://127.0.0.1:8730/ws/agent MM_ENROLL_TOKEN=<token> \
  MM_AGENT_DATA_DIR=./_agentdata python -m minemanager_agent.main
```

Tests live in one root `tests/` tree, because the contract suites span
packages. Anything needing real `tmux` skips itself without one.

```bash
python -m pip install -e shared -e hub -e agent -r requirements-dev.txt && python -m pytest
```

Adding a hub↔agent operation is always the same four steps: a new `Action` and
payload in `shared/minemanager_shared/protocol.py`, a branch in the agent's
`handlers.py`, a route in `hub/minemanager_hub/api/`, then the UI.
