#!/bin/sh
set -eu

uvicorn app.main:app --host "${RAG_HOST:-0.0.0.0}" --port "${RAG_PORT:-8090}" &
api_pid=$!

python -m app.worker &
worker_pid=$!

terminate() {
  kill "$api_pid" "$worker_pid" 2>/dev/null || true
  wait "$api_pid" "$worker_pid" 2>/dev/null || true
}

trap terminate INT TERM

while true; do
  if ! kill -0 "$api_pid" 2>/dev/null; then
    wait "$api_pid"
    exit $?
  fi

  if ! kill -0 "$worker_pid" 2>/dev/null; then
    wait "$worker_pid"
    exit $?
  fi

  sleep 2
done
