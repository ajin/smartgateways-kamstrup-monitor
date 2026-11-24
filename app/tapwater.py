# SPDX-License-Identifier: GPL-3.0-or-later
"""Hot tap water or space heating? An estimate from the pattern in the readings.

The meter measures all heat going into the home's heat unit (delivery set). That unit makes both the
space heating (floor heating, radiators) and the hot tap water, and the meter cannot tell them apart.
This module recognises the pattern of hot tap water, and everything it says is "probably":

- It learns the home's normal heating level: a slowly moving average of the heat coming in (kW) and
  the temperature of the water going back, over the last ~40 minutes of readings that were not hot
  tap water. Floor heating is slow and steady, so this follows it.
- Hot tap water is recognised as a sudden jump in heat (at least 2 kW above that level) while the
  water going back gets clearly colder (at least 4 °C below normal): the cold mains water cools the
  heat unit's tap water heat exchanger. When no heating was running, a jump with a large difference
  between the water in and back counts too.
- A stretch ends when the heat drops back to the normal level or the water going back is warm again.
  Stretches longer than 45 minutes are not tap water (heating starting up, most likely) and are dropped.
- The heat for hot tap water is only the part above the normal heating level; the rest stays heating.
- Each stretch gets a likelihood (0-1) from how big the jump and the temperature drop were and how
  long it lasted, shown as "very likely", "likely" or "possibly".

Nothing here is measured as such: a second meter on the tap water would be needed for that.
"""

from __future__ import annotations

import math

TAU = 40 * 60            # seconds: how quickly the normal level follows changes
JUMP_KW = 2.0            # start: at least this much more heat than normal ...
DROP_C = 4.0             # ... and the water going back at least this much colder than normal
COLD_C = 10.0            # or: the water going back very much colder, with a smaller jump
MERGE_S = 180            # a new start this soon after the previous one is the same use (a shower paused)
IDLE_KW = 0.5            # below this, no heating is running
IDLE_DIFF_C = 30.0       # when idle: water in minus back at least this much
KEEP_KW = 1.0            # continue while still this much above normal ...
WARM_C = 3.0             # ... and the water going back is not this much warmer than normal
MAX_MIN = 45             # longer is not tap water
MIN_KWH = 0.05           # smaller is noise
KWH_PER_KWS = 1 / 3600


def label(score: float) -> str:
    return "very likely" if score >= 0.75 else "likely" if score >= 0.5 else "possibly"


def detect(rows, max_gap: int = 300, end: float | None = None) -> dict:
    """rows: (ts, power kW, flow l/h, temp in °C, temp back °C), sorted by time.

    Returns {"events": [...], "now": event or None, "baseline": {...}}. Each event: start, end, minutes,
    kwh (heat above the normal heating level), p_max, t_back_min, normal_kw, normal_back, score, label,
    ongoing.
    """
    events: list[dict] = []
    bp = br = None            # normal heating level: kW, water going back °C
    cur = None
    prev = None               # (ts, power) of the previous reading, for integrating

    def close(t_end, live=False):
        nonlocal cur
        ev, cur = cur, None
        ev["end"] = t_end
        mins = (t_end - ev["start"]) / 60
        ev["minutes"] = round(mins, 1)
        if mins > MAX_MIN or (ev["kwh"] < MIN_KWH and not live):
            return
        ev["_kwh"], ev["_j"], ev["_d"] = ev["kwh"], ev["_jump"], ev["_drop"]  # to carry on if it starts again soon
        jump = min(1.0, ev.pop("_jump") / 6.0)
        drop = min(1.0, ev.pop("_drop") / 12.0)
        fit = 1.0 if 1 <= mins <= 25 else 0.6
        ev["score"] = round(0.4 * jump + 0.4 * drop + 0.2 * fit, 2)
        ev["label"] = label(ev["score"])
        ev["kwh"] = round(ev["kwh"], 3)
        events.append(ev)

    for ts, p, f, t1, t2 in rows:
        if p is None or t2 is None:
            continue
        t1 = t1 if t1 is not None else t2
        gap = prev is not None and ts - prev[0] > max_gap
        if gap and cur is not None:
            close(prev[0] + 60)
        if cur is not None and not gap:  # heat above normal since the previous reading (step: a value holds until the next)
            cur["kwh"] += max(0.0, prev[1] - cur["normal_kw"]) * (ts - prev[0]) * KWH_PER_KWS
        if bp is None:
            bp, br = p, t2
        idle = bp < IDLE_KW
        if cur is None:
            jump, drop = p - bp, br - t2
            if idle:
                start = p >= JUMP_KW + IDLE_KW and t1 - t2 >= IDLE_DIFF_C
                drop = max(drop, (t1 - t2) - IDLE_DIFF_C + DROP_C)
            else:
                start = (jump >= max(JUMP_KW, 0.5 * bp) and drop >= DROP_C) or (drop >= COLD_C and jump >= KEEP_KW)
            if start and events and ts - events[-1]["end"] <= MERGE_S and not gap:
                cur = events.pop()  # carry on with the previous one
                cur["kwh"] = cur.pop("_kwh"); cur["_jump"] = cur.pop("_j"); cur["_drop"] = cur.pop("_d")
                cur["_jump"] = max(cur["_jump"], jump); cur["_drop"] = max(cur["_drop"], drop)
                for k in ("score", "label", "end", "minutes"):
                    cur.pop(k, None)
            elif start:
                cur = {"start": ts, "kwh": 0.0, "p_max": p, "t_back_min": t2, "normal_kw": round(bp, 2),
                       "normal_back": round(br, 1), "_jump": jump, "_drop": drop, "ongoing": False}
        else:
            if idle:
                go_on = p >= KEEP_KW and t1 - t2 >= IDLE_DIFF_C - 10
            else:
                go_on = ((p - cur["normal_kw"] >= KEEP_KW and t2 <= cur["normal_back"] + WARM_C)
                         or t2 <= cur["normal_back"] - DROP_C)  # the meter's kW jumps about; cold water going back is steadier
            if go_on:
                cur["p_max"] = max(cur["p_max"], p)
                cur["t_back_min"] = min(cur["t_back_min"], t2)
                cur["_jump"] = max(cur["_jump"], p - cur["normal_kw"])
                cur["_drop"] = max(cur["_drop"], cur["normal_back"] - t2)
            else:
                close(ts)
        if cur is None:  # learn the normal level only from readings that are not tap water
            dt = 0 if prev is None or gap else ts - prev[0]
            a = 1 - math.exp(-dt / TAU) if dt else (1.0 if prev is None or gap else 0.0)
            if gap:
                a = 0.5  # after a gap, move halfway: the heating may have changed meanwhile
            bp += a * (p - bp)
            br += a * (t2 - br)
        prev = (ts, p)

    now_ev = None
    if cur is not None:
        last = prev[0]
        live = end is not None and end - last <= max_gap
        if live:
            cur["kwh"] += max(0.0, prev[1] - cur["normal_kw"]) * (end - last) * KWH_PER_KWS
        started = cur["start"]
        close(end if live else last + 60, live)
        if live and events and events[-1]["start"] == started:
            events[-1]["ongoing"] = True
            now_ev = events[-1]
    for ev in events:
        for k in ("_kwh", "_j", "_d"):
            ev.pop(k, None)
    return {"events": events, "now": now_ev,
            "baseline": {"kw": round(bp, 2) if bp is not None else None, "back": round(br, 1) if br is not None else None}}
