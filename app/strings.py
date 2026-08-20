"""Deduce which MPPT string each panel (optimizer) is wired to.

The API exposes no wiring data, so this is an inference: each panel's
5-minute power curve is correlated (Pearson) against the two MPPT input
powers (V x A) over a full day; a panel is assigned to the MPPT it
correlates with most. Clouds and different orientations make the curves
diverge, which is what carries the signal — on a perfectly uniform day
the two strings look alike and confidence drops.
"""

import asyncio
import logging
from typing import Any

from .isolarcloud import ISolarCloudError, client

log = logging.getLogger("strings")

# Daylight window: enough signal, fewer API calls (2h chunks).
HOURS = range(6, 20, 2)


async def _panel_series(ps_keys: list[str], date: str) -> dict[str, dict[str, float]]:
    """{ps_key: {timestamp: watts}} for all panels, chunked by 2h."""
    out: dict[str, dict[str, float]] = {k: {} for k in ps_keys}

    async def chunk(h: int):
        try:
            r = await client.get_device_minute_data(
                ps_keys, "p58107", f"{date}{h:02d}0000", f"{date}{h + 1:02d}5959"
            )
        except ISolarCloudError:
            return
        for key in ps_keys:
            for row in r.get(key, []):
                v = row.get("p58107")
                if v not in (None, "", "--"):
                    out[key][row["time_stamp"]] = float(v)

    await asyncio.gather(*[chunk(h) for h in HOURS])
    return out


async def _mppt_series(ess_key: str, date: str) -> dict[int, dict[str, float]]:
    """{1: {ts: watts}, 2: {ts: watts}} from MPPT V x A points."""
    out: dict[int, dict[str, float]] = {1: {}, 2: {}}

    async def chunk(h: int):
        try:
            r = await client.get_device_minute_data(
                [ess_key], "p13001,p13002,p13105,p13106",
                f"{date}{h:02d}0000", f"{date}{h + 1:02d}5959",
            )
        except ISolarCloudError:
            return
        for row in r.get(ess_key, []):
            ts = row["time_stamp"]
            try:
                out[1][ts] = float(row["p13001"]) * float(row["p13002"])
                out[2][ts] = float(row["p13105"]) * float(row["p13106"])
            except (KeyError, TypeError, ValueError):
                continue

    await asyncio.gather(*[chunk(h) for h in HOURS])
    return out


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 12:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx * syy) ** 0.5


async def deduce(ps_keys: list[str], sn_by_key: dict[str, str],
                 ess_key: str, date: str) -> dict[str, Any]:
    panels = await _panel_series(ps_keys, date)
    mppt = await _mppt_series(ess_key, date)

    results = []
    for key in ps_keys:
        series = panels[key]
        # Only compare while the panel actually produces.
        ts = [t for t in series if series[t] > 10 and t in mppt[1] and t in mppt[2]]
        ts.sort()
        xs = [series[t] for t in ts]
        c1 = _pearson(xs, [mppt[1][t] for t in ts])
        c2 = _pearson(xs, [mppt[2][t] for t in ts])
        if c1 is None or c2 is None:
            string, confidence = None, None
        else:
            string = 1 if c1 >= c2 else 2
            confidence = round(abs(c1 - c2), 3)
        results.append({
            "ps_key": key,
            "sn": sn_by_key.get(key),
            "string": string,
            "corr_mppt1": None if c1 is None else round(c1, 3),
            "corr_mppt2": None if c2 is None else round(c2, 3),
            "confidence": confidence,
            "samples": len(ts),
        })

    def avg(d: dict[str, float]) -> float | None:
        vals = [v for v in d.values() if v > 10]
        return round(sum(vals) / len(vals), 1) if vals else None

    n1 = sum(1 for r in results if r["string"] == 1)
    n2 = sum(1 for r in results if r["string"] == 2)
    log.info("string deduction %s: %d -> MPPT1, %d -> MPPT2", date, n1, n2)
    return {
        "date": date,
        "panels": results,
        "mppt_avg_w": {"1": avg(mppt[1]), "2": avg(mppt[2])},
        "note": (
            "Deduced grouping: panels are assigned to the MPPT whose power "
            "curve their own curve correlates with best. This is an "
            "inference, not wiring data from the API."
        ),
    }
