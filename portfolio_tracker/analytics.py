"""Portfolio engine: daily valuation, cost basis, returns, attribution, benchmark."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .data import DataError, Provider, fx_series
from .returns import (
    Drawdown,
    annualise,
    deannualise,
    max_drawdown,
    modified_dietz,
    twr_between,
    twr_daily_returns,
    twr_index,
    xirr,
)
from .transactions import LedgerError, Transaction

log = logging.getLogger(__name__)

QTY_EPS = 1e-9
PRICE_TOLERANCE = 1.5  # trade price vs market close; beyond this the row is flagged
STALE_DAYS = 7
SHORT_HISTORY_DAYS = 365
PERIODS = [("1W", pd.DateOffset(weeks=1)), ("1M", pd.DateOffset(months=1)), ("3M", pd.DateOffset(months=3)),
           ("6M", pd.DateOffset(months=6)), ("YTD", None), ("1Y", pd.DateOffset(years=1)),
           ("3Y", pd.DateOffset(years=3)), ("5Y", pd.DateOffset(years=5))]


# ---------------------------------------------------------------- results
@dataclass
class Holding:
    ticker: str
    currency: str
    quantity: float
    price_native: Optional[float]
    price_base: float
    value: float
    weight: float
    avg_cost: float           # per unit, base currency, incl. fees
    cost_basis: float         # open position, base currency
    unrealized: float
    realized: float
    dividends: float
    fees: float
    total_pl: float           # realized + unrealized + dividends
    invested: float           # all buys incl. fees
    xirr: Optional[float]
    first_buy: pd.Timestamp
    short_history: bool
    last_price_date: Optional[pd.Timestamp]
    stale: bool


@dataclass
class Contribution:
    ticker: str
    abs_pl: float
    pct_of_portfolio: Optional[float]   # additive share of the portfolio's Dietz return
    own_return: Optional[float]          # holding's own TWR in the period
    start_value: float
    end_value: float


@dataclass
class PeriodResult:
    label: str
    start: pd.Timestamp
    end: pd.Timestamp
    start_value: float
    end_value: float
    net_flows: float
    abs_pl: float
    twr: Optional[float]
    mwr: Optional[float]                 # Modified Dietz, money weighted, not annualised
    benchmark_twr: dict[str, Optional[float]] = field(default_factory=dict)
    contributions: list[Contribution] = field(default_factory=list)


@dataclass
class BenchmarkResult:
    symbol: str
    end_value: float
    total_pl: float
    xirr: Optional[float]
    twr_total: Optional[float]
    twr_annualised: Optional[float]
    clamped: bool


@dataclass
class Report:
    base: str
    as_of: pd.Timestamp
    inception: pd.Timestamp
    days: int
    value: float
    total_bought: float
    total_sold: float
    dividends: float
    fees: float
    net_invested: float
    total_pl: float
    realized: float
    unrealized: float
    simple_return: Optional[float]
    xirr: Optional[float]
    xirr_period: Optional[float]
    twr_total: Optional[float]
    twr_annualised: Optional[float]
    short_history: bool
    drawdown: Optional[Drawdown]
    holdings: list[Holding]
    periods: list[PeriodResult]
    alltime_contributions: list[Contribution]
    benchmarks: list[BenchmarkResult]
    contributions_count: int
    warnings: list[str]
    daily: pd.DataFrame = field(repr=False, default=None)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers
def _xirr(flows: list[tuple[pd.Timestamp, float]]) -> Optional[float]:
    """xirr, but -100% when everything was lost (only outflows from the investor, end value 0)."""
    r = xirr(flows)
    if r is None:
        nz = [a for _, a in flows if abs(a) > 1e-9]
        span = (max(d for d, _ in flows) - min(d for d, _ in flows)).days if flows else 0
        if nz and all(a < 0 for a in nz) and span > 0:
            return -1.0
    return r


def _rate_on(series: pd.Series, day: pd.Timestamp) -> Optional[float]:
    """Last value on or before `day`; first value after it if none before."""
    s = series.dropna()
    if s.empty:
        return None
    before = s.loc[:day]
    if not before.empty:
        return float(before.iloc[-1])
    return float(s.iloc[0])


def _split_factor(splits: pd.Series, trade_date: pd.Timestamp) -> float:
    """Product of split ratios with ex date after the trade date.

    All later splits count, including ones after --as-of, because Yahoo's Close
    history is adjusted for every split up to today.
    """
    if splits is None or splits.empty:
        return 1.0
    s = splits[splits.index > trade_date]
    f = 1.0
    for r in s:
        if r and r > 0:
            f *= float(r)
    return f


class Portfolio:
    def __init__(
        self,
        transactions: list[Transaction],
        provider: Provider,
        base: str = "EUR",
        as_of: Optional[pd.Timestamp] = None,
        benchmarks: Optional[list[str]] = None,
        split_adjust: bool = True,
    ):
        if not transactions:
            raise LedgerError("no transactions")
        self.txs = sorted(transactions, key=lambda t: (t.date, {"buy": 0, "sell": 1, "dividend": 2}[t.type], t.line))
        self.provider = provider
        self.base = base.upper()
        today = pd.Timestamp.today().normalize()
        self.as_of = pd.Timestamp(as_of or today).normalize()
        self.benchmarks = [b.upper() for b in (benchmarks or [])]
        self.split_adjust = split_adjust
        self.warnings: list[str] = []
        self.problems: list[str] = []
        if self.as_of > today:
            self.warnings.append(f"as-of date is in the future, valued as of {today.date()}")
            self.as_of = today
        future = [t for t in self.txs if t.date > self.as_of]
        if future:
            self.warnings.append(f"{len(future)} transaction(s) after {self.as_of.date()} ignored")
            self.txs = [t for t in self.txs if t.date <= self.as_of]
            if not self.txs:
                raise LedgerError("all transactions are after the valuation date")
        self.inception = self.txs[0].date
        self.cal = pd.date_range(self.inception - pd.Timedelta(days=1), self.as_of, freq="D")
        self.fetch_start = self.inception - pd.Timedelta(days=10)
        self._fx_cache: dict[str, pd.Series] = {}
        self._build()

    # -------------------------------------------------------- data
    def _fx(self, ccy: str) -> pd.Series:
        if ccy not in self._fx_cache:
            s = fx_series(self.provider, ccy, self.base, self.fetch_start, self.as_of)
            self._fx_cache[ccy] = s
        return self._fx_cache[ccy]

    def _fx_daily(self, ccy: str) -> pd.Series:
        s = self._fx(ccy)
        full = s.reindex(self.cal.union(s.index)).ffill().bfill()
        return full.reindex(self.cal)

    def _market_ccy(self, ticker: str, txs: list[Transaction]) -> tuple[str, float, bool]:
        try:
            ccy, scale = self.provider.currency(ticker)
            return ccy, scale, True
        except DataError as e:
            ccys = {t.currency for t in txs if t.currency}
            if len(ccys) == 1:
                self.warnings.append(f"{ticker}: {e}; using transaction currency {next(iter(ccys))}")
                return next(iter(ccys)), 1.0, False
            raise

    # -------------------------------------------------------- core build
    def _build(self) -> None:
        tickers = list(dict.fromkeys(t.ticker for t in self.txs))
        cal = self.cal
        self.meta: dict[str, dict] = {}
        qty_df, price_df = {}, {}
        inflow = pd.DataFrame(0.0, index=cal, columns=tickers)
        outflow = pd.DataFrame(0.0, index=cal, columns=tickers)

        for tk in tickers:
            txs = [t for t in self.txs if t.ticker == tk]
            mccy, scale, have_ccy = self._market_ccy(tk, txs)
            splits = self.provider.splits(tk) if self.split_adjust else pd.Series(dtype=float)
            try:
                market = self.provider.close(tk, self.fetch_start, self.as_of) * scale
            except DataError as e:
                self.warnings.append(f"{tk}: {e}")
                market = pd.Series(dtype=float)
            if market.empty:
                self.warnings.append(f"{tk}: no market prices, valued at last transaction price")
            mfx = self._fx_daily(mccy)

            # transaction amounts in base currency
            fallback = pd.Series(index=cal, dtype=float)
            for t in txs:
                f = _split_factor(splits, t.date)
                t.adj_quantity = t.quantity * f
                t.adj_price = t.price / f if f else t.price
                tccy = t.currency or mccy
                if t.fx_rate is not None:
                    rate = t.fx_rate
                    try:
                        mkt = 1.0 if tccy == self.base else _rate_on(self._fx(tccy), t.date)
                    except DataError:
                        mkt = None
                    if mkt and not (0.9 < rate / mkt < 1.1):
                        self.warnings.append(
                            f"line {t.line}: fx_rate {rate:g} differs from the market rate {mkt:.4g} "
                            f"{self.base} per 1 {tccy}; fx_rate must be {self.base} per 1 {tccy}"
                            + (" (it looks inverted)" if 0.9 < rate * mkt < 1.1 else "")
                        )
                elif tccy == self.base:
                    rate = 1.0
                else:
                    r = _rate_on(self._fx(tccy), t.date)
                    if r is None:
                        raise DataError(f"line {t.line}: no FX rate {tccy}->{self.base}")
                    rate = r
                t._rate = rate  # type: ignore[attr-defined]
                if t.type == "buy":
                    t._base_amount = (t.gross + t.fee) * rate  # type: ignore[attr-defined]
                    inflow.at[t.date, tk] += t._base_amount
                    if t.adj_price > 0:
                        fallback.at[t.date] = t.adj_price * rate
                elif t.type == "sell":
                    t._base_amount = (t.gross - t.fee) * rate  # type: ignore[attr-defined]
                    outflow.at[t.date, tk] += t._base_amount
                    if t.adj_price > 0:
                        fallback.at[t.date] = t.adj_price * rate
                else:
                    t._base_amount = t.amount * rate  # type: ignore[attr-defined]
                    outflow.at[t.date, tk] += t._base_amount

            # quantity path + validation
            q = pd.Series(0.0, index=cal)
            running = 0.0
            for t in txs:
                if t.type == "buy":
                    running += t.adj_quantity
                elif t.type == "sell":
                    if t.adj_quantity > running + 1e-6:
                        raise LedgerError(
                            f"line {t.line}: selling {t.quantity:g} {tk} on {t.date.date()} but only "
                            f"{running:g} held (split adjusted)"
                        )
                    running -= t.adj_quantity
                    if abs(running) < 1e-6:
                        running = 0.0
                q.loc[t.date:] = running
            qty_df[tk] = q

            # price path in base currency
            m = market.reindex(cal.union(market.index)).ffill().reindex(cal)
            priced = m * mfx
            fb = fallback.ffill()
            used_fallback = priced.isna() & (q > 0)
            price = priced.where(priced.notna(), fb)
            if used_fallback.any() and not market.empty:
                self.warnings.append(
                    f"{tk}: {int(used_fallback.sum())} day(s) without market price, used transaction price"
                )
            price_df[tk] = price.fillna(0.0)

            if not market.empty:
                # trade prices must be close to that day's market price (in base currency);
                # a big mismatch almost always means a wrong currency column or pence vs pounds
                bad, checked = [], 0
                for t in txs:
                    if t.type == "dividend" or t.adj_price <= 0:
                        continue
                    mp = _rate_on(priced, t.date)
                    if not mp or mp <= 0:
                        continue
                    checked += 1
                    tp = t.adj_price * t._rate
                    if not (1 / PRICE_TOLERANCE < tp / mp < PRICE_TOLERANCE):
                        bad.append((t, tp, mp))
                if bad:
                    t, tp, mp = bad[0]
                    self.problems.append(
                        f"{tk}: {len(bad)} of {checked} trade prices do not match the market price "
                        f"(e.g. line {t.line}: {t.price:g} {t.currency or mccy} = {tp:,.2f} {self.base}, "
                        f"market that day {mp:,.2f} {self.base}, {tp / mp:.3g}x). Usually the currency column "
                        f"is wrong (e.g. CZK written for a EUR price) or pence and pounds are mixed up. "
                        f"Fix those rows; the results below are NOT reliable."
                    )

            last_px_date = market.index.max() if not market.empty else None
            stale = q.iloc[-1] > 0 and (last_px_date is None or (self.as_of - last_px_date).days > STALE_DAYS)
            if stale and last_px_date is not None:
                self.warnings.append(f"{tk}: last market price is from {last_px_date.date()}")
            self.meta[tk] = dict(
                currency=mccy, scale=scale, market=market, last_price_date=last_px_date, stale=stale,
                price_native_last=float(market.iloc[-1]) if not market.empty else None,
            )

        self.qty = pd.DataFrame(qty_df)
        self.price = pd.DataFrame(price_df)
        self.values = (self.qty * self.price).fillna(0.0)
        self.inflow = inflow
        self.outflow = outflow
        self.V = self.values.sum(axis=1)
        self.In = inflow.sum(axis=1)
        self.Out = outflow.sum(axis=1)
        self.r = twr_daily_returns(self.V, self.In, self.Out)
        self.index = twr_index(self.r)
        self.ticker_index = {
            tk: twr_index(twr_daily_returns(self.values[tk], inflow[tk], outflow[tk])) for tk in tickers
        }
        self.tickers = tickers

    # -------------------------------------------------------- cost basis (average cost, base currency)
    def _cost_basis(self) -> dict[str, dict]:
        out = {}
        for tk in self.tickers:
            qty = cost = realized = dividends = fees = invested = 0.0
            for t in (x for x in self.txs if x.ticker == tk):
                if t.type != "dividend":  # dividend amounts are entered net
                    fees += t.fee * t._rate
                if t.type == "buy":
                    qty += t.adj_quantity
                    cost += t._base_amount
                    invested += t._base_amount
                elif t.type == "sell":
                    avg = cost / qty if qty > QTY_EPS else 0.0
                    realized += t._base_amount - avg * t.adj_quantity
                    cost -= avg * t.adj_quantity
                    qty -= t.adj_quantity
                    if qty <= 1e-6:
                        qty, cost = 0.0, 0.0
                else:
                    dividends += t._base_amount
            out[tk] = dict(qty=qty, cost=cost, realized=realized, dividends=dividends, fees=fees, invested=invested)
        return out

    # -------------------------------------------------------- flows
    def _flows(self, ticker: Optional[str], start: Optional[pd.Timestamp], end: pd.Timestamp,
               start_value: float, end_value: float) -> list[tuple[pd.Timestamp, float]]:
        inflow = self.In if ticker is None else self.inflow[ticker]
        outflow = self.Out if ticker is None else self.outflow[ticker]
        lo = start if start is not None else self.cal[0]
        sl = slice(lo + pd.Timedelta(days=1), end)
        net = (outflow.loc[sl] - inflow.loc[sl])
        flows = [(d, float(a)) for d, a in net.items() if abs(a) > 1e-12]
        if start is not None and start_value > 0:
            flows.insert(0, (start, -start_value))
        flows.append((end, end_value))
        return flows

    # -------------------------------------------------------- periods / attribution
    def contributions(self, start: pd.Timestamp, end: pd.Timestamp, dietz_denominator: Optional[float]) -> list[Contribution]:
        res = []
        sl = slice(start + pd.Timedelta(days=1), end)
        for tk in self.tickers:
            v0 = float(self.values[tk].get(start, 0.0)) if start >= self.cal[0] else 0.0
            v1 = float(self.values[tk].loc[end])
            fin = float(self.inflow[tk].loc[sl].sum())
            fout = float(self.outflow[tk].loc[sl].sum())
            if v0 == 0 and v1 == 0 and fin == 0 and fout == 0:
                continue
            pl = v1 - v0 - fin + fout
            own = twr_between(self.ticker_index[tk], start, end) if (v0 > 0 or fin > 0) else None
            pct = pl / dietz_denominator if dietz_denominator and dietz_denominator > 1e-9 else None
            res.append(Contribution(tk, pl, pct, own, v0, v1))
        res.sort(key=lambda c: c.abs_pl, reverse=True)
        return res

    def period(self, label: str, start: pd.Timestamp, end: Optional[pd.Timestamp] = None) -> Optional[PeriodResult]:
        end = end or self.as_of
        if start < self.cal[0]:
            return None
        v0, v1 = float(self.V.loc[start]), float(self.V.loc[end])
        sl = slice(start + pd.Timedelta(days=1), end)
        net_series = self.In.loc[sl] - self.Out.loc[sl]
        flows = [(d, float(a)) for d, a in net_series.items() if abs(a) > 1e-12]
        net = float(net_series.sum())
        mwr, denom = modified_dietz(v0, v1, flows, start, end)
        pr = PeriodResult(
            label=label, start=start, end=end, start_value=v0, end_value=v1, net_flows=net,
            abs_pl=v1 - v0 - net, twr=twr_between(self.index, start, end), mwr=mwr,
        )
        pr.contributions = self.contributions(start, end, denom)
        return pr

    def standard_periods(self) -> list[PeriodResult]:
        out = []
        for label, off in PERIODS:
            start = pd.Timestamp(self.as_of.year - 1, 12, 31) if off is None else self.as_of - off
            p = self.period(label, start)
            if p is not None:
                out.append(p)
        since = self.period("ALL", self.cal[0])  # since inception, always available
        if since is not None:
            out.append(since)
        return out

    # -------------------------------------------------------- benchmark
    def benchmark(self, symbol: str) -> BenchmarkResult:
        ccy, scale = self.provider.currency(symbol)
        px = self.provider.close(symbol, self.fetch_start, self.as_of, adjusted=True) * scale
        if px.empty:
            raise DataError(f"benchmark {symbol}: no price data")
        p = px.reindex(self.cal.union(px.index)).ffill().reindex(self.cal) * self._fx_daily(ccy)
        if p.isna().any():
            first = p.first_valid_index()
            self.warnings.append(f"benchmark {symbol}: no data before {first.date() if first is not None else '?'}; "
                                 "earlier flows bought at the first available price")
            p = p.bfill()
        units = 0.0
        clamped = False
        executed_out = pd.Series(0.0, index=self.cal)
        for d in self.cal:
            pr = float(p.loc[d])
            units += self.In.loc[d] / pr
            want = self.Out.loc[d] / pr
            sold = min(units, want)
            if want - sold > 1e-9:
                clamped = True  # benchmark position too small to fund this withdrawal
            units -= sold
            executed_out.loc[d] = sold * pr
        end_v = float(units * p.iloc[-1])
        net = executed_out - self.In
        flows = [(d, float(a)) for d, a in net.items() if abs(a) > 1e-12] + [(self.as_of, end_v)]
        total_pl = end_v + float(executed_out.sum()) - float(self.In.sum())
        if clamped:
            self.warnings.append(
                f"benchmark {symbol}: some of your withdrawals exceeded the benchmark position and were capped; "
                "comparison is approximate"
            )
        idx = p / p.loc[self.inception]
        twr_tot = float(idx.iloc[-1] - 1.0)
        self._bm_index = getattr(self, "_bm_index", {})
        self._bm_index[symbol] = p
        return BenchmarkResult(symbol, end_v, total_pl, _xirr(flows), twr_tot,
                               annualise(twr_tot, (self.as_of - self.inception).days), clamped)

    # -------------------------------------------------------- report
    def report(self, top_n: int = 5) -> Report:
        cb = self._cost_basis()
        total_v = float(self.V.iloc[-1])
        holdings = []
        for tk in self.tickers:
            c = cb[tk]
            m = self.meta[tk]
            v = float(self.values[tk].iloc[-1])
            unreal = v - c["cost"]
            flows = self._flows(tk, None, self.as_of, 0.0, v)
            holdings.append(Holding(
                ticker=tk, currency=m["currency"], quantity=c["qty"],
                price_native=m["price_native_last"], price_base=float(self.price[tk].iloc[-1]), value=v,
                weight=v / total_v if total_v > 0 else 0.0,
                avg_cost=c["cost"] / c["qty"] if c["qty"] > QTY_EPS else 0.0, cost_basis=c["cost"],
                unrealized=unreal, realized=c["realized"], dividends=c["dividends"], fees=c["fees"],
                total_pl=unreal + c["realized"] + c["dividends"], invested=c["invested"],
                xirr=_xirr(flows),
                first_buy=(fb := min(t.date for t in self.txs if t.ticker == tk)),
                short_history=(self.as_of - fb).days < SHORT_HISTORY_DAYS,
                last_price_date=m["last_price_date"], stale=m["stale"],
            ))
        holdings.sort(key=lambda h: h.value, reverse=True)

        bought, sold_div = float(self.In.sum()), float(self.Out.sum())
        divs = sum(c["dividends"] for c in cb.values())
        sold = sold_div - divs
        total_pl = total_v + sold_div - bought
        days = (self.as_of - self.inception).days
        flows = self._flows(None, None, self.as_of, 0.0, total_v)
        x = _xirr(flows)
        twr_tot = float(self.index.iloc[-1] - 1.0)

        alltime_start = self.cal[0]
        _, denom = modified_dietz(0.0, total_v, [(d, float(a)) for d, a in (self.In - self.Out).items() if abs(a) > 1e-12],
                                  alltime_start, self.as_of)
        alltime = self.contributions(alltime_start, self.as_of, denom)

        periods = self.standard_periods()
        bms = []
        for b in self.benchmarks:
            try:
                bms.append(self.benchmark(b))
            except DataError as e:
                self.warnings.append(f"benchmark {b} skipped: {e}")
        for p in periods:
            for b, idx in getattr(self, "_bm_index", {}).items():
                p.benchmark_twr[b] = twr_between(idx, p.start, p.end)

        daily = pd.DataFrame({"value": self.V, "inflow": self.In, "outflow": self.Out,
                              "daily_return": self.r, "twr_index": self.index})
        return Report(
            base=self.base, as_of=self.as_of, inception=self.inception, days=days, value=total_v,
            total_bought=bought, total_sold=sold, dividends=divs, fees=sum(c["fees"] for c in cb.values()),
            net_invested=bought - sold, total_pl=total_pl,
            realized=sum(c["realized"] for c in cb.values()), unrealized=sum(h.unrealized for h in holdings),
            simple_return=total_pl / bought if bought > 0 else None,
            xirr=x, xirr_period=deannualise(x, days), twr_total=twr_tot,
            twr_annualised=annualise(twr_tot, days), short_history=days < SHORT_HISTORY_DAYS,
            drawdown=max_drawdown(self.index.loc[self.inception:]),
            holdings=holdings, periods=periods, alltime_contributions=alltime, benchmarks=bms,
            contributions_count=sum(1 for t in self.txs if t.type == "buy"),
            warnings=list(dict.fromkeys(self.warnings)), daily=daily, problems=list(self.problems),
        )
