"""Path, positioning and regime features — ONE definition for backtest and live.

The decide prompt has handed the model a snapshot: 24h change, 24h turnover, a
5-day taker share, a near-chance prior. It is then asked which name will RUN UP
before falling 8% over 72 hours — a question about the path, with no path.
These are the features an expert would want and the screen's own bars already
contain: multi-horizon returns and the same relative to BTC, realised
volatility, distance from the week's high and low, a volume surge ratio, and
taker flow at two horizons.

Defined here, in one place, on purpose. The backtest replay validates a feature
over six months BEFORE it reaches the prompt, and that validation is only worth
anything if the live screen computes the identical number from identical bars.
Two implementations would drift, and a drift here is a backtest that measured
a feature nobody trades on.

Every function reads bars[: i + 1] only. Lookahead is the deadliest backtest
bug, so the slice is structural.

Bars are Binance klines: [open_time, open, high, low, close, volume,
close_time, quote_volume, trades, taker_base, taker_quote, ...].
"""

from __future__ import annotations

import math
from itertools import pairwise

# Feature horizons in HOURLY bars. These name the features rather than tune
# them: ret_7d means seven days, and changing it would change the feature's
# meaning, not its calibration.
HORIZONS_H = {"1h": 1, "4h": 4, "24h": 24, "72h": 72, "7d": 168}
WEEK_H = 168
DAY_H = 24


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def path_features(bars: list[list], i: int | None = None) -> dict:
    """Shape of the recent path at bar i (default: the last bar). Missing history
    yields None for that feature, never a silently shorter window."""
    if not bars:
        return {}
    i = len(bars) - 1 if i is None else i
    window = bars[: i + 1]
    closes = [_f(b[4]) for b in window]
    close = closes[-1]
    out: dict[str, float | None] = {}
    if close <= 0:
        return out

    for label, h in HORIZONS_H.items():
        ref = closes[-1 - h] if len(closes) > h else None
        out[f"ret_{label}"] = round((close / ref - 1) * 100, 4) if ref and ref > 0 else None

    week = window[-WEEK_H:] if len(window) >= WEEK_H else None
    if week:
        logs = [
            math.log(_f(b[4]) / _f(a[4]))
            for a, b in pairwise(week)
            if _f(a[4]) > 0 and _f(b[4]) > 0
        ]
        if len(logs) > 2:
            mean = sum(logs) / len(logs)
            var = sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)
            # Typical DAILY move, in %: hourly sigma scaled to a day.
            out["vol_24h_pct"] = round(math.sqrt(var) * math.sqrt(DAY_H) * 100, 4)
        hi = max(_f(b[2]) for b in week)
        lo = min(_f(b[3]) for b in week)
        out["from_7d_high_pct"] = round((close / hi - 1) * 100, 4) if hi > 0 else None
        out["from_7d_low_pct"] = round((close / lo - 1) * 100, 4) if lo > 0 else None
        out["range_pos_7d"] = round((close - lo) / (hi - lo), 4) if hi > lo else None
        week_qv = sum(_f(b[7]) for b in week)
        day_qv = sum(_f(b[7]) for b in week[-DAY_H:])
        out["vol_ratio_24h"] = round(day_qv / (week_qv / 7), 4) if week_qv > 0 else None
        week_taker = sum(_f(b[10]) for b in week)
        day_taker = sum(_f(b[10]) for b in week[-DAY_H:])
        out["taker_share_24h"] = round(day_taker / day_qv, 4) if day_qv > 0 else None
        out["taker_share_7d"] = round(week_taker / week_qv, 4) if week_qv > 0 else None
    else:
        for k in (
            "vol_24h_pct",
            "from_7d_high_pct",
            "from_7d_low_pct",
            "range_pos_7d",
            "vol_ratio_24h",
            "taker_share_24h",
            "taker_share_7d",
        ):
            out[k] = None
    return out


def daily_vol_from_closes(closes: list[float]) -> float | None:
    """Typical daily move in %, from hourly closes: hourly sigma scaled to a day.

    The same number `path_features` reports as `vol_24h_pct`, exposed on bare
    closes so the exit supervisor and the exit grid size a stop from it with
    one definition.
    """
    closes = [c for c in closes if c and c > 0]
    if len(closes) < DAY_H:
        return None
    logs = [math.log(b / a) for a, b in pairwise(closes)]
    if len(logs) < 2:
        return None
    mean = sum(logs) / len(logs)
    var = sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)
    return math.sqrt(var) * math.sqrt(DAY_H) * 100


def relative(feats: dict, benchmark: dict) -> dict:
    """Relative strength: the name's return minus the benchmark's, per horizon."""
    out = {}
    for label in HORIZONS_H:
        a, b = feats.get(f"ret_{label}"), benchmark.get(f"ret_{label}")
        out[f"rs_{label}"] = round(a - b, 4) if a is not None and b is not None else None
    return out


def regime(benchmark: dict, cross_section: list[dict] | None = None) -> dict:
    """Market state: the benchmark's own path plus breadth over the cross-section."""
    out = {f"btc_{k}": benchmark.get(k) for k in ("ret_24h", "ret_7d", "vol_24h_pct")}
    if cross_section:
        for label in ("24h", "7d"):
            vals = [r.get(f"ret_{label}") for r in cross_section]
            vals = [v for v in vals if v is not None]
            out[f"breadth_{label}"] = (
                round(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None
            )
    return out


# --- Equities: the same ideas in DAILY bars ---------------------------------
#
# A KR or US hourly bar exists only in session, so "24 bars back" is four
# sessions ago and the hourly definitions above would silently mean something
# else. Equities get their own definition in daily bars -- one definition for
# the KR backtest and the KR screen, as `path_features` is for Binance. Volume
# is not carried by the archive's bars, so there is no surge or flow feature
# here; what there is: returns, realised vol, range position and distance from
# the month's high/low, and relative strength against the venue's benchmark.

HORIZONS_D = {"1d": 1, "3d": 3, "5d": 5, "20d": 20}
MONTH_D = 20


def daily_path_features(bars, i: int | None = None) -> dict:
    """Shape of the recent path at daily bar i, from objects with .high/.low/.close
    (the archive's `Bar`). Missing history yields None, never a shorter window."""
    if not bars:
        return {}
    i = len(bars) - 1 if i is None else i
    window = bars[: i + 1]
    closes = [float(b.close) for b in window]
    close = closes[-1]
    out: dict[str, float | None] = {}
    if close <= 0:
        return out
    for label, h in HORIZONS_D.items():
        ref = closes[-1 - h] if len(closes) > h else None
        out[f"ret_{label}"] = round((close / ref - 1) * 100, 4) if ref and ref > 0 else None
    month = window[-MONTH_D:] if len(window) >= MONTH_D else None
    if month:
        mc = [float(b.close) for b in month]
        logs = [math.log(b / a) for a, b in pairwise(mc) if a > 0 and b > 0]
        if len(logs) > 2:
            mean = sum(logs) / len(logs)
            var = sum((x - mean) ** 2 for x in logs) / (len(logs) - 1)
            out["vol_d_pct"] = round(math.sqrt(var) * 100, 4)  # typical daily move, %
        hi = max(float(b.high) for b in month)
        lo = min(float(b.low) for b in month)
        out["from_20d_high_pct"] = round((close / hi - 1) * 100, 4) if hi > 0 else None
        out["from_20d_low_pct"] = round((close / lo - 1) * 100, 4) if lo > 0 else None
        out["range_pos_20d"] = round((close - lo) / (hi - lo), 4) if hi > lo else None
    else:
        for k in ("vol_d_pct", "from_20d_high_pct", "from_20d_low_pct", "range_pos_20d"):
            out[k] = None
    return out


def relative_daily(feats: dict, benchmark: dict) -> dict:
    out = {}
    for label in HORIZONS_D:
        a, b = feats.get(f"ret_{label}"), benchmark.get(f"ret_{label}")
        out[f"rs_{label}"] = round(a - b, 4) if a is not None and b is not None else None
    return out


def regime_daily(benchmark: dict, cross_section: list[dict] | None = None) -> dict:
    out = {f"bench_{k}": benchmark.get(k) for k in ("ret_5d", "ret_20d", "vol_d_pct")}
    if cross_section:
        for label in ("5d", "20d"):
            vals = [r.get(f"ret_{label}") for r in cross_section]
            vals = [v for v in vals if v is not None]
            out[f"breadth_{label}"] = (
                round(sum(1 for v in vals if v > 0) / len(vals), 3) if vals else None
            )
    return out


def turnover_ratio_daily(turnovers: list[float]) -> float | None:
    """Last 5 sessions' turnover over the 20-session daily average (surge)."""
    t = [float(x) for x in turnovers if x is not None]
    if len(t) < MONTH_D:
        return None
    month = t[-MONTH_D:]
    avg = sum(month) / MONTH_D
    return round(sum(month[-5:]) / 5 / avg, 4) if avg > 0 else None


DAILY_PATH_KEYS = [
    "ret_1d",
    "ret_3d",
    "ret_5d",
    "ret_20d",
    "vol_d_pct",
    "from_20d_high_pct",
    "from_20d_low_pct",
    "range_pos_20d",
    "turnover_ratio_5d",
]
DAILY_RS_KEYS = [f"rs_{k}" for k in HORIZONS_D]


PATH_KEYS = [
    "ret_1h",
    "ret_4h",
    "ret_24h",
    "ret_72h",
    "ret_7d",
    "vol_24h_pct",
    "from_7d_high_pct",
    "from_7d_low_pct",
    "range_pos_7d",
    "vol_ratio_24h",
    "taker_share_24h",
    "taker_share_7d",
]
RS_KEYS = [f"rs_{k}" for k in HORIZONS_H]
