#!/usr/bin/env bash
# Local dev: API (auto-reload) + triage worker + index worker in one terminal, logs prefixed.
# Ctrl-C stops all three. Production runs them as separate processes (see docker-compose.yml).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

TRIAGE_TYPES=triage_ticket
INDEX_TYPES=index_ticket,index_batch,reindex_all,refresh_bm25,load_logs
export PYTHONUNBUFFERED=1

if ! grep -qE '^API_KEY=.+' backend/.env 2>/dev/null; then
  echo "warning: API_KEY is not set in backend/.env; /jobs and the dashboard will answer 401" >&2
fi

docker compose up -d db loki

if [ ! -d frontend/dist ]; then
  echo "building the dashboard (first run only)..."
  (cd frontend && npm install --no-audit --no-fund && npm run build) >/dev/null
fi

cd backend     # the API and workers run from backend/: data/, models/ and .env are found there
uv sync -q
BIN=.venv/bin   # run the programs directly, so a signal reaches them and not a wrapper

PIDS=()
NAMES=()
# run name color cmd...: start cmd in the background, every output line prefixed with its name
run() {
  local name=$1 color=$2; shift 2
  local prefix
  prefix=$(printf '\033[%sm%-7s\033[0m| ' "$color" "$name")
  # exec: the background job becomes the program itself, so $! is its pid and signals reach it
  { exec "$@"; } > >(awk -v p="$prefix" '{ print p $0; fflush() }') 2>&1 &
  PIDS+=($!)
  NAMES+=("$name")
}

stop() {
  trap - INT TERM EXIT
  echo; echo "stopping (workers finish their current job first)..."
  kill -TERM "${PIDS[@]}" 2>/dev/null || true
  wait "${PIDS[@]}" 2>/dev/null || true
  exit 0
}
trap stop INT TERM EXIT

run api    36 "$BIN/uvicorn" triagerag.service.app:app --port 8000 --reload --reload-dir src
run triage 33 "$BIN/python" -m triagerag.jobs.worker --types "$TRIAGE_TYPES"
run index  35 "$BIN/python" -m triagerag.jobs.worker --types "$INDEX_TYPES"

echo "api: http://localhost:8000   dashboard: http://localhost:8000/ui   Ctrl-C to stop"

# if any one of them dies (port in use, crash), stop the rest instead of half-running
while true; do
  for i in "${!PIDS[@]}"; do
    if ! kill -0 "${PIDS[$i]}" 2>/dev/null; then
      echo "${NAMES[$i]} exited; stopping the others" >&2
      exit 1
    fi
  done
  sleep 1
done
