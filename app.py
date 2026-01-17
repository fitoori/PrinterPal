#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from printerpal.web import create_app
from printerpal.util import PrinterPalError, run_cmd


app = create_app()


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but we might not be able to signal it.
        return True


def _read_pid(pidfile: str) -> Optional[int]:
    try:
        data = Path(pidfile).read_text(encoding="utf-8").strip()
        return int(data)
    except Exception:
        return None


def _write_pid(pidfile: str, pid: int) -> None:
    p = Path(pidfile)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{pid}\n", encoding="utf-8")


def _stop(pidfile: str, *, timeout_s: float = 10.0) -> int:
    pid = _read_pid(pidfile)
    if not pid:
        print(f"No pidfile found at {pidfile}", file=sys.stderr)
        return 1

    if not _pid_is_running(pid):
        print(f"Process {pid} not running; removing stale pidfile.")
        try:
            Path(pidfile).unlink(missing_ok=True)
        except Exception:
            pass
        return 0

    print(f"Stopping PrinterPal (pid {pid})…")
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError as e:
        print(f"Permission error stopping pid {pid}: {e}", file=sys.stderr)
        return 1

    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if not _pid_is_running(pid):
            try:
                Path(pidfile).unlink(missing_ok=True)
            except Exception:
                pass
            print("Stopped.")
            return 0
        time.sleep(0.2)

    print("Timed out waiting for process to stop.", file=sys.stderr)
    return 1


def _daemonize(host: str, port: int, pidfile: str, access_log: str, error_log: str, workers: int, threads: int) -> int:
    existing = _read_pid(pidfile)
    if existing and _pid_is_running(existing):
        print(f"PrinterPal already running (pid {existing}).", file=sys.stderr)
        return 1

    argv = [
        sys.executable,
        "-m",
        "gunicorn",
        "--daemon",
        "--bind",
        f"{host}:{port}",
        "--workers",
        str(workers),
        "--threads",
        str(threads),
        "--pid",
        pidfile,
        "--access-logfile",
        access_log,
        "--error-logfile",
        error_log,
        "--timeout",
        "120",
        "app:app",
    ]

    try:
        run_cmd(argv, timeout_s=20.0, check=True)
    except PrinterPalError as e:
        print(f"Failed to start gunicorn daemon: {e}", file=sys.stderr)
        return 1

    pid = _read_pid(pidfile)
    if not pid:
        print("Gunicorn started, but pidfile was not created.", file=sys.stderr)
        return 1

    print(f"PrinterPal daemon started (pid {pid}) on http://{host}:{port}/")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="PrinterPal")
    parser.add_argument("--host", default=os.environ.get("PRINTERPAL_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PRINTERPAL_PORT", "80")))
    parser.add_argument("--debug", action="store_true")

    parser.add_argument("--daemon", action="store_true", help="Run under gunicorn in daemon mode")
    parser.add_argument("--stop", action="store_true", help="Stop daemon mode (uses --pidfile)")
    parser.add_argument("--pidfile", default=os.environ.get("PRINTERPAL_PIDFILE", "/tmp/printerpal.pid"))
    parser.add_argument("--access-log", default=os.environ.get("PRINTERPAL_ACCESS_LOG", "/tmp/printerpal-access.log"))
    parser.add_argument("--error-log", default=os.environ.get("PRINTERPAL_ERROR_LOG", "/tmp/printerpal-error.log"))
    parser.add_argument("--workers", type=int, default=int(os.environ.get("PRINTERPAL_WORKERS", "2")))
    parser.add_argument("--threads", type=int, default=int(os.environ.get("PRINTERPAL_THREADS", "4")))

    args = parser.parse_args()

    if args.stop:
        return _stop(args.pidfile)

    if args.daemon:
        return _daemonize(args.host, args.port, args.pidfile, args.access_log, args.error_log, args.workers, args.threads)

    # Foreground mode (Werkzeug). This is intended for development/troubleshooting only.
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
