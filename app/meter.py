# SPDX-License-Identifier: GPL-3.0-or-later
"""Reads the Kamst-IR gateway (Smart Gateways) that sits on a Kamstrup MULTICAL heat meter.

The gateway answers GET /kamst-ir/api/read with a flat JSON object of strings, e.g.
{"heat_energy": "387.421", "power": "4.800", "temp1": "72.06", ...}. It reads the meter over
infrared about once a minute, so polling faster than that only returns the same values again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger(__name__)

TIMEOUT = 10


# values that look like numbers but are names, versions or clock readings
TEXT_KEYS = {"mac_address", "gateway_model", "startup_time", "firmware_running", "firmware_available", "device_time", "device_date"}


def parse(raw: dict) -> dict:
    """Numbers become floats, "true"/"false" become booleans, everything else stays text."""
    out: dict = {}
    for k, v in raw.items():
        if k in TEXT_KEYS:
            out[k] = None if v is None else str(v).strip()
            continue
        if isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
            continue
        s = str(v).strip()
        if s.lower() in ("true", "false"):
            out[k] = s.lower() == "true"
            continue
        try:
            out[k] = float(s)
        except ValueError:
            out[k] = s
    return out


async def fetch(session: aiohttp.ClientSession, url: str) -> dict:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
        if r.status != 200:
            raise ConnectionError(f"the gateway answered HTTP {r.status}")
        text = await r.text()
    try:
        data = json.loads(text)
    except ValueError:
        raise ConnectionError("the gateway did not send meter data (not JSON); is this the right address?") from None
    if not isinstance(data, dict) or "heat_energy" not in data:
        raise ConnectionError("the answer has no meter values (heat_energy is missing); is this a Kamst-IR gateway?")
    return data


def describe(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"no answer within {TIMEOUT} s"
    if isinstance(exc, aiohttp.ClientConnectorError):
        return "could not connect (wrong address, or the gateway is off or out of WiFi range)"
    if isinstance(exc, ConnectionError):
        return str(exc)
    return f"{exc.__class__.__name__}: {exc}"


class Meter:
    def __init__(self, url: str, interval: int, on_reading: Callable[[float, dict, dict], Awaitable[None] | None],
                 on_status: Callable[[str, dict], None]):
        self.url = url
        self.interval = interval
        self.on_reading = on_reading
        self.on_status = on_status  # ("offline" | "online", status)
        self.status = {"url": url, "connected": None, "last_ok": None, "last_attempt": None, "error": None, "fails": 0}
        self._wake = asyncio.Event()
        self.session: aiohttp.ClientSession | None = None

    def set_url(self, url: str) -> None:
        self.url = url
        self.status.update(url=url, connected=None, error=None, fails=0)
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    async def read_once(self) -> None:
        if not self.url:
            self.status.update(connected=None, error="no gateway address yet")
            return
        st = self.status
        st["last_attempt"] = time.time()
        try:
            raw = await fetch(self.session, self.url)
        except Exception as exc:  # noqa: BLE001
            st["fails"] += 1
            st["error"] = describe(exc)
            if st["fails"] == 3 or (st["connected"] is None and st["fails"] == 1):
                log.warning("gateway %s: %s", self.url, st["error"])
            if st["connected"] is not False and st["fails"] >= 3:
                st["connected"] = False
                self.on_status("offline", st)
            return
        was = st["connected"]
        st.update(connected=True, last_ok=time.time(), error=None, fails=0)
        if was is False:
            self.on_status("online", st)
        res = self.on_reading(time.time(), raw, parse(raw))
        if asyncio.iscoroutine(res):
            await res

    async def run(self) -> None:
        self.session = aiohttp.ClientSession()
        try:
            while True:
                try:
                    await self.read_once()
                except Exception as exc:  # noqa: BLE001  never let one bad reading stop the loop
                    log.exception("reading failed: %s", exc)
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.session.close()
