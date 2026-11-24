# SPDX-License-Identifier: GPL-3.0-or-later
"""Kamstrup Monitor: a local dashboard and history for a Kamstrup MULTICAL heat meter,
read through the Smart Gateways Kamst-IR gateway.

Copyright (C) 2026 the Kamstrup Monitor contributors. Licensed under the GPL-3.0-or-later (see LICENSE).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from .apikeys import ApiKeys, key_allows
from .auth import Auth
from .meter import Meter, describe, fetch, parse
from .mqtt import Mqtt
from .settings import Settings, check_gateway
from .store import Store, bucket_next, bucket_start

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
PORT = int(os.environ.get("PORT", "8080"))
HOST_PORT = os.environ.get("HOST_PORT") or PORT
RESET_PASSWORD = os.environ.get("RESET_PASSWORD", "").strip().lower() in ("1", "true", "yes")
COOKIE = "km_session"
OPEN_PATHS = {"/", "/api/auth/status", "/api/auth/login"}  # with sign-in on, everything else needs a session
STATIC = Path(__file__).parent / "static"
MAX_HOURS = 24 * 3660

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kamstrup-monitor")
os.makedirs(DATA_DIR, exist_ok=True)


def number(request, key: str, default: float, lo: float, hi: float) -> float:
    raw = request.query.get(key) or default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    if value != value:
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": f"{key} must be a number"}), content_type="application/json")
    return max(lo, min(value, hi))


def local_midnight(ts: float, days_back: int = 0) -> int:
    lt = time.localtime(ts)
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - days_back, 0, 0, 0, 0, 0, -1)))


class App:
    def __init__(self):
        self.settings = Settings(DATA_DIR)
        self.auth = Auth(DATA_DIR, reset=RESET_PASSWORD)
        self.apikeys = ApiKeys(DATA_DIR)
        self.store = Store(os.path.join(DATA_DIR, "meter.db"))
        self.store.retention_days = self.settings["reading_days"]
        self.meter = Meter(self.settings["gateway"], self.settings["poll_interval"], self.on_reading, self.on_status)
        self.settings.listeners.append(self.apply_settings)
        self.last_raw: dict = {}
        self.mqtt = Mqtt(self)

    def apply_settings(self, changed: dict) -> None:
        v = self.settings
        self.meter.interval = v["poll_interval"]
        self.store.retention_days = v["reading_days"]
        if "gateway" in changed:
            old, new = changed["gateway"]
            self.store.event("settings", f"Gateway address changed to {new}" + (f" (was {old})." if old else "."))
            self.meter.set_url(new)
        if "reading_days" in changed:
            self.store.prune()
        if any(k.startswith("mqtt_") for k in changed):
            self.mqtt.restart()

    def on_reading(self, ts: float, raw: dict, parsed: dict) -> None:
        self.last_raw = raw
        self.store.add(ts, parsed)

    def on_status(self, what: str, st: dict) -> None:
        if what == "offline":
            self.store.event("offline", f"The gateway stopped answering: {st['error']}.")
        else:
            self.store.event("online", "The gateway answers again.")
            log.info("gateway back online")

    def meter_view(self) -> dict:
        """The latest values as flat JSON with plain names: GET /api/meter and the MQTT state topic."""
        r, ts, st = self.store.last, self.store.last_ts, self.meter.status
        key = (ts, st["connected"], self.settings["price_per_gj"], int(time.time() // 60))
        if getattr(self, "_view", (None,))[0] == key:
            return self._view[1]
        num = lambda k: r.get(k) if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool) else None  # noqa: E731
        flow, power, e = num("flow"), num("power"), num("heat_energy")
        on = None if flow is None and power is None else (flow or 0) > 0 or (power or 0) >= 0.05
        today = self.store.energy_between(local_midnight(time.time()), time.time())["energy"] if ts else None
        runs = self.store.runs(time.time() - 86400, time.time(), self._gap())["runs"] if ts else []
        cur = runs[-1] if runs and runs[-1].get("ongoing") else None
        tw = self.store.tap_water(local_midnight(time.time()), time.time(), self._gap()) if ts else None
        tw_kwh = round(min(tw["kwh"], today * 277.7778), 2) if tw and today is not None else None
        price = self.settings["price_per_gj"]
        view = {
            "ts": ts, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)) if ts else None,
            "online": bool(st["connected"]), "heat_on": on, "heat_on_since": cur["start"] if cur and on else None,
            "power_kw": power, "flow_lh": flow, "temp_in_c": num("temp1"), "temp_back_c": num("temp2"), "temp_diff_c": num("tempdiff"),
            "heat_energy_gj": e, "heat_energy_kwh": round(e * 277.7778, 1) if e is not None else None,
            "today_gj": today, "today_kwh": round(today * 277.7778, 2) if today is not None else None,
            "today_cost": round(today * price, 2) if today is not None and price else None,
            # an estimate from the pattern in the readings, not measured (see tapwater.py)
            "tap_water_now": bool(tw and tw["now"]) if tw else None,
            "tap_water_now_likelihood": tw["now"]["label"] if tw and tw["now"] else None,
            "tap_water_today_kwh": tw_kwh, "tap_water_today_uses": tw["uses"] if tw else None,
            "heating_today_kwh": round(today * 277.7778 - tw_kwh, 2) if tw_kwh is not None else None,
            "cooling_energy_gj": num("cooling_energy"), "volume_m3": num("volume"), "hourcounter": num("hourcounter"),
            "infoevent": num("infoevent"), "leak_burst": num("leak_burst"), "wifi_rssi": num("wifi_rssi"),
            "max_power_month_kw": num("maxpower_m"), "gateway_mac": str(r.get("mac_address") or "").replace("_", ":") or None,
            "gateway_firmware": r.get("firmware_running"),
        }
        self._view = (key, view)
        return view

    # ------------------------------------------------------------------ background
    async def housekeeping(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                n = self.store.prune()
                if n:
                    log.info("thinned out %d old readings", n)
            except Exception as exc:  # noqa: BLE001
                log.warning("prune: %s", exc)

    # ------------------------------------------------------------------ http
    async def index(self, request):
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    def _settings_public(self) -> dict:
        v = self.settings.values
        return {"gateway": v["gateway"], "unit": v["unit"], "price_per_gj": v["price_per_gj"], "currency": v["currency"],
                "poll_interval": v["poll_interval"]}

    async def api_state(self, request):
        st = dict(self.meter.status)
        return web.json_response({"ok": True, "now": time.time(), "status": st, "ts": self.store.last_ts, "reading": self.store.last,
                                  "settings": self._settings_public(), "stats": self.store.stats(), "tz": time.strftime("%Z")})

    async def api_meter(self, request):
        return web.json_response(self.meter_view())

    async def api_series(self, request):
        hours = number(request, "hours", 24, 0.25, MAX_HOURS)
        end = time.time()
        start = end - hours * 3600
        first = self.store.stats()["first_ts"]
        if first and start < first:
            start = first if hours >= 24 * 3000 else start
        return web.json_response({"t0": start, "t1": end, **self.store.series(start, end)})

    async def api_usage(self, request):
        """Heat per hour / day / week / month / year in local time, for the last `hours` (All = since the first reading)."""
        hours = number(request, "hours", 24 * 7, 1, MAX_HOURS)
        group = request.query.get("group") or ""
        end = time.time()
        start = end - hours * 3600
        first = self.store.stats()["first_ts"]
        if hours >= 24 * 3000 and first:
            start = first
        if group not in ("hour", "day", "week", "month", "year"):
            span = end - start
            group = "hour" if span <= 2 * 86400 else "day" if span <= 62 * 86400 else "week" if span <= 200 * 86400 else "month"
        if group == "hour" and end - start > 31 * 86400:
            group = "day"
        start = bucket_start(start, group) if group != "hour" else start
        buckets = self.store.usage(start, end, group)
        self._add_tap(buckets, start, end)
        return web.json_response({"group": group, "t0": start, "t1": end, "buckets": buckets,
                                  "typical": self.store.typical_day(max(1, min(int(hours / 24), 365)))})

    def _add_tap(self, buckets: list[dict], start: float, end: float) -> None:
        """Per bucket: heat that was probably hot tap water (kWh, estimate); None without detailed readings."""
        tw = self.store.tap_water(start, end, self._gap())
        first = self.store.db.execute("SELECT MIN(ts) FROM readings").fetchone()[0]
        for b in buckets:
            ok = first is not None and b["end"] > first and b["hours_data"]
            k = sum(e["kwh"] for e in tw["events"] if b["t"] <= e["start"] < b["end"])
            b["tap_kwh"] = round(min(k, b["energy"] * 277.7778), 3) if ok else None
            b["tap_uses"] = sum(1 for e in tw["events"] if b["t"] <= e["start"] < b["end"]) if ok else None

    def _compare(self, a: float, b: float, pa: float, pb: float) -> dict:
        cur, prev = self.store.energy_between(a, b), self.store.energy_between(pa, pb)
        return {"start": a, "end": b, "energy": cur["energy"], "coverage": cur["coverage"],
                "prev_start": pa, "prev_end": pb, "prev_energy": prev["energy"], "prev_coverage": prev["coverage"]}

    async def api_summary(self, request):
        now = time.time()
        today = local_midnight(now)
        week = bucket_start(now, "week")
        month = bucket_start(now, "month")
        prev_month = bucket_start(month - 86400, "month")
        elapsed_month = now - month
        prev_month_end = min(month, prev_month + elapsed_month)
        year = bucket_start(now, "year")
        prev_year = bucket_start(year - 86400, "year")
        hours_today = self.store.usage(today, now, "hour")
        days = self.store.usage(local_midnight(now, 6), now, "day")
        self._add_tap(hours_today, today, now)
        self._add_tap(days, local_midnight(now, 6), now)
        return web.json_response({
            "now": now,
            "today": {**self.store.energy_between(today, now), "hours": hours_today},
            "days": days,
            "compare": {
                "today": self._compare(today, now, today - 86400, now - 86400),
                "week": self._compare(week, now, week - 7 * 86400, now - 7 * 86400),
                "last7": self._compare(now - 7 * 86400, now, now - 14 * 86400, now - 7 * 86400),
                "month": self._compare(month, now, prev_month, prev_month_end),
                "year": self._compare(year, now, prev_year, prev_year + (now - year)),
            },
            "last24": self._last24(now),
            "typical": self.store.typical_day(28),
            "stats": self.store.stats(),
        })

    def _last24(self, now: float) -> dict:
        b = self.store.usage(now - 86400, now, "year")  # one bucket that covers the whole 24 h (two around New Year)
        e = sum(x["energy"] for x in b)
        dts = [x for x in b if x["dt"] is not None]
        return {"energy": round(e, 4), "dt": dts[-1]["dt"] if dts else None, "t1": dts[-1]["t1"] if dts else None,
                "t2": dts[-1]["t2"] if dts else None, "p_max": max((x["p_max"] for x in b if x["p_max"] is not None), default=None)}

    async def api_runs(self, request):
        """Every time heat came in during the last `hours` (flow through the meter), with totals."""
        hours = number(request, "hours", 24, 0.25, 24 * 366)
        end = time.time()
        res = self.store.runs(end - hours * 3600, end, self._gap())
        res["runs"] = list(reversed(res["runs"]))
        return web.json_response(res)

    def _gap(self) -> int:
        return max(300, int(self.settings["poll_interval"]) * 4)

    async def api_tapwater(self, request):
        """Probably hot tap water (shower, tap) during the last `hours`: an estimate, newest first."""
        hours = number(request, "hours", 24, 0.25, 24 * 366)
        end = time.time()
        res = self.store.tap_water(end - hours * 3600, end, self._gap())
        total = self.store.energy_between(end - hours * 3600, end)["energy"] * 277.7778
        res["kwh"] = round(min(res["kwh"], total), 3)
        res["total_kwh"] = round(total, 3)
        res["heating_kwh"] = round(total - res["kwh"], 3)
        res["events"] = list(reversed(res["events"]))
        res["estimate"] = True
        return web.json_response(res)

    async def api_events(self, request):
        return web.json_response(self.store.events(int(number(request, "limit", 200, 1, 5000))))

    async def api_export(self, request):
        hours = number(request, "hours", MAX_HOURS, 1, MAX_HOURS)
        end = time.time()
        start = end - hours * 3600
        resp = web.StreamResponse(headers={"Content-Type": "text/csv; charset=utf-8",
                                           "Content-Disposition": f'attachment; filename="kamstrup-readings-{time.strftime("%Y%m%d")}.csv"'})
        await resp.prepare(request)
        for chunk in self.store.export_csv(start, end):
            await resp.write(chunk.encode())
        await resp.write_eof()
        return resp

    async def api_refresh(self, request):
        await self.meter.read_once()
        return web.json_response({"ok": bool(self.meter.status["connected"]), "error": self.meter.status["error"]})

    async def api_settings(self, request, signed_in: bool | None = None):
        if signed_in is None:
            signed_in = bool(self.auth.session(request.cookies.get(COOKIE)))
        return web.json_response({"ok": True, **self.settings.view(), "port": HOST_PORT, "signed_in": signed_in,
                                  "default_password": self.auth.must_change, "tz": os.environ.get("TZ") or time.strftime("%Z"),
                                  "status": self.meter.status, "stats": self.store.stats(), "mqtt": self.mqtt.view()})

    async def api_settings_save(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("send the settings as an object")
            password = body.pop("password", None)
            token = None
            if "require_signin" in body:
                want = str(body["require_signin"]).lower() in ("1", "true", "yes", "on")
                if want != self.settings["require_signin"]:
                    if want and self.auth.must_change:
                        # still the default password: switching sign-in on means choosing a real one first
                        self.auth.change(None, str(password or ""), None)
                    elif not self.auth.check(password):
                        return web.json_response({"ok": False, "error": "the password is not right", "need_password": True}, status=403)
                    if want and not self.auth.session(request.cookies.get(COOKIE)):
                        token, ttl = self.auth.login(str(password), request.remote or "?", True)
            self.settings.update(body)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        resp = await self.api_settings(request, signed_in=True if token else None)
        if token:  # this device stays signed in when sign-in is switched on
            resp.set_cookie(COOKIE, token, max_age=ttl, httponly=True, samesite="Strict", secure=request.secure, path="/")
        return resp

    async def api_settings_retention(self, request):
        days = request.query.get("days", "")
        return web.json_response({"ok": True, "count": self.store.count_older(int(days)) if days.isdigit() and int(days) > 0 else 0})

    async def api_settings_test(self, request):
        """Check that an address reaches a Kamst-IR gateway, without switching to it."""
        try:
            url = check_gateway((await request.json()).get("gateway", ""))
            if not url:
                raise ValueError("enter the gateway's IP address")
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)})
        try:
            async with aiohttp.ClientSession() as s:
                raw = await fetch(s, url)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "url": url, "error": describe(exc)})
        p = parse(raw)
        return web.json_response({"ok": True, "url": url, "model": p.get("gateway_model"), "heat_energy": p.get("heat_energy"), "power": p.get("power")})

    # ------------------------------------------------------------------ signing in
    @web.middleware
    async def require_login(self, request, handler):
        path = request.path
        if not self.settings["require_signin"] or path in OPEN_PATHS or path.startswith("/static/"):
            return await handler(request)
        if self.auth.session(request.cookies.get(COOKIE)):
            if self.auth.must_change and path not in ("/api/auth/password", "/api/auth/logout"):
                return web.json_response({"ok": False, "error": "choose a new password first", "must_change": True}, status=403)
            return await handler(request)
        # other applications: an API key in a header (never in the URL, where it would end up in logs)
        auth = request.headers.get("Authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("X-API-Key", "").strip()
        if key:
            entry = self.apikeys.find(key)
            if not entry:
                return web.json_response({"ok": False, "error": "unknown or revoked API key"}, status=401)
            if not key_allows(entry["scope"], request.method, path):
                return web.json_response({"ok": False, "error": "not allowed with an API key: keys can only read"}, status=403)
            self.apikeys.touch(entry)
            return await handler(request)
        return web.json_response({"ok": False, "error": "sign in first", "signed_out": True}, status=401)

    # ------------------------------------------------------------------ API keys (managed by a person, never by a key)
    async def api_keys(self, request):
        return web.json_response({"ok": True, "keys": self.apikeys.list()})

    async def api_keys_create(self, request):
        """{"label": "Home Assistant"}: a read-only key, shown once."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        try:
            entry, key = self.apikeys.create(body.get("label", ""), "read")
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        log.info("API key created: %s", entry["label"])
        return web.json_response({"ok": True, "key": key, "entry": entry, "keys": self.apikeys.list()})

    async def api_keys_revoke(self, request):
        if not self.apikeys.revoke(request.match_info["id"]):
            return web.json_response({"ok": False, "error": "no such key"}, status=404)
        log.info("API key revoked: %s", request.match_info["id"])
        return web.json_response({"ok": True, "keys": self.apikeys.list()})

    async def api_auth_status(self, request):
        enabled = self.settings["require_signin"]
        signed_in = bool(self.auth.session(request.cookies.get(COOKIE)))
        return web.json_response({"ok": True, "enabled": enabled, "signed_in": signed_in or not enabled,
                                  "must_change": enabled and signed_in and self.auth.must_change, "reset_active": self.auth.reset_active})

    async def api_auth_login(self, request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        ip = request.remote or "?"
        wait = self.auth.locked_for(ip)
        if wait:
            return web.json_response({"ok": False, "error": f"too many wrong passwords; try again in {max(1, round(wait / 60))} min"}, status=429)
        res = self.auth.login(str(body.get("password", "")), ip, bool(body.get("remember", True)))
        if not res:
            return web.json_response({"ok": False, "error": "that password is not right"}, status=401)
        token, ttl = res
        resp = web.json_response({"ok": True, "must_change": self.auth.must_change})
        resp.set_cookie(COOKIE, token, max_age=ttl if body.get("remember", True) else None, httponly=True,
                        samesite="Strict", secure=request.secure, path="/")
        return resp

    async def api_auth_logout(self, request):
        try:
            everywhere = bool((await request.json()).get("everywhere"))
        except Exception:  # noqa: BLE001
            everywhere = False
        token = request.cookies.get(COOKIE)
        n = self.auth.logout_others(token) if everywhere else 0
        if not everywhere:
            self.auth.logout(token)
        resp = web.json_response({"ok": True, "signed_out_others": n})
        if not everywhere:
            resp.del_cookie(COOKIE, path="/")
        return resp

    async def api_auth_password(self, request):
        try:
            body = await request.json()
            self.auth.change(body.get("current"), str(body.get("new", "")), request.cookies.get(COOKIE))
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "send the passwords as JSON"}, status=400)
        return web.json_response({"ok": True})

    async def on_startup(self, app):
        app["tasks"] = [asyncio.create_task(self.meter.run()), asyncio.create_task(self.housekeeping())]
        self.mqtt.restart()
        log.info("gateway=%s poll=%ss sign-in=%s data=%s", self.settings["gateway"] or "(not set)", self.settings["poll_interval"],
                 "on" if self.settings["require_signin"] else "off", DATA_DIR)

    async def on_cleanup(self, app):
        await self.mqtt.stop()
        for t in app["tasks"]:
            t.cancel()


def main() -> None:
    a = App()
    app = web.Application(middlewares=[a.require_login])
    app.add_routes([
        web.get("/", a.index),
        web.get("/api/state", a.api_state),
        web.get("/api/meter", a.api_meter),
        web.get("/api/keys", a.api_keys),
        web.post("/api/keys", a.api_keys_create),
        web.delete("/api/keys/{id}", a.api_keys_revoke),
        web.get("/api/auth/status", a.api_auth_status),
        web.post("/api/auth/login", a.api_auth_login),
        web.post("/api/auth/logout", a.api_auth_logout),
        web.post("/api/auth/password", a.api_auth_password),
        web.get("/api/series", a.api_series),
        web.get("/api/usage", a.api_usage),
        web.get("/api/summary", a.api_summary),
        web.get("/api/events", a.api_events),
        web.get("/api/runs", a.api_runs),
        web.get("/api/tapwater", a.api_tapwater),
        web.get("/api/export.csv", a.api_export),
        web.post("/api/refresh", a.api_refresh),
        web.get("/api/settings", a.api_settings),
        web.post("/api/settings", a.api_settings_save),
        web.get("/api/settings/retention", a.api_settings_retention),
        web.post("/api/settings/test", a.api_settings_test),
        web.static("/static", STATIC),
    ])
    app.on_startup.append(a.on_startup)
    app.on_cleanup.append(a.on_cleanup)
    web.run_app(app, port=PORT, print=None)


if __name__ == "__main__":
    main()
