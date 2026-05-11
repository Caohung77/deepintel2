#!/usr/bin/env bash
# Start worker daemon, then Streamlit. PID-based watchdog: if either dies,
# the container exits so Docker can restart it.

set -e

cd /app

mkdir -p "$DEEPINTEL_JOBS_DIR" "$DEEPINTEL_CACHE_DIR"

# Start worker in background
python /app/worker.py &
WORKER_PID=$!
echo "[entrypoint] worker pid: $WORKER_PID"

# Start Streamlit in background
streamlit run /app/app.py \
  --server.headless true \
  --server.port 8501 \
  --server.address 0.0.0.0 \
  --server.enableCORS false \
  --server.enableXsrfProtection false \
  --browser.gatherUsageStats false &
STREAMLIT_PID=$!
echo "[entrypoint] streamlit pid: $STREAMLIT_PID"

# Forward SIGTERM / SIGINT to children for graceful shutdown
term_handler() {
  echo "[entrypoint] shutdown signal — stopping children"
  kill -TERM "$WORKER_PID" "$STREAMLIT_PID" 2>/dev/null || true
  wait "$WORKER_PID" "$STREAMLIT_PID" 2>/dev/null || true
  exit 0
}
trap term_handler SIGTERM SIGINT

# Exit if either child dies
while true; do
  if ! kill -0 "$WORKER_PID" 2>/dev/null; then
    echo "[entrypoint] worker died — exiting so container restarts"
    kill -TERM "$STREAMLIT_PID" 2>/dev/null || true
    exit 1
  fi
  if ! kill -0 "$STREAMLIT_PID" 2>/dev/null; then
    echo "[entrypoint] streamlit died — exiting so container restarts"
    kill -TERM "$WORKER_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 5
done
