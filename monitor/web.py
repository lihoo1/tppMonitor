from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from monitor.config import (
    AppConfig,
    ConfigHolder,
    config_from_dict,
    format_price_text,
    build_city_options,
)
from monitor.store import Store

log = logging.getLogger(__name__)
PAGE = Path(__file__).with_name("static") / "index.html"
ASSETS = Path(__file__).resolve().parent.parent / "assets"
QR_FILES = {
    "微信群.jpg": "image/jpeg",
    "微信.jpg": "image/jpeg",
}
MAX_BODY = 1_000_000


class WebApp:
    def __init__(self, holder: ConfigHolder, store: Store):
        self.holder = holder
        self.store = store
        self._sessions: set[str] = set()
        self._lock = threading.Lock()
        # 复用一个常驻事件循环跑搜索，避免每次请求新建循环触发风控
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, name="web-loop", daemon=True)
        self._loop_thread.start()

    def login(self, token: str) -> str | None:
        import hmac
        import secrets

        cfg = self.holder.get()
        if not cfg.ui_token or hmac.compare_digest(token, cfg.ui_token):
            session = secrets.token_hex(16)
            with self._lock:
                self._sessions.add(session)
            return session
        return None

    def allowed(self, session: str) -> bool:
        if not self.holder.get().ui_token:
            return True
        with self._lock:
            return session in self._sessions

    def logout(self, session: str) -> None:
        with self._lock:
            self._sessions.discard(session)

    def public_config(self) -> dict:
        cfg = self.holder.get()
        return {
            "monitor_enabled": cfg.monitor_enabled,
            "interval_seconds": cfg.interval_seconds,
            "only_meetup_tags": cfg.only_meetup_tags,
            "process_existing_on_start": cfg.process_existing_on_start,
            "price_whitelist_yuan": format_price_text(cfg.price_whitelist_fen),
            "wechat_id": cfg.wecom.wechat_id,
            "webhook_count": len(cfg.wecom.webhooks),
            "targets": [
                {
                    "show_id": item.show_id,
                    "city_code": item.city_code,
                    "city_name": item.city_name,
                    "date": item.show_date,
                }
                for item in cfg.targets
            ],
            "auth_required": bool(cfg.ui_token),
            "cities": build_city_options(),
        }

    def save(self, payload: dict) -> AppConfig:
        current = self.holder.get()
        merged = {
            "listen_host": current.listen_host,
            "listen_port": current.listen_port,
            "ui_token": current.ui_token,
            "database": current.database,
            # 间隔、微信号、Webhook、票价白名单只允许后台（config.yaml / 管理员）修改，前端/API 均不可改
            "interval_seconds": current.interval_seconds,
            "wecom": {
                "webhooks": list(current.wecom.webhooks),
                "wechat_id": current.wecom.wechat_id,
            },
            "monitor_enabled": bool(payload.get("monitor_enabled")),
            "only_meetup_tags": bool(payload.get("only_meetup_tags")),
            "process_existing_on_start": bool(payload.get("process_existing_on_start")),
            "price_whitelist_yuan": format_price_text(current.price_whitelist_fen),
            "targets": payload.get("targets") or [],
        }
        cfg = config_from_dict(merged)
        self.holder.replace(cfg)
        return cfg

    async def search_shows(self, city_code: str, keyword: str) -> list[dict]:
        from monitor.api import search_shows as _search, search_shows_anywhere as _search_any
        from monitor.mtop import MtopClient

        client = MtopClient()
        try:
            if city_code:
                shows = await _search(client, city_code, keyword)
            else:
                shows = await _search_any(client, keyword)
            log.info("搜索 city=%s kw=%s 命中 %d 部", city_code or "不限", keyword, len(shows))
            return shows
        except Exception:
            log.exception("搜索失败 city=%s kw=%s", city_code or "不限", keyword)
            raise
        finally:
            await client.aclose()


def serve(app: WebApp, host: str, port: int) -> ThreadingHTTPServer:
    handler = _handler_class(app)
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="web", daemon=True)
    thread.start()
    return server


def _handler_class(app: WebApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/":
                body = PAGE.read_bytes()
                self._send(200, "text/html; charset=utf-8", body)
                return
            if path.startswith("/assets/"):
                self._asset(unquote(path[len("/assets/") :]))
                return
            if path == "/api/state":
                if not self._authorized():
                    self._json(401, {"error": "需要口令"})
                    return
                state = app.store.snapshot(app.holder.get().targets)
                state["config"] = app.public_config()
                self._json(200, state)
                return
            if path == "/api/shows":
                if not self._authorized():
                    self._json(401, {"error": "需要口令"})
                    return
                import asyncio
                query = urlsplit(self.path).query
                params = dict(
                    item.split("=", 1)
                    for item in query.split("&")
                    if "=" in item
                )
                city_code = unquote(params.get("city_code", "")).strip()
                keyword = unquote(params.get("keyword", "")).strip()
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        app.search_shows(city_code, keyword), app._loop
                    )
                    shows = future.result(timeout=120)
                except Exception as exc:
                    self._json(500, {"error": f"查询失败：{exc}"})
                    return
                self._json(200, {"shows": shows})
                return
            self._json(404, {"error": "没有这个页面"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            if path == "/api/login":
                session = app.login(str(payload.get("token") or ""))
                if session is None:
                    self._json(401, {"error": "口令不对"})
                    return
                self._json(200, {"ok": True}, extra_headers=[("Set-Cookie", _session_cookie(session))])
                return
            if path == "/api/logout":
                app.logout(self._session())
                self._json(200, {"ok": True}, extra_headers=[("Set-Cookie", _session_cookie("", clear=True))])
                return
            if not self._authorized():
                self._json(401, {"error": "需要口令"})
                return
            if path == "/api/config":
                try:
                    app.save(payload)
                except ValueError as exc:
                    self._json(400, {"error": str(exc)})
                    return
                self._json(200, {"ok": True})
                return
            self._json(404, {"error": "没有这个接口"})

        def _asset(self, name: str) -> None:
            content_type = QR_FILES.get(name)
            if content_type is None or "/" in name or "\\" in name:
                self._json(404, {"error": "没有这个图片"})
                return
            file_path = (ASSETS / name).resolve()
            if not file_path.is_relative_to(ASSETS.resolve()) or not file_path.is_file():
                self._json(404, {"error": "没有这个图片"})
                return
            self._send(200, content_type, file_path.read_bytes())

        def _authorized(self) -> bool:
            return app.allowed(self._session())

        def _session(self) -> str:
            raw = self.headers.get("Cookie") or ""
            for piece in raw.split(";"):
                name, _, value = piece.strip().partition("=")
                if name == "monitor_session":
                    return value
            return ""

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > MAX_BODY:
                raise ValueError("请求体过大")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError("请求不是 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求必须是对象")
            return payload

        def _json(self, status: int, payload: dict, extra_headers: list[tuple[str, str]] | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, "application/json; charset=utf-8", body, extra_headers)

        def _send(
            self,
            status: int,
            content_type: str,
            body: bytes,
            extra_headers: list[tuple[str, str]] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in extra_headers or []:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            path = urlsplit(self.path).path
            if path == "/api/state":
                return
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def _session_cookie(value: str, clear: bool = False) -> str:
    if clear:
        return "monitor_session=; HttpOnly; Path=/; Max-Age=0; SameSite=Lax"
    return f"monitor_session={value}; HttpOnly; Path=/; SameSite=Lax"
