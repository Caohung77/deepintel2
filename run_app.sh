#!/usr/bin/env bash
# Launch worker daemon FIRST (clean Network.framework state),
# then Streamlit UI. Streamlit talks to worker via filesystem jobs/.

set -e
cd "$(dirname "$0")"

export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
export PYTHONUNBUFFERED=1
export DEEPINTEL_JOBS_DIR="${DEEPINTEL_JOBS_DIR:-/tmp/deepintel2_jobs}"

mkdir -p "$DEEPINTEL_JOBS_DIR"
mkdir -p /tmp/deepintel2_logs

# Kill any prior worker
pkill -f "deepintel2/worker.py" 2>/dev/null || true
sleep 1

# Start worker BEFORE streamlit so it inherits clean process state
.venv/bin/python worker.py > /tmp/deepintel2_logs/worker.log 2>&1 &
WORKER_PID=$!
echo "[run_app] worker pid: $WORKER_PID"

# Make sure worker dies when this script exits
trap "kill $WORKER_PID 2>/dev/null || true" EXIT

# Wait briefly for worker to start
sleep 1

# Now start Streamlit
exec .venv/bin/streamlit run app.py \
  --server.headless true \
  --server.port 8501 \
  --browser.gatherUsageStats false
