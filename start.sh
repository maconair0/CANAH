#!/usr/bin/env bash
# Start CANAH: the agent's HTTP service and its web page, in the background.
#
#   ./start.sh                     start (http://127.0.0.1:8350)
#   ./start.sh --testbed           also start the emulated domains (packet, optical, fabric) and point the agent at them
#   ./start.sh --port 9000 --host 0.0.0.0 [--tls-cert F --tls-key F]
#   ./start.sh --no-sandbox        without the Docker sandbox
#   ./start.sh --foreground        run in this terminal (ctrl-c stops it); logs go to the screen
#   ./start.sh --status            what is running
#
# Stop with ./teardown.sh. Settings: canah.yaml and .env (copy .env.example). State and logs: $CANAH_HOME
# (default ./canah_data); pid files in $CANAH_HOME/run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

HOST=127.0.0.1
PORT=8350
TESTBED=0
TESTBED_PORT=8401
SANDBOX=1
FOREGROUND=0
TLS_CERT=""
TLS_KEY=""
STATUS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --testbed) TESTBED=1; shift ;;
    --testbed-port) TESTBED_PORT="$2"; shift 2 ;;
    --no-sandbox) SANDBOX=0; shift ;;
    --foreground) FOREGROUND=1; shift ;;
    --tls-cert) TLS_CERT="$2"; shift 2 ;;
    --tls-key) TLS_KEY="$2"; shift 2 ;;
    --status) STATUS=1; shift ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see ./start.sh --help)"; exit 2 ;;
  esac
done

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*"; exit 1; }

# ----- settings: .env, then what the shell already has -----
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
LOGS="$CANAH_HOME/logs"
mkdir -p "$RUN" "$LOGS"

alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

if [ "$STATUS" = 1 ]; then
  say "Controller Agnostic Networked Agent Harness (C.A.N.A.H)"
  if alive "$RUN/canah.pid"; then ok "service: pid $(cat "$RUN/canah.pid"), $(cat "$RUN/canah.url" 2>/dev/null)"; else warn "service: not running"; fi
  if alive "$RUN/testbed.pid"; then ok "testbed domains: pid $(cat "$RUN/testbed.pid"), $(cat "$RUN/testbed.url" 2>/dev/null)"; else warn "testbed domains: not running"; fi
  n=$(docker --context "${CANAH_DOCKER_CONTEXT:-default}" ps -q --filter label=canah.sandbox=1 2>/dev/null | wc -l || echo 0)
  ok "sandbox containers running: $n"
  exit 0
fi

if alive "$RUN/canah.pid"; then
  say "CANAH is already running (pid $(cat "$RUN/canah.pid")): $(cat "$RUN/canah.url" 2>/dev/null)"
  echo "  stop it first with ./teardown.sh"
  exit 0
fi

say "Starting CANAH"

# ----- the Python environment -----
PY="$HERE/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "  creating .venv"
  python3 -m venv .venv
fi
# The package metadata, not an import: importing deepagents or torch costs seconds each.
pkg() { "$PY" -c "import importlib.metadata as m, sys
try: print(m.version(sys.argv[1]))
except m.PackageNotFoundError: pass" "$1" 2>/dev/null; }
if [ -z "$(pkg deepagents)" ] || [ -z "$(pkg fastapi)" ] || [ -z "$(pkg uvicorn)" ]; then
  echo "  installing requirements (first start takes a few minutes)"
  "$PY" -m pip install -q -r requirements.txt
fi
# The re-ranker and the embedder run on an NVIDIA GPU when torch is built for CUDA. pip's default torch is
# CPU-only, so with a GPU and a CPU build, put the CUDA build of the same version in (CANAH_NO_GPU=1 skips it).
TORCH="$(pkg torch)"
GPU="$(command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
if [ -z "${CANAH_NO_GPU:-}" ] && [ -n "$GPU" ] && [ -n "$TORCH" ] && [[ "$TORCH" != *+cu* ]]; then
  TV="${TORCH%%+*}"
  CU="$(nvidia-smi 2>/dev/null | grep -o 'CUDA Version: [0-9.]*' | grep -o '[0-9.]*$' | tr -d .)"
  echo "  installing torch $TV for CUDA (cu$CU) so retrieval runs on the GPU"
  "$PY" -m pip install -q "torch==$TV+cu$CU" --index-url "https://download.pytorch.org/whl/cu$CU" \
      --extra-index-url https://pypi.org/simple 2>/dev/null \
    || echo "  no torch $TV build for cu$CU: retrieval stays on the CPU (see README, Retrieval)"
  TORCH="$(pkg torch)"
fi
if [ -z "${CANAH_NO_GPU:-}" ] && [ -n "$GPU" ] && [[ "$TORCH" == *+cu* ]]; then
  ok "retrieval on the GPU ($GPU, torch $TORCH)"
fi
ok "python environment (deepagents $(pkg deepagents))"

# ----- diagrams: mermaid.js for the page and draw_diagram, Chromium to render with -----
MERMAID_VERSION=11.4.1
MERMAID_JS="$HERE/canah/static/vendor/mermaid.min.js"
if [ ! -s "$MERMAID_JS" ]; then
  mkdir -p "$(dirname "$MERMAID_JS")"
  echo "  fetching mermaid.js $MERMAID_VERSION (diagrams)"
  curl -fsSL -m 120 -o "$MERMAID_JS.tmp" "https://cdn.jsdelivr.net/npm/mermaid@$MERMAID_VERSION/dist/mermaid.min.js" \
    && mv "$MERMAID_JS.tmp" "$MERMAID_JS" || { rm -f "$MERMAID_JS.tmp"; echo "  could not fetch mermaid.js: diagrams are shown as code"; }
fi
if [ -n "$(pkg playwright)" ] && ! ls ~/.cache/ms-playwright/chromium*-*/ >/dev/null 2>&1; then
  echo "  installing Playwright's Chromium (draw_diagram renders with it)"
  "$PY" -m playwright install chromium >/dev/null 2>&1 || echo "  could not install Chromium: draw_diagram will say so"
fi
[ -s "$MERMAID_JS" ] && ok "diagrams (mermaid.js $MERMAID_VERSION$( ls ~/.cache/ms-playwright/chromium*-*/ >/dev/null 2>&1 && echo ", Chromium"))"

# ----- the model -----
MODEL_DESC="$("$PY" -c 'from canah import models; print(models.describe()["model"])' 2>/dev/null || echo "?")"
case "$MODEL_DESC" in
  ollama:*)
    OLLAMA="${OLLAMA_HOST:-http://localhost:11434}"; case "$OLLAMA" in http*) ;; *) OLLAMA="http://$OLLAMA" ;; esac
    NAME="${MODEL_DESC#ollama:}"
    if ! curl -s -m 5 "$OLLAMA/api/tags" >/dev/null; then
      die "Ollama is not answering at $OLLAMA (start it, or choose another model in canah/models.py)"
    fi
    if curl -s -m 5 "$OLLAMA/api/tags" | grep -q "\"$NAME\""; then ok "model $MODEL_DESC (Ollama at $OLLAMA)"
    else warn "model $NAME is not pulled in Ollama: ollama pull $NAME"; fi ;;
  anthropic:*) [ -n "${ANTHROPIC_API_KEY:-}" ] && ok "model $MODEL_DESC" || die "model $MODEL_DESC needs ANTHROPIC_API_KEY in .env" ;;
  openai:*)    [ -n "${OPENAI_API_KEY:-}" ] && ok "model $MODEL_DESC" || warn "model $MODEL_DESC: OPENAI_API_KEY is not set" ;;
  *)           ok "model $MODEL_DESC" ;;
esac

# ----- the sandbox -----
SERVE_ARGS=(--host "$HOST" --port "$PORT")
if [ "$SANDBOX" = 1 ]; then
  CTX="${CANAH_DOCKER_CONTEXT:-default}"
  if docker --context "$CTX" version --format '{{.Server.Version}}' >/dev/null 2>&1; then
    IMG="${CANAH_SANDBOX_IMAGE:-canah-sandbox:1}"
    if ! docker --context "$CTX" image inspect "$IMG" >/dev/null 2>&1; then
      echo "  building the sandbox image $IMG"
      docker --context "$CTX" build -q -t "$IMG" canah/sandbox >/dev/null
    fi
    ok "sandbox: Docker ($CTX), image $IMG"
  else
    warn "Docker ($CTX) is not answering: starting without the sandbox"
    SANDBOX=0
  fi
fi
[ "$SANDBOX" = 0 ] && SERVE_ARGS+=(--no-sandbox)
if [ -n "$TLS_CERT" ] && [ -n "$TLS_KEY" ]; then SERVE_ARGS+=(--tls-cert "$TLS_CERT" --tls-key "$TLS_KEY"); SCHEME=https; else SCHEME=http; fi

# ----- the test bed's domains (optional) -----
if [ "$TESTBED" = 1 ]; then
  if alive "$RUN/testbed.pid"; then
    ok "testbed domains already running: $(cat "$RUN/testbed.url")"
  else
    nohup "$PY" -m testbed.emulator --port "$TESTBED_PORT" >"$LOGS/testbed.log" 2>&1 &
    echo $! >"$RUN/testbed.pid"
    echo "http://127.0.0.1:$TESTBED_PORT" >"$RUN/testbed.url"
    for _ in $(seq 40); do curl -s -m 2 "http://127.0.0.1:$TESTBED_PORT/health" >/dev/null && break; sleep 0.5; done
    curl -s -m 2 "http://127.0.0.1:$TESTBED_PORT/health" >/dev/null || die "the testbed domains did not start (see $LOGS/testbed.log)"
    ok "testbed domains: packet, optical, fabric on http://127.0.0.1:$TESTBED_PORT"
  fi
  if "$PY" -m canah docs add testbed/docs/*.md >"$LOGS/testbed-docs.log" 2>&1; then ok "testbed design rules indexed"
  else warn "the testbed's documents were not indexed (see $LOGS/testbed-docs.log)"; fi
  export AGENTIC_MCP_URL="http://127.0.0.1:$TESTBED_PORT"
  export AGENTIC_MCP_TOKEN=""
  # The test bed's own context and skills, alongside the harness's.
  export CANAH_TESTBED=1
fi

# ----- accounts -----
if [ -z "${CANAH_ADMIN_PASSWORD:-}" ] && [ ! -s "$CANAH_HOME/users.json" ]; then
  warn "no CANAH_ADMIN_PASSWORD in .env and no accounts (python -m canah user add NAME): the documented default passwords apply"
fi

URL="$SCHEME://$HOST:$PORT/"
[ "$HOST" = "0.0.0.0" ] && URL="$SCHEME://127.0.0.1:$PORT/"

if [ "$FOREGROUND" = 1 ]; then
  say "CANAH on $URL (ctrl-c stops it)"
  exec "$PY" -m canah serve "${SERVE_ARGS[@]}"
fi

nohup "$PY" -m canah serve "${SERVE_ARGS[@]}" >"$LOGS/canah.log" 2>&1 &
echo $! >"$RUN/canah.pid"
echo "$URL" >"$RUN/canah.url"
printf '  waiting for the service'
for _ in $(seq 480); do
  if curl -sk -m 2 "${URL}health" >/dev/null 2>&1; then echo; break; fi
  if ! alive "$RUN/canah.pid"; then echo; rm -f "$RUN/canah.pid"; tail -20 "$LOGS/canah.log"; die "CANAH stopped while starting (log above, in $LOGS/canah.log)"; fi
  printf '.'; sleep 0.25
done
curl -sk -m 2 "${URL}health" >/dev/null 2>&1 || die "CANAH did not answer within 2 minutes (see $LOGS/canah.log)"

INFO="$(curl -sk -m 5 "${URL}info" | "$PY" -c "import json,sys; i=json.load(sys.stdin); print(i.get('tools'), 'tools; subagents:', ', '.join(i.get('subagents') or []) + '; sandbox:', (i.get('sandbox') or 'none')[:50])" 2>/dev/null || true)"
ok "service: pid $(cat "$RUN/canah.pid"), $INFO"
say "CANAH is up: $URL"
echo "  web page and API on that address; log: $LOGS/canah.log; stop: ./teardown.sh"
