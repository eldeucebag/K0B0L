#!/usr/bin/env bash
# Dedicated Ollama instance for the red-team loop.
#
# Why this exists: flash attention + a quantized KV cache are SERVER-WIDE
# settings, and the system service sets neither. Enabling them globally would
# apply the cache-quantization quality hit to every model the user runs,
# including non-red-team work. This instance gives the harness a 4x smaller
# KV cache on its own port so the shared service keeps fp16 quality.
#
# Verified on this box (GTX 1070, CC 6.1, Ollama 0.32.6): the card runs on
# the cuda_v12 build, where flash attention uses llama.cpp's "vec" kernels and
# K/V both q4_0 is honoured -- llama.cpp logs `flash_attn = enabled` and
# `K (q4_0): 486.00 MiB, V (q4_0): 486.00 MiB`.
#
#   ./rt-fa-server.sh start     # launch on :11436
#   ./rt-fa-server.sh status
#   ./rt-fa-server.sh stop
#
# Then run the loop against it:
#   OLLAMA_URL=http://127.0.0.1:11436 ./thinlizzy.sh
set -uo pipefail

PORT="${RT_FA_PORT:-11436}"
KV="${RT_FA_KV:-q4_0}"
CTX="${RT_FA_CTX:-65536}"
MODELS="${RT_FA_MODELS:-/usr/share/ollama/.ollama/models}"
LOG="${RT_FA_LOG:-/tmp/rt-fa-server.log}"
PIDFILE="/tmp/rt-fa-server.${PORT}.pid"
API="http://127.0.0.1:${PORT}"

up() { curl -sS --max-time 2 "${API}/api/version" >/dev/null 2>&1; }

start() {
    if up; then
        echo "already running on :${PORT} -- $(curl -sS --max-time 5 ${API}/api/version)"
        return 0
    fi
    if [[ ! -r "${MODELS}/blobs" ]]; then
        echo "cannot read model store ${MODELS}" >&2
        return 1
    fi
    OLLAMA_HOST="127.0.0.1:${PORT}" \
    OLLAMA_MODELS="${MODELS}" \
    OLLAMA_FLASH_ATTENTION=1 \
    OLLAMA_KV_CACHE_TYPE="${KV}" \
    OLLAMA_CONTEXT_LENGTH="${CTX}" \
    nohup ollama serve >"${LOG}" 2>&1 &
    echo $! >"${PIDFILE}"
    for _ in $(seq 1 60); do
        if up; then
            echo "up on :${PORT}  KV=${KV}  ctx=${CTX}  pid=$(cat "${PIDFILE}")"
            echo "log: ${LOG}"
            return 0
        fi
        sleep 1
    done
    echo "did not come up; see ${LOG}" >&2
    return 1
}

stop() {
    if [[ -f "${PIDFILE}" ]]; then
        local pid
        pid="$(cat "${PIDFILE}")"
        kill "${pid}" 2>/dev/null && echo "stopped pid ${pid}" || echo "pid ${pid} was not running"
        rm -f "${PIDFILE}"
    else
        echo "no pidfile for :${PORT}"
    fi
}

status() {
    up || { echo "not listening on :${PORT}"; return 1; }
    echo "version: $(curl -sS --max-time 5 "${API}/api/version")"
    curl -sS --max-time 10 "${API}/api/ps" | python3 -c "
import json,sys
ms = json.load(sys.stdin).get('models', [])
if not ms:
    print('nothing loaded')
for m in ms:
    print('  %s  ctx=%s  resident=%.2f GiB  vram=%.2f GiB' % (
        m.get('name'), m.get('context_length'),
        (m.get('size') or 0)/2**30, (m.get('size_vram') or 0)/2**30))
"
}

case "${1:-start}" in
    start)  start ;;
    stop)   stop ;;
    status) status ;;
    *) echo "usage: $0 {start|stop|status}" >&2; exit 2 ;;
esac
