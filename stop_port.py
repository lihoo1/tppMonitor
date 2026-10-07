"""Stop helpers: kill whatever listens on the given port (default 8787).

With --guards, also kill wscript.exe so the guard loop cannot relaunch
the monitor after stop (the .stop-monitor flag alone would also work,
but killing wscript stops it immediately).
"""
import subprocess
import sys


def kill_by_port(port: str) -> int:
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], stderr=subprocess.DEVNULL
        ).decode(errors="replace")
    except Exception as exc:
        print("netstat failed:", exc)
        return 0
    killed = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[3].upper() != "LISTENING":
            continue
        if not parts[1].endswith(":" + port):
            continue
        pid = parts[4]
        if pid == "0":
            continue
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
        killed += 1
    return killed


def main() -> None:
    args = sys.argv[1:]
    guards = "--guards" in args
    args = [a for a in args if a != "--guards"]
    port = args[0] if args else "8787"
    n = kill_by_port(port)
    print("killed", n)
    if guards:
        subprocess.run(
            ["taskkill", "/F", "/IM", "wscript.exe"], capture_output=True
        )


if __name__ == "__main__":
    main()
