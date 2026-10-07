from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Any, Callable

from monitor.api import (
    Cinema,
    Hit,
    extract_hits,
    get_cinema_schedules,
    get_show_cinemas,
    should_query_schedule,
)
from monitor.config import AppConfig, ConfigHolder, Target
from monitor.store import Store

log = logging.getLogger(__name__)
STOP = object()
CITY_CONCURRENCY = 4
SCHEDULE_CONCURRENCY = 48


def backoff_seconds(interval: int, fail_count: int) -> int:
    exponent = min(max(fail_count, 0), 4)
    return min(300, interval * (2**exponent))


def wave_sleep_seconds(interval: int, any_success: bool) -> float:
    if any_success:
        return float(interval)
    return float(min(interval, 30))


class Dispatcher:
    def __init__(self, interval_getter: Callable[[], int]):
        self._interval_getter = interval_getter
        self._lock = threading.Lock()
        self.remaining = 0
        self.any_success = False
        self.done = threading.Event()
        self.fail_counts: dict[tuple[str, str, str], int] = {}
        self.backoff_until: dict[tuple[str, str, str], float] = {}

    def begin(self, count: int) -> None:
        with self._lock:
            self.remaining = count
            self.any_success = False
            self.done.clear()
            if count <= 0:
                self.done.set()

    def eligible(self, targets: tuple[Target, ...]) -> list[Target]:
        now = time.monotonic()
        with self._lock:
            return [item for item in targets if self.backoff_until.get(item.key, 0) <= now]

    def finish(self, key: tuple[str, str, str], ok: bool) -> bool:
        rebuild = False
        with self._lock:
            if ok:
                self.fail_counts[key] = 0
                self.backoff_until.pop(key, None)
                self.any_success = True
            else:
                count = self.fail_counts.get(key, 0) + 1
                self.fail_counts[key] = count
                delay = backoff_seconds(self._interval_getter(), count)
                self.backoff_until[key] = time.monotonic() + delay
                rebuild = count >= 3
                log.warning("请求异常，自动重试 %s，%.0f 秒后再扫", key, delay)
            self._tick()
        return rebuild

    def skip(self) -> None:
        with self._lock:
            self._tick()

    def _tick(self) -> None:
        if self.remaining > 0:
            self.remaining -= 1
        if self.remaining <= 0:
            self.done.set()


class AllDayMonitor:
    def __init__(
        self,
        holder: ConfigHolder,
        store: Store,
        notices: queue.Queue,
        client_factory: Callable[[], Any],
        city_concurrency: int = CITY_CONCURRENCY,
    ):
        self._holder = holder
        self._store = store
        self._notices = notices
        self._client_factory = client_factory
        self._city_concurrency = city_concurrency
        self.stop = threading.Event()
        self._work: queue.Queue = queue.Queue()
        self._dispatcher = Dispatcher(lambda: self._holder.get().interval_seconds)
        self._threads: list[threading.Thread] = []

    def run(self, max_waves: int | None = None) -> None:
        self._start_workers()
        waves = 0
        try:
            while not self.stop.is_set():
                cfg = self._holder.get()
                self._store.clear_missing(cfg.targets)
                if not cfg.monitor_enabled:
                    for target in cfg.targets:
                        self._store.set_status(target, "已停止")
                    if self.stop.wait(min(cfg.interval_seconds, 1)):
                        break
                    continue
                cities = self._dispatcher.eligible(cfg.targets)
                if not cities and cfg.targets:
                    log.info("目标都在退避窗口，本轮跳过")
                self._dispatcher.begin(len(cities))
                for city in cities:
                    self._work.put(city)
                while not self._dispatcher.done.wait(0.2):
                    if self.stop.is_set():
                        break
                if self.stop.is_set():
                    break
                waves += 1
                if max_waves is not None and waves >= max_waves:
                    break
                delay = wave_sleep_seconds(cfg.interval_seconds, self._dispatcher.any_success)
                if self.stop.wait(delay):
                    break
        finally:
            self.stop.set()
            for _ in self._threads:
                self._work.put(STOP)
            for thread in self._threads:
                thread.join(timeout=30)

    def _start_workers(self) -> None:
        for index in range(self._city_concurrency):
            thread = threading.Thread(target=self._worker, name=f"city-{index + 1}", daemon=False)
            thread.start()
            self._threads.append(thread)

    def _worker(self) -> None:
        asyncio.run(self._worker_async())

    async def _worker_async(self) -> None:
        client = self._client_factory()
        try:
            while True:
                item = await asyncio.get_running_loop().run_in_executor(None, self._work.get)
                if item is STOP:
                    break
                if self.stop.is_set():
                    self._dispatcher.skip()
                    continue
                # 目标被删除后立即停扫，不再浪费请求
                if not self._target_alive(item):
                    log.info("目标已删除，跳过扫描 %s %s %s", item.show_id, item.city_name, item.show_date)
                    self._store.set_status(item, "已删除")
                    self._dispatcher.skip()
                    continue
                rebuild = False
                try:
                    hits = await self._scan_city(client, item)
                    self._publish(item, hits)
                    rebuild = self._dispatcher.finish(item.key, ok=True)
                    log.info("扫完 %s %s %s，命中 %s 条", item.show_id, item.city_name, item.show_date, len(hits))
                except Exception as exc:
                    log.warning("请求异常，自动重试 %s %s %s：%s", item.show_id, item.city_name, item.show_date, exc)
                    self._store.set_status(item, "请求异常，自动重试")
                    rebuild = self._dispatcher.finish(item.key, ok=False)
                if rebuild:
                    await client.aclose()
                    client = self._client_factory()
        finally:
            await client.aclose()

    def _target_alive(self, target: Target) -> bool:
        """目标是否仍在当前配置里（被用户删掉后应立即停扫）。"""
        keys = {item.key for item in self._holder.get().targets}
        return target.key in keys

    async def _scan_city(self, client: Any, target: Target) -> list[Hit]:
        cfg = self._holder.get()
        self._store.set_status(target, "扫描中")
        cinemas = await get_show_cinemas(client, target.show_id, target.city_code, target.show_date)
        selected = [
            cinema
            for cinema in cinemas
            if should_query_schedule(cinema, cfg.only_meetup_tags, cfg.price_whitelist_fen)
        ]
        total = len(selected)
        checked = 0
        errors: list[BaseException] = []
        found: list[Hit] = []
        lock = asyncio.Lock()
        semaphore = asyncio.Semaphore(SCHEDULE_CONCURRENCY)
        show_name_box: list[str] = []

        async def one(cinema: Cinema) -> None:
            nonlocal checked
            # 扫描中目标被删，立即中止剩余影院
            if not self._target_alive(target):
                return
            async with semaphore:
                try:
                    schedule = await get_cinema_schedules(client, cinema.cinema_id)
                    # 顺手抓影片名，让用户知道在监控哪部片
                    if not show_name_box:
                        for vo in schedule.get("showVos") or []:
                            if isinstance(vo, dict) and str(vo.get("showId") or "") == target.show_id:
                                name = str(vo.get("showName") or "").strip()
                                if name:
                                    show_name_box.append(name)
                                break
                    rows = extract_hits(
                        schedule,
                        cinema,
                        show_id=target.show_id,
                        city_code=target.city_code,
                        city_name=target.city_name,
                        show_date=target.show_date,
                        only_meetup_tags=cfg.only_meetup_tags,
                        whitelist=cfg.price_whitelist_fen,
                    )
                except Exception as exc:
                    errors.append(exc)
                    rows = []
            async with lock:
                checked += 1
                found.extend(rows)
                if checked % 25 == 0 or checked == total:
                    self._store.set_status(target, f"已核对 {checked}/{total} 家", checked, total,
                                           show_name=show_name_box[0] if show_name_box else "")
                    log.info("已核对 %s/%s 家 %s %s", checked, total, target.city_name, target.show_id)

        if selected:
            await asyncio.gather(*(one(cinema) for cinema in selected))
        else:
            self._store.set_status(target, "已核对 0/0 家", 0, 0)
        if errors:
            raise errors[0]
        # 影片名回填状态，让用户知道在监控哪部片
        final_name = show_name_box[0] if show_name_box else (found[0].show_name if found else "")
        self._store.set_status(target, f"已核对 {total}/{total} 家", total, total, show_name=final_name)
        return found

    def _publish(self, target: Target, hits: list[Hit]) -> None:
        cfg = self._holder.get()
        if not cfg.process_existing_on_start and not self._store.city_has_baseline(target):
            self._store.mark_baseline(hits, target)
            log.info("已在映（启动基线） %s %s %s 条", target.show_id, target.city_name, len(hits))
            return
        for hit in hits:
            if not self._store.claim(hit.cinema_id, hit.schedule_id):
                continue
            try:
                self._notices.put(hit, timeout=1)
            except queue.Full:
                self._store.release(hit.cinema_id, hit.schedule_id)
                log.warning("通知队列已满，留到下一轮 %s", hit.schedule_id)
                continue
            self._store.add_recent(hit)
