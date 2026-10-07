from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
from pathlib import Path

from monitor.config import ConfigHolder
from monitor.mtop import MtopClient
from monitor.notify import Notifier
from monitor.scanner import AllDayMonitor
from monitor.store import Store
from monitor.web import WebApp, serve

log = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
    )
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    try:
        holder = ConfigHolder(path)
    except ValueError as exc:
        log.error("%s", exc)
        sys.exit(1)
    cfg = holder.get()
    store = Store(cfg.database)
    notices: queue.Queue = queue.Queue(maxsize=1000)
    notifier = Notifier(notices, store, lambda: _wecom(holder))
    monitor = AllDayMonitor(holder, store, notices, MtopClient)
    web = WebApp(holder, store)
    stop = threading.Event()

    def handle(signum, _frame) -> None:
        log.info("收到停止信号 %s", signum)
        stop.set()
        monitor.stop.set()

    signal.signal(signal.SIGINT, handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle)

    notifier.start()
    scan_thread = threading.Thread(target=monitor.run, name="scan-loop", daemon=False)
    scan_thread.start()
    try:
        server = serve(web, cfg.listen_host, cfg.listen_port)
    except OSError as exc:
        log.error("网页监听失败 %s:%s %s", cfg.listen_host, cfg.listen_port, exc)
        monitor.stop.set()
        scan_thread.join(timeout=30)
        notifier.stop()
        store.close()
        sys.exit(1)
    log.info("网页已打开 http://%s:%s", _display_host(cfg.listen_host), cfg.listen_port)
    if not cfg.wecom.webhooks:
        log.info("未配置企业微信 Webhook，命中只写在页面和日志里")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        monitor.stop.set()
        scan_thread.join(timeout=35)
        notifier.stop()
        server.shutdown()
        server.server_close()
        store.close()


def _wecom(holder: ConfigHolder) -> tuple[tuple[str, ...], str]:
    cfg = holder.get()
    return cfg.wecom.webhooks, cfg.wecom.wechat_id


def _display_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


if __name__ == "__main__":
    main()
