from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any

import httpx

from monitor.api import Hit
from monitor.store import Store

log = logging.getLogger(__name__)
STOP = object()


def format_hit(hit: Hit, wechat_id: str) -> str:
    lines = [
        "## 路演场次",
        f"城市：{hit.city_name}",
        f"影院：{hit.cinema_name}",
        f"影片：{hit.show_name or hit.show_id}",
        f"日期：{hit.show_date}",
        f"开场：{hit.open_time}",
        f"标签：{hit.tags}",
        f"票价：{hit.price_fen / 100:.2f}元",
    ]
    if wechat_id:
        lines.append(f"加微信 {wechat_id}，进群接收开售通知")
    return "\n".join(lines)


def redact_webhook(url: str) -> str:
    return url.split("?", 1)[0]


class Notifier:
    def __init__(self, notices: queue.Queue, store: Store, webhooks_getter: Any):
        self._notices = notices
        self._store = store
        self._webhooks_getter = webhooks_getter
        self._thread = threading.Thread(target=self._run, name="notify", daemon=False)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._notices.put(STOP)
        self._thread.join(timeout=20)

    def _run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                item = await asyncio.get_running_loop().run_in_executor(None, self._notices.get)
                if item is STOP:
                    break
                try:
                    ok = await self._send(client, item)
                    if ok:
                        self._store.mark_notified(item)
                    else:
                        self._store.release(item.cinema_id, item.schedule_id)
                except Exception:
                    log.exception("企业微信通知失败")
                    self._store.release(item.cinema_id, item.schedule_id)

    async def _send(self, client: httpx.AsyncClient, hit: Hit) -> bool:
        webhooks, wechat_id = self._webhooks_getter()
        content = format_hit(hit, wechat_id)
        if not webhooks:
            log.info("命中（未配置 Webhook）\n%s", content)
            return True
        results = await asyncio.gather(
            *(self._post(client, url, content) for url in webhooks),
            return_exceptions=True,
        )
        ok = True
        for url, result in zip(webhooks, results):
            if isinstance(result, Exception) or result is False:
                ok = False
                log.error("企业微信通知失败 %s %s", redact_webhook(url), result)
        return ok

    async def _post(self, client: httpx.AsyncClient, url: str, content: str) -> bool:
        response = await client.post(url, json={"msgtype": "markdown", "markdown": {"content": content}})
        response.raise_for_status()
        body = response.json()
        return body.get("errcode") == 0
