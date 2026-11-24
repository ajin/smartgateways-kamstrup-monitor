# SPDX-License-Identifier: GPL-3.0-or-later
"""SQLite store for every meter reading, an hourly roll-up for fast charts, and an event log.

readings  every reading the gateway gave (deduplicated when the gateway had nothing new). The values
          that change are columns; the rest of the gateway's answer (monthly records, firmware,
          WiFi, ...) is kept as JSON in `raw`, written again only when one of those values changes.
hours     one row per clock hour: the meter counters at the end of the hour and sums for averages.
          Kept forever, even when old detailed readings are thinned out (Settings › Keep readings).
events    things worth knowing later: gateway offline/back, restarts, firmware, alarms, meter events.

Heat used in a period is the difference of the meter's own energy counter, so it is exact as long
as there are readings on both sides. When the app was off for a while, the use in between is
spread evenly over the missing hours and marked as estimated.
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import time

from .tapwater import detect as detect_tap_water

COLS = ["heat_energy", "cooling_energy", "power", "temp1", "temp2", "tempdiff", "flow", "volume",
        "hourcounter", "infoevent", "wifi_rssi", "leak_burst", "temp1xm3", "temp2xm3"]
# fields that change with every reading; the rest of the answer is stored again only when it changes
VOLATILE = set(COLS) | {"device_time", "device_date"}

HOUR_SUMS = ["p_sum", "p_n", "f_sum", "f_n", "t1_sum", "t2_sum", "dt_sum", "t_n", "t1f", "t2f", "dtf"]


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ------------------------------------------------------------------ local calendar buckets
def bucket_start(ts: float, group: str) -> int:
    if group == "hour":
        return int(ts // 3600 * 3600)
    lt = time.localtime(ts)
    if group == "day":
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
    if group == "week":
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - lt.tm_wday, 0, 0, 0, 0, 0, -1)))
    if group == "month":
        return int(time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1)))
    if group == "year":
        return int(time.mktime((lt.tm_year, 1, 1, 0, 0, 0, 0, 0, -1)))
    raise ValueError(group)


def bucket_next(bs: int, group: str) -> int:
    if group == "hour":
        return bs + 3600
    lt = time.localtime(bs + 7200)  # safely inside the bucket's first day, whatever DST does
    if group == "day":
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1)))
    if group == "week":
        return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 7, 0, 0, 0, 0, 0, -1)))
    if group == "month":
        return int(time.mktime((lt.tm_year + lt.tm_mon // 12, lt.tm_mon % 12 + 1, 1, 0, 0, 0, 0, 0, -1)))
    if group == "year":
        return int(time.mktime((lt.tm_year + 1, 1, 1, 0, 0, 0, 0, 0, -1)))
    raise ValueError(group)


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"CREATE TABLE IF NOT EXISTS readings (ts INTEGER PRIMARY KEY, {', '.join(c + ' REAL' for c in COLS)}, meter_clock TEXT, raw TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS hours (h INTEGER PRIMARY KEY, n INTEGER, e_first REAL, e_last REAL, v_first REAL, v_last REAL, "
                        "c_first REAL, c_last REAL, p_max REAL, " + ", ".join(f"{c} REAL" for c in HOUR_SUMS) + ")")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (ts INTEGER, kind TEXT, text TEXT)")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_events ON events (ts)")
        self.db.commit()
        self.retention_days = 0
        self.last_ts: float | None = None
        self.last: dict = {}           # the latest reading, every field
        self._static: dict = {}        # the non-volatile part of the latest stored raw answer
        self._cache: tuple | None = None
        self._tap_cache: dict = {}     # finished days: hot tap water estimate
        self._load_last()
        if not self.db.execute("SELECT 1 FROM hours LIMIT 1").fetchone() and self.last_ts:
            self.rebuild_hours()

    # ------------------------------------------------------------------ writing
    def _load_last(self) -> None:
        row = self.db.execute(f"SELECT ts, {', '.join(COLS)} FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
        if not row:
            return
        raw = self.db.execute("SELECT raw FROM readings WHERE raw IS NOT NULL ORDER BY ts DESC LIMIT 1").fetchone()
        merged = json.loads(raw[0]) if raw else {}
        self._static = {k: v for k, v in merged.items() if k not in VOLATILE}
        merged.update({c: v for c, v in zip(COLS, row[1:]) if v is not None})
        self.last_ts, self.last = row[0], merged

    def add(self, ts: float, p: dict) -> bool:
        """Store one parsed reading. Returns False when it was the same as the previous one."""
        ts = int(ts)
        vals = [_num(p.get(c)) for c in COLS]
        clock = f"{p.get('device_date') or ''}{p.get('device_time') or ''}" or None
        if self.last_ts is not None:
            if ts <= self.last_ts:
                return False
            same = vals == [_num(self.last.get(c)) for c in COLS] and p.get("device_time") == self.last.get("device_time")
            if same and ts - self.last_ts < 600:
                self.last.update(p)
                return False
        static = {k: v for k, v in p.items() if k not in VOLATILE}
        raw = json.dumps(p, separators=(",", ":")) if static != self._static else None
        prev, prev_static = self.last, self._static
        self.db.execute(f"INSERT OR REPLACE INTO readings VALUES ({', '.join('?' * (len(COLS) + 3))})", [ts, *vals, clock, raw])
        self._roll_up(ts, dict(zip(COLS, vals)))
        if raw:
            self._static = static
        self._events(ts, prev, prev_static, p)
        self.db.commit()
        self.last_ts, self.last = ts, dict(p)
        self._cache = None
        return True

    def _roll_up(self, ts: int, v: dict) -> None:
        e, vol, cool, pw, fl = v["heat_energy"], v["volume"], v["cooling_energy"], v["power"], v["flow"]
        t1, t2, dt = v["temp1"], v["temp2"], v["tempdiff"]
        has_t = t1 is not None and t2 is not None
        if dt is None and has_t:
            dt = t1 - t2
        f = fl or 0.0
        row = {
            "p_sum": pw or 0.0, "p_n": 1 if pw is not None else 0,
            "f_sum": f, "f_n": 1 if fl is not None else 0,
            "t1_sum": t1 if has_t else 0.0, "t2_sum": t2 if has_t else 0.0, "dt_sum": dt if has_t else 0.0, "t_n": 1 if has_t else 0,
            "t1f": t1 * f if has_t else 0.0, "t2f": t2 * f if has_t else 0.0, "dtf": dt * f if has_t else 0.0,
        }
        self.db.execute(
            "INSERT INTO hours (h, n, e_first, e_last, v_first, v_last, c_first, c_last, p_max, " + ", ".join(HOUR_SUMS) + ") "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, " + ", ".join("?" * len(HOUR_SUMS)) + ") "
            "ON CONFLICT(h) DO UPDATE SET n = n + 1, "
            "e_first = COALESCE(e_first, excluded.e_first), e_last = COALESCE(excluded.e_last, e_last), "
            "v_first = COALESCE(v_first, excluded.v_first), v_last = COALESCE(excluded.v_last, v_last), "
            "c_first = COALESCE(c_first, excluded.c_first), c_last = COALESCE(excluded.c_last, c_last), "
            "p_max = MAX(COALESCE(p_max, excluded.p_max), COALESCE(excluded.p_max, p_max)), "
            + ", ".join(f"{c} = {c} + excluded.{c}" for c in HOUR_SUMS),
            [ts // 3600, e, e, vol, vol, cool, cool, pw, *[row[c] for c in HOUR_SUMS]],
        )

    def rebuild_hours(self) -> None:
        self.db.execute("DELETE FROM hours")
        for row in self.db.execute(f"SELECT ts, {', '.join(COLS)} FROM readings ORDER BY ts").fetchall():
            self._roll_up(row[0], dict(zip(COLS, row[1:])))
        self.db.commit()

    def event(self, kind: str, text: str, ts: float | None = None) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (int(ts or time.time()), kind, text))
        self.db.commit()

    def _events(self, ts: int, prev: dict, prev_static: dict, p: dict) -> None:
        add = lambda kind, text: self.db.execute("INSERT INTO events VALUES (?,?,?)", (ts, kind, text))  # noqa: E731
        if not prev:
            add("start", f"First reading stored. The meter's counter stood at {p.get('heat_energy')} GJ.")
            return
        if prev_static.get("startup_time") and p.get("startup_time") and p["startup_time"] != prev_static["startup_time"]:
            add("gateway", "The gateway restarted (for example after a power cut or a firmware update).")
        if prev_static.get("firmware_running") and p.get("firmware_running") != prev_static.get("firmware_running"):
            add("gateway", f"Gateway firmware changed from {prev_static['firmware_running']} to {p.get('firmware_running')}.")
        if p.get("firmware_update_available") is True and prev_static.get("firmware_update_available") is False:
            add("gateway", f"A firmware update for the gateway is available ({p.get('firmware_available')}).")
        lb, plb = _num(p.get("leak_burst")), _num(prev.get("leak_burst"))
        if lb != plb and lb is not None:
            add("alarm" if lb else "ok", f"The meter reports a leak or burst (code {int(lb)})." if lb else "The leak/burst alarm has cleared.")
        ie, pie = _num(p.get("infoevent")), _num(prev.get("infoevent"))
        if ie is not None and pie is not None and ie > pie:
            n = int(ie - pie)
            add("meter", f"The meter logged {n} new info event{'s' if n > 1 else ''} (now {int(ie)} in total).")
        e, pe = _num(p.get("heat_energy")), _num(prev.get("heat_energy"))
        if e is not None and pe is not None and e < pe - 0.0005:
            add("meter", f"The energy counter went back from {pe} to {e} GJ. Was the meter replaced or reset?")

    def prune(self) -> int:
        if self.retention_days <= 0:
            return 0
        cur = self.db.execute("DELETE FROM readings WHERE ts < ? AND raw IS NULL", (int(time.time()) - self.retention_days * 86400,))
        self.db.commit()
        return cur.rowcount

    def count_older(self, days: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM readings WHERE ts < ? AND raw IS NULL", (int(time.time()) - days * 86400,)).fetchone()[0]

    # ------------------------------------------------------------------ reading
    def stats(self) -> dict:
        n, first = self.db.execute("SELECT COUNT(*), MIN(ts) FROM readings").fetchone()
        first_h = self.db.execute("SELECT MIN(h) FROM hours").fetchone()[0]
        size = sum(os.path.getsize(self.path + ext) for ext in ("", "-wal") if os.path.exists(self.path + ext))
        return {"readings": n, "first_ts": first_h * 3600 if first_h is not None else first, "last_ts": self.last_ts, "db_bytes": size}

    def events(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT ts, kind, text FROM events ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": t, "kind": k, "text": x} for t, k, x in rows]

    def _hour_usage(self) -> dict[int, list]:
        """{hour: [heat GJ, water m³, cooling GJ, estimated]} from the meter's own counters."""
        key = (self.last_ts,)
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        rows = self.db.execute("SELECT h, e_first, e_last, v_first, v_last, c_first, c_last FROM hours WHERE e_last IS NOT NULL ORDER BY h").fetchall()
        out: dict[int, list] = {}
        prev = None
        for h, ef, el, vf, vl, cf, cl in rows:
            if prev is None:  # the very first hour: only what was used after the first reading
                out[h] = [max(0.0, el - ef), max(0.0, (vl or 0) - (vf or 0)), max(0.0, (cl or 0) - (cf or 0)), False]
            else:
                ph, pe, pv, pc = prev
                d = [max(0.0, el - pe), max(0.0, (vl or 0) - (pv or 0)) if pv is not None else 0.0, max(0.0, (cl or 0) - (pc or 0)) if pc is not None else 0.0]
                k = h - ph
                if k <= 1:
                    out[h] = [*d, False]
                else:  # the app was off: spread what was used over the hours it missed
                    for j in range(ph + 1, h + 1):
                        out[j] = [d[0] / k, d[1] / k, d[2] / k, True]
            prev = (h, el, vl, cl)
        self._cache = (key, out)
        return out

    def energy_between(self, a: float, b: float) -> dict:
        hu = self._hour_usage()
        e = v = 0.0
        hours = est = 0
        for h in range(int(a // 3600), int((b - 1) // 3600) + 1):
            u = hu.get(h)
            if u:
                e += u[0]; v += u[1]; hours += 1; est += u[3]
        total = max(1, int((b - 1) // 3600) - int(a // 3600) + 1)
        return {"energy": round(e, 4), "volume": round(v, 4), "hours": hours, "coverage": round(hours / total, 3), "estimated": est}

    def usage(self, start: float, end: float, group: str) -> list[dict]:
        hu = self._hour_usage()
        h0, h1 = int(start // 3600), int(end // 3600)
        stats = {r[0]: r[1:] for r in self.db.execute(
            "SELECT h, p_max, " + ", ".join(HOUR_SUMS) + " FROM hours WHERE h BETWEEN ? AND ?", (h0, h1))}
        out = []
        bs = bucket_start(start, group)
        while bs < end:
            be = bucket_next(bs, group)
            agg = dict.fromkeys(HOUR_SUMS, 0.0)
            e = v = c = 0.0
            pmax = None
            n_hours = n_data = n_est = 0
            for h in range(max(h0, bs // 3600), min(h1, (be - 1) // 3600) + 1):
                n_hours += 1
                u = hu.get(h)
                if u:
                    e += u[0]; v += u[1]; c += u[2]; n_data += 1; n_est += u[3]
                s = stats.get(h)
                if s:
                    if s[0] is not None:
                        pmax = s[0] if pmax is None else max(pmax, s[0])
                    for k, x in zip(HOUR_SUMS, s[1:]):
                        agg[k] += x or 0
            fw = agg["f_sum"] > 0
            out.append({
                "t": bs, "end": be, "energy": round(e, 4), "volume": round(v, 4), "cooling": round(c, 4),
                "hours": n_hours, "hours_data": n_data, "hours_est": n_est,
                "p_avg": round(agg["p_sum"] / agg["p_n"], 2) if agg["p_n"] else None, "p_max": pmax,
                "t1": round((agg["t1f"] / agg["f_sum"]) if fw else agg["t1_sum"] / agg["t_n"], 1) if agg["t_n"] else None,
                "t2": round((agg["t2f"] / agg["f_sum"]) if fw else agg["t2_sum"] / agg["t_n"], 1) if agg["t_n"] else None,
                "dt": round((agg["dtf"] / agg["f_sum"]) if fw else agg["dt_sum"] / agg["t_n"], 1) if agg["t_n"] else None,
                "flow": round(agg["f_sum"] / agg["f_n"], 1) if agg["f_n"] else None,
            })
            bs = be
        return out

    def runs(self, start: float, end: float, max_gap: int = 300) -> dict:
        """Every time heat came in: a run is a stretch of readings with water flowing (or power above 0).

        The valve is closed when no hot water flows through the meter, so flow is the clearest sign.
        A reading's state counts until the next reading; a gap longer than max_gap (app or gateway off)
        ends a run and is not counted as on or off.
        """
        prev = self.db.execute("SELECT ts, flow, power, heat_energy FROM readings WHERE ts < ? ORDER BY ts DESC LIMIT 1", (int(start),)).fetchone()
        rows = self.db.execute("SELECT ts, flow, power, heat_energy FROM readings WHERE ts >= ? AND ts <= ? ORDER BY ts", (int(start), int(end))).fetchall()
        if prev and start - prev[0] <= max_gap:
            rows.insert(0, (int(start), *prev[1:]))
        is_on = lambda f, p: (f or 0) > 0 or (p or 0) >= 0.05  # noqa: E731
        out: list[dict] = []
        cur = None
        on_s = covered = 0
        base_e = None  # counter at the last reading before the run started

        def close(run, t_end, e_end):
            run["end"] = t_end
            run["minutes"] = round((t_end - run["start"]) / 60, 1)
            run["energy"] = round(max(0.0, (e_end or 0) - run.pop("_e0")), 4) if run["_e0_ok"] else None
            run.pop("_e0_ok")
            n = run.pop("_n")
            run["p_avg"] = round(run.pop("_p") / n, 2) if n else None
            run["flow_avg"] = round(run.pop("_f") / n) if n else None
            out.append(run)

        prev_ts = None
        for i, (ts, f, p, e) in enumerate(rows):
            if prev_ts is not None and ts - prev_ts > max_gap:
                base_e = None  # readings were missing: what came in during the gap isn't part of this run
            prev_ts = ts
            nxt = rows[i + 1][0] if i + 1 < len(rows) else min(end, ts + max_gap)
            span = max(0, min(nxt, end) - ts)
            gap = span > max_gap
            on = is_on(f, p)
            if on:
                if cur is None:
                    cur = {"start": ts, "_e0": base_e if base_e is not None else (e or 0), "_e0_ok": e is not None, "_n": 0, "_p": 0.0, "_f": 0.0, "p_max": 0.0, "flow_max": 0.0}
                cur["_n"] += 1; cur["_p"] += p or 0; cur["_f"] += f or 0
                cur["p_max"] = max(cur["p_max"], p or 0); cur["flow_max"] = max(cur["flow_max"], f or 0)
            elif cur is not None:
                close(cur, ts, e)
                cur = None
            if not gap:
                covered += span
                on_s += span if on else 0
            elif cur is not None:  # readings stop: end the run at this reading (+ one interval at most)
                close(cur, ts + max_gap // 2, e)
                cur = None
            if e is not None:
                base_e = e
        if cur is not None:
            last = rows[-1]
            close(cur, min(end, last[0] + max_gap), last[3])
            out[-1]["ongoing"] = end - last[0] <= max_gap
        durs = [r["minutes"] for r in out]
        return {
            "start": start, "end": end,
            "stats": {"on_minutes": round(on_s / 60), "covered_minutes": round(covered / 60),
                      "on_pct": round(100 * on_s / covered, 1) if covered else None, "runs": len(out),
                      "avg_minutes": round(sum(durs) / len(durs), 1) if durs else None, "longest_minutes": max(durs) if durs else None},
            "runs": out,
        }

    # ------------------------------------------------------------------ hot tap water (estimate)
    def _tap_day(self, d0: int, d1: int, max_gap: int, now: float) -> tuple[list, bool, dict | None]:
        """Hot tap water that started on one local day: (events, had readings, the use going on now)."""
        done = d1 <= (self.last_ts or 0) - max_gap
        key = (d0, max_gap)
        if done and key in self._tap_cache:
            return self._tap_cache[key]
        hi = d1 if done else min(d1, now)
        rows = self.db.execute("SELECT ts, power, flow, temp1, temp2 FROM readings WHERE ts >= ? AND ts < ? ORDER BY ts",
                               (d0 - 3 * 3600, hi)).fetchall()  # 3 h before: to learn the normal heating level
        had = any(r[0] >= d0 for r in rows)
        res = detect_tap_water(rows, max_gap, None if done else now)
        out = ([e for e in res["events"] if d0 <= e["start"] < d1], had, res["now"])
        if done:
            self._tap_cache[key] = out
        return out

    def tap_water(self, start: float, end: float, max_gap: int = 300) -> dict:
        """Probably hot tap water (shower, tap): every use from `start` to `end`, an estimate (see tapwater.py)."""
        now = time.time()
        events, days, days_data, cur = [], 0, 0, None
        d = bucket_start(start, "day")
        while d < end:
            dn = bucket_next(d, "day")
            evs, had, live = self._tap_day(d, dn, max_gap, now)
            days += 1; days_data += had
            events += [e for e in evs if start <= e["start"] < end]
            cur = live or cur
            d = dn
        return {"start": start, "end": end, "events": events, "now": cur,
                "kwh": round(sum(e["kwh"] for e in events), 3), "uses": len(events),
                "minutes": round(sum(e["minutes"] for e in events), 1), "days": days, "days_with_readings": days_data}

    def typical_day(self, days: int = 28) -> list[dict]:
        """Average heat per hour of the day over the last `days` days (hours with real readings only)."""
        hu = self._hour_usage()
        since = time.time() - days * 86400
        tot = [0.0] * 24
        n = [0] * 24
        for h, u in hu.items():
            if h * 3600 < since or u[3]:
                continue
            hr = time.localtime(h * 3600).tm_hour
            tot[hr] += u[0]; n[hr] += 1
        return [{"hour": i, "energy": round(tot[i] / n[i], 5) if n[i] else None, "days": n[i]} for i in range(24)]

    def series(self, start: float, end: float) -> dict:
        span = end - start
        if span <= 3 * 86400:
            b = max(60, int(span / 720))
            rows = self.db.execute(
                "SELECT (ts / ?) * ? AS b, AVG(power), MAX(power), AVG(temp1), AVG(temp2), AVG(tempdiff), AVG(flow) "
                "FROM readings WHERE ts >= ? AND ts <= ? GROUP BY b ORDER BY b", (b, b, int(start), int(end))).fetchall()
        else:
            b = max(3600, int(span / 720) // 3600 * 3600)
            rows = self.db.execute(
                "SELECT (h * 3600 / ?) * ? AS b, SUM(p_sum) / NULLIF(SUM(p_n), 0), MAX(p_max), SUM(t1_sum) / NULLIF(SUM(t_n), 0), "
                "SUM(t2_sum) / NULLIF(SUM(t_n), 0), SUM(dt_sum) / NULLIF(SUM(t_n), 0), SUM(f_sum) / NULLIF(SUM(f_n), 0) "
                "FROM hours WHERE h >= ? AND h <= ? GROUP BY b ORDER BY b", (b, b, int(start // 3600), int(end // 3600))).fetchall()
        names = ["power", "power_max", "temp1", "temp2", "tempdiff", "flow"]
        out: dict[str, list] = {k: [] for k in names}
        for r in rows:
            for k, v in zip(names, r[1:]):
                if v is not None:
                    out[k].append([r[0], round(v, 2)])
        return {"bucket": b, "series": out}

    def export_csv(self, start: float, end: float):
        """Every stored reading in the period, with the gateway's other values filled in from the last full answer."""
        extra_keys: list[str] = []
        static: dict = {}
        prior = self.db.execute("SELECT raw FROM readings WHERE raw IS NOT NULL AND ts < ? ORDER BY ts DESC LIMIT 1", (int(start),)).fetchone()
        if prior:
            static = {k: v for k, v in json.loads(prior[0]).items() if k not in VOLATILE}
        for (raw,) in self.db.execute("SELECT raw FROM readings WHERE raw IS NOT NULL AND ts >= ? AND ts <= ?", (int(start), int(end))):
            for k in json.loads(raw):
                if k not in VOLATILE and k not in extra_keys:
                    extra_keys.append(k)
        for k in static:
            if k not in extra_keys:
                extra_keys.append(k)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time_utc", "time_local", *COLS, "meter_clock", *extra_keys])
        yield buf.getvalue()
        cur = self.db.execute(f"SELECT ts, {', '.join(COLS)}, meter_clock, raw FROM readings WHERE ts >= ? AND ts <= ? ORDER BY ts", (int(start), int(end)))
        while True:
            rows = cur.fetchmany(2000)
            if not rows:
                break
            buf = io.StringIO()
            w = csv.writer(buf)
            for r in rows:
                if r[-1]:
                    static = {k: v for k, v in json.loads(r[-1]).items() if k not in VOLATILE}
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r[0])), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r[0])),
                            *["" if x is None else x for x in r[1:-1]], *[static.get(k, "") for k in extra_keys]])
            yield buf.getvalue()
