#!/usr/bin/env bash
# MineManager installer. Run from a checkout of this repo:
#
#   sudo ./deploy/install.sh --hub            # control plane only
#   sudo ./deploy/install.sh --agent          # a node that runs servers
#   sudo ./deploy/install.sh --hub --agent    # both on one machine
#
# Idempotent: safe to re-run to upgrade the code. Never overwrites config you
# have already edited. Add --dry-run to see what it would do.
set -euo pipefail

VENV=/opt/minemanager/venv
ETC=/etc/minemanager
HUB_USER=minemanager-hub
AGENT_USER=minemanager-agent
HUB_DATA=/var/lib/minemanager
AGENT_DATA=/var/lib/minemanager-agent
SERVERS=/srv/minecraft

DO_HUB=0 DO_AGENT=0 DRY=0
for arg in "$@"; do
  case "$arg" in
    --hub) DO_HUB=1 ;;
    --agent) DO_AGENT=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,9p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done
[ $((DO_HUB + DO_AGENT)) -gt 0 ] || { echo "pick --hub and/or --agent (try --help)" >&2; exit 2; }
# A dry run only prints, so it does not need root, that is the point of it.
[ "$DRY" = 1 ] || [ "$(id -u)" -eq 0 ] || { echo "run me with sudo (or --dry-run to preview)" >&2; exit 1; }

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -d "$REPO/shared" ] || { echo "run this from a checkout of the repo" >&2; exit 1; }

run() { if [ "$DRY" = 1 ]; then echo "  + $*"; else "$@"; fi; }
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
[ "$DRY" = 1 ] && say "DRY RUN -> nothing will be changed"

# --- prerequisites ---------------------------------------------------------
# In a dry run these are reported but not fatal, so you can still see the plan.
need() {
  if [ "$DRY" = 1 ]; then echo "  ! missing prerequisite: $1"; else echo "$1" >&2; exit 1; fi
}
command -v python3 >/dev/null || need "python3 is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null \
  || need "python 3.11+ is required"
[ "$DO_AGENT" = 0 ] || command -v tmux >/dev/null \
  || need "tmux is required on a node (apt install tmux / dnf install tmux)"

# --- code ------------------------------------------------------------------
say "Installing code into $VENV"
run mkdir -p /opt/minemanager "$ETC"
[ -d "$VENV" ] || run python3 -m venv "$VENV"
PKGS=("$REPO/shared")
[ "$DO_HUB" = 1 ] && PKGS+=("$REPO/hub")
[ "$DO_AGENT" = 1 ] && PKGS+=("$REPO/agent")
run "$VENV/bin/pip" install --quiet --upgrade "${PKGS[@]}"
note "installed: ${PKGS[*]##*/}"

# --- hub -------------------------------------------------------------------
if [ "$DO_HUB" = 1 ]; then
  say "Hub"
  id -u "$HUB_USER" >/dev/null 2>&1 \
    || run useradd --system --home "$HUB_DATA" --shell /usr/sbin/nologin "$HUB_USER"
  run mkdir -p "$HUB_DATA"
  run chown "$HUB_USER:$HUB_USER" "$HUB_DATA"
  run chmod 0750 "$HUB_DATA"

  if [ -f "$ETC/hub.env" ]; then
    note "$ETC/hub.env exists, left alone"
  else
    run install -o root -g "$HUB_USER" -m 0640 "$REPO/deploy/hub.env.example" "$ETC/hub.env"
    note "wrote $ETC/hub.env"
  fi
  run install -m 0644 "$REPO/deploy/minemanager-hub.service" /etc/systemd/system/
fi

# --- agent -----------------------------------------------------------------
if [ "$DO_AGENT" = 1 ]; then
  say "Agent"
  id -u "$AGENT_USER" >/dev/null 2>&1 \
    || run useradd --system --home "$AGENT_DATA" --shell /usr/sbin/nologin "$AGENT_USER"
  run mkdir -p "$AGENT_DATA" "$SERVERS"
  run chown "$AGENT_USER:$AGENT_USER" "$AGENT_DATA" "$SERVERS"
  run chmod 0750 "$AGENT_DATA"

  if [ -f "$ETC/agent.env" ]; then
    note "$ETC/agent.env exists, left alone"
  else
    run install -o root -g "$AGENT_USER" -m 0640 "$REPO/deploy/agent.env.example" "$ETC/agent.env"
    note "wrote $ETC/agent.env"
  fi
  run install -m 0644 "$REPO/deploy/minemanager-agent.service" /etc/systemd/system/
fi

run systemctl daemon-reload

# --- what's left for a human ----------------------------------------------
say "Done. Remaining steps (config, so not automated):"
if [ "$DO_HUB" = 1 ]; then
  note "1. sudoedit $ETC/hub.env"
  note "   set MM_ALLOWED_HOSTS to the hostname clients use, or the hub 400s every request"
  note "2. sudo systemctl enable --now minemanager-hub"
  note "3. put your reverse proxy + auth in front; expose only over WireGuard"
fi
if [ "$DO_AGENT" = 1 ]; then
  note "$([ "$DO_HUB" = 1 ] && echo 4 || echo 1). sudoedit $ETC/agent.env  -> set MM_HUB_URL"
  note "$([ "$DO_HUB" = 1 ] && echo 5 || echo 2). create the node in the hub UI, paste its token in as MM_ENROLL_TOKEN"
  note "$([ "$DO_HUB" = 1 ] && echo 6 || echo 3). sudo systemctl enable --now minemanager-agent"
  note "   servers live under $SERVERS; widen ReadWritePaths in the unit if yours do not"
fi
note ""
note "Check it worked:  journalctl -u 'minemanager-*' -n 40 --no-pager"
