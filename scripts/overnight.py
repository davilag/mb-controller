"""Run the POC under a detached, restartable macOS overnight supervisor."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PID_FILE = DATA / "overnight.pid"
LOG_FILE = DATA / "logs" / "overnight.log"


def is_running():
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text())
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, ValueError):
        return None


def supervise():
    PID_FILE.write_text(str(os.getpid()))
    PID_FILE.chmod(0o600)
    stopping = False
    child = None

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    environment = {
        **os.environ, "HOST": "127.0.0.1", "PORT": "8080",
        "DATA_DIR": str(DATA), "TZ": "Europe/London", "PYTHONUNBUFFERED": "1",
    }
    try:
        while not stopping:
            print("Starting MB Local with sleep prevention.", flush=True)
            child = subprocess.Popen(
                ["/usr/bin/caffeinate", "-i", "-s", sys.executable, "-m", "server.app"],
                cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
            )
            child.wait()
            if not stopping:
                print("Backend exited; restarting in 10 seconds.", flush=True)
                for _ in range(10):
                    if stopping:
                        break
                    time.sleep(1)
    finally:
        PID_FILE.unlink(missing_ok=True)


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "start"
    if action == "run":
        supervise()
        return
    pid = is_running()
    if action == "stop":
        if pid:
            os.killpg(pid, signal.SIGTERM)
            print("Stopping MB Local and its keep-awake assertion.")
        else:
            print("Overnight supervisor is not running.")
        return
    if action == "status":
        print(f"Overnight supervisor running (PID {pid})." if pid else "Overnight supervisor is not running.")
        return
    if action != "start":
        raise SystemExit("Usage: overnight.py [start|stop|status]")
    if pid:
        print(f"Already running (PID {pid}).")
        return
    DATA.mkdir(mode=0o700, exist_ok=True)
    LOG_FILE.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "run"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )
    print(f"Started detached overnight supervisor (PID {process.pid}).")


if __name__ == "__main__":
    main()
