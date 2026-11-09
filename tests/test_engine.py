"""Engine tests with hand computed expectations. No network: StaticProvider only."""
import numpy as np
import pandas as pd
import pytest

from portfolio_tracker.analytics import Portfolio
from portfolio_tracker.data import StaticProvider
from portfolio_tracker.transactions import LedgerError, parse_rows

T = pd.Timestamp


def tx(date, type, ticker, quantity="", price="", fee="", currency="", amount="", fx_rate=""):
    return dict(date=date, type=type, ticker=ticker, quantity=quantity, price=price, fee=fee,
                currency=currency, amount=amount, fx_rate=fx_rate)


def knots(points, start="2023-12-01"):
    """Daily series linearly interpolated between {date: price} knots, flat before the first."""
    s = pd.Series({T(k): float(v) for k, v in points.items()}).sort_index()
    idx = pd.date_range(min(T(start), s.index[0]), s.index[-1], freq="D")
    return s.reindex(idx).interpolate(method="time").bfill()


def flat(price, start="2023-12-01", end="2025-12-31"):
    return pd.Series(float(price), index=pd.date_range(start, end, freq="D"))


def build(rows, prices, ccy=None, base="EUR", as_of="2025-01-01", **kw):
    split_adjust = kw.pop("split_adjust", True)
    prov = StaticProvider(prices, currencies=ccy or {t: "EUR" for t in prices if "=X" not in t},
                          splits=kw.pop("splits", None), adjusted=kw.pop("adjusted", None))
    return Portfolio(parse_rows(rows), prov, base=base, as_of=T(as_of), split_adjust=split_adjust,
                     benchmarks=kw.pop("benchmarks", None))


def holding(rep, tk):
    return next(h for h in rep.holdings if h.ticker == tk)


# 1 ---------------------------------------------------------------- lump sum
def test_lump_sum_doubles_in_one_year():
    px = flat(100, end="2024-12-31")
    px[T("2025-01-01")] = 200.0
    px = px.sort_index()
    p = build([tx("2024-01-02", "buy", "AAA", 10, 100)], {"AAA": px})
    r = p.report()
    assert r.days == 365
    assert r.value == pytest.approx(2000)
    assert r.total_pl == pytest.approx(1000)
    assert r.total_bought == pytest.approx(1000)
    assert r.simple_return == pytest.approx(1.0)
    assert r.xirr == pytest.approx(1.0, abs=1e-6)
    assert r.twr_total == pytest.approx(1.0)
    assert r.twr_annualised == pytest.approx(1.0, abs=1e-9)
    assert r.unrealized == pytest.approx(1000) and r.realized == 0


# 2 ---------------------------------------------------------------- DCA flat
def test_dca_flat_price_no_gain():
    rows = [tx(f"2024-{m:02d}-01", "buy", "AAA", 2, 50) for m in range(1, 7)]
    p = build(rows, {"AAA": flat(50)}, as_of="2024-08-01")
    r = p.report()
    assert r.value == pytest.approx(600)
    assert r.total_pl == pytest.approx(0, abs=1e-9)
    assert r.twr_total == pytest.approx(0, abs=1e-12)
    assert r.xirr == pytest.approx(0, abs=1e-6)
    for per in r.periods:
        assert per.abs_pl == pytest.approx(0, abs=1e-9)
        assert per.twr == pytest.approx(0, abs=1e-12)
    # money was added in the 1M..6M windows yet P/L is zero
    six = next(x for x in r.periods if x.label == "6M")
    assert six.net_flows > 0


# 3 ---------------------------------------------------------------- DCA dip
def test_dca_into_dip_xirr_exceeds_twr():
    px = knots({"2024-01-01": 100, "2024-07-01": 50, "2025-01-01": 100})
    rows = [tx(f"2024-{m:02d}-01", "buy", "AAA", 1, float(px[T(f"2024-{m:02d}-01")])) for m in range(1, 7)]
    p = build(rows, {"AAA": px}, as_of="2025-01-01")
    r = p.report()
    # price round trip; small tolerance: buy-day returns are diluted by the start-of-day inflow convention
    assert r.twr_total == pytest.approx(0, abs=0.02)
    assert r.total_pl > 0
    assert r.xirr > 0.0
    assert r.xirr > r.twr_annualised  # bought the dip


def test_dca_into_rising_then_crash_xirr_below_twr():
    px = knots({"2024-01-01": 100, "2024-06-01": 200, "2025-01-01": 100})
    rows = [tx("2024-01-01", "buy", "AAA", 1, 100), tx("2024-06-01", "buy", "AAA", 10, 200)]
    r = build(rows, {"AAA": px}, as_of="2025-01-01").report()
    assert r.twr_total == pytest.approx(0, abs=0.02)
    assert r.total_pl < 0
    assert r.xirr < r.twr_annualised


# 4 ---------------------------------------------------------------- fees
def test_fees_and_average_cost_realized():
    px = knots({"2024-01-01": 100, "2024-03-01": 120, "2024-06-01": 130})
    rows = [tx("2024-01-02", "buy", "AAA", 10, 100, fee=5),
            tx("2024-03-01", "sell", "AAA", 4, 120, fee=3)]
    r = build(rows, {"AAA": px}, as_of="2024-06-01").report()
    h = holding(r, "AAA")
    assert r.total_bought == pytest.approx(1005)          # fee in cost
    assert r.total_sold == pytest.approx(477)             # fee out of proceeds
    assert h.avg_cost == pytest.approx(100.5)
    assert h.realized == pytest.approx(477 - 4 * 100.5)   # 75
    assert h.quantity == pytest.approx(6)
    assert h.cost_basis == pytest.approx(603)
    assert h.value == pytest.approx(780)
    assert h.unrealized == pytest.approx(780 - 603)
    assert r.total_pl == pytest.approx(780 + 477 - 1005)
    assert h.fees == pytest.approx(8)
    assert r.fees == pytest.approx(8)


# 5 ---------------------------------------------------------------- oversell / liquidation
def test_sell_more_than_held_raises():
    rows = [tx("2024-01-02", "buy", "AAA", 5, 100), tx("2024-02-01", "sell", "AAA", 5.5, 100)]
    with pytest.raises(LedgerError):
        build(rows, {"AAA": flat(100)}, as_of="2024-06-01")


def test_sell_before_any_buy_raises():
    rows = [tx("2024-01-02", "sell", "AAA", 1, 100), tx("2024-02-01", "buy", "AAA", 1, 100)]
    with pytest.raises(LedgerError):
        build(rows, {"AAA": flat(100)}, as_of="2024-06-01")


def test_full_liquidation_then_rebuy():
    px = knots({"2024-01-01": 100, "2024-03-01": 110, "2024-05-01": 120, "2024-06-01": 130})
    rows = [tx("2024-01-02", "buy", "AAA", 10, 100),
            tx("2024-03-01", "sell", "AAA", 10, 110),
            tx("2024-05-01", "buy", "AAA", 5, 120)]
    r = build(rows, {"AAA": px}, as_of="2024-06-01").report()
    h = holding(r, "AAA")
    assert h.realized == pytest.approx(100)
    assert h.quantity == pytest.approx(5)
    assert h.avg_cost == pytest.approx(120)  # old basis does not leak into new lot
    assert h.unrealized == pytest.approx(50)
    assert r.total_pl == pytest.approx(150)
    assert np.isfinite(r.twr_total)
    assert r.xirr is not None and r.xirr > 0


def test_full_liquidation_ends_flat():
    rows = [tx("2024-01-02", "buy", "AAA", 10, 100), tx("2024-03-01", "sell", "AAA", 10, 110)]
    r = build(rows, {"AAA": knots({"2024-01-01": 100, "2024-03-01": 110, "2024-06-01": 150})},
              as_of="2024-06-01").report()
    assert r.value == 0
    assert r.total_pl == pytest.approx(100)
    assert holding(r, "AAA").weight == 0
    assert holding(r, "AAA").avg_cost == 0


# 6 ---------------------------------------------------------------- FX
def _fx_prov():
    fx = flat(0.9)
    fx[T("2024-06-01"):] = 1.0
    return {"AAPL": flat(120, start="2023-12-01"), "USDEUR=X": fx}


def test_multicurrency_cost_at_trade_fx_value_at_today_fx():
    prices = _fx_prov()
    r = build([tx("2024-01-02", "buy", "AAPL", 10, 100, fee=10)], prices, ccy={"AAPL": "USD"},
              as_of="2024-08-01").report()
    h = holding(r, "AAPL")
    assert h.currency == "USD"
    assert h.cost_basis == pytest.approx((1000 + 10) * 0.9)
    assert h.value == pytest.approx(10 * 120 * 1.0)
    assert h.unrealized == pytest.approx(1200 - 909)
    assert r.total_pl == pytest.approx(1200 - 909)
    assert r.fees == pytest.approx(10 * 0.9)


def test_multicurrency_inverse_fx_series():
    prices = {"AAPL": flat(100), "EURUSD=X": flat(2.0)}  # 1 EUR = 2 USD -> 1 USD = 0.5 EUR
    r = build([tx("2024-01-02", "buy", "AAPL", 10, 100)], prices, ccy={"AAPL": "USD"}, as_of="2024-03-01").report()
    assert r.total_bought == pytest.approx(500)
    assert r.value == pytest.approx(500)


def test_fx_rate_override_honoured():
    prices = _fx_prov()
    r = build([tx("2024-01-02", "buy", "AAPL", 10, 100, fx_rate=0.8)], prices, ccy={"AAPL": "USD"},
              as_of="2024-08-01").report()
    h = holding(r, "AAPL")
    assert h.cost_basis == pytest.approx(800)   # 1000 * 0.8 not 0.9
    assert r.total_bought == pytest.approx(800)
    assert h.value == pytest.approx(1200)
    assert r.total_pl == pytest.approx(400)


def test_fx_rate_override_on_sell_and_dividend():
    prices = _fx_prov()
    rows = [tx("2024-01-02", "buy", "AAPL", 10, 100),
            tx("2024-03-01", "sell", "AAPL", 5, 110, fx_rate=0.95),
            tx("2024-03-02", "dividend", "AAPL", amount=10, currency="USD", fx_rate=0.5)]
    r = build(rows, prices, ccy={"AAPL": "USD"}, as_of="2024-08-01").report()
    h = holding(r, "AAPL")
    assert h.realized == pytest.approx(5 * 110 * 0.95 - 500 * 0.9)
    assert h.dividends == pytest.approx(5.0)


def test_tx_currency_differs_from_market_currency_in_base():
    # tx in EUR for a USD-quoted ticker with base EUR: no FX applied to the cost
    prices = _fx_prov()
    r = build([tx("2024-01-02", "buy", "AAPL", 10, 90, currency="EUR")], prices, ccy={"AAPL": "USD"},
              as_of="2024-08-01").report()
    assert r.total_bought == pytest.approx(900)


# 7 ---------------------------------------------------------------- GBp
def test_gbp_pence_ticker():
    prices = {"VUSA.L": flat(8000), "GBPEUR=X": flat(1.2)}  # 8000p = 80 GBP
    r = build([tx("2024-01-02", "buy", "VUSA.L", 10, 75)], prices, ccy={"VUSA.L": "GBp"},
              as_of="2024-03-01").report()
    h = holding(r, "VUSA.L")
    assert h.price_native == pytest.approx(80)          # scaled to GBP
    assert h.price_base == pytest.approx(96)
    assert h.value == pytest.approx(960)
    assert h.cost_basis == pytest.approx(900)           # 75 GBP * 10 * 1.2, not pence
    assert r.total_pl == pytest.approx(60)


def test_gbp_pence_ticker_gbx_alias_and_gbp_base():
    prices = {"VUSA.L": flat(8000)}
    r = build([tx("2024-01-02", "buy", "VUSA.L", 10, 75, fee=2)], prices, ccy={"VUSA.L": "GBX"},
              base="GBP", as_of="2024-03-01").report()
    assert r.value == pytest.approx(800)
    assert r.total_pl == pytest.approx(800 - 752)


def test_gbp_pence_ticker_explicit_gbp_currency_in_tx():
    prices = {"VUSA.L": flat(8000), "GBPEUR=X": flat(1.2)}
    r = build([tx("2024-01-02", "buy", "VUSA.L", 10, 75, currency="GBP")], prices, ccy={"VUSA.L": "GBp"},
              as_of="2024-03-01").report()
    assert r.value == pytest.approx(960) and r.total_pl == pytest.approx(60)


# 8 ---------------------------------------------------------------- split
def _split_setup():
    px = flat(100)                                  # Yahoo style: already split adjusted
    px[T("2024-07-01"):] = 110.0
    return {"AAA": px}, {"AAA": pd.Series({T("2024-06-03"): 4.0})}


def test_split_4_for_1_adjusts_quantity_and_value():
    prices, splits = _split_setup()
    r = build([tx("2024-03-01", "buy", "AAA", 10, 400)], prices, splits=splits, as_of="2024-08-01").report()
    h = holding(r, "AAA")
    assert h.quantity == pytest.approx(40)
    assert h.value == pytest.approx(4400)
    assert h.cost_basis == pytest.approx(4000)
    assert h.avg_cost == pytest.approx(100)
    assert r.total_pl == pytest.approx(400)
    assert r.twr_total == pytest.approx(0.10)       # no phantom -75% on split day


def test_split_buy_after_split_not_adjusted_and_sell_split_adjusted():
    prices, splits = _split_setup()
    rows = [tx("2024-03-01", "buy", "AAA", 10, 400),
            tx("2024-07-02", "buy", "AAA", 10, 100),
            tx("2024-07-03", "sell", "AAA", 50, 110)]
    h = holding(build(rows, prices, splits=splits, as_of="2024-08-01").report(), "AAA")
    assert h.quantity == pytest.approx(0)


def test_split_ignored_when_disabled():
    prices, splits = _split_setup()
    r = build([tx("2024-03-01", "buy", "AAA", 10, 400)], prices, splits=splits, as_of="2024-08-01",
              split_adjust=False).report()
    h = holding(r, "AAA")
    assert h.quantity == pytest.approx(10)
    assert h.value == pytest.approx(1100)
    assert h.cost_basis == pytest.approx(4000)


def test_split_after_as_of_still_applied():
    # Yahoo's Close history is adjusted for every split up to today, including splits
    # after --as-of, so quantities must be expressed in the same post-split units.
    prices, splits = _split_setup()
    r = build([tx("2024-03-01", "buy", "AAA", 10, 400)], prices, splits=splits, as_of="2024-05-01").report()
    h = holding(r, "AAA")
    assert h.quantity == pytest.approx(40)       # post-split units
    assert h.value == pytest.approx(4000)        # 40 x adjusted 100 = 10 x nominal 400
    assert r.total_pl == pytest.approx(0)


def test_reverse_split():
    px = flat(100)  # adjusted for 1-for-10 reverse: pre-split nominal price 10 -> 100 adjusted
    r = build([tx("2024-03-01", "buy", "AAA", 100, 10)], {"AAA": px},
              splits={"AAA": pd.Series({T("2024-06-03"): 0.1})}, as_of="2024-08-01").report()
    h = holding(r, "AAA")
    assert h.quantity == pytest.approx(10)
    assert h.value == pytest.approx(1000)
    assert r.total_pl == pytest.approx(0, abs=1e-9)


# 9 ---------------------------------------------------------------- dividends
def test_dividends_counted():
    rows = [tx("2024-01-02", "buy", "AAA", 10, 100), tx("2024-04-01", "dividend", "AAA", amount=20)]
    r = build(rows, {"AAA": flat(100)}, as_of="2024-07-01").report()
    assert r.dividends == pytest.approx(20)
    assert r.total_pl == pytest.approx(20)
    assert r.realized == 0 and r.unrealized == pytest.approx(0)
    assert holding(r, "AAA").dividends == pytest.approx(20)
    assert holding(r, "AAA").total_pl == pytest.approx(20)
    assert r.total_sold == pytest.approx(0)          # dividends are not sale proceeds
    assert r.twr_total == pytest.approx(0.02)
    assert r.xirr > 0
    assert float(r.daily["outflow"].sum()) == pytest.approx(20)


def test_dividend_per_share_form_and_fx():
    prices = {"AAPL": flat(100), "USDEUR=X": flat(0.9)}
    rows = [tx("2024-01-02", "buy", "AAPL", 10, 100),
            tx("2024-04-01", "dividend", "AAPL", quantity=10, price=0.5, currency="USD")]
    r = build(rows, prices, ccy={"AAPL": "USD"}, as_of="2024-07-01").report()
    assert r.dividends == pytest.approx(4.5)


def test_dividend_period_pl_includes_dividend():
    rows = [tx("2024-01-02", "buy", "AAA", 10, 100), tx("2024-04-01", "dividend", "AAA", amount=20)]
    r = build(rows, {"AAA": flat(100)}, as_of="2024-07-01").report()
    six = next(p for p in r.periods if p.label == "6M")
    assert six.abs_pl == pytest.approx(20)


# 10 --------------------------------------------------------------- invariants
@pytest.fixture(scope="module")
def random_report():
    rng = np.random.default_rng(42)
    idx = pd.date_range("2023-06-01", "2025-06-30", freq="D")
    def walk(p0, vol):
        return pd.Series(p0 * np.exp(np.cumsum(rng.normal(0.0003, vol, len(idx)))), index=idx)
    prices = {"AAA": walk(100, 0.01), "BBB": walk(50, 0.02), "CCC": walk(200, 0.015),
              "USDEUR=X": walk(0.9, 0.003), "GBPEUR=X": walk(1.15, 0.003)}
    ccy = {"AAA": "EUR", "BBB": "USD", "CCC": "GBp"}
    prices["CCC"] = prices["CCC"] * 100  # pence
    def pxd(tk, d):
        return float(prices[tk][T(d)])
    rows = []
    for i, m in enumerate(pd.date_range("2024-01-05", "2025-04-05", freq="MS")):
        d = (m + pd.Timedelta(days=4)).strftime("%Y-%m-%d")
        rows.append(tx(d, "buy", "AAA", 3 + i % 3, round(pxd("AAA", d), 2), fee=1))
        if i % 2 == 0:
            rows.append(tx(d, "buy", "BBB", 5, round(pxd("BBB", d), 2), currency="USD", fee=0.5))
        if i % 3 == 0:
            rows.append(tx(d, "buy", "CCC", 2, round(pxd("CCC", d) / 100, 2)))
    rows += [tx("2024-09-10", "sell", "AAA", 4, 105, fee=1), tx("2025-01-15", "sell", "BBB", 10, 60, currency="USD"),
             tx("2024-06-14", "dividend", "AAA", amount=7.5), tx("2025-02-14", "dividend", "BBB", amount=3, currency="USD"),
             tx("2025-03-02", "sell", "CCC", 1, 2.0)]
    return build(rows, prices, ccy=ccy, as_of="2025-06-20").report()


def test_invariant_holdings_pl_sum(random_report):
    r = random_report
    assert sum(h.total_pl for h in r.holdings) == pytest.approx(r.total_pl, abs=1e-6)
    assert sum(c.abs_pl for c in r.alltime_contributions) == pytest.approx(r.total_pl, abs=1e-6)


def test_invariant_total_pl_identity(random_report):
    r = random_report
    assert r.total_pl == pytest.approx(r.value + r.total_sold + r.dividends - r.total_bought, abs=1e-6)
    assert r.realized + r.unrealized + r.dividends == pytest.approx(r.total_pl, abs=1e-6)
    assert r.net_invested == pytest.approx(r.total_bought - r.total_sold)
    assert sum(h.weight for h in r.holdings) == pytest.approx(1.0)


def test_invariant_period_contributions(random_report):
    r = random_report
    assert len(r.periods) >= 5
    for p in r.periods:
        assert sum(c.abs_pl for c in p.contributions) == pytest.approx(p.abs_pl, abs=1e-6), p.label
        assert p.abs_pl == pytest.approx(p.end_value - p.start_value - p.net_flows, abs=1e-6)
        if p.mwr is not None:
            assert sum(c.pct_of_portfolio for c in p.contributions) == pytest.approx(p.mwr, abs=1e-9), p.label


def test_invariant_twr_index_matches_daily_returns(random_report):
    r = random_report
    assert (1 + r.daily["daily_return"]).prod() - 1 == pytest.approx(r.twr_total)
    assert r.xirr is not None and np.isfinite(r.xirr)


# 11 --------------------------------------------------------------- benchmark
def test_benchmark_identical_to_held_asset():
    px = knots({"2024-01-01": 100, "2024-04-01": 130, "2024-08-01": 90, "2025-01-01": 140})
    rows = [tx("2024-01-02", "buy", "AAA", 10, round(px[T("2024-01-02")], 10)),
            tx("2024-04-01", "buy", "AAA", 5, 130),
            tx("2024-08-01", "sell", "AAA", 6, 90),
            tx("2024-10-01", "buy", "AAA", 2, round(px[T("2024-10-01")], 10))]
    p = build(rows, {"AAA": px}, adjusted={"AAA": px}, benchmarks=["AAA"], as_of="2025-01-01")
    r = p.report()
    b = r.benchmarks[0]
    assert b.end_value == pytest.approx(r.value)
    assert b.total_pl == pytest.approx(r.total_pl)
    assert b.xirr == pytest.approx(r.xirr, abs=1e-8)
    assert not b.clamped
    for per in r.periods:
        assert per.benchmark_twr["AAA"] == pytest.approx(per.twr, abs=0.01)  # buy-day dilution convention


def test_benchmark_uses_adjusted_prices():
    px = flat(100)
    adj = flat(100)
    adj[T("2024-07-01"):] = 110.0
    rows = [tx("2024-01-02", "buy", "AAA", 10, 100)]
    r = build(rows, {"AAA": px, "BM": px}, adjusted={"BM": adj}, benchmarks=["BM"], ccy={"AAA": "EUR", "BM": "EUR"},
              as_of="2024-12-01").report()
    assert r.benchmarks[0].end_value == pytest.approx(1100)
    assert r.benchmarks[0].total_pl == pytest.approx(100)


# 12 --------------------------------------------------------------- dates
def test_weekend_transaction_dates():
    # 2024-03-02 Sat, 2024-03-03 Sun; prices only on weekdays
    idx = pd.bdate_range("2024-02-01", "2024-04-30")
    px = pd.Series(100.0, index=idx)
    px[T("2024-03-04"):] = 102.0
    rows = [tx("2024-03-02", "buy", "AAA", 10, 100), tx("2024-03-10", "sell", "AAA", 4, 102)]
    r = build(rows, {"AAA": px}, as_of="2024-03-17").report()  # as_of is a Sunday
    h = holding(r, "AAA")
    assert h.quantity == pytest.approx(6)
    assert h.value == pytest.approx(612)
    assert r.total_pl == pytest.approx(612 + 408 - 1000)
    assert r.inception == T("2024-03-02")
    assert np.isfinite(r.twr_total)


def test_weekend_fx_uses_previous_close():
    idx = pd.bdate_range("2023-12-01", "2024-06-30")
    fx = pd.Series(0.9, index=idx)
    fx[T("2024-03-04"):] = 1.0
    r = build([tx("2024-03-03", "buy", "AAPL", 10, 100)], {"AAPL": flat(100), "USDEUR=X": fx},
              ccy={"AAPL": "USD"}, as_of="2024-03-08").report()
    assert r.total_bought == pytest.approx(900)  # Friday's 0.9, not Monday's 1.0


def test_future_transactions_ignored_with_warning():
    rows = [tx("2024-03-01", "buy", "AAA", 10, 100), tx("2024-09-01", "buy", "AAA", 100, 100),
            tx("2024-10-01", "sell", "AAA", 110, 100)]
    p = build(rows, {"AAA": flat(100)}, as_of="2024-06-14")
    r = p.report()
    assert any("2 transaction" in w and "ignored" in w for w in r.warnings)
    assert holding(r, "AAA").quantity == pytest.approx(10)
    assert r.total_bought == pytest.approx(1000)
    assert r.as_of == T("2024-06-14")
    assert len(r.daily) == len(pd.date_range("2024-02-29", "2024-06-14"))


def test_all_transactions_after_as_of_raises():
    with pytest.raises(LedgerError):
        build([tx("2024-09-01", "buy", "AAA", 1, 100)], {"AAA": flat(100)}, as_of="2024-06-14")


def test_as_of_before_market_data_end_uses_as_of_price():
    px = knots({"2024-01-01": 100, "2024-06-01": 120, "2024-12-01": 200})
    r = build([tx("2024-01-02", "buy", "AAA", 10, 100)], {"AAA": px}, as_of="2024-06-01").report()
    assert r.value == pytest.approx(1200)
    assert not any("last market price" in w for w in r.warnings)


def test_transaction_on_as_of_date_included():
    r = build([tx("2024-06-01", "buy", "AAA", 10, 100)], {"AAA": flat(100)}, as_of="2024-06-01")
    assert r.report().value == pytest.approx(1000)


def test_unsorted_rows_same_day_buy_before_sell():
    rows = [tx("2024-03-01", "sell", "AAA", 10, 100), tx("2024-03-01", "buy", "AAA", 10, 100)]
    r = build(rows, {"AAA": flat(100)}, as_of="2024-04-01").report()
    assert r.value == 0 and r.total_pl == pytest.approx(0)


def test_stale_price_warning():
    px = flat(100, end="2024-03-01")
    r = build([tx("2024-01-02", "buy", "AAA", 10, 100)], {"AAA": px}, as_of="2024-06-01").report()
    assert any("last market price" in w for w in r.warnings)
    assert holding(r, "AAA").stale
    assert r.value == pytest.approx(1000)


# 13 --------------------------------------------------------------- no market data
def test_no_market_data_valued_at_transaction_price_with_warning():
    p = build([tx("2024-01-02", "buy", "ZZZ", 10, 100, currency="EUR"),
               tx("2024-02-01", "buy", "ZZZ", 10, 110, currency="EUR")],
              {}, ccy={}, as_of="2024-06-01")
    r = p.report()
    h = holding(r, "ZZZ")
    assert any("ZZZ" in w and "no market prices" in w for w in r.warnings)
    assert h.value == pytest.approx(20 * 110)      # last transaction price
    assert h.quantity == pytest.approx(20)
    assert h.price_native is None
    assert r.total_pl == pytest.approx(2200 - 2100)
    assert np.isfinite(r.twr_total)


def test_no_market_data_no_currency_anywhere_raises():
    from portfolio_tracker.data import DataError
    with pytest.raises(DataError):
        build([tx("2024-01-02", "buy", "ZZZ", 10, 100)], {}, ccy={}, as_of="2024-06-01")


def test_no_market_data_foreign_currency_uses_fx():
    r = build([tx("2024-01-02", "buy", "ZZZ", 10, 100, currency="USD")], {"USDEUR=X": flat(0.9)}, ccy={},
              as_of="2024-06-01").report()
    assert r.value == pytest.approx(900)
