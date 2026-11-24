# SPDX-License-Identifier: GPL-3.0-or-later
"""MQTT: publish the meter to a broker, with Home Assistant discovery. Read-only: it takes no commands.

Everything is retained, so a client that connects later sees the current values straight away.

  <base>/status   online / offline (last will)
  <base>/state    the same flat JSON as GET /api/meter: {"heat_on", "power_kw", "temp_in_c", "temp_back_c",
                  "temp_diff_c", "flow_lh", "heat_energy_gj", "heat_energy_kwh", "today_gj", "today_kwh", ...}

Home Assistant finds one device, "Kamstrup heat meter", with a sensor for every value (heat energy
works in the Energy dashboard), a "Heat coming in" binary sensor, a leak alarm and the gateway connection,
plus estimates of hot tap water and space heating (recognised from the readings, see tapwater.py).
Homey (MQTT Hub / MQTT Client apps) and others can subscribe to <base>/state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time

log = logging.getLogger(__name__)

# (key in the state JSON, name, unit, device_class, state_class, icon, diagnostic)
SENSORS = [
    ("power_kw", "Heat coming in", "kW", "power", "measurement", None, False),
    ("heat_energy_gj", "Heat used", "GJ", "energy", "total_increasing", None, False),
    ("heat_energy_kwh", "Heat used (kWh)", "kWh", "energy", "total_increasing", None, False),
    ("today_kwh", "Heat used today", "kWh", "energy", "total_increasing", None, False),
    # estimates: recognised from the pattern in the readings, not measured
    ("tap_water_today_kwh", "Hot tap water today (estimate)", "kWh", "energy", "total", "mdi:shower-head", False),
    ("heating_today_kwh", "Space heating today (estimate)", "kWh", "energy", "total", "mdi:heating-coil", False),
    ("temp_in_c", "Hot water in", "°C", "temperature", "measurement", None, False),
    ("temp_back_c", "Water going back", "°C", "temperature", "measurement", None, False),
    ("temp_diff_c", "Heat taken out", "°C", None, "measurement", "mdi:thermometer-minus", False),
    ("flow_lh", "Hot water flow", "L/h", None, "measurement", "mdi:water-pump", False),
    ("volume_m3", "Hot water through the meter", "m³", "volume", "total_increasing", None, False),
    ("cooling_energy_gj", "Cooling", "GJ", "energy", "total_increasing", None, True),
    ("wifi_rssi", "Gateway WiFi signal", "dBm", "signal_strength", "measurement", None, True),
    ("hourcounter", "Meter running time", "h", "duration", "total_increasing", None, True),
    ("infoevent", "Meter info events", None, None, "total_increasing", "mdi:information-outline", True),
]


def discovery_messages(view: dict, base: str, prefix: str) -> list[tuple[str, str]]:
    mac = str(view.get("gateway_mac") or "").replace(":", "").lower()
    if not mac:
        return []
    uid = f"km_{mac}"
    device = {"identifiers": [uid], "name": "Kamstrup heat meter", "manufacturer": "Kamstrup", "model": "MULTICAL (via Kamst-IR gateway)",
              "sw_version": view.get("gateway_firmware")}
    common = {"availability_topic": f"{base}/status", "state_topic": f"{base}/state", "device": device}
    out = []
    for key, name, unit, dc, sc, ic, diag in SENSORS:
        body = {**common, "name": name, "unique_id": f"{uid}_{key}", "value_template": f"{{{{ value_json.{key} }}}}"}
        if unit:
            body["unit_of_measurement"] = unit
        if dc:
            body["device_class"] = dc
        if sc:
            body["state_class"] = sc
        if ic:
            body["icon"] = ic
        if diag:
            body["entity_category"] = "diagnostic"
        out.append((f"{prefix}/sensor/{uid}/{key}/config", json.dumps(body)))
    for key, name, dc, tmpl, diag in [
        ("heat_on", "Heat coming in", "heat", "{{ 'ON' if value_json.heat_on else 'OFF' }}", False),
        ("tap_water_now", "Hot tap water (probably)", "running", "{{ 'ON' if value_json.tap_water_now else 'OFF' }}", False),
        ("leak", "Leak or burst", "problem", "{{ 'ON' if value_json.leak_burst else 'OFF' }}", False),
        ("online", "Gateway connected", "connectivity", "{{ 'ON' if value_json.online else 'OFF' }}", True),
    ]:
        body = {**common, "name": name, "unique_id": f"{uid}_{key}", "device_class": dc, "value_template": tmpl}
        if diag:
            body["entity_category"] = "diagnostic"
        out.append((f"{prefix}/binary_sensor/{uid}/{key}/config", json.dumps(body)))
    return out


class Mqtt:
    def __init__(self, app):
        self.app = app
        self.task: asyncio.Task | None = None
        self.status = {"enabled": False, "connected": False, "error": None, "since": None, "last_publish": None}

    def view(self) -> dict:
        return dict(self.status)

    def restart(self) -> None:
        old = self.task
        self.task = asyncio.create_task(self._restart(old))

    async def _restart(self, old: asyncio.Task | None) -> None:
        if old and not old.done():
            old.cancel()
            try:
                await old  # let it publish "offline" and disconnect before connecting again
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.run()

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def run(self) -> None:
        import aiomqtt

        s = self.app.settings
        self.status.update(enabled=s["mqtt_enabled"], connected=False, error=None)
        if not s["mqtt_enabled"]:
            return
        if not s["mqtt_host"]:
            self.status["error"] = "no broker address"
            return
        base, delay = s["mqtt_base"], 5
        while True:
            try:
                tls = ssl.create_default_context() if s["mqtt_tls"] else None
                async with aiomqtt.Client(
                    hostname=s["mqtt_host"], port=s["mqtt_port"], username=s["mqtt_user"] or None,
                    password=s["mqtt_password"] or None, identifier=f"kamstrup-monitor-{base.replace('/', '-')}",
                    tls_context=tls, will=aiomqtt.Will(f"{base}/status", "offline", qos=1, retain=True),
                ) as client:
                    self.status.update(connected=True, error=None, since=time.time())
                    log.info("MQTT connected to %s:%s", s["mqtt_host"], s["mqtt_port"])
                    delay = 5
                    await client.publish(f"{base}/status", "online", qos=1, retain=True)
                    try:
                        await self._publish_loop(client, base)
                    except asyncio.CancelledError:
                        try:  # a clean disconnect does not trigger the last will
                            await asyncio.wait_for(client.publish(f"{base}/status", "offline", qos=1, retain=True), 2)
                        except Exception:  # noqa: BLE001
                            pass
                        raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.status.update(connected=False, error=str(exc) or exc.__class__.__name__)
                log.warning("MQTT: %s (retry in %ss)", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 120)

    async def _publish_loop(self, client, base: str) -> None:
        s = self.app.settings
        sent_disc: dict[str, str] = {}
        last_state, last_full = None, 0.0
        while True:
            view = self.app.meter_view()
            now = time.time()
            for topic, payload in discovery_messages(view, base, s["mqtt_discovery"]):
                if sent_disc.get(topic) != payload:
                    await client.publish(topic, payload, qos=1, retain=True)
                    sent_disc[topic] = payload
            payload = json.dumps(view)
            key = (view.get("ts"), view.get("online"))
            if key != last_state or now - last_full > 300:
                await client.publish(f"{base}/state", payload, qos=0, retain=True)
                last_state, last_full = key, now
                self.status["last_publish"] = now
            await asyncio.sleep(3)
