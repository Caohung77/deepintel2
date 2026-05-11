"""
Background pipeline worker.

Watches JOBS_DIR for *.req.json job files. For each, runs the full pipeline
and writes <id>.out.json. Removes the request file when done.

Started by run_app.sh BEFORE Streamlit so the worker process has clean
Network.framework state (no HTTP server initialised in this process).

Layout:
  /tmp/deepintel2_jobs/
    <id>.req.json       Streamlit drops a request here
    <id>.out.json       Worker writes result here
    <id>.err            Worker writes error string here on failure
    <id>.status         Worker writes "running" / "done" / "error"
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pipeline import run_full_pipeline
from fast_extractor import fast_extract

JOBS_DIR = Path(os.environ.get("DEEPINTEL_JOBS_DIR", "/tmp/deepintel2_jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
PID_FILE = JOBS_DIR / "worker.pid"

POLL_INTERVAL = 1.0
RUNNING = True


def _stop(_signum, _frame):  # noqa: ARG001
    global RUNNING  # noqa: PLW0603
    RUNNING = False
    print("[worker] shutdown signal received", flush=True)
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


def _write_pid_file() -> None:
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")


def _remove_pid_file() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink()
    except OSError:
        pass


def _write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


async def _process_job(req_path: Path) -> None:
    job_id = req_path.stem.removesuffix(".req")
    out_path = JOBS_DIR / f"{job_id}.out.json"
    err_path = JOBS_DIR / f"{job_id}.err"
    status_path = JOBS_DIR / f"{job_id}.status"

    try:
        spec = json.loads(req_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _write(err_path, f"bad request file: {e}")
        _write(status_path, "error")
        try:
            req_path.unlink()
        except OSError:
            pass
        return

    _write(status_path, "running")
    mode = spec.get("mode", "full")
    print(f"[worker] {job_id} start ({mode}): {spec.get('url')}", flush=True)
    t0 = time.time()

    try:
        if mode == "fast":
            report = await fast_extract(
                spec["url"],
                with_profile=bool(spec.get("with_profile", True)),
                with_enrichment=bool(spec.get("with_enrichment", True)),
            )
        else:
            report = await run_full_pipeline(
                spec["url"],
                max_pages=int(spec.get("max_pages", 60)),
                max_product_pages=int(spec.get("max_product_pages", 6)),
                with_profile=bool(spec.get("with_profile", True)),
                skip_enrichment=bool(spec.get("skip_enrichment", False)),
            )
        _write(out_path, json.dumps(report, indent=2, ensure_ascii=False, default=str))
        _write(status_path, "done")
        print(f"[worker] {job_id} done in {time.time() - t0:.1f}s", flush=True)
    except Exception as e:  # noqa: BLE001 — surface all errors to client
        _write(err_path, f"{e}\n\n{traceback.format_exc()}")
        _write(status_path, "error")
        print(f"[worker] {job_id} failed: {e}", flush=True)
    finally:
        try:
            req_path.unlink()
        except OSError:
            pass


async def main() -> None:
    _write_pid_file()
    print(f"[worker] pid {os.getpid()} watching {JOBS_DIR}", flush=True)
    while RUNNING:
        try:
            reqs = sorted(JOBS_DIR.glob("*.req.json"))
        except OSError:
            reqs = []
        if not reqs:
            await asyncio.sleep(POLL_INTERVAL)
            continue
        for req in reqs:
            if not RUNNING:
                break
            await _process_job(req)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
    finally:
        _remove_pid_file()
