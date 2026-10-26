# Deploying MineManager

Two independent services. Install whichever a machine needs.

- **hub**: the web control plane.
- **agent**: runs on every machine that hosts servers or proxies. Owns the processes (one
  `tmux` session per instance) and the files.

| Machine | Install |
|---|---|
| Hub only | `sudo ./deploy/install.sh --hub` |
| Agent only | `sudo ./deploy/install.sh --agent` |
| Both on one box | `sudo ./deploy/install.sh --hub --agent` |

Add `--dry-run` to see what it would do. Re-run it to upgrade the code; it never
overwrites config you have edited.

## Install

```bash
git clone https://github.com/rafaelbelda/minemanager.git && cd minemanager
sudo ./deploy/install.sh --hub --agent --dry-run   # preview
sudo ./deploy/install.sh --hub --agent
```

It creates the users and directories, installs a venv, generates the vault key,
writes `/etc/minemanager/*.env` and the systemd units while printing every step.

## Configure

**Hub** - `sudoedit /etc/minemanager/hub.env`

Set `MM_ALLOWED_HOSTS` to the hostname clients actually use. It defaults to
loopback only; anything else gets HTTP 400.

```bash
sudo systemctl enable --now minemanager-hub
```

Then, put your reverse proxy and authentication in front. **MineManager Hub does not authenticate end users itself.** For extra security, expose it only through WireGuard.

See [`Caddyfile`](Caddyfile) for a working config. Caddy's defaults already
suit large transfers — no body buffering, no size limit, no timeouts — so the
only thing to get wrong is adding `request_body max_size` or a transport
`read_timeout`, either of which breaks multi-GB uploads. (nginx is the opposite:
its defaults spool both directions to disk and cap bodies at 1 MB.)

If the hub is reached over WireGuard, check the MTU. A tunnel MTU above the real
path MTU, with PMTU discovery blackholed, presents as a few-MB/s ceiling with no
error anywhere; `ping -M do -s 1372 <hub>` from the client finds it.

**Agent** - `sudoedit /etc/minemanager/agent.env`

Set `MM_HUB_URL` (use `wss://`; a plaintext `ws://` to a remote host is refused
unless you set `MM_ALLOW_INSECURE=1`). Create the node in the hub UI, paste its
one-time token in as `MM_ENROLL_TOKEN`, then:

```bash
sudo systemctl enable --now minemanager-agent
```

Once the node shows online, delete the `MM_ENROLL_TOKEN` line.

If your servers do not live under `/srv/minecraft`, set `MM_ROOT_DIR` to the
directory they are in and add that directory to `ReadWritePaths=` in
`/etc/systemd/system/minemanager-agent.service`. The unit runs with
`ProtectSystem=strict`, so anything not listed is read-only.

`MM_ROOT_DIR` is also the agent's own boundary: every instance on the node must
sit inside it, and the agent refuses file, power and console commands for one
that does not. On a node whose servers are spread across several directories,
point it at a parent that covers them all, or move them first — the hub logs
which instances a root leaves out when the agent connects, and the UI marks
them.

## Check

```bash
journalctl -u 'minemanager-*' -n 40 --no-pager
```

Both services print their resolved configuration on startup.
Most misconfiguration is visible in those lines alone.

## Two users, and when you need them

The installer creates `minemanager-hub` and `minemanager-agent`.
On a multi-node setup this matters: every Minecraft server runs as the agent's user,
so any plugin already has code execution as that user. If the hub ran as the same user, a compromised plugin could get code execution across the whole fleet.

On a single machine whose hub manages only itself there is nothing to escalate
to, so one user is mostly fine; You **could** set `User=`/`Group=` in both units
to the same account and give it both data directories.

## Notes

- **Do not create per-server systemd units.** The agent supervises the servers.
- **Back up `/var/lib/minemanager/minemanager.db`** before upgrading. Schema
  migrations run at startup and are not reversible.
- Attach to a server console directly:
  `sudo -u minemanager-agent tmux -L minemanager attach -t mm-<instance_id>`

Every option is documented in [`hub.env.example`](hub.env.example) and
[`agent.env.example`](agent.env.example).
