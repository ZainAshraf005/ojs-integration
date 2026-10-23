#!/usr/bin/env bash
# Local demo helper.   ./run.sh start | stop | status
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p data/logs

running() { pgrep -f "[u]vicorn app.main:app" >/dev/null || pgrep -f "[a]rq app.worker" >/dev/null; }

case "${1:-}" in
  start)
    docker compose up -d db redis
    uv run alembic upgrade head >/dev/null
    if running; then echo "Already running."; else
      nohup uv run uvicorn app.main:app --port 8000 > data/logs/web.log 2>&1 &
      nohup uv run arq app.worker.main.WorkerSettings > data/logs/worker.log 2>&1 &
    fi
    for _ in $(seq 1 40); do curl -s -o /dev/null http://localhost:8000/healthz && break; sleep 0.5; done
    echo
    echo "  Public site:  http://localhost:8000"
    echo "  Dashboard:    http://localhost:8000/admin"
    echo "  Logs:         data/logs/web.log, data/logs/worker.log"
    ;;
  stop)
    pkill -f "[u]vicorn app.main:app" || true
    pkill -f "[a]rq app.worker" || true
    docker compose stop db redis
    echo "Stopped. All data is kept."
    ;;
  status)
    pgrep -f "[u]vicorn app.main:app" >/dev/null && echo "web:    running" || echo "web:    stopped"
    pgrep -f "[a]rq app.worker" >/dev/null && echo "worker: running" || echo "worker: stopped"
    docker compose ps --format "{{.Service}}: {{.Status}}"
    ;;
  *)
    echo "Usage: ./run.sh start | stop | status"; exit 1 ;;
esac
