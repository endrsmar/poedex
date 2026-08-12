#!/usr/bin/env bash
#
# Build the plugin and push it to a Steam Deck.
#
# The install is four steps that are easy to get subtly wrong — the unzip needs
# sudo because Decky owns ~/homebrew/plugins, the tree needs chown back to deck
# because the loader setuids plugin children to that user, the directory name has
# to match plugin.json, and the loader needs restarting. Getting one wrong fails
# in a way that looks like a code problem.
#
# Configure once in .env (gitignored, and this script refuses to run if that ever
# stops being true — the repo is public):
#
#     DECK_IP=192.168.1.42
#     DECK_USER=deck            # optional, defaults to deck
#     DECK_PASSWORD=hunter2     # optional; omit it and SSH keys are used
#
# Usage:
#     scripts/deploy-deck.sh              # full build, install, restart
#     scripts/deploy-deck.sh --fast       # skip re-vendoring py_modules (~8 MB)
#     scripts/deploy-deck.sh --logs       # ...and tail the loader afterwards
#     scripts/deploy-deck.sh --logs-only  # just tail, deploy nothing

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
step() { printf '\n\033[36m▸ %s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

FAST=0 LOGS=0 LOGS_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    --logs) LOGS=1 ;;
    --logs-only) LOGS_ONLY=1 ;;
    -h|--help) awk 'NR>2 && /^#/ {sub(/^# ?/,""); print; next} NR>2 {exit}' "$0"; exit 0 ;;
    *) die "unknown option $arg (try --help)" ;;
  esac
done

# -- config -------------------------------------------------------------------

[[ -f .env ]] || die $'no .env. Create one:\n\n    DECK_IP=192.168.1.42\n    DECK_PASSWORD=your-deck-password   # or omit, and use SSH keys\n'

# A password is about to be read from a file in a public repo's working tree. If
# .env were ever committed it would be on GitHub within the hour, so this is
# checked every run rather than trusted to stay true.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  die ".env is TRACKED BY GIT and this repo is public. Run: git rm --cached .env"
fi
git check-ignore -q .env || die ".env is not gitignored, and this repo is public. Add it to .gitignore first."

set -a; . ./.env; set +a
DECK_IP="${DECK_IP:-}"
DECK_USER="${DECK_USER:-deck}"
DECK_PASSWORD="${DECK_PASSWORD:-}"
[[ -n "$DECK_IP" ]] || die "DECK_IP is not set in .env"

PLUGIN_NAME="$(python3 -c 'import json;print(json.load(open("plugin/plugin.json"))["name"])')"
REMOTE="$DECK_USER@$DECK_IP"

# SteamOS is reimaged by updates, so its host key legitimately changes. Accepting
# it silently is the pragmatic choice for a LAN device you own; it is called out
# here rather than hidden in a flag.
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$HOME/.ssh/known_hosts")

if [[ -n "$DECK_PASSWORD" ]]; then
  command -v sshpass >/dev/null || die "DECK_PASSWORD is set but sshpass is not installed (apt install sshpass)"
  SSH=(sshpass -e ssh "${SSH_OPTS[@]}")
  SCP=(sshpass -e scp "${SSH_OPTS[@]}")
  export SSHPASS="$DECK_PASSWORD"   # -e, so it never appears in ps or the shell history
else
  SSH=(ssh "${SSH_OPTS[@]}")
  SCP=(scp "${SSH_OPTS[@]}")
fi

# `sudo` over ssh prompts on a tty that is not there, so it hangs or fails. When a
# password is configured, prime sudo's timestamp once by feeding it on **stdin** —
# not on the command line, where it would be visible in the Deck's process list —
# and every later sudo in the same session runs from the cache.
remote_sudo() {
  local script="$1"
  if [[ -n "$DECK_PASSWORD" ]]; then
    printf '%s\n' "$DECK_PASSWORD" | "${SSH[@]}" "$REMOTE" "sudo -S -p '' -v && { $script; }"
  else
    # No password configured: either sudo is passwordless, or ssh needs a tty so
    # the user can answer. -t gives them the chance rather than hanging silently.
    "${SSH[@]}" -t "$REMOTE" "sudo -v && { $script; }"
  fi
}

tail_logs() {
  step "tailing plugin_loader on $DECK_IP (Ctrl-C to stop)"
  "${SSH[@]}" "$REMOTE" "journalctl -u plugin_loader -f -n 40"
}

if (( LOGS_ONLY )); then tail_logs; exit 0; fi

# -- build --------------------------------------------------------------------

PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3.12 || command -v python3)"

step "building the Decky frontend bundle"
( cd surfaces/decky && pnpm run build >/dev/null ) || die "frontend build failed"
note "surfaces/decky/dist/index.js"

step "assembling the plugin"
if (( FAST )); then
  # py_modules is ~8 MB of vendored wheels that only change when a dependency
  # does. Skipping it turns a two-minute build into a couple of seconds, which is
  # the difference between iterating and waiting.
  "$PY" scripts/build_plugin.py --no-vendor --no-zip >/dev/null || die "plugin build failed"
  note "source only — py_modules left as it is on the Deck"
  PAYLOAD="dist/$PLUGIN_NAME"
else
  "$PY" scripts/build_plugin.py >/dev/null || die "plugin build failed"
  note "$(du -h "dist/$PLUGIN_NAME.zip" | cut -f1) → dist/$PLUGIN_NAME.zip"
  PAYLOAD="dist/$PLUGIN_NAME.zip"
fi

# -- install ------------------------------------------------------------------

step "checking $REMOTE"
"${SSH[@]}" -o ConnectTimeout=8 "$REMOTE" true 2>/dev/null \
  || die "cannot reach $REMOTE. Is the Deck awake, on the network, and is SSH enabled (Settings → System → Developer Mode → Enable SSH)?"

if (( FAST )); then
  step "syncing source to $REMOTE"
  # --no-vendor leaves py_modules out of the payload entirely, so this must not
  # delete the copy already on the Deck.
  RSYNC_RSH="${SSH[*]}"
  rsync -a --delete --exclude py_modules --rsh="$RSYNC_RSH" \
    "$PAYLOAD/" "$REMOTE:/tmp/$PLUGIN_NAME/" || die "rsync failed"
  remote_sudo "
    set -e
    sudo rsync -a --delete --exclude py_modules /tmp/$PLUGIN_NAME/ /home/$DECK_USER/homebrew/plugins/$PLUGIN_NAME/
    sudo chown -R $DECK_USER:$DECK_USER /home/$DECK_USER/homebrew/plugins/$PLUGIN_NAME
    rm -rf /tmp/$PLUGIN_NAME
  " || die "install failed"
else
  step "copying to $REMOTE"
  "${SCP[@]}" -q "$PAYLOAD" "$REMOTE:/tmp/" || die "scp failed"
  step "installing"
  remote_sudo "
    set -e
    sudo unzip -qo /tmp/$PLUGIN_NAME.zip -d /home/$DECK_USER/homebrew/plugins/
    sudo chown -R $DECK_USER:$DECK_USER /home/$DECK_USER/homebrew/plugins/$PLUGIN_NAME
    rm -f /tmp/$PLUGIN_NAME.zip
  " || die "install failed"
fi
note "/home/$DECK_USER/homebrew/plugins/$PLUGIN_NAME"

step "restarting plugin_loader"
remote_sudo "sudo systemctl restart plugin_loader" || die "restart failed"

printf '\n\033[32m✓ %s deployed to %s\033[0m\n' "$PLUGIN_NAME" "$DECK_IP"
note "open the QAM → PoEDex. If the panel is blank, the reason is in the log:"
note "  scripts/deploy-deck.sh --logs-only"

if (( LOGS )); then tail_logs; fi
