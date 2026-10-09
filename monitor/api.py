from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from monitor.mtop import MtopError

log = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))
MEETUP_TAGS = ("明星见面会", "映后见面会", "主创见面会", "路演")
PAGE_SIZE = 100
MAX_PAGES = 50
PRICE_TOLERANCE_FEN = 100
CINEMA_API = "mtop.film.MtopCinemaAPI.getCinemaListInPage"
CINEMA_VERSION = "8.0"
SCHEDULE_API = "mtop.film.MtopScheduleAPI.getNewCinemaSchedules"
SCHEDULE_VERSION = "2.0"


class ApiError(MtopError):
    pass


@dataclass(frozen=True)
class Cinema:
    cinema_id: str
    cinema_name: str
    show_time: str
    tag_text: str


@dataclass(frozen=True)
class Hit:
    city_name: str
    city_code: str
    cinema_id: str
    cinema_name: str
    show_id: str
    show_name: str
    schedule_id: str
    show_date: str
    open_time: str
    tags: str
    price_fen: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "city_name": self.city_name,
            "city_code": self.city_code,
            "cinema_id": self.cinema_id,
            "cinema_name": self.cinema_name,
            "show_id": self.show_id,
            "show_name": self.show_name,
            "schedule_id": self.schedule_id,
            "show_date": self.show_date,
            "open_time": self.open_time,
            "tags": self.tags,
            "price_fen": self.price_fen,
            "price_yuan": f"{self.price_fen / 100:.2f}",
        }


def has_meetup(text: str) -> bool:
    return any(tag in text for tag in MEETUP_TAGS)


def schedule_tag_text(raw: Any) -> str:
    """场次标签。字典只取展示名，避免把优惠明细整段拼进标签后误判见面会。"""
    if raw is None or raw == "":
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "".join(schedule_tag_text(item) for item in raw)
    if not isinstance(raw, dict):
        return ""
    parts: list[str] = []
    for key in ("tagName", "tag", "scheduleTag", "showTag", "activityTag", "tinyTag"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    for item in raw.get("tagList") or []:
        if isinstance(item, dict):
            name = item.get("tagName") or item.get("tag") or ""
            if name:
                parts.append(str(name))
        elif item:
            parts.append(str(item))
    return "".join(parts)


def price_matches(price_fen: int, whitelist: tuple[int, ...]) -> bool:
    return any(abs(price_fen - item) <= PRICE_TOLERANCE_FEN for item in whitelist)


def should_query_schedule(cinema: Cinema, only_meetup_tags: bool, whitelist: tuple[int, ...]) -> bool:
    if not only_meetup_tags:
        return True
    if whitelist:
        return True
    return has_meetup(cinema.tag_text)


def day_start_unix(show_date: str) -> int:
    day = datetime.strptime(show_date, "%Y-%m-%d").replace(tzinfo=CST)
    return int(day.timestamp())


def dates_match(raw: Any, target: date) -> bool:
    if raw is None or raw == "":
        return False
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return _timestamp_date(_normalize_unix(int(raw))) == target
    text = str(raw).strip()
    if re.fullmatch(r"\d{10,13}", text):
        return _timestamp_date(_normalize_unix(int(text))) == target
    for fmt, size in (("%Y-%m-%d", 10), ("%Y/%m/%d", 10), ("%Y%m%d", 8)):
        try:
            return datetime.strptime(text[:size], fmt).date() == target
        except ValueError:
            continue
    matched = re.search(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日", text)
    if not matched:
        return False
    year = int(matched.group(1) or target.year)
    try:
        return date(year, int(matched.group(2)), int(matched.group(3))) == target
    except ValueError:
        return False


def _timestamp_date(seconds: int) -> date:
    return datetime.fromtimestamp(seconds, tz=CST).date()


def _normalize_unix(number: int) -> int:
    if abs(number) >= 10_000_000_000:
        return int(number // 1000)
    return int(number)


def channel_fields() -> dict[str, Any]:
    return {
        "platform": 42,
        "comboChannel": 108,
        "dmChannel": "damai@tppnewh5_h5",
    }


def return_value(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ApiError("缺 data")
    value = data.get("returnValue")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ApiError("returnValue 不是 JSON") from exc
    if not isinstance(value, dict):
        raise ApiError("缺 returnValue")
    return value


def cinema_tag_text(raw: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in raw.get("supportList") or []:
        if isinstance(item, dict):
            name = item.get("tagName") or item.get("tag") or ""
        else:
            name = item
        if name:
            parts.append(str(name))
    if not parts and raw.get("tag"):
        parts.append(str(raw.get("tag")))
    return "".join(parts)


async def get_show_cinemas(client: Any, show_id: str, city_code: str, show_date: str) -> list[Cinema]:
    seen: set[str] = set()
    found: list[Cinema] = []
    show_ts = day_start_unix(show_date)
    for page in range(1, MAX_PAGES + 1):
        payload = await client.call(
            CINEMA_API,
            CINEMA_VERSION,
            {
                "showId": show_id,
                "cityCode": city_code,
                "showDate": show_ts,
                "pageIndex": page,
                "pageSize": PAGE_SIZE,
                "pageCode": "APP_SHOW_CINEMA",
                "needShowItem": False,
                "sortType": 3,
                "version": "20220210",
                "longitude": 0,
                "latitude": 0,
                **channel_fields(),
            },
            10.0,
        )
        value = return_value(payload)
        if "cinemas" not in value or value.get("cinemas") is None:
            raise ApiError("缺 cinemas")
        cinemas = value["cinemas"]
        if not isinstance(cinemas, list):
            raise ApiError("缺 cinemas")
        added = 0
        for raw in cinemas:
            if not isinstance(raw, dict):
                continue
            cinema_id = str(raw.get("cinemaId") or "").strip()
            if not cinema_id or cinema_id in seen:
                continue
            seen.add(cinema_id)
            added += 1
            show_time = raw.get("showTimeStr") or raw.get("showTime") or ""
            found.append(
                Cinema(
                    cinema_id=cinema_id,
                    cinema_name=str(raw.get("cinemaName") or ""),
                    show_time=str(show_time),
                    tag_text=cinema_tag_text(raw),
                )
            )
        if len(cinemas) < PAGE_SIZE or added == 0:
            return found
    raise ApiError("分页超过 50 页")


async def search_shows(
    client: Any,
    city_code: str,
    keyword: str,
    max_cinemas: int = 40,
) -> list[dict[str, Any]]:
    """按城市 + 关键词查影片，返回 [{showId, showName, tags}]。

    走「全城影院 -> 各影院排期的 showVos -> 汇总去重」这条路，因为
    淘票票的影片列表接口 getShowList 已下线。
    """
    keyword = (keyword or "").strip()
    seen: set[str] = set()
    results: dict[str, dict[str, Any]] = {}
    cinemas = await _city_cinemas(client, city_code, max_cinemas)
    for cinema in cinemas:
        try:
            schedule = await get_cinema_schedules(client, cinema.cinema_id)
        except Exception:
            continue
        for item in schedule.get("showVos") or []:
            if not isinstance(item, dict):
                continue
            show_id = str(item.get("showId") or "").strip()
            show_name = str(item.get("showName") or "").strip()
            if not show_id or not show_name:
                continue
            if show_id in seen:
                continue
            if keyword and keyword not in show_name:
                continue
            seen.add(show_id)
            special_tag = str(item.get("specialTag") or "")
            results[show_id] = {
                "show_id": show_id,
                "show_name": show_name,
                "tags": special_tag,
            }
    return list(results.values())


# ShowID 全网唯一，搜片不必限城市。扫热门票仓城市覆盖绝大多数在映影片。
SEARCH_CITIES: tuple[str, ...] = (
    "310100",  # 上海
    "110100",  # 北京
    "440100",  # 广州
    "440300",  # 深圳
    "510100",  # 成都
    "330100",  # 杭州
    "420100",  # 武汉
    "320100",  # 南京
    "500100",  # 重庆
    "610100",  # 西安
    "120100",  # 天津
    "320500",  # 苏州
)


async def search_shows_anywhere(
    client: Any,
    keyword: str,
    cities: tuple[str, ...] = SEARCH_CITIES,
    per_city_cinemas: int = 80,
    concurrency: int = 16,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    """不限城市按片名关键词搜影片（ShowID 全网唯一，哪个城市排到都行）。

    高并发会触发淘票票风控（"接口未成功"），所以用信号量限流 + 每请求独立重试。
    影院列表分批拉取，排期请求在信号量内并发，兼顾速度与成功率。
    """
    from monitor.config import city_name_for

    keyword = (keyword or "").strip()
    found: dict[str, dict[str, Any]] = {}
    sem = asyncio.Semaphore(concurrency)
    stats = {"cities_ok": 0, "cities_fail": 0, "cinemas_ok": 0, "cinemas_fail": 0}

    async def call_with_retry(fn, *args):
        for attempt in range(max_retries):
            try:
                return await fn(*args)
            except Exception:
                if attempt == max_retries - 1:
                    return None
                await asyncio.sleep(0.4 * (attempt + 1))
        return None

    async def fetch_schedules(cinema_id: str) -> dict[str, Any] | None:
        async with sem:
            result = await call_with_retry(get_cinema_schedules, client, cinema_id)
            if result is not None:
                stats["cinemas_ok"] += 1
            else:
                stats["cinemas_fail"] += 1
            return result

    async def scan(city: str) -> None:
        cinemas = await call_with_retry(_city_cinemas, client, city, per_city_cinemas)
        if not cinemas:
            return
        schedules = await asyncio.gather(*(fetch_schedules(c.cinema_id) for c in cinemas))
        for schedule in schedules:
            if not isinstance(schedule, dict):
                continue
            for item in schedule.get("showVos") or []:
                if not isinstance(item, dict):
                    continue
                show_id = str(item.get("showId") or "").strip()
                show_name = str(item.get("showName") or "").strip()
                if not show_id or not show_name:
                    continue
                if keyword and keyword not in show_name:
                    continue
                if show_id in found:
                    continue
                found[show_id] = {
                    "show_id": show_id,
                    "show_name": show_name,
                    "tags": str(item.get("specialTag") or ""),
                    "found_city_code": city,
                    "found_city_name": city_name_for(city),
                }

    # 城市间也并发，但限制同时扫的城市数，避免拉影院列表时就被风控
    city_sem = asyncio.Semaphore(4)

    async def scan_guarded(city: str) -> None:
        async with city_sem:
            before = stats["cinemas_ok"]
            await scan(city)
            if stats["cinemas_ok"] > before:
                stats["cities_ok"] += 1
            else:
                stats["cities_fail"] += 1

    await asyncio.gather(*(scan_guarded(city) for city in cities))
    log.info(
        "search_shows_anywhere kw=%s 命中%d 城市ok=%d fail=%d 影院ok=%d fail=%d",
        keyword, len(found), stats["cities_ok"], stats["cities_fail"],
        stats["cinemas_ok"], stats["cinemas_fail"],
    )
    return list(found.values())


async def _city_cinemas(client: Any, city_code: str, limit: int) -> list[Cinema]:
    """拉取一个城市的影院列表（pageCode=APP_CINEMA，不依赖 showId）。"""
    seen: set[str] = set()
    found: list[Cinema] = []
    for page in range(1, MAX_PAGES + 1):
        payload = await client.call(
            CINEMA_API,
            CINEMA_VERSION,
            {
                "cityCode": city_code,
                "pageIndex": page,
                "pageSize": PAGE_SIZE,
                "pageCode": "APP_CINEMA",
                "needShowItem": False,
                "sortType": 3,
                "longitude": 0,
                "latitude": 0,
                "version": "20220210",
                **channel_fields(),
            },
            10.0,
        )
        value = return_value(payload)
        cinemas = value.get("cinemas")
        if not isinstance(cinemas, list):
            raise ApiError("缺 cinemas")
        added = 0
        for raw in cinemas:
            if not isinstance(raw, dict):
                continue
            cinema_id = str(raw.get("cinemaId") or "").strip()
            if not cinema_id or cinema_id in seen:
                continue
            seen.add(cinema_id)
            added += 1
            found.append(
                Cinema(
                    cinema_id=cinema_id,
                    cinema_name=str(raw.get("cinemaName") or ""),
                    show_time="",
                    tag_text="",
                )
            )
            if len(found) >= limit:
                return found
        if len(cinemas) < PAGE_SIZE or added == 0:
            break
    return found


async def get_cinema_schedules(client: Any, cinema_id: str) -> dict[str, Any]:
    payload = await client.call(
        SCHEDULE_API,
        SCHEDULE_VERSION,
        {
            "cinemaId": cinema_id,
            "h5AccessFlag": 1,
            "isMovieDate": 0,
            "longitude": 120.15515,
            "latitude": 30.27415,
            **channel_fields(),
        },
        3.0,
    )
    value = return_value(payload)
    if "showScheduleMap" not in value or value.get("showScheduleMap") is None:
        raise ApiError("缺 showScheduleMap")
    return value


def extract_hits(
    schedule: dict[str, Any],
    cinema: Cinema,
    *,
    show_id: str,
    city_code: str,
    city_name: str,
    show_date: str,
    only_meetup_tags: bool,
    whitelist: tuple[int, ...],
) -> list[Hit]:
    target_day = datetime.strptime(show_date, "%Y-%m-%d").date()
    show_name, special_tag = _show_meta(schedule.get("showVos"), show_id)
    schedule_map = schedule.get("showScheduleMap") or {}
    days = schedule_map.get(show_id)
    if days is None:
        days = schedule_map.get(str(show_id)) or []
    if not isinstance(days, list):
        days = []

    hits: list[Hit] = []
    saw_target_session = False
    for day in days:
        if not isinstance(day, dict):
            continue
        raw_date = _first_present(day.get("date"), day.get("showDate"), day.get("dateStr"), day.get("dateDesc"), day.get("dateTip"))
        if not dates_match(raw_date, target_day):
            continue
        for session in day.get("scheduleVos") or []:
            if not isinstance(session, dict):
                continue
            saw_target_session = True
            schedule_id = str(session.get("scheduleId") or "").strip()
            if not schedule_id:
                continue
            open_time = session.get("openTime")
            if open_time is None or open_time == "":
                open_time = session.get("showTime") or ""
            price_raw = session.get("tradePrice")
            if price_raw is None or price_raw == "":
                price_raw = session.get("memberTradePrice")
            price_fen = _to_int(price_raw)
            # 影院列表上的「明星见面会」只说明这家可能有见面会，不能让当天普通场次免票价检查。
            label = f"{special_tag}{schedule_tag_text(session.get('scheduleTag'))}"
            if only_meetup_tags and not has_meetup(label):
                if not whitelist or not price_matches(price_fen, whitelist):
                    continue
                label = f"{label}白名单票价"
            hits.append(
                _hit(
                    city_name,
                    city_code,
                    cinema,
                    show_id,
                    show_name,
                    schedule_id,
                    show_date,
                    str(open_time),
                    label,
                    price_fen,
                )
            )

    if hits or saw_target_session or not has_meetup(cinema.tag_text) or not _show_present(schedule, show_id):
        return hits
    return [
        _hit(
            city_name,
            city_code,
            cinema,
            show_id,
            show_name,
            f"list-{cinema.cinema_id}",
            show_date,
            cinema.show_time,
            cinema.tag_text,
            0,
        )
    ]


def _hit(
    city_name: str,
    city_code: str,
    cinema: Cinema,
    show_id: str,
    show_name: str,
    schedule_id: str,
    show_date: str,
    open_time: str,
    tags: str,
    price_fen: int,
) -> Hit:
    return Hit(
        city_name=city_name,
        city_code=city_code,
        cinema_id=cinema.cinema_id,
        cinema_name=cinema.cinema_name,
        show_id=show_id,
        show_name=show_name,
        schedule_id=schedule_id,
        show_date=show_date,
        open_time=open_time,
        tags=tags,
        price_fen=price_fen,
    )


def _show_meta(show_vos: Any, show_id: str) -> tuple[str, str]:
    items = [item for item in show_vos or [] if isinstance(item, dict)]
    matched = [item for item in items if _vo_show_id(item) == str(show_id)]
    chosen = matched[0] if matched else (items[0] if len(items) == 1 else None)
    if chosen is None:
        return "", ""
    return str(chosen.get("showName") or ""), str(chosen.get("specialTag") or "")


def _vo_show_id(item: dict[str, Any]) -> str:
    for key in ("showId", "id", "showID"):
        value = item.get(key)
        if value is not None and value != "":
            return str(value)
    return ""


def _show_present(schedule: dict[str, Any], show_id: str) -> bool:
    schedule_map = schedule.get("showScheduleMap") or {}
    if str(show_id) in {str(key) for key in schedule_map}:
        return True
    for item in schedule.get("showVos") or []:
        if isinstance(item, dict) and _vo_show_id(item) == str(show_id):
            return True
    return False


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(round(float(value)))
