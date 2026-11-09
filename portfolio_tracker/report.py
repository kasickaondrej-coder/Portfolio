"""Plain text and JSON rendering of a Report."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, is_dataclass
from typing import Optional

import pandas as pd

from .analytics import Report


def pct(x: Optional[float], digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x * 100:+.{digits}f}%"


def money(x: Optional[float], ccy: str = "") -> str:
    if x is None:
        return "n/a"
    s = f"{x:,.2f}"
    return f"{s} {ccy}".strip()


def _table(headers: list[str], rows: list[list[str]], align: Optional[str] = None) -> str:
    align = align or "l" + "r" * (len(headers) - 1)
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def fmt(cells):
        return "  ".join(str(c).ljust(w) if a == "l" else str(c).rjust(w) for c, w, a in zip(cells, widths, align))

    out = [fmt(headers), "  ".join("-" * w for w in widths)]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


def render_text(r: Report, top_n: int = 5) -> str:
    b = r.base
    L = []
    L.append(f"PORTFOLIO REPORT  as of {r.as_of.date()}  base {b}")
    L.append(f"History: {r.inception.date()} to {r.as_of.date()} ({r.days} days), {r.contributions_count} buy transactions")
    L.append("")
    L.append("SUMMARY")
    rows = [
        ["Market value", money(r.value, b)],
        ["Total bought (incl. fees)", money(r.total_bought, b)],
        ["Total sold (net of fees)", money(r.total_sold, b)],
        ["Dividends received", money(r.dividends, b)],
        ["Net invested (bought - sold)", money(r.net_invested, b)],
        ["Fees paid", money(r.fees, b)],
        ["Total P/L", money(r.total_pl, b)],
        ["  realized", money(r.realized, b)],
        ["  unrealized", money(r.unrealized, b)],
        ["  dividends", money(r.dividends, b)],
        ["Total return (P/L / total bought)", pct(r.simple_return)],
        ["Money-weighted return, annualised (XIRR)", pct(r.xirr) + ("  [short history, extrapolated]" if r.short_history else "")],
        ["Money-weighted return, cumulative", pct(r.xirr_period)],
        ["Time-weighted return, cumulative", pct(r.twr_total)],
        ["Time-weighted return, annualised", pct(r.twr_annualised) + ("  [short history, extrapolated]" if r.short_history else "")],
    ]
    if r.drawdown:
        d = r.drawdown
        rec = d.recovered.date() if d.recovered is not None else "not recovered"
        rows.append(["Max drawdown (TWR)", f"{pct(d.depth)}  peak {d.peak.date()} trough {d.trough.date()} recovered {rec}"])
    L.append(_table(["Metric", "Value"], rows, "ll"))
    L.append("")
    L.append("  XIRR = what your money actually earned, including the timing of your DCA buys.")
    L.append("  TWR  = how the investments performed, independent of when you added money.")
    L.append("  XIRR below TWR means your contributions were timed unfavourably (bought before drops).")

    if r.benchmarks:
        L.append("")
        L.append("BENCHMARK (same cash flows on the same dates invested in the benchmark, total return)")
        rows = []
        for bm in r.benchmarks:
            diff_x = pct(r.xirr - bm.xirr) if r.xirr is not None and bm.xirr is not None else "n/a"
            diff_v = money(r.total_pl - bm.total_pl, b)
            if bm.clamped:
                diff_x, diff_v = diff_x + " (approx)", diff_v + " (approx)"
            rows.append([bm.symbol, money(bm.end_value, b), money(bm.total_pl, b), pct(bm.xirr), diff_x, diff_v])
        L.append(_table(["Benchmark", "Value", "P/L", "XIRR", "Your XIRR - bm", "Your P/L - bm"], rows))

    L.append("")
    L.append("PERIOD RETURNS")
    rows = []
    for p in r.periods:
        row = [p.label, str(p.start.date()), money(p.start_value), money(p.net_flows), money(p.end_value),
               money(p.abs_pl), pct(p.twr), pct(p.mwr)]
        row += [pct(p.benchmark_twr.get(bm.symbol)) for bm in r.benchmarks]
        rows.append(row)
    hdr = ["Period", "From", "Start value", "Net added", "End value", "P/L", "TWR", "MWR(Dietz)"]
    hdr += [f"{bm.symbol} TWR" for bm in r.benchmarks]
    L.append(_table(hdr, rows))
    L.append("  P/L = end value - start value - money added in the period (contributions are not counted as gains).")

    L.append("")
    L.append("HOLDINGS")
    rows = []
    for h in r.holdings:
        rows.append([h.ticker + (" (stale)" if h.stale else ""), h.currency, f"{h.quantity:,.4f}".rstrip("0").rstrip("."),
                     money(h.price_base), money(h.value), f"{h.weight * 100:.1f}%", money(h.avg_cost),
                     money(h.cost_basis), money(h.unrealized), money(h.realized), money(h.dividends),
                     money(h.total_pl), pct(h.total_pl / h.invested if h.invested else None),
                     pct(h.xirr) + ("*" if h.short_history else "")])
    L.append(_table(["Ticker", "Ccy", "Qty", f"Price {b}", "Value", "Weight", "Avg cost", "Cost basis",
                     "Unrealized", "Realized", "Dividends", "Total P/L", "P/L %", "XIRR"], rows))
    if any(h.short_history for h in r.holdings):
        L.append("  * held under 1 year: annualised XIRR is an extrapolation, look at P/L % instead.")

    def movers(title, contribs):
        L.append("")
        L.append(title)
        up = [c for c in contribs if c.abs_pl > 0][:top_n]
        down = sorted([c for c in contribs if c.abs_pl < 0], key=lambda c: c.abs_pl)[:top_n]
        rows = [["UP", c.ticker, money(c.abs_pl, b), pct(c.pct_of_portfolio), pct(c.own_return)] for c in up]
        rows += [["DOWN", c.ticker, money(c.abs_pl, b), pct(c.pct_of_portfolio), pct(c.own_return)] for c in down]
        if rows:
            L.append(_table(["", "Ticker", "P/L", "Contribution", "Own TWR"], rows, "llrrr"))
        else:
            L.append("  no movement")

    for p in r.periods:
        if p.label in ("1M", "1Y"):
            movers(f"BIGGEST MOVERS {p.label} ({p.start.date()} to {p.end.date()})", p.contributions)
    movers("BIGGEST MOVERS ALL TIME", r.alltime_contributions)
    L.append("  Contribution = share of the portfolio's money-weighted return; contributions add up to the portfolio return.")

    if r.warnings:
        L.append("")
        L.append("WARNINGS")
        L += [f"  - {w}" for w in r.warnings]
    return "\n".join(L)


def _jsonable(o):
    if is_dataclass(o):
        return {k: _jsonable(v) for k, v in asdict(o).items() if k != "daily"}
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, pd.Timestamp):
        return o.date().isoformat()
    if isinstance(o, float) and not math.isfinite(o):
        return None
    return o


def render_json(r: Report) -> str:
    d = _jsonable(r)
    return json.dumps(d, indent=2, default=str)
