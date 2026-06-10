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

# Kill any prior worker. The process is launched as `python worker.py` (relative,
# after the cd above), so its cmdline never contains "deepintel2/worker.py" — the old
# pattern matched nothing and stale workers piled up. Kill the precise prior worker via
# the pidfile it writes at startup, then sweep any stragglers by the real cmdline.
PRIOR_PID_FILE="$DEEPINTEL_JOBS_DIR/worker.pid"
[ -f "$PRIOR_PID_FILE" ] && kill "$(cat "$PRIOR_PID_FILE")" 2>/dev/null || true
pkill -f "[w]orker.py" 2>/dev/null || true
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
