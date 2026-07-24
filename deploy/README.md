# Deploying MineManager

MineManager is a **hub + agents** system (see [`../PLAN.md`](../PLAN.md)). One
machine runs the **hub** (web control plane); **every** managed machine — including
the hub's own box, if it hosts servers — runs an **agent**.

## Hub

The hub sits behind Authelia + WireGuard and does not authenticate end users
itself. It needs a secret-vault key from the environment.

```bash
# Generate a vault key ONCE and keep it safe (systemd EnvironmentFile / secret mgr):
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

MM_SECRET_KEY=<that-key> \
MM_DATA_DIR=/var/lib/minemanager \
MM_HOST=127.0.0.1 MM_PORT=8730 \
  python -m minemanager_hub        # or the installed `minemanager-hub`
```

> Use the `minemanager-hub` launcher (above), **not** bare
> `uvicorn minemanager_hub.main:app` — the raw uvicorn CLI ignores
> `MM_HOST`/`MM_PORT` and binds to its own default `127.0.0.1:8000`.

Put it behind your reverse proxy (Authelia) and expose it only on the WireGuard
interface. Never expose it to the public internet.

## Agent (per node)

The agent is the **only** MineManager daemon on a node. systemd supervises the
agent; the agent supervises the Minecraft servers in tmux sessions. **Do not
create per-server systemd units.**

Requirements on the node: Python 3.11+, `tmux`, and a non-root user that owns the
server directories (e.g. `minecraft`).

```bash
# 1. install the agent into a venv
sudo mkdir -p /opt/minemanager && cd /opt/minemanager
sudo python3 -m venv venv
sudo ./venv/bin/pip install ./shared ./agent   # from a checkout of this repo

# 2. config
sudo mkdir -p /etc/minemanager /var/lib/minemanager-agent
sudo cp deploy/agent.env.example /etc/minemanager/agent.env
sudoedit /etc/minemanager/agent.env             # set MM_HUB_URL

# 3. enrollment: create the node in the hub UI, copy the one-time token into
#    agent.env as MM_ENROLL_TOKEN, then:
sudo cp deploy/minemanager-agent.service /etc/systemd/system/
sudoedit /etc/systemd/system/minemanager-agent.service   # set User + ReadWritePaths
sudo systemctl daemon-reload
sudo systemctl enable --now minemanager-agent

# 4. once "online" in the hub, remove MM_ENROLL_TOKEN from agent.env.
#    The agent has persisted its long-lived credential to identity.json.
```

## Recommended server layout

Each instance is a directory the agent user can read/write, containing the jar
and (for consoles) writing to `logs/latest.log`. You declare its `root_dir` and
`start_command` in the hub when adding the instance; the agent launches that
command inside a tmux session named `mm-<instance_id>`.

You can always attach to a running server's console directly for debugging:

```bash
sudo -u minecraft tmux attach -t mm-<instance_id>
```
