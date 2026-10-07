from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor.api import Hit
from monitor.config import Target


class Store:
    def __init__(self, path: str):
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(file_path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                cinema_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                status TEXT NOT NULL,
                PRIMARY KEY (cinema_id, schedule_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS city_baseline (
                show_id TEXT NOT NULL,
                city_code TEXT NOT NULL,
                show_date TEXT NOT NULL,
                PRIMARY KEY (show_id, city_code, show_date)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cinema_id TEXT NOT NULL,
                schedule_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hits_created ON hits (created_at DESC)"
        )
        self._conn.commit()
        self._inflight: set[tuple[str, str]] = set()
        self._status: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._recent: list[dict[str, Any]] = self._load_recent()

    def _load_recent(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT payload FROM hits ORDER BY id DESC LIMIT 50"
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def claim(self, cinema_id: str, schedule_id: str) -> bool:
        key = (cinema_id, schedule_id)
        with self._lock:
            if key in self._inflight or self._has_record(key):
                return False
            self._inflight.add(key)
            return True

    def mark_notified(self, hit: Hit) -> None:
        key = (hit.cinema_id, hit.schedule_id)
        with self._lock:
            self._inflight.discard(key)
            self._insert_record(key, "notified")

    def release(self, cinema_id: str, schedule_id: str) -> None:
        with self._lock:
            self._inflight.discard((cinema_id, schedule_id))

    def mark_baseline(self, hits: list[Hit], target: Target) -> None:
        with self._lock:
            for hit in hits:
                key = (hit.cinema_id, hit.schedule_id)
                self._inflight.discard(key)
                self._insert_record(key, "baseline")
            self._conn.execute(
                "INSERT OR IGNORE INTO city_baseline (show_id, city_code, show_date) VALUES (?, ?, ?)",
                target.key,
            )
            self._conn.commit()

    def city_has_baseline(self, target: Target) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM city_baseline WHERE show_id = ? AND city_code = ? AND show_date = ?",
                target.key,
            ).fetchone()
            return row is not None

    def set_status(self, target: Target, text: str, checked: int = 0, total: int = 0, show_name: str = "") -> None:
        with self._lock:
            existing = self._status.get(target.key) or {}
            # show_name 为空时保留已有值，避免覆盖
            name = show_name or existing.get("show_name", "")
            self._status[target.key] = {
                "show_id": target.show_id,
                "show_name": name,
                "city_code": target.city_code,
                "city_name": target.city_name,
                "date": target.show_date,
                "status": text,
                "checked": checked,
                "total": total,
            }

    def clear_missing(self, targets: tuple[Target, ...]) -> None:
        live = {item.key for item in targets}
        with self._lock:
            for key in list(self._status):
                if key not in live:
                    del self._status[key]

    def add_recent(self, hit: Hit) -> None:
        with self._lock:
            payload = hit.as_dict()
            payload["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._conn.execute(
                "INSERT INTO hits (cinema_id, schedule_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (hit.cinema_id, hit.schedule_id, json.dumps(payload, ensure_ascii=False), payload["created_at"]),
            )
            self._conn.commit()
            self._recent.insert(0, payload)
            del self._recent[50:]

    def snapshot(self, targets: tuple[Target, ...]) -> dict[str, Any]:
        with self._lock:
            rows = []
            for target in targets:
                current = self._status.get(target.key)
                if current is None:
                    current = {
                        "show_id": target.show_id,
                        "show_name": "",
                        "city_code": target.city_code,
                        "city_name": target.city_name,
                        "date": target.show_date,
                        "status": "等待扫描",
                        "checked": 0,
                        "total": 0,
                    }
                rows.append(current)
            return {"statuses": rows, "hits": list(self._recent)}

    def _has_record(self, key: tuple[str, str]) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM records WHERE cinema_id = ? AND schedule_id = ?",
            key,
        ).fetchone()
        return row is not None

    def _insert_record(self, key: tuple[str, str], status: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO records (cinema_id, schedule_id, status) VALUES (?, ?, ?)",
            (key[0], key[1], status),
        )
        self._conn.commit()
