# SPDX-License-Identifier: GPL-3.0-or-later
"""App settings that can be changed on the Settings page.

Saved in data/settings.json. Environment variables (.env) only give the starting values: once a
setting is saved in the app, the saved value wins.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

log = logging.getLogger(__name__)

DEFAULT_PATH = "/kamst-ir/api/read"


def check_gateway(v: Any) -> str:
    """Accept an IP address, host:port or a full URL; return the full URL of the gateway's read API."""
    s = str(v or "").strip()
    if not s:
        return ""
    if not re.match(r"^https?://", s, re.I):
        s = "http://" + s
    u = urlparse(s)
    if not u.hostname or not re.fullmatch(r"[A-Za-z0-9.\-:]+", u.hostname) or "@" in u.netloc:
        raise ValueError("use the gateway's IP address, e.g. 192.168.1.50 or 192.168.1.50:82")
    try:
        u.port  # noqa: B018  raises on a bad port
    except ValueError:
        raise ValueError("the port must be a number between 1 and 65535") from None
    path = u.path if u.path not in ("", "/") else DEFAULT_PATH
    return urlunparse((u.scheme.lower(), u.netloc, path, "", u.query, ""))


def _int_range(lo: int, hi: int) -> Callable[[Any], int]:
    def check(v: Any) -> int:
        n = int(float(v))
        if not lo <= n <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return n
    return check


def _price(v: Any) -> float:
    s = str(v if v is not None else "").strip().replace(",", ".") or "0"
    n = float(s)
    if not 0 <= n <= 10000:
        raise ValueError("must be between 0 and 10000")
    return round(n, 4)


def _unit(v: Any) -> str:
    s = str(v).strip()
    if s.lower() == "gj":
        return "GJ"
    if s.lower() == "kwh":
        return "kWh"
    raise ValueError("must be GJ or kWh")


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    raise ValueError("must be on or off")


def _host(v: Any) -> str:
    s = str(v or "").strip()
    if s and not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", s):
        raise ValueError("a host name or IP address, e.g. 192.168.1.10 or homeassistant.local")
    return s


def _text(limit: int) -> Callable[[Any], str]:
    def check(v: Any) -> str:
        s = str(v or "")
        if len(s) > limit:
            raise ValueError(f"at most {limit} characters")
        return s
    return check


def _topic(v: Any) -> str:
    s = str(v or "").strip().strip("/")
    if not s or not re.fullmatch(r"[A-Za-z0-9_\-/]+", s):
        raise ValueError("letters, digits, _ - and / only, without + or #")
    return s


def _currency(v: Any) -> str:
    s = str(v or "").strip()
    if not 1 <= len(s) <= 4:
        raise ValueError("one to four characters, e.g. €")
    return s


# key: (environment variable for the starting value or None, default, check)
FIELDS: dict[str, tuple[str | None, Any, Callable[[Any], Any]]] = {
    "gateway": ("GATEWAY", "", check_gateway),
    "poll_interval": ("POLL_INTERVAL", 60, _int_range(10, 3600)),
    "unit": ("UNIT", "GJ", _unit),
    "price_per_gj": ("PRICE_PER_GJ", 0, _price),   # 0 = don't show costs
    "currency": (None, "€", _currency),
    "reading_days": ("READING_DAYS", 0, _int_range(0, 36500)),  # detailed readings; 0 = keep forever
    "require_signin": ("REQUIRE_SIGNIN", True, _bool),  # off: anyone on the network can open the dashboard
    # MQTT, for Home Assistant (with discovery), Homey and anything else that speaks MQTT
    "mqtt_enabled": (None, False, _bool),
    "mqtt_host": (None, "", _host),
    "mqtt_port": (None, 1883, _int_range(1, 65535)),
    "mqtt_tls": (None, False, _bool),
    "mqtt_user": (None, "", _text(200)),
    "mqtt_password": (None, "", _text(200)),
    "mqtt_base": (None, "kamstrup_monitor", _topic),
    "mqtt_discovery": (None, "homeassistant", _topic),
}


class Settings:
    def __init__(self, data_dir: str):
        self.path = os.path.join(data_dir, "settings.json")
        self.values: dict[str, Any] = {}
        self.source: dict[str, str] = {}
        saved = self._read()
        for key, (env, default, check) in FIELDS.items():
            if key in saved:
                raw, src = saved[key], "app"
            elif env and os.environ.get(env, "").strip():
                raw, src = os.environ[env], "env"
            else:
                raw, src = default, "default"
            try:
                self.values[key] = check(raw)
            except (TypeError, ValueError) as exc:
                log.warning("setting %s=%r ignored: %s", key, raw, exc)
                self.values[key], src = check(default), "default"
            self.source[key] = src
        self.listeners: list[Callable[[dict], None]] = []

    def _read(self) -> dict:
        try:
            with open(self.path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def update(self, changes: dict) -> dict:
        """Validate and save; returns {key: (old, new)} for what actually changed."""
        clean = {}
        for key, value in changes.items():
            if key not in FIELDS:
                raise ValueError(f"unknown setting {key!r}")
            try:
                clean[key] = FIELDS[key][2](value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}: {exc}") from None
        changed = {k: (self.values[k], v) for k, v in clean.items() if self.values[k] != v}
        for key, value in clean.items():
            self.values[key] = value
            self.source[key] = "app"
        saved = self._read()
        saved.update(clean)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(saved, f, indent=1)
        os.chmod(tmp, 0o600)  # may hold the MQTT password
        os.replace(tmp, self.path)
        if changed:
            log.info("settings changed: %s", {k: v[1] for k, v in changed.items()})
            for fn in self.listeners:
                fn(changed)
        return changed

    SECRET = ("mqtt_password",)

    def view(self) -> dict:
        """What the Settings page may see: secrets only as "set" / "not set"."""
        values = {k: ("" if k in self.SECRET else v) for k, v in self.values.items()}
        values.update({f"{k}_set": bool(self.values[k]) for k in self.SECRET})
        return {"values": values, "source": dict(self.source)}
