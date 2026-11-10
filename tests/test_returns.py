from datetime import date, timedelta

import math
import pandas as pd
import pytest

from portfolio_tracker.returns import (
    annualise, deannualise, max_drawdown, modified_dietz, twr_daily_returns,
    twr_index, xirr,
)


def brute_xirr(flows, lo=-0.9999, hi=1000.0):
    """Independent oracle: bisection on NPV with act/365."""
    d0 = min(d for d, _ in flows)
    pts = [((d - d0).days / 365.0, a) for d, a in flows]

    def npv(r):
        return sum(a * math.exp(-t * math.log1p(r)) for t, a in pts)
    flo, fhi = npv(lo), npv(hi)
    assert flo * fhi < 0
    for _ in range(300):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if flo * fm <= 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2


EXCEL = [
    (date(2008, 1, 1), -10000), (date(2008, 3, 1), 2750), (date(2008, 10, 30), 4250),
    (date(2009, 2, 15), 3250), (date(2009, 4, 1), 2750),
]


def test_xirr_excel_example():
    assert xirr(EXCEL) == pytest.approx(0.373362535, abs=1e-6)


def test_xirr_excel_matches_bruteforce():
    assert xirr(EXCEL) == pytest.approx(brute_xirr(EXCEL), abs=1e-8)


def test_xirr_unsorted():
    assert xirr(list(reversed(EXCEL))) == pytest.approx(0.373362535, abs=1e-6)
    sh = [EXCEL[2], EXCEL[0], EXCEL[4], EXCEL[1], EXCEL[3]]
    assert xirr(sh) == pytest.approx(0.373362535, abs=1e-6)


def test_xirr_doubling_365_days():
    f = [(date(2023, 1, 1), -100), (date(2024, 1, 1), 200)]  # 365 days
    assert xirr(f) == pytest.approx(1.0, abs=1e-9)


def test_xirr_halving_365_days():
    f = [(date(2023, 1, 1), -100), (date(2024, 1, 1), 50)]
    assert xirr(f) == pytest.approx(-0.5, abs=1e-9)


def test_xirr_total_loss():
    f = [(date(2023, 1, 1), -100), (date(2024, 1, 1), 0.0)]
    # zero positive flow -> undefined (None) or approx -1
    r = xirr(f)
    assert r is None or r == pytest.approx(-1.0, abs=1e-6)


def test_xirr_near_total_loss():
    f = [(date(2023, 1, 1), -100), (date(2024, 1, 1), 0.01)]
    assert xirr(f) == pytest.approx(-0.9999, abs=1e-6)
    assert xirr(f) == pytest.approx(brute_xirr(f), abs=1e-8)


def test_xirr_negative_return_multi():
    f = [(date(2022, 1, 1), -1000), (date(2022, 7, 1), -500), (date(2023, 6, 1), 1200)]
    assert xirr(f) == pytest.approx(brute_xirr(f), abs=1e-8)
    assert xirr(f) < 0


def test_xirr_very_high_return():
    f = [(date(2024, 1, 1), -100), (date(2024, 1, 31), 300)]  # 30 days, 3x
    exp = 3.0 ** (365 / 30) - 1
    assert xirr(f) == pytest.approx(exp, rel=1e-6)


def test_xirr_extreme_return_one_day():
    f = [(date(2024, 1, 1), -100), (date(2024, 1, 2), 200)]
    exp = 2.0 ** 365 - 1
    r = xirr(f)
    assert r is not None
    assert r == pytest.approx(exp, rel=1e-6)


def test_xirr_same_day_netted():
    a = [(date(2023, 1, 1), -60), (date(2023, 1, 1), -40), (date(2024, 1, 1), 150),
         (date(2024, 1, 1), 50)]
    b = [(date(2023, 1, 1), -100), (date(2024, 1, 1), 200)]
    assert xirr(a) == pytest.approx(xirr(b), abs=1e-12)
    assert xirr(a) == pytest.approx(1.0, abs=1e-9)


def test_xirr_same_day_net_cancels_to_none():
    f = [(date(2023, 1, 1), -100), (date(2023, 1, 1), 100)]
    assert xirr(f) is None


def test_xirr_all_negative_none():
    assert xirr([(date(2023, 1, 1), -100), (date(2023, 6, 1), -50)]) is None


def test_xirr_all_positive_none():
    assert xirr([(date(2023, 1, 1), 100), (date(2023, 6, 1), 50)]) is None


def test_xirr_single_flow_none():
    assert xirr([(date(2023, 1, 1), -100)]) is None
    assert xirr([]) is None


def test_xirr_zero_return():
    f = [(date(2023, 1, 1), -100), (date(2023, 9, 1), 100)]
    assert xirr(f) == pytest.approx(0.0, abs=1e-9)


def test_xirr_dca_120_months():
    start = date(2014, 1, 1)
    flows = []
    for i in range(120):
        y, m = divmod(i, 12)
        flows.append((date(2014 + y, 1 + m, 1), -500.0))
    end = date(2024, 1, 1)
    flows.append((end, 120 * 500 * 1.6))
    got = xirr(flows)
    assert got == pytest.approx(brute_xirr(flows), abs=1e-8)
    # NPV at the result must vanish
    d0 = start
    npv = sum(a / (1 + got) ** ((d - d0).days / 365.0) for d, a in flows)
    assert abs(npv) < 1e-4


def test_xirr_dca_flat_value_is_about_zero():
    flows = [(date(2020, 1, 1) + timedelta(days=30 * i), -100.0) for i in range(36)]
    flows.append((date(2020, 1, 1) + timedelta(days=30 * 36), 3600.0))
    assert xirr(flows) == pytest.approx(0.0, abs=1e-8)


def test_xirr_bad_guess_still_converges():
    assert xirr(EXCEL, guess=-0.9) == pytest.approx(0.373362535, abs=1e-6)
    assert xirr(EXCEL, guess=50.0) == pytest.approx(0.373362535, abs=1e-6)


def test_xirr_with_timestamps_and_intraday():
    f = [(pd.Timestamp("2023-01-01 09:30"), -100), (pd.Timestamp("2023-01-01 16:00"), -100),
         (pd.Timestamp("2024-01-01"), 400)]
    assert xirr(f) == pytest.approx(1.0, abs=1e-9)


# ---- annualise ----

@pytest.mark.parametrize("r,days", [(0.1, 100), (-0.3, 400), (2.0, 30), (0.0, 10), (-0.99, 365)])
def test_annualise_roundtrip(r, days):
    a = annualise(r, days)
    assert deannualise(a, days) == pytest.approx(r, abs=1e-9)


def test_annualise_known():
    assert annualise(0.21, 730) == pytest.approx(0.1, abs=1e-12)
    assert annualise(0.5, 365) == pytest.approx(0.5)
    assert deannualise(0.1, 730) == pytest.approx(0.21)


def test_annualise_total_loss():
    assert annualise(-1.0, 100) == -1.0
    assert annualise(-1.5, 100) == -1.0


def test_annualise_none_and_bad_days():
    assert annualise(None, 10) is None
    assert annualise(0.1, 0) is None
    assert annualise(0.1, -5) is None
    assert deannualise(None, 10) is None


def test_deannualise_minus_100():
    assert deannualise(-1.0, 100) == pytest.approx(-1.0)


# ---- TWR ----

def _s(vals):
    return pd.Series(vals, index=pd.date_range("2024-01-01", periods=len(vals)), dtype=float)


def test_twr_flat_prices_large_inflows_zero_return():
    # price flat at 10; buy more each day: value = units*10
    values = _s([1000, 11000, 111000, 111000])
    inflows = _s([1000, 10000, 100000, 0])
    out = _s([0, 0, 0, 0])
    r = twr_daily_returns(values, inflows, out)
    assert r.abs().max() < 1e-12
    assert twr_index(r).iloc[-1] == pytest.approx(1.0)


def test_twr_flat_prices_with_outflows_zero_return():
    values = _s([1000, 900, 400, 400])
    inflows = _s([1000, 0, 0, 0])
    out = _s([0, 100, 500, 0])
    r = twr_daily_returns(values, inflows, out)
    assert r.abs().max() < 1e-12


def test_twr_known_return():
    # day1 buy 100; day2 value 110 (+10%); day3 old 110 gains 10% -> 121, plus 100 bought at the close = 221
    values = _s([100, 110, 221])
    inflows = _s([100, 0, 100])
    r = twr_daily_returns(values, inflows, _s([0, 0, 0]))
    assert list(r) == pytest.approx([0.0, 0.10, 0.10])
    assert twr_index(r).iloc[-1] == pytest.approx(1.21)


def test_twr_full_liquidation_day():
    # held 100 at close of day0, day1 price +20%, sold everything at that price
    values = _s([100, 0, 0])
    out = _s([0, 120, 0])
    r = twr_daily_returns(values, _s([0, 0, 0]), out)
    assert r.iloc[1] == pytest.approx(0.2)
    assert r.iloc[2] == 0.0
    assert r.iloc[0] == 0.0
    assert not r.isna().any()


def test_twr_zero_base_gives_zero_not_nan():
    r = twr_daily_returns(_s([0, 0, 0]), _s([0, 0, 0]), _s([0, 0, 0]))
    assert (r == 0).all()


def test_twr_reentry_after_liquidation():
    values = _s([100, 0, 50, 55])
    inflows = _s([100, 0, 50, 0])
    out = _s([0, 100, 0, 0])
    r = twr_daily_returns(values, inflows, out)
    assert list(r) == pytest.approx([0.0, 0.0, 0.0, 0.1])


def test_twr_index_equals_product():
    r = pd.Series([0.01, -0.02, 0.03, 0.0, -0.5, 1.0], index=pd.date_range("2024-01-01", periods=6))
    idx = twr_index(r)
    prod = 1.0
    for i, x in enumerate(r):
        prod *= 1 + x
        assert idx.iloc[i] == pytest.approx(prod)
    assert idx.iloc[-1] == pytest.approx(math.prod(1 + x for x in r))


# ---- Modified Dietz ----

def test_modified_dietz_textbook():
    start = pd.Timestamp("2024-01-01")
    end = pd.Timestamp("2024-01-31")  # T=30
    flows = [(pd.Timestamp("2024-01-16"), 50.0)]  # day 15
    ret, denom = modified_dietz(100, 160, flows, start, end)
    assert denom == pytest.approx(125.0)
    assert ret == pytest.approx(0.08)


def test_modified_dietz_no_flows():
    ret, denom = modified_dietz(100, 110, [], pd.Timestamp("2024-01-01"), pd.Timestamp("2024-02-01"))
    assert ret == pytest.approx(0.10)
    assert denom == 100


def test_modified_dietz_income_counts_as_gain():
    ret, _ = modified_dietz(100, 100, [], pd.Timestamp("2024-01-01"), pd.Timestamp("2024-02-01"), income=5)
    assert ret == pytest.approx(0.05)


def test_modified_dietz_flow_on_last_day_has_zero_weight():
    s, e = pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-11")
    ret, denom = modified_dietz(100, 160, [(e, 50.0)], s, e)
    assert denom == pytest.approx(100.0)
    assert ret == pytest.approx(0.10)


def test_modified_dietz_withdrawal():
    s, e = pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-11")
    # withdraw 20 at day 5: weight 0.5 -> denom 100-10=90; gain = 90-100+20 = 10
    ret, denom = modified_dietz(100, 90, [(pd.Timestamp("2024-01-06"), -20.0)], s, e)
    assert denom == pytest.approx(90.0)
    assert ret == pytest.approx(10 / 90)


def test_modified_dietz_zero_denominator_none():
    s, e = pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-11")
    ret, denom = modified_dietz(0, 0, [], s, e)
    assert ret is None and denom == 0
    # weighted flow exactly cancels the start value
    ret, denom = modified_dietz(100, 0, [(pd.Timestamp("2024-01-06"), -200.0)], s, e)
    assert ret is None
    assert denom == pytest.approx(0.0)


def test_modified_dietz_bad_period_none():
    s = pd.Timestamp("2024-01-01")
    assert modified_dietz(100, 110, [], s, s)[0] is None
    assert modified_dietz(100, 110, [], s, s - pd.Timedelta(days=1))[0] is None


# ---- Max drawdown ----

def _ix(vals):
    return pd.Series(vals, index=pd.date_range("2024-01-01", periods=len(vals)), dtype=float)


def test_max_drawdown_known_recovered():
    s = _ix([100, 120, 90, 110, 130, 80, 100, 135])
    dd = max_drawdown(s)
    assert dd.depth == pytest.approx(80 / 130 - 1)
    assert dd.peak == s.index[4]
    assert dd.trough == s.index[5]
    assert dd.recovered == s.index[7]


def test_max_drawdown_never_recovered():
    s = _ix([100, 120, 90, 110, 130, 80, 100])
    dd = max_drawdown(s)
    assert dd.depth == pytest.approx(80 / 130 - 1)
    assert dd.recovered is None


def test_max_drawdown_monotonic_up():
    dd = max_drawdown(_ix([1, 2, 3, 4]))
    assert dd.depth == 0.0


def test_max_drawdown_first_is_peak_and_recovers():
    s = _ix([100, 50, 100, 100])
    dd = max_drawdown(s)
    assert dd.depth == pytest.approx(-0.5)
    assert dd.peak == s.index[0]
    assert dd.trough == s.index[1]
    assert dd.recovered == s.index[2]


def test_max_drawdown_short_series_none():
    assert max_drawdown(_ix([1.0])) is None
    assert max_drawdown(_ix([])) is None
    assert max_drawdown(pd.Series([float("nan"), 1.0])) is None


def test_max_drawdown_ignores_nan():
    s = _ix([100, float("nan"), 50, 100])
    dd = max_drawdown(s)
    assert dd.depth == pytest.approx(-0.5)


def test_max_drawdown_deepest_not_first():
    s = _ix([100, 90, 100, 200, 100, 200])
    dd = max_drawdown(s)
    assert dd.depth == pytest.approx(-0.5)
    assert dd.peak == s.index[3]
    assert dd.trough == s.index[4]
    assert dd.recovered == s.index[5]
