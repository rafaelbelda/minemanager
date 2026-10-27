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
WEB=/opt/minemanager/web
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
[ -d "$REPO/shared" ] && [ -d "$REPO/web" ] || { echo "run this from a checkout of the repo" >&2; exit 1; }

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
# Debian/Ubuntu ship venv without ensurepip unless python3-venv is installed.
python3 -c 'import ensurepip, venv' 2>/dev/null \
  || need "python3 venv support is required (apt install python3-venv)"
[ "$DO_AGENT" = 0 ] || command -v tmux >/dev/null \
  || need "tmux is required on a node (apt install tmux / dnf install tmux)"
# Not fatal: an instance can name its own JDK (java_home).
[ "$DO_AGENT" = 0 ] || command -v java >/dev/null \
  || echo "  ! no java on PATH; servers will need a per-instance java_home"

# --- code ------------------------------------------------------------------
say "Installing code into $VENV"
run mkdir -p /opt/minemanager "$ETC"
[ -d "$VENV" ] || run python3 -m venv "$VENV"
# Built from a copy, so pip leaves no root-owned build/ or *.egg-info behind in
# the checkout.
PKGS=(shared)
[ "$DO_HUB" = 1 ] && PKGS+=(hub)
[ "$DO_AGENT" = 1 ] && PKGS+=(agent)
if [ "$DRY" = 1 ]; then
  echo "  + pip install --upgrade ${PKGS[*]} (from a temporary copy of $REPO)"
else
  SRC="$(mktemp -d)"
  trap 'rm -rf "$SRC"' EXIT
  for p in "${PKGS[@]}"; do cp -r "$REPO/$p" "$SRC/$p"; done
  "$VENV/bin/pip" install --quiet --upgrade "${PKGS[@]/#/$SRC/}"
fi
note "installed: ${PKGS[*]}"

# --- hub -------------------------------------------------------------------
if [ "$DO_HUB" = 1 ]; then
  say "Hub"
  id -u "$HUB_USER" >/dev/null 2>&1 \
    || run useradd --system --user-group --home "$HUB_DATA" --shell /usr/sbin/nologin "$HUB_USER"
  run mkdir -p "$HUB_DATA"
  run chown "$HUB_USER:$HUB_USER" "$HUB_DATA"
  run chmod 0750 "$HUB_DATA"

  # The UI. The installed package cannot find web/ in the checkout, so it gets
  # a root-owned copy, replaced whole so deleted files do not linger. The unit
  # points MM_WEB_DIR here.
  run rm -rf "$WEB.new"
  run cp -r "$REPO/web" "$WEB.new"
  run chmod -R u=rwX,go=rX "$WEB.new"
  run rm -rf "$WEB"
  run mv "$WEB.new" "$WEB"
  note "UI copied to $WEB"

  # Env files are root-only; systemd reads them as root.
  if [ -f "$ETC/hub.env" ]; then
    note "$ETC/hub.env exists, left alone"
    grep -qE '^MM_PROXY_SECRET=.{32,}' "$ETC/hub.env" 2>/dev/null \
      || note "! $ETC/hub.env has no MM_PROXY_SECRET: see deploy/README.md (Proxy secret)"
  else
    run install -o root -g root -m 0600 "$REPO/deploy/hub.env.example" "$ETC/hub.env"
    # Secret is never printed; proxy.env is the reverse proxy's copy.
    if [ "$DRY" = 1 ]; then
      echo "  + generate MM_PROXY_SECRET into $ETC/hub.env and $ETC/proxy.env"
    else
      secret="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
      sed -i "s|^MM_PROXY_SECRET=.*|MM_PROXY_SECRET=$secret|" "$ETC/hub.env"
      ( umask 077; printf 'MM_PROXY_SECRET=%s\n' "$secret" > "$ETC/proxy.env" )
      unset secret
    fi
    note "wrote $ETC/hub.env and $ETC/proxy.env (generated MM_PROXY_SECRET)"
  fi
  run install -m 0644 "$REPO/deploy/minemanager-hub.service" /etc/systemd/system/
fi

# --- agent -----------------------------------------------------------------
if [ "$DO_AGENT" = 1 ]; then
  say "Agent"
  id -u "$AGENT_USER" >/dev/null 2>&1 \
    || run useradd --system --user-group --home "$AGENT_DATA" --shell /usr/sbin/nologin "$AGENT_USER"
  # Server configs hold secrets (RCON passwords), so a new servers dir is not
  # world-readable. An existing one keeps the permissions you gave it.
  [ -d "$SERVERS" ] || { run mkdir -p "$SERVERS"; run chmod 0750 "$SERVERS"; }
  run mkdir -p "$AGENT_DATA"
  run chown "$AGENT_USER:$AGENT_USER" "$AGENT_DATA" "$SERVERS"
  run chmod 0750 "$AGENT_DATA"

  if [ -f "$ETC/agent.env" ]; then
    note "$ETC/agent.env exists, left alone"
  else
    # Not readable by the agent user (= the servers): holds the enroll token.
    run install -o root -g root -m 0600 "$REPO/deploy/agent.env.example" "$ETC/agent.env"
    note "wrote $ETC/agent.env"
  fi
  run install -m 0644 "$REPO/deploy/minemanager-agent.service" /etc/systemd/system/
fi

run systemctl daemon-reload

# A re-run upgrades the code but running services keep the old one. Not
# restarted here: restarting the agent stops every server on the node.
if [ "$DRY" = 0 ]; then
  for unit in $([ "$DO_HUB" = 1 ] && echo minemanager-hub) $([ "$DO_AGENT" = 1 ] && echo minemanager-agent); do
    if systemctl is-active --quiet "$unit"; then
      note "! $unit is still running the previous code: sudo systemctl restart $unit"
    fi
  done
fi

# --- what's left for a human ----------------------------------------------
say "Done. Remaining steps (config, so not automated):"
if [ "$DO_HUB" = 1 ]; then
  note "1. sudoedit $ETC/hub.env"
  note "   set MM_ALLOWED_HOSTS to the hostname clients use, or the hub 400s every request"
  note "2. sudo systemctl enable --now minemanager-hub"
  note "3. put your reverse proxy + auth in front (deploy/Caddyfile); give it"
  note "   $ETC/proxy.env as an EnvironmentFile. Expose only over WireGuard"
fi
if [ "$DO_AGENT" = 1 ]; then
  note "$([ "$DO_HUB" = 1 ] && echo 4 || echo 1). sudoedit $ETC/agent.env  -> set MM_HUB_URL"
  note "$([ "$DO_HUB" = 1 ] && echo 5 || echo 2). create the node in the hub UI, paste its token in as MM_ENROLL_TOKEN"
  note "$([ "$DO_HUB" = 1 ] && echo 6 || echo 3). sudo systemctl enable --now minemanager-agent"
  note "   servers live under $SERVERS; if yours do not, set MM_ROOT_DIR and widen"
  note "   ReadWritePaths in the unit to match"
fi
note ""
note "Check it worked:  journalctl -u 'minemanager-*' -n 40 --no-pager"
