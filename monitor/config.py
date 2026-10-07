from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

DEFAULT_PRICES_YUAN = (69, 99, 199, 299, 399, 405, 499, 398.9)
DEFAULT_WECHAT_ID = "liwuhe2023"

# 城市编码 -> 城市名。目标里只填编码时自动补名。
CITY_NAMES = {
    "110100": "北京",
    "120100": "天津",
    "130100": "石家庄",
    "130200": "唐山",
    "140100": "太原",
    "150100": "呼和浩特",
    "210100": "沈阳",
    "210200": "大连",
    "220100": "长春",
    "230100": "哈尔滨",
    "310100": "上海",
    "320100": "南京",
    "320200": "无锡",
    "320300": "徐州",
    "320400": "常州",
    "320500": "苏州",
    "320600": "南通",
    "320900": "盐城",
    "330100": "杭州",
    "330200": "宁波",
    "330300": "温州",
    "330400": "嘉兴",
    "330600": "绍兴",
    "330700": "金华",
    "330800": "衢州",
    "331000": "台州",
    "340100": "合肥",
    "340200": "芜湖",
    "350100": "福州",
    "350200": "厦门",
    "350300": "莆田",
    "350500": "泉州",
    "360100": "南昌",
    "370100": "济南",
    "370200": "青岛",
    "370600": "烟台",
    "370700": "潍坊",
    "410100": "郑州",
    "410300": "洛阳",
    "420100": "武汉",
    "420600": "襄阳",
    "430100": "长沙",
    "430300": "湘潭",
    "440100": "广州",
    "440300": "深圳",
    "440400": "珠海",
    "440600": "佛山",
    "440800": "湛江",
    "441300": "惠州",
    "441900": "东莞",
    "442000": "中山",
    "450100": "南宁",
    "460100": "海口",
    "460200": "三亚",
    "500100": "重庆",
    "510100": "成都",
    "510600": "德阳",
    "510700": "绵阳",
    "520100": "贵阳",
    "530100": "昆明",
    "610100": "西安",
    "620100": "兰州",
    "630100": "西宁",
    "640100": "银川",
    "650100": "乌鲁木齐",
}


def city_name_for(code: str) -> str:
    return CITY_NAMES.get(code.strip(), "")


def build_city_options() -> list[dict[str, str]]:
    return [{"code": code, "name": name} for code, name in CITY_NAMES.items()]


@dataclass(frozen=True)
class Target:
    show_id: str
    city_code: str
    city_name: str
    show_date: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.show_id, self.city_code, self.show_date)


@dataclass(frozen=True)
class WeComConfig:
    webhooks: tuple[str, ...]
    wechat_id: str


@dataclass(frozen=True)
class AppConfig:
    listen_host: str
    listen_port: int
    ui_token: str
    monitor_enabled: bool
    interval_seconds: int
    only_meetup_tags: bool
    process_existing_on_start: bool
    price_whitelist_fen: tuple[int, ...]
    database: str
    wecom: WeComConfig
    targets: tuple[Target, ...]


def to_fen(value: float | str) -> int:
    number = float(value)
    if number < 10000:
        return int(round(number * 100))
    return int(round(number))


def fen_to_yuan_text(fen: int) -> str:
    if fen % 100 == 0:
        return str(fen // 100)
    return f"{fen / 100:.2f}".rstrip("0").rstrip(".")


def parse_price_text(text: str) -> tuple[int, ...]:
    raw = str(text or "").strip()
    if not raw:
        return ()
    parts = [item for item in re.split(r"[,，\s]+", raw) if item]
    return tuple(to_fen(item) for item in parts)


def format_price_text(whitelist_fen: tuple[int, ...]) -> str:
    return ",".join(fen_to_yuan_text(item) for item in whitelist_fen)


class ConfigHolder:
    """网页保存后替换内存配置，扫描下一轮读取。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cfg = load_config(self.path)

    def get(self) -> AppConfig:
        with self._lock:
            return self._cfg

    def replace(self, cfg: AppConfig) -> None:
        with self._lock:
            save_config(self.path, cfg)
            self._cfg = cfg


def load_config(path: str | Path) -> AppConfig:
    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"找不到配置文件 {file_path}")
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是映射")
    return config_from_dict(raw)


def config_from_dict(raw: dict) -> AppConfig:
    interval = int(raw.get("interval_seconds", 10))
    if interval < 1 or interval > 1800:
        raise ValueError("发现间隔必须在 1 到 1800 秒之间")

    port = int(raw.get("listen_port", 8787))
    if port < 1 or port > 65535:
        raise ValueError("监听端口无效")

    prices = raw.get("price_whitelist_yuan", list(DEFAULT_PRICES_YUAN))
    if prices is None:
        whitelist: tuple[int, ...] = ()
    elif isinstance(prices, str):
        whitelist = parse_price_text(prices)
    elif isinstance(prices, list):
        whitelist = tuple(to_fen(item) for item in prices)
    else:
        raise ValueError("票价白名单必须是列表或逗号分隔文本")

    wecom_raw = raw.get("wecom") or {}
    if not isinstance(wecom_raw, dict):
        raise ValueError("wecom 必须是映射")
    hooks = wecom_raw.get("webhooks") or []
    if isinstance(hooks, str):
        hooks = [line.strip() for line in hooks.splitlines() if line.strip()]
    if not isinstance(hooks, list) or not all(isinstance(item, str) for item in hooks):
        raise ValueError("企业微信 webhooks 必须是字符串列表")
    webhooks = tuple(item.strip() for item in hooks if item.strip())
    for hook in webhooks:
        if not hook.startswith("https://"):
            raise ValueError("企业微信 Webhook 必须以 https:// 开头")

    targets = parse_targets(raw.get("targets") or [])
    database = str(raw.get("database") or "data/monitor.sqlite3")
    return AppConfig(
        listen_host=str(raw.get("listen_host") or "0.0.0.0").strip() or "0.0.0.0",
        listen_port=port,
        ui_token=str(raw.get("ui_token") or "").strip(),
        monitor_enabled=bool(raw.get("monitor_enabled", True)),
        interval_seconds=interval,
        only_meetup_tags=bool(raw.get("only_meetup_tags", True)),
        process_existing_on_start=bool(raw.get("process_existing_on_start", True)),
        price_whitelist_fen=whitelist,
        database=database,
        wecom=WeComConfig(
            webhooks=webhooks,
            wechat_id=str(wecom_raw.get("wechat_id") or DEFAULT_WECHAT_ID).strip(),
        ),
        targets=targets,
    )


def _normalize_date(text: str) -> str:
    """把 2026/10/1、2026-1-5、2026年10月1日 等写法统一成 YYYY-MM-DD。"""
    text = (text or "").strip()
    if not text:
        return ""
    # 抽出所有数字段
    parts = re.findall(r"\d+", text)
    if len(parts) >= 3:
        year, month, day = parts[0], parts[1], parts[2]
        if len(year) == 4:
            return f"{year}-{int(month):02d}-{int(day):02d}"
    return text


def parse_targets(targets_raw: object) -> tuple[Target, ...]:
    if not isinstance(targets_raw, list):
        raise ValueError("目标必须是列表")
    targets: list[Target] = []
    seen: set[tuple[str, str, str]] = set()
    for item in targets_raw:
        if not isinstance(item, dict):
            raise ValueError("目标必须是映射")
        show_id = str(item.get("show_id") or "").strip()
        city_code = str(item.get("city_code") or "").strip()
        show_date = str(item.get("date") or item.get("show_date") or "").strip()
        show_date = _normalize_date(show_date)
        city_name = str(item.get("city_name") or "").strip()
        if not city_name:
            city_name = city_name_for(city_code)
        if not city_name:
            city_name = city_code
        if not show_id and not city_code and not show_date:
            continue
        if not show_id or not city_code or not show_date:
            raise ValueError("每条目标都要有 ShowID、城市编码、日期")
        try:
            datetime.strptime(show_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"日期必须是 YYYY-MM-DD：{show_date}") from exc
        target = Target(show_id, city_code, city_name, show_date)
        if target.key in seen:
            raise ValueError(f"目标重复：ShowID {show_id}，城市 {city_code}，日期 {show_date}")
        seen.add(target.key)
        targets.append(target)
    return tuple(targets)


def save_config(path: str | Path, cfg: AppConfig) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "listen_host": cfg.listen_host,
        "listen_port": cfg.listen_port,
        "ui_token": cfg.ui_token,
        "monitor_enabled": cfg.monitor_enabled,
        "interval_seconds": cfg.interval_seconds,
        "only_meetup_tags": cfg.only_meetup_tags,
        "process_existing_on_start": cfg.process_existing_on_start,
        "price_whitelist_yuan": [yaml_price(item) for item in cfg.price_whitelist_fen],
        "database": cfg.database,
        "wecom": {
            "webhooks": list(cfg.wecom.webhooks),
            "wechat_id": cfg.wecom.wechat_id,
        },
        "targets": [
            {
                "show_id": item.show_id,
                "city_code": item.city_code,
                "city_name": item.city_name,
                "date": item.show_date,
            }
            for item in cfg.targets
        ],
    }
    text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(file_path)


def yaml_price(fen: int) -> int | float:
    if fen % 100 == 0:
        return fen // 100
    return round(fen / 100, 2)
