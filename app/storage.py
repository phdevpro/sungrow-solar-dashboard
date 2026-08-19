"""SQLite time-series storage for collected plant data.

Volumes are tiny (5-minute samples for one plant), so plain sqlite3 with
short-lived synchronous transactions is fine; writes happen from the
collector task every few minutes.
"""

import os
import sqlite3
import threading
from typing import Any

DB_PATH = os.getenv("ISC_DB_PATH", "data/solar.db")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS power_samples (
    ps_id   TEXT NOT NULL,
    ts      TEXT NOT NULL,          -- yyyyMMddHHmmss (plant local time)
    watts   REAL,
    PRIMARY KEY (ps_id, ts)
);
CREATE TABLE IF NOT EXISTS panel_samples (
    ps_key   TEXT NOT NULL,
    ts       TEXT NOT NULL,
    watts    REAL,
    total_wh REAL,
    PRIMARY KEY (ps_key, ts)
);
CREATE TABLE IF NOT EXISTS battery_samples (
    sn      TEXT NOT NULL,
    ts      TEXT NOT NULL,
    soc     REAL,
    soh     REAL,
    temp_c  REAL,
    voltage REAL,
    current REAL,
    PRIMARY KEY (sn, ts)
);
CREATE TABLE IF NOT EXISTS flow_samples (
    ps_id   TEXT NOT NULL,
    ts      TEXT NOT NULL,
    pv_w    REAL,               -- PV production (DC)
    load_w  REAL,               -- house consumption
    grid_w  REAL,               -- + import from grid / - export to grid
    batt_w  REAL,               -- + charging / - discharging
    soc     REAL,
    PRIMARY KEY (ps_id, ts)
);
CREATE TABLE IF NOT EXISTS ev_samples (
    ts         TEXT PRIMARY KEY,   -- yyyyMMddHHmmss
    power_w    REAL,
    session_wh REAL,
    connected  INTEGER,
    charging   INTEGER
);
CREATE TABLE IF NOT EXISTS plant_kpi (
    ps_id        TEXT NOT NULL,
    ts           TEXT NOT NULL,
    curr_power_w REAL,
    today_kwh    REAL,
    total_kwh    REAL,
    PRIMARY KEY (ps_id, ts)
);
"""


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


def _execmany(sql: str, rows: list[tuple]) -> None:
    if not rows:
        return
    with _lock:
        conn = connect()
        conn.executemany(sql, rows)
        conn.commit()


def store_power(ps_id: str, samples: list[tuple[str, float]]) -> None:
    """samples: [(ts, watts)]"""
    _execmany(
        "INSERT OR REPLACE INTO power_samples (ps_id, ts, watts) VALUES (?, ?, ?)",
        [(ps_id, ts, w) for ts, w in samples],
    )


def store_panels(rows: list[dict[str, Any]]) -> None:
    _execmany(
        "INSERT OR REPLACE INTO panel_samples (ps_key, ts, watts, total_wh) VALUES (?, ?, ?, ?)",
        [
            (r["ps_key"], r["time"], r.get("power_w"), r.get("total_wh"))
            for r in rows
            if r.get("ps_key") and r.get("time")
        ],
    )


def store_batteries(ts: str, rows: list[dict[str, Any]]) -> None:
    _execmany(
        "INSERT OR REPLACE INTO battery_samples (sn, ts, soc, soh, temp_c, voltage, current) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (r["sn"], ts, r.get("soc"), r.get("soh"), r.get("temp_c"),
             r.get("voltage"), r.get("current"))
            for r in rows
            if r.get("sn")
        ],
    )


def store_kpi(ps_id: str, ts: str, curr_power_w, today_kwh, total_kwh) -> None:
    _execmany(
        "INSERT OR REPLACE INTO plant_kpi (ps_id, ts, curr_power_w, today_kwh, total_kwh) "
        "VALUES (?, ?, ?, ?, ?)",
        [(ps_id, ts, curr_power_w, today_kwh, total_kwh)],
    )


def store_flow(ps_id: str, ts: str, flow: dict[str, Any]) -> None:
    _execmany(
        "INSERT OR REPLACE INTO flow_samples (ps_id, ts, pv_w, load_w, grid_w, batt_w, soc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(ps_id, ts, flow.get("pv_w"), flow.get("load_w"), flow.get("grid_w"),
          flow.get("batt_w"), flow.get("soc"))],
    )


def load_flow_day(ps_id: str, date: str) -> list[dict[str, Any]]:
    with _lock:
        cur = connect().execute(
            "SELECT ts, pv_w, load_w, grid_w, batt_w, soc FROM flow_samples "
            "WHERE ps_id = ? AND ts LIKE ? ORDER BY ts",
            (ps_id, f"{date}%"),
        )
        return [
            {"t": ts[8:12], "pv_w": pv, "load_w": lo, "grid_w": gr, "batt_w": ba, "soc": soc}
            for ts, pv, lo, gr, ba, soc in cur.fetchall()
        ]


def store_ev(ts: str, s: dict[str, Any]) -> None:
    _execmany(
        "INSERT OR REPLACE INTO ev_samples (ts, power_w, session_wh, connected, charging) "
        "VALUES (?, ?, ?, ?, ?)",
        [(ts, s.get("power_w"), s.get("session_wh"),
          int(bool(s.get("vehicle_connected"))), int(bool(s.get("charging"))))],
    )


def load_ev_day(date: str) -> list[dict[str, Any]]:
    with _lock:
        cur = connect().execute(
            "SELECT ts, power_w, session_wh, connected, charging FROM ev_samples "
            "WHERE ts LIKE ? ORDER BY ts",
            (f"{date}%",),
        )
        return [
            {"t": ts[8:12], "power_w": p, "session_wh": sw,
             "connected": bool(c), "charging": bool(ch)}
            for ts, p, sw, c, ch in cur.fetchall()
        ]


def load_power_day(ps_id: str, date: str) -> list[dict[str, Any]]:
    """All power samples for one YYYYMMDD day, time-ordered."""
    with _lock:
        cur = connect().execute(
            "SELECT ts, watts FROM power_samples "
            "WHERE ps_id = ? AND ts LIKE ? ORDER BY ts",
            (ps_id, f"{date}%"),
        )
        return [{"t": ts[8:12], "w": w} for ts, w in cur.fetchall()]


def power_day_count(ps_id: str, date: str) -> int:
    with _lock:
        cur = connect().execute(
            "SELECT COUNT(*) FROM power_samples WHERE ps_id = ? AND ts LIKE ?",
            (ps_id, f"{date}%"),
        )
        return cur.fetchone()[0]


def load_battery_day(sn: str, date: str) -> list[dict[str, Any]]:
    with _lock:
        cur = connect().execute(
            "SELECT ts, soc, soh, temp_c, voltage, current FROM battery_samples "
            "WHERE sn = ? AND ts LIKE ? ORDER BY ts",
            (sn, f"{date}%"),
        )
        return [
            {"t": ts[8:12], "soc": soc, "soh": soh, "temp_c": t, "voltage": v, "current": c}
            for ts, soc, soh, t, v, c in cur.fetchall()
        ]


def load_panel_day(ps_key: str, date: str) -> list[dict[str, Any]]:
    with _lock:
        cur = connect().execute(
            "SELECT ts, watts, total_wh FROM panel_samples "
            "WHERE ps_key = ? AND ts LIKE ? ORDER BY ts",
            (ps_key, f"{date}%"),
        )
        return [{"t": ts[8:12], "w": w, "total_wh": twh} for ts, w, twh in cur.fetchall()]
