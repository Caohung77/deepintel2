#!/usr/bin/env bash
# Start worker daemon, then Streamlit. PID-based watchdog: if either dies,
# the container exits so Docker can restart it.

set -e

cd /app

mkdir -p "$DEEPINTEL_JOBS_DIR" "$DEEPINTEL_CACHE_DIR"

# Start worker in background (used by Streamlit UI)
python /app/worker.py &
WORKER_PID=$!
echo "[entrypoint] worker pid: $WORKER_PID"

# Start FastAPI in background
uvicorn api.server:app --host 0.0.0.0 --port 8000 --no-access-log &
API_PID=$!
echo "[entrypoint] api pid: $API_PID"

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

term_handler() {
  echo "[entrypoint] shutdown signal — stopping children"
  kill -TERM "$WORKER_PID" "$STREAMLIT_PID" "$API_PID" 2>/dev/null || true
  wait "$WORKER_PID" "$STREAMLIT_PID" "$API_PID" 2>/dev/null || true
  exit 0
}
trap term_handler SIGTERM SIGINT

while true; do
  for pid in "$WORKER_PID" "$STREAMLIT_PID" "$API_PID"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "[entrypoint] child $pid died — exiting so container restarts"
      kill -TERM "$WORKER_PID" "$STREAMLIT_PID" "$API_PID" 2>/dev/null || true
      exit 1
    fi
  done
  sleep 5
done
