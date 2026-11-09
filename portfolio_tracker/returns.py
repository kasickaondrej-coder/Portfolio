"""Pure return math: XIRR, time-weighted return, Modified Dietz, annualisation, drawdown.

Sign convention for XIRR flows is the investor's view: money the investor pays
into the portfolio is negative, money received back (sales, dividends, the
terminal value) is positive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional, Sequence

import pandas as pd

DAYS_PER_YEAR = 365.0


def _year_fracs(flows: Sequence[tuple[date, float]]) -> tuple[list[float], list[float]]:
    d0 = min(d for d, _ in flows)
    ts = [(pd.Timestamp(d) - pd.Timestamp(d0)).days / DAYS_PER_YEAR for d, _ in flows]
    cfs = [float(a) for _, a in flows]
    return ts, cfs


def _npv(rate: float, ts: list[float], cfs: list[float]) -> float:
    base = 1.0 + rate
    try:
        return sum(cf / base ** t for t, cf in zip(ts, cfs))
    except (OverflowError, ZeroDivisionError):
        # base -> 0: the latest flows dominate
        return math.copysign(math.inf, cfs[ts.index(max(ts))])


def _net_same_day(flows: Iterable[tuple[date, float]]) -> list[tuple[date, float]]:
    acc: dict[pd.Timestamp, float] = {}
    for d, a in flows:
        k = pd.Timestamp(d).normalize()
        acc[k] = acc.get(k, 0.0) + float(a)
    return sorted(((k, v) for k, v in acc.items() if abs(v) > 1e-12), key=lambda x: x[0])


def xirr(flows: Iterable[tuple[date, float]], guess: float = 0.1) -> Optional[float]:
    """Annualised internal rate of return for dated cash flows (act/365, Excel XIRR).

    Returns None when undefined: fewer than one negative and one positive flow,
    all flows on the same day, or no root found.
    """
    fl = _net_same_day(flows)
    if len(fl) < 2:
        return None
    if not (any(a < 0 for _, a in fl) and any(a > 0 for _, a in fl)):
        return None
    ts, cfs = _year_fracs(fl)
    if max(ts) == 0:
        return None

    # Newton
    r = guess
    for _ in range(100):
        try:
            f = _npv(r, ts, cfs)
            df = sum(-t * cf / (1.0 + r) ** (t + 1) for t, cf in zip(ts, cfs))
        except (OverflowError, ZeroDivisionError):
            break
        if df == 0 or not math.isfinite(f) or not math.isfinite(df):
            break
        step = f / df
        r_new = r - step
        if r_new <= -1.0:
            r_new = (r - 1.0) / 2.0  # stay inside the domain
        if abs(r_new - r) < 1e-12:
            r = r_new
            if abs(_npv(r, ts, cfs)) < 1e-6 * max(1.0, max(abs(c) for c in cfs)):
                return r
            break
        r = r_new

    # Bisection fallback on (-1, hi]
    lo, hi = -0.999999999, 1.0
    f_lo = _npv(lo, ts, cfs)
    f_hi = _npv(hi, ts, cfs)
    while f_lo * f_hi > 0 and hi < 1e6:
        hi *= 10.0
        f_hi = _npv(hi, ts, cfs)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2.0
        f_mid = _npv(mid, ts, cfs)
        if f_mid == 0 or (hi - lo) < 1e-12:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def annualise(period_return: Optional[float], days: int) -> Optional[float]:
    if period_return is None or days <= 0:
        return None
    if period_return <= -1.0:
        return -1.0
    return (1.0 + period_return) ** (DAYS_PER_YEAR / days) - 1.0


def deannualise(annual_rate: Optional[float], days: int) -> Optional[float]:
    if annual_rate is None:
        return None
    return (1.0 + annual_rate) ** (days / DAYS_PER_YEAR) - 1.0


def twr_daily_returns(
    values: pd.Series,
    inflows: pd.Series,
    outflows: pd.Series,
    min_base: float = 1e-9,
    small_base_ratio: float = 0.05,
) -> pd.Series:
    """Daily sub-period returns with external flows.

    values:   end-of-day market value V_t (after that day's trades)
    inflows:  money put in on day t (buys incl. fees), >= 0
    outflows: money taken out on day t (net sale proceeds, cash dividends), >= 0

    Flows are treated as happening at the end of the day (daily valuation, trades
    roughly at the close):
        r_t = (V_t + Out_t - In_t) / V_{t-1} - 1
    so a buy at the closing price neither adds nor dilutes return. When there is
    no prior value, or it is tiny compared with the inflow (leftover dust), the
    inflow is treated as arriving at the start of the day instead:
        r_t = (V_t + Out_t) / (V_{t-1} + In_t) - 1
    which measures the move from the trade price to the close without blowing up.
    """
    v_prev = values.shift(1).fillna(0.0)
    end_of_day = (values + outflows - inflows) / v_prev - 1.0
    start_of_day = (values + outflows) / (v_prev + inflows) - 1.0
    use_start = v_prev < small_base_ratio * inflows
    r = end_of_day.where(~use_start, start_of_day)
    r[(v_prev + inflows) <= min_base] = 0.0
    r[(~use_start) & (v_prev <= min_base)] = 0.0
    return r.replace([float("inf"), float("-inf")], 0.0).fillna(0.0)


def twr_index(daily_returns: pd.Series) -> pd.Series:
    return (1.0 + daily_returns).cumprod()


def twr_between(index: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> Optional[float]:
    """TWR for (start, end] from an index. start value is the index at end of `start` day."""
    idx = index.loc[:end]
    if idx.empty:
        return None
    end_v = idx.iloc[-1]
    before = index.loc[:start]
    start_v = before.iloc[-1] if not before.empty else 1.0
    if start_v == 0:
        return None
    return float(end_v / start_v - 1.0)


def modified_dietz(
    v_start: float,
    v_end: float,
    flows: Sequence[tuple[pd.Timestamp, float]],
    start: pd.Timestamp,
    end: pd.Timestamp,
    income: float = 0.0,
) -> tuple[Optional[float], float]:
    """Modified Dietz return for (start, end].

    flows: net external flows into the portfolio (positive = money in), dated
    inside (start, end], assumed at the end of their day. income: cash paid out
    (dividends) counted as gain. Returns (return or None, denominator).
    """
    T = (end - start).days
    if T <= 0:
        return None, 0.0
    net = sum(a for _, a in flows)
    weighted = sum(a * (T - (pd.Timestamp(d) - start).days) / T for d, a in flows)
    denom = v_start + weighted
    gain = v_end - v_start - net + income
    if denom <= 1e-9:
        return None, denom
    return gain / denom, denom


@dataclass
class Drawdown:
    depth: float
    peak: pd.Timestamp
    trough: pd.Timestamp
    recovered: Optional[pd.Timestamp]


def max_drawdown(index: pd.Series) -> Optional[Drawdown]:
    s = index.dropna()
    if len(s) < 2:
        return None
    running_max = s.cummax()
    dd = s / running_max - 1.0
    trough = dd.idxmin()
    depth = float(dd.loc[trough])
    if depth >= 0:
        return Drawdown(0.0, s.index[0], s.index[0], s.index[0])
    peak = s.loc[:trough].idxmax()
    after = s.loc[trough:]
    rec = after[after >= s.loc[peak]]
    return Drawdown(depth, peak, trough, rec.index[0] if not rec.empty else None)
