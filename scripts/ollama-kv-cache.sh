#!/usr/bin/env bash
# Two KV-cache slots for Ollama in the GPU memory of one: flash attention, an 8-bit KV cache, two parallel
# slots. Each caller (CANAH's main agent, a subagent, another application) keeps its own cached prompt, and
# Ollama sends each request to the slot whose cache matches it best. See README, "Context window and KV cache".
#
#   sudo bash scripts/ollama-kv-cache.sh          apply and restart Ollama
#   sudo bash scripts/ollama-kv-cache.sh --undo   back to Ollama's defaults
set -euo pipefail
DIR=/etc/systemd/system/ollama.service.d
CONF=$DIR/canah-kv-cache.conf
if [ "$(id -u)" != 0 ]; then echo "run it with sudo"; exit 1; fi
if [ "${1:-}" = "--undo" ]; then
  rm -f "$CONF"
else
  mkdir -p "$DIR"
  cat >"$CONF" <<'EOF'
[Service]
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
Environment="OLLAMA_NUM_PARALLEL=2"
EOF
fi
systemctl daemon-reload
systemctl restart ollama
for _ in $(seq 30); do curl -s -m 2 http://127.0.0.1:11434/api/version >/dev/null && break; sleep 1; done
systemctl show ollama -p Environment | tr ' ' '\n' | grep -E "OLLAMA_(FLASH|KV|NUM_PARALLEL)" || echo "Ollama defaults"
echo "Ollama restarted: $(curl -s http://127.0.0.1:11434/api/version)"
