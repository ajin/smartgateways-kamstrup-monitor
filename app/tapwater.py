# SPDX-License-Identifier: GPL-3.0-or-later
"""Hot tap water or space heating? An estimate from the pattern in the readings.

The meter measures all heat going into the home's heat unit (delivery set). That unit makes both the
space heating (floor heating, radiators) and the hot tap water, and the meter cannot tell them apart.
This module recognises the pattern of hot tap water, and everything it says is "probably".

What it learns, from readings that were not hot tap water (a moving average over ~40 minutes):
- the heating's usual flow (l/h): the heating's pump and valve give a steady flow, e.g. ~220 l/h;
- the heating's usual power (kW) and temperature of the water going back, while it runs.

Each reading with heat coming in is then checked for the signs of hot tap water:
- the water going back is very cold (28 °C or less): cold mains water is cooling the heat unit's tap
  water heat exchanger. The heating never sends water back that cold, not even when it starts up;
- or the flow is far below the heating's usual flow while the water going back is cold (33 °C or less):
  only the tap water side is taking heat;
- or the flow jumps far above the heating's usual flow (60 l/h and 25 % or more) with at least 2 kW more
  heat, and the water going back is colder than usual: a tap running on top of the heating.
When no heating has been seen yet (summer), heat with cold water going back (35 °C or less) counts.

Readings with these signs that are at most 3 minutes apart form one use (a shower turned off for a
moment stays one). Uses longer than 45 minutes are not counted (that is the heating).

Heat for hot tap water: all of it while the heating is not flowing (low flow), only the part above
the heating's usual power while the heating runs too. Each use gets a likelihood (0-1) from how cold
the water going back got, how far the flow moved from the heating's usual flow and how long it lasted:
"very likely", "likely" or "possibly".

Nothing here is measured as such: a second meter on the tap water would be needed for that.
"""

from __future__ import annotations

import math

TAU = 40 * 60            # seconds: how quickly the learned heating values follow changes
MIN_KW = 1.0             # below this, too little heat to say anything
VERY_COLD_C = 28.0       # water going back this cold: hot tap water
COLD_C = 33.0            # with a low flow: hot tap water
SUMMER_COLD_C = 35.0     # no heating seen yet: hot tap water
LOW_FLOW = 0.6           # "low flow": below this share of the heating's usual flow
JUMP_FLOW = (60.0, 0.25)  # "flow jump": at least this many l/h and this share above the usual flow ...
JUMP_KW = 2.0            # ... with at least this much more heat than the heating's usual power
MERGE_S = 180            # signs this close together are one use
MAX_MIN = 45             # longer is not tap water
MIN_KWH = 0.05           # smaller is noise
KWH_PER_KWS = 1 / 3600


def label(score: float) -> str:
    return "very likely" if score >= 0.75 else "likely" if score >= 0.5 else "possibly"


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def detect(rows, max_gap: int = 300, end: float | None = None) -> dict:
    """rows: (ts, power kW, flow l/h, temp in °C, temp back °C), sorted by time.

    Returns {"events": [...], "now": event or None, "baseline": {...}}. Each event: start, end, minutes,
    kwh, p_max, flow_max, t_back_min, normal_kw, normal_back, normal_flow, score, label, ongoing.
    """
    events: list[dict] = []
    fh = bp = br = None       # the heating's usual flow (l/h), power (kW) and water going back (°C)
    cur = None                # the use being followed
    prev = None               # (ts, tap kW) of the previous reading, for adding up the heat

    def tap_kw(p, f, t2):
        """kW of hot tap water in this reading, or None when it shows no sign of hot tap water."""
        if p is None or f is None or t2 is None or p < MIN_KW or f <= 0:
            return None
        if fh is None:
            return p if t2 <= SUMMER_COLD_C else None
        low = f < LOW_FLOW * fh
        if t2 <= VERY_COLD_C or (low and t2 <= COLD_C):
            return p if low else max(0.0, p - (bp or 0))
        if f >= fh + max(JUMP_FLOW[0], JUMP_FLOW[1] * fh) and p >= (bp or 0) + JUMP_KW and (br is None or t2 <= br - 2):
            return p - (bp or 0)
        return None

    def close(live=False):
        nonlocal cur
        ev, cur = cur, None
        mins = (ev["end"] - ev["start"]) / 60
        ev["minutes"] = round(mins, 1)
        if mins > MAX_MIN or (ev["kwh"] < MIN_KWH and not live):
            return
        cold = _clamp((40 - ev["t_back_min"]) / 15)
        dev = _clamp(ev.pop("_dev") / 0.6) if ev["normal_flow"] else 0.5
        fit = 1.0 if 1 <= mins <= 25 else 0.6
        ev["score"] = round(0.45 * cold + 0.35 * dev + 0.2 * fit, 2)
        ev["label"] = label(ev["score"])
        ev["kwh"] = round(ev["kwh"], 3)
        ev.pop("_dev", None)
        events.append(ev)

    for ts, p, f, t1, t2 in rows:
        if p is None or t2 is None:
            continue
        gap = prev is not None and ts - prev[0] > max_gap
        if prev is not None and not gap and cur is not None and prev[1]:
            cur["kwh"] += prev[1] * (ts - prev[0]) * KWH_PER_KWS  # a value holds until the next reading
        k = tap_kw(p, f, t2)
        if cur is not None and (gap or (k is not None and ts - cur["end"] > MERGE_S) or (k is None and ts - cur["end"] > MERGE_S)):
            cur["end"] = min(cur["end"] + 60, ts)
            close()
        if k is not None:
            if cur is None:
                cur = {"start": ts, "end": ts, "kwh": 0.0, "p_max": p, "flow_max": f, "t_back_min": t2,
                       "normal_kw": round(bp, 2) if bp is not None else None, "normal_back": round(br, 1) if br is not None else None,
                       "normal_flow": round(fh) if fh is not None else None, "_dev": 0.0, "ongoing": False}
            cur["end"] = ts
            cur["p_max"] = max(cur["p_max"], p)
            cur["flow_max"] = max(cur["flow_max"], f)
            cur["t_back_min"] = min(cur["t_back_min"], t2)
            if fh:
                cur["_dev"] = max(cur["_dev"], abs(f - fh) / fh)
        elif f and f > 0 and p >= 0.5 and cur is None:  # the heating running: learn its usual values
            dt = 0 if prev is None or gap else ts - prev[0]
            a = 1.0 if fh is None else 0.5 if gap else 1 - math.exp(-dt / TAU)
            fh = f if fh is None else fh + a * (f - fh)
            bp = p if bp is None else bp + a * (p - bp)
            br = t2 if br is None else br + a * (t2 - br)
        prev = (ts, k or 0.0)

    now_ev = None
    if cur is not None:
        live = end is not None and end - cur["end"] <= MERGE_S and end - prev[0] <= max_gap
        if live:
            cur["kwh"] += prev[1] * (end - prev[0]) * KWH_PER_KWS
            cur["end"] = end
        else:
            cur["end"] = cur["end"] + 60
        started = cur["start"]
        close(live)
        if live and events and events[-1]["start"] == started:
            events[-1]["ongoing"] = True
            now_ev = events[-1]
    return {"events": events, "now": now_ev,
            "baseline": {"kw": round(bp, 2) if bp is not None else None, "back": round(br, 1) if br is not None else None,
                         "flow": round(fh) if fh is not None else None}}
