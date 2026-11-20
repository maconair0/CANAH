#!/usr/bin/env bash
# Stop what ./start.sh started: the service (and its web page), the testbed domains, and the sandboxes.
#
#   ./teardown.sh                 stop the service and the testbed domains; remove sandbox containers left behind
#   ./teardown.sh --purge         also delete the state in $CANAH_HOME (threads, memories, audit, sessions,
#                                 indexed documents) and the sandbox's workspace volume; asks first
#   ./teardown.sh --purge --yes   the same without asking
#
# Processes are stopped through their pid files ($CANAH_HOME/run), never by matching names: SIGTERM first
# (the agent closes its sandbox and its databases), SIGKILL only if it has not stopped after 20 s.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PURGE=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see ./teardown.sh --help)"; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

# .env fills in what the shell has not set (a variable already set in the shell wins, as in canah/models.py).
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key#export }"; key="${key// /}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [ -z "${!key+x}" ]; then
      val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
      export "$key=$val"
    fi
  done < .env
fi
export CANAH_HOME="${CANAH_HOME:-$HERE/canah_data}"
RUN="$CANAH_HOME/run"
CTX="${CANAH_DOCKER_CONTEXT:-default}"

stop() {   # stop <name> <pid file>
  local name="$1" pidfile="$2" pid
  if [ ! -f "$pidfile" ]; then ok "$name: not running"; return; fi
  pid="$(cat "$pidfile")"
  if ! kill -0 "$pid" 2>/dev/null; then rm -f "$pidfile"; ok "$name: not running (stale pid file removed)"; return; fi
  kill -TERM "$pid" 2>/dev/null
  for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null
    warn "$name: did not stop on SIGTERM within 20 s; killed (pid $pid)"
  else
    ok "$name: stopped (pid $pid)"
  fi
  rm -f "$pidfile"
}

say "Stopping CANAH"
stop "service" "$RUN/canah.pid"
stop "testbed domains" "$RUN/testbed.pid"
rm -f "$RUN/canah.url" "$RUN/testbed.url"

# Sandboxes: those of agents on this machine that are no longer running (the service just stopped, or a
# process that was killed). A sandbox whose agent is still running elsewhere on this host is left alone.
if docker --context "$CTX" version >/dev/null 2>&1; then
  host="$(hostname)"
  removed=0 kept=0
  while IFS=$'\t' read -r name owner; do
    [ -z "$name" ] && continue
    pid="${owner##*:}"; ohost="${owner%:*}"
    if [ -n "$owner" ] && [ "$ohost" = "$host" ] && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kept=$((kept + 1)); continue
    fi
    docker --context "$CTX" rm -f "$name" >/dev/null 2>&1 && removed=$((removed + 1))
  done < <(docker --context "$CTX" ps -a --filter label=canah.sandbox=1 --format '{{.Names}}{{"\t"}}{{.Label "canah.owner"}}')
  ok "sandbox containers removed: $removed$([ "$kept" -gt 0 ] && echo ", kept $kept of agents still running")"
else
  warn "Docker ($CTX) is not answering: sandbox containers not checked"
fi

if [ "$PURGE" = 1 ]; then
  if [ "$YES" != 1 ]; then
    printf '  delete everything in %s (threads, memories, audit log, sessions, accounts, indexed documents) and the sandbox workspace? [y/N] ' "$CANAH_HOME"
    read -r answer
    case "$answer" in y|Y|yes) ;; *) say "Kept the state."; exit 0 ;; esac
  fi
  rm -rf "${CANAH_HOME:?}"
  ok "state deleted: $CANAH_HOME"
  if docker --context "$CTX" version >/dev/null 2>&1; then
    docker --context "$CTX" volume rm -f "${CANAH_SANDBOX_WORKSPACE:-canah-workspace}" >/dev/null 2>&1 \
      && ok "sandbox workspace volume deleted"
  fi
fi
say "CANAH is stopped."
