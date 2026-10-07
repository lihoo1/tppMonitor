from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import httpx

APP_KEY = "12574478"
HOST = "https://wxapi.m.taopiaopiao.com"
# getShowList 已不存在。错签请求打影院列表接口，服务端才会下发 _m_h5_tk。
HANDSHAKE_API = "mtop.film.MtopCinemaAPI.getCinemaListInPage"
HANDSHAKE_VERSION = "8.0"
ZERO_SIGN = "0" * 32
TOKEN_MARKERS = ("TOKEN_EMPTY", "TOKEN_EXOIRED", "TOKEN_EXPIRED", "ILLEGAL_SIGN")


class MtopError(Exception):
    def __init__(self, message: str, ret: list[Any] | None = None):
        super().__init__(message)
        self.ret = list(ret or [])


def sign(token: str, timestamp_ms: str, data: str) -> str:
    raw = f"{token}&{timestamp_ms}&{APP_KEY}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def token_from_m_h5_tk(value: str) -> str:
    if not value:
        return ""
    return value.split("_", 1)[0]


def dumps_data(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def parse_body(text: str) -> dict[str, Any]:
    body = text.strip().lstrip("\ufeff")
    if not body:
        raise MtopError("空响应")
    if body[0] in "{[":
        payload = json.loads(body)
    else:
        start = body.find("(")
        end = body.rfind(")")
        if start < 0 or end <= start:
            raise MtopError("无法解析响应")
        payload = json.loads(body[start + 1 : end])
    if not isinstance(payload, dict):
        raise MtopError("响应不是对象")
    return payload


def ret_codes(payload: dict[str, Any]) -> list[str]:
    ret = payload.get("ret") or []
    if isinstance(ret, str):
        ret = [ret]
    return [str(item) for item in ret]


def is_success(payload: dict[str, Any]) -> bool:
    codes = ret_codes(payload)
    if not any(item.split("::", 1)[0] == "SUCCESS" for item in codes):
        return False
    data = payload.get("data")
    if isinstance(data, dict) and data.get("success") is False:
        return False
    return True


def is_token_empty(payload: dict[str, Any]) -> bool:
    return any("TOKEN_EMPTY" in item for item in ret_codes(payload))


def needs_new_token(payload: dict[str, Any]) -> bool:
    codes = ret_codes(payload)
    return any(any(marker in item for marker in TOKEN_MARKERS) for item in codes)


class MtopClient:
    """一条城市线程独占的客户端。令牌过期时只在这个客户端里自动续。"""

    def __init__(self) -> None:
        self._token = ""
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
                ),
                "Referer": "https://m.taopiaopiao.com/",
            },
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=64),
            follow_redirects=True,
        )

    @property
    def token(self) -> str:
        return self._token

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, api: str, version: str, data: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self._token:
            await self.handshake()
        payload = await self._once(api, version, data, timeout, sign_override=None)
        if is_token_empty(payload) and self._token:
            payload = await self._once(api, version, data, timeout, sign_override=None)
        if needs_new_token(payload):
            await self.handshake()
            payload = await self._once(api, version, data, timeout, sign_override=None)
        if not is_success(payload):
            raise MtopError("接口未成功", ret=payload.get("ret"))
        return payload

    async def handshake(self) -> None:
        payload = await self._once(HANDSHAKE_API, HANDSHAKE_VERSION, {}, 10.0, sign_override=ZERO_SIGN)
        if not self._token:
            raise MtopError("握手未拿到令牌", ret=payload.get("ret"))

    async def _once(
        self,
        api: str,
        version: str,
        data: dict[str, Any],
        timeout: float,
        sign_override: str | None,
    ) -> dict[str, Any]:
        data_text = dumps_data(data)
        timestamp = str(int(time.time() * 1000))
        signed = sign_override if sign_override is not None else sign(self._token, timestamp, data_text)
        url = f"{HOST}/h5/{api.lower()}/{version}/"
        params = {
            "jsv": "2.7.4",
            "appKey": APP_KEY,
            "t": timestamp,
            "sign": signed,
            "api": api,
            "v": version,
            "H5Request": "true",
            "type": "jsonp",
            "dataType": "jsonp",
            "callback": "mtopjsonp1",
            "data": data_text,
        }
        try:
            response = await self._http.get(url, params=params, timeout=timeout)
        except httpx.HTTPError as exc:
            raise MtopError(f"请求失败：{exc}") from exc
        self._absorb(response)
        try:
            return parse_body(response.text)
        except json.JSONDecodeError as exc:
            raise MtopError("响应不是 JSON") from exc

    def _absorb(self, response: httpx.Response) -> None:
        raw = ""
        enc = ""
        jar_value = response.cookies.get("_m_h5_tk")
        if jar_value:
            raw = str(jar_value)
        jar_enc = response.cookies.get("_m_h5_tk_enc")
        if jar_enc:
            enc = str(jar_enc)
        for line in response.headers.get_list("set-cookie"):
            piece = line.split(";", 1)[0]
            if piece.startswith("_m_h5_tk="):
                raw = piece.split("=", 1)[1]
            elif piece.startswith("_m_h5_tk_enc="):
                enc = piece.split("=", 1)[1]
        if not raw:
            return
        self._token = token_from_m_h5_tk(raw)
        self._http.cookies.set("_m_h5_tk", raw, domain="wxapi.m.taopiaopiao.com", path="/")
        if enc:
            self._http.cookies.set("_m_h5_tk_enc", enc, domain="wxapi.m.taopiaopiao.com", path="/")
