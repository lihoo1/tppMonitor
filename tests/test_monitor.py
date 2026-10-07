from __future__ import annotations

import asyncio
import hashlib
import json
import urllib.error
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitor.api import Cinema, dates_match, extract_hits, get_show_cinemas, price_matches
from monitor.config import ConfigHolder, load_config, parse_price_text, save_config, to_fen as config_to_fen
from monitor.mtop import APP_KEY, is_token_empty, needs_new_token, parse_body, sign, token_from_m_h5_tk
from monitor.scanner import AllDayMonitor, backoff_seconds, wave_sleep_seconds
from monitor.store import Store
from monitor.web import WebApp, serve


def success(value):
    return {"ret": ["SUCCESS::调用成功"], "data": {"returnValue": value}}


class SignTests(unittest.TestCase):
    def test_sign_matches_spec_formula(self):
        token, timestamp, data = "abc", "1000", "{}"
        raw = f"{token}&{timestamp}&{APP_KEY}&{data}".encode("utf-8")
        self.assertEqual(sign(token, timestamp, data), hashlib.md5(raw).hexdigest())

    def test_token_is_the_part_before_underscore(self):
        self.assertEqual(token_from_m_h5_tk("deadbeef_170000"), "deadbeef")

    def test_jsonp_body(self):
        payload = parse_body('mtopjsonp1({"ret":["SUCCESS::ok"],"data":{}})')
        self.assertEqual(payload["ret"], ["SUCCESS::ok"])

    def test_token_empty_retries_before_handshake_signal(self):
        payload = {"ret": ["FAIL_SYS_TOKEN_EMPTY::令牌为空"]}
        self.assertTrue(is_token_empty(payload))
        self.assertTrue(needs_new_token(payload))


class DateTests(unittest.TestCase):
    def setUp(self):
        self.target = datetime(2026, 10, 1).date()

    def test_accepted_formats(self):
        noon = datetime(2026, 10, 1, 12, tzinfo=timezone(timedelta(hours=8)))
        samples = ["2026-10-01", "2026/10/01", "20261001", "10月1日", "2026年10月1日", int(noon.timestamp()), int(noon.timestamp() * 1000)]
        for sample in samples:
            self.assertTrue(dates_match(sample, self.target), sample)

    def test_other_day_does_not_match(self):
        self.assertFalse(dates_match("2026-10-02", self.target))

    def test_day_start_is_china_midnight(self):
        from monitor.api import day_start_unix

        expected = int(datetime(2026, 10, 1, tzinfo=timezone(timedelta(hours=8))).timestamp())
        self.assertEqual(day_start_unix("2026-10-01"), expected)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.cinema = Cinema("c1", "影院", "19:00", "")
        self.tagged = Cinema("c1", "影院", "19:00", "路演")
        self.whitelist = (19900,)

    def test_price_window(self):
        self.assertTrue(price_matches(19990, self.whitelist))
        self.assertFalse(price_matches(18900, self.whitelist))

    def test_untagged_price_hit_appends_label(self):
        hits = extract_hits(self._schedule(19990, ""), self.cinema, **self._args())
        self.assertEqual(len(hits), 1)
        self.assertTrue(hits[0].tags.endswith("白名单票价"))

    def test_far_price_is_dropped(self):
        hits = extract_hits(self._schedule(18900, ""), self.cinema, **self._args())
        self.assertEqual(hits, [])

    def test_tagged_session_ignores_price(self):
        hits = extract_hits(self._schedule(18900, "路演"), self.cinema, **self._args())
        self.assertEqual(len(hits), 1)
        self.assertNotIn("白名单票价", hits[0].tags)

    def test_other_show_is_not_mixed_in(self):
        schedule = self._schedule(19900, "路演")
        schedule["showScheduleMap"]["other"] = schedule["showScheduleMap"]["100"]
        hits = extract_hits(schedule, self.tagged, **self._args())
        self.assertEqual([hit.show_id for hit in hits], ["100"])
        self.assertEqual(hits[0].schedule_id, "s1")

    def test_placeholder_when_tagged_cinema_has_no_session(self):
        schedule = {
            "showVos": [{"showId": "100", "showName": "电影A"}],
            "showScheduleMap": {"100": []},
        }
        hits = extract_hits(schedule, self.tagged, **self._args())
        self.assertEqual(hits[0].schedule_id, "list-c1")
        self.assertEqual(hits[0].price_fen, 0)
        self.assertEqual(hits[0].open_time, "19:00")

    def test_yuan_conversion(self):
        self.assertEqual(config_to_fen(69), 6900)
        self.assertEqual(config_to_fen(398.9), 39890)
        self.assertEqual(config_to_fen(19900), 19900)
        self.assertEqual(parse_price_text("69,99,398.9"), (6900, 9900, 39890))

    def _args(self):
        return {
            "show_id": "100",
            "city_code": "310100",
            "city_name": "上海",
            "show_date": "2026-10-01",
            "only_meetup_tags": True,
            "whitelist": self.whitelist,
        }

    def _schedule(self, price, tag):
        return {
            "showVos": [{"showId": "100", "showName": "电影A", "specialTag": ""}],
            "showScheduleMap": {
                "100": [
                    {
                        "date": "2026-10-01",
                        "scheduleVos": [
                            {"scheduleId": "s1", "scheduleTag": tag, "openTime": "19:30", "tradePrice": price}
                        ],
                    }
                ]
            },
        }


class PagingTests(unittest.IsolatedAsyncioTestCase):
    async def test_stops_when_page_is_short(self):
        client = PageClient([100, 40])
        cinemas = await get_show_cinemas(client, "100", "310100", "2026-10-01")
        self.assertEqual(len(cinemas), 140)
        self.assertEqual(client.calls, 2)

    async def test_stops_when_page_has_no_new_cinema(self):
        client = PageClient([100, 100], repeat_same=True)
        cinemas = await get_show_cinemas(client, "100", "310100", "2026-10-01")
        self.assertEqual(len(cinemas), 1)
        self.assertEqual(client.calls, 2)

    async def test_page_limit_is_an_error(self):
        client = PageClient([100] * 50)
        with self.assertRaises(Exception):
            await get_show_cinemas(client, "100", "310100", "2026-10-01")


class PageClient:
    def __init__(self, sizes, repeat_same=False):
        self.sizes = sizes
        self.repeat_same = repeat_same
        self.calls = 0

    async def call(self, api, version, data, timeout):
        size = self.sizes[min(self.calls, len(self.sizes) - 1)]
        page = data["pageIndex"]
        self.calls += 1
        rows = []
        for index in range(size):
            cinema_id = "same" if self.repeat_same else f"p{page}-{index}"
            rows.append({"cinemaId": cinema_id, "cinemaName": cinema_id, "supportList": []})
        return success({"cinemas": rows})


class BackoffTests(unittest.TestCase):
    def test_backoff_caps(self):
        self.assertEqual(backoff_seconds(10, 1), 20)
        self.assertEqual(backoff_seconds(10, 4), 160)
        self.assertEqual(backoff_seconds(10, 5), 160)
        self.assertEqual(backoff_seconds(30, 4), 300)

    def test_wave_sleep(self):
        self.assertEqual(wave_sleep_seconds(10, True), 10)
        self.assertEqual(wave_sleep_seconds(60, False), 30)


class StoreTests(unittest.TestCase):
    def test_baseline_then_claim(self):
        from monitor.api import Hit
        from monitor.config import Target

        with tempfile.TemporaryDirectory() as folder:
            store = Store(str(Path(folder) / "t.sqlite3"))
            target = Target("100", "310100", "上海", "2026-10-01")
            hit = Hit("上海", "310100", "c1", "影院", "100", "电影A", "s1", "2026-10-01", "19:30", "路演", 19900)
            self.assertFalse(store.city_has_baseline(target))
            store.mark_baseline([hit], target)
            self.assertTrue(store.city_has_baseline(target))
            self.assertFalse(store.claim("c1", "s1"))
            self.assertTrue(store.claim("c1", "s2"))
            store.release("c1", "s2")
            self.assertTrue(store.claim("c1", "s2"))
            store.close()


class ScanTests(unittest.TestCase):
    def test_two_movies_four_cities_run_together_without_mixing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_text(
                """
listen_port: 8791
monitor_enabled: true
interval_seconds: 30
only_meetup_tags: true
process_existing_on_start: true
price_whitelist_yuan: [199]
database: data/monitor.sqlite3
wecom: {webhooks: [], wechat_id: wx1}
targets:
  - {show_id: "100", city_code: "310100", city_name: 上海, date: "2026-10-01"}
  - {show_id: "100", city_code: "110100", city_name: 北京, date: "2026-10-01"}
  - {show_id: "200", city_code: "440100", city_name: 广州, date: "2026-10-01"}
  - {show_id: "200", city_code: "440300", city_name: 深圳, date: "2026-10-01"}
""".strip(),
                encoding="utf-8",
            )
            holder = ConfigHolder(path)
            store = Store(str(Path(folder) / "t.sqlite3"))
            notices = __import__("queue").Queue()
            shared = {"threads": set(), "release": threading.Event(), "lock": threading.Lock()}

            def factory():
                return OverlapClient(shared)

            monitor = AllDayMonitor(holder, store, notices, factory, city_concurrency=4)
            monitor.run(max_waves=1)
            hits = []
            while not notices.empty():
                hits.append(notices.get())
            self.assertEqual(len(shared["threads"]), 4)
            self.assertEqual(sorted(hit.show_id for hit in hits), ["100", "100", "200", "200"])
            self.assertTrue(all(hit.schedule_id.startswith("s-") for hit in hits))
            self.assertEqual({hit.city_code for hit in hits if hit.show_id == "100"}, {"310100", "110100"})
            store.close()


class OverlapClient:
    def __init__(self, shared):
        self.shared = shared
        self.show_of = {}

    async def aclose(self):
        return None

    async def call(self, api, version, data, timeout):
        if "getcinemalistinpage" in api.lower():
            with self.shared["lock"]:
                self.shared["threads"].add(threading.get_ident())
                if len(self.shared["threads"]) >= 4:
                    self.shared["release"].set()
            for _ in range(200):
                if self.shared["release"].is_set():
                    break
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("四座城市没有同时进入扫描")
            cinema_id = f"{data['showId']}-{data['cityCode']}"
            self.show_of[cinema_id] = str(data["showId"])
            return success(
                {
                    "cinemas": [
                        {
                            "cinemaId": cinema_id,
                            "cinemaName": cinema_id,
                            "showTime": "18:00",
                            "supportList": [{"tagName": "路演"}],
                        }
                    ]
                }
            )
        show_id = self.show_of[str(data["cinemaId"])]
        return success(
            {
                "showVos": [{"showId": show_id, "showName": f"电影{show_id}"}],
                "showScheduleMap": {
                    show_id: [
                        {
                            "date": "2026-10-01",
                            "scheduleVos": [
                                {"scheduleId": f"s-{show_id}", "openTime": "19:30", "tradePrice": 19900, "scheduleTag": ""}
                            ],
                        }
                    ],
                    "999": [
                        {
                            "date": "2026-10-01",
                            "scheduleVos": [
                                {"scheduleId": "other-show", "openTime": "20:00", "tradePrice": 100, "scheduleTag": "路演"}
                            ],
                        }
                    ],
                },
            }
        )


class WebTests(unittest.TestCase):
    def test_save_targets_for_next_wave(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            save_config(load_config_from_example(path), _empty_config(path))
            holder = ConfigHolder(path)
            store = Store(str(Path(folder) / "t.sqlite3"))
            server = serve(WebApp(holder, store), "127.0.0.1", 0)
            port = server.server_address[1]
            try:
                body = {
                    "monitor_enabled": True,
                    "interval_seconds": 15,
                    "only_meetup_tags": True,
                    "process_existing_on_start": True,
                    "price_whitelist_yuan": "69,199",
                    "wechat_id": "wx-user",
                    "webhooks": [],
                    "targets": [
                        {"show_id": "100", "city_code": "310100", "city_name": "上海", "date": "2026-10-01"},
                        {"show_id": "100", "city_code": "110100", "city_name": "北京", "date": "2026-10-01"},
                        {"show_id": "200", "city_code": "440100", "city_name": "广州", "date": "2026-10-01"},
                    ],
                }
                status, payload = post(port, "/api/config", body)
                self.assertEqual(status, 200, payload)
                saved = holder.get()
                self.assertEqual([item.show_id for item in saved.targets], ["100", "100", "200"])
                # interval, price whitelist, wechat_id, webhooks are admin-only: frontend/API cannot change them
                self.assertEqual(saved.interval_seconds, 10)
                self.assertEqual(saved.price_whitelist_fen, (6900,))
                self.assertEqual(saved.wecom.webhooks, ())
                self.assertEqual(saved.wecom.wechat_id, "liwuhe2023")
                reloaded = load_config(path)
                self.assertEqual(len(reloaded.targets), 3)
            finally:
                server.shutdown()
                server.server_close()
                store.close()


def load_config_from_example(path: Path):
    return path


def _empty_config(path: Path):
    from monitor.config import AppConfig, WeComConfig

    return AppConfig(
        listen_host="127.0.0.1",
        listen_port=8787,
        ui_token="",
        monitor_enabled=True,
        interval_seconds=10,
        only_meetup_tags=True,
        process_existing_on_start=True,
        price_whitelist_fen=(6900,),
        database="data/monitor.sqlite3",
        wecom=WeComConfig((), ""),
        targets=(),
    )


def post(port: int, path: str, payload: dict):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
