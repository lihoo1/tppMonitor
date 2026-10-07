"""Install or remove the Task Scheduler guard for the monitor.

Usage (run as Administrator):
    python guard_task.py install    # create task "TaopiaopiaoMonitorGuard"
    python guard_task.py remove     # delete the task
    python guard_task.py status      # show task state
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(HERE, ".venv", "Scripts", "python.exe")
GUARD = os.path.join(HERE, "guard.py")
TASK = "TaopiaopiaoMonitorGuard"


def run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(args, capture_output=True)
    out = (proc.stdout + proc.stderr).decode(errors="replace").strip()
    return proc.returncode, out


def install() -> int:
    if not os.path.exists(VENV_PY):
        print("error: .venv not found. run start.bat once first.")
        return 1
    cmd = (
        f'schtasks /Create /F /TN {TASK} /SC MINUTE /MO 1 '
        f'/TR "\\"{VENV_PY}\\" \\"{GUARD}\\"" '
        f'/RL LIMITED'
    )
    code, out = run(["cmd", "/c", cmd])
    print(out or ("task created" if code == 0 else "create failed"))
    return code


def remove() -> int:
    code, out = run(["schtasks", "/Delete", "/F", "/TN", TASK])
    print(out or ("task removed" if code == 0 else "delete failed"))
    return code


def status() -> int:
    code, out = run(["schtasks", "/Query", "/TN", TASK])
    print(out)
    return code


def main() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "install":
        sys.exit(install())
    if action == "remove":
        sys.exit(remove())
    sys.exit(status())


if __name__ == "__main__":
    main()
