"""Guard script for the taopiaopiao monitor.

Runs as a scheduled task (or standalone). Checks whether the monitor's
web port answers; if not, relaunches it via start.bat (which handles
venv, port cleanup, and hidden launch). Intended to be invoked every
minute by Task Scheduler.

Usage:
    python guard.py            # one check cycle, relaunch if down
    python guard.py loop       # internal loop mode (used by schtasks)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(HERE, ".venv", "Scripts", "python.exe")
STOP_FLAG = os.path.join(HERE, ".stop-monitor")
BAT = os.path.join(HERE, "start.bat")
DEFAULT_PORT = "8787"


def read_port() -> str:
    try:
        import yaml

        cfg = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
        return str(cfg.get("listen_port") or DEFAULT_PORT)
    except Exception:
        return DEFAULT_PORT


def is_up(port: str) -> bool:
    import urllib.request

    try:
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://127.0.0.1:{port}/", timeout=3
        )
        return True
    except Exception:
        return False


def one_cycle() -> bool:
    """Return True if the monitor is up (or was relaunched OK)."""
    if os.path.exists(STOP_FLAG):
        return False  # user asked to stop; do not relaunch
    port = read_port()
    if is_up(port):
        return True
    # down: relaunch via start.bat without opening a browser
    env = dict(os.environ)
    env["NOBROWSER"] = "1"
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    subprocess.run(
        ["cmd", "/c", BAT, "start"],
        cwd=HERE,
        env=env,
        capture_output=True,
        timeout=180,
    )
    return is_up(port)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "loop":
        while not os.path.exists(STOP_FLAG):
            one_cycle()
            for _ in range(30):
                if os.path.exists(STOP_FLAG):
                    break
                time.sleep(2)
        return
    one_cycle()


if __name__ == "__main__":
    main()
