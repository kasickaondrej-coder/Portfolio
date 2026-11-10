"""Regression tests: all-or-nothing savings plan, file-format-preserving appends, validation, messages."""
from argparse import Namespace

import pandas as pd
import pytest

from portfolio_tracker import cli
from portfolio_tracker.analytics import Portfolio
from portfolio_tracker.data import DataError, StaticProvider, YahooProvider, fx_series
from portfolio_tracker.report import render_text
from portfolio_tracker.transactions import LedgerError, append_transaction, load_transactions, parse_rows

HEADER = "date,type,ticker,quantity,price,fee,currency,amount,fx_rate"


def _provider(price=10.0, start="2024-12-01", end="2025-08-31"):
    idx = pd.date_range(start, end, freq="D")
    return StaticProvider({"AAA": pd.Series(price, index=idx)}, {"AAA": "EUR"})


def _args(path, **kw):
    d = dict(transactions=str(path), ticker="AAA", amount="100", start="2025-01-01", end="2025-04-30",
             weekday="mon", monthly=None, fee="0", currency=None, decimals=4)
    d.update(kw)
    return Namespace(**d)


def _row(**kw):
    r = dict(date="2025-01-02", type="buy", ticker="X", quantity="1", price="10", fee="", currency="", amount="", fx_rate="")
    r.update(kw)
    return r


# ---------------------------------------------------------------- 1 / 3 / 5 savings plan
class TestPlan:
    def test_all_or_nothing_and_message_names_plan_date(self, tmp_path):
        f = tmp_path / "t.csv"
        f.write_text(HEADER + "\n")
        before = f.read_text()
        idx = pd.date_range("2024-12-01", "2025-08-31", freq="D")
        px = pd.Series(10.0, index=idx)
        px[px.index >= "2025-03-01"] = 1e6  # later rows buy 0 units
        sp = StaticProvider({"AAA": px}, {"AAA": "EUR"})
        with pytest.raises(LedgerError, match=r"^2025-03-\d\d: amount 100 buys 0 units at price 1e\+06 with 0 decimals") as e:
            cli.cmd_dca(_args(f, decimals=0), provider=sp)
        assert "line" not in str(e.value)
        assert f.read_text() == before

    def test_success_writes_all_rows(self, tmp_path):
        f = tmp_path / "t.csv"
        assert cli.cmd_dca(_args(f, monthly=1), provider=_provider()) == 0
        assert len(load_transactions(f)) == 4

    @pytest.mark.parametrize("amount", ["1,234", "nan", "inf", "abc"])
    def test_amount_parsing_rejects(self, tmp_path, amount):
        f = tmp_path / "t.csv"
        with pytest.raises(LedgerError):
            cli.cmd_dca(_args(f, amount=amount), provider=_provider())
        assert not f.exists()

    def test_amount_parsing_accepts_european_and_floats(self, tmp_path):
        f = tmp_path / "t.csv"
        cli.cmd_dca(_args(f, amount="1.234,5", fee=2.5, monthly=1, end="2025-01-31"), provider=_provider())
        tx = load_transactions(f)[0]
        assert tx.quantity == pytest.approx((1234.5 - 2.5) / 10)
        assert tx.fee == 2.5

    def test_bad_fee_rejected(self, tmp_path):
        with pytest.raises(LedgerError, match="fee"):
            cli.cmd_dca(_args(tmp_path / "t.csv", fee="1,234"), provider=_provider())

    def test_monthly_day_31_clamps_to_month_end(self, tmp_path):
        f = tmp_path / "t.csv"
        cli.cmd_dca(_args(f, monthly=31, start="2025-01-01", end="2025-04-30", amount=100.0), provider=_provider())
        days = [t.date for t in load_transactions(f)]
        # trade day is the plan day or the next trading day: 4 plan dates (Jan 31, Feb 28, Mar 31, Apr 30)
        assert [d.strftime("%Y-%m-%d") for d in days] == ["2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30"]

    def test_monthly_day_29_leap_year(self, tmp_path):
        f = tmp_path / "t.csv"
        sp = _provider(start="2023-12-01", end="2024-04-30")
        cli.cmd_dca(_args(f, monthly=29, start="2024-01-01", end="2024-03-31"), provider=sp)
        assert [t.date.strftime("%m-%d") for t in load_transactions(f)] == ["01-29", "02-29", "03-29"]


# ---------------------------------------------------------------- 2 / 4 append
class TestAppend:
    def test_error_has_no_fake_line(self, tmp_path):
        with pytest.raises(LedgerError) as e:
            append_transaction(tmp_path / "a.csv", date="2025-01-02", type="buy", ticker="X", quantity=0, price=1)
        assert "line" not in str(e.value) and "quantity must be > 0" in str(e.value)

    def test_semicolon_decimal_comma_crlf_bom(self, tmp_path):
        f = tmp_path / "a.csv"
        head = HEADER.replace(",", ";")
        f.write_bytes(b"\xef\xbb\xbf" + (head + "\r\n2025-01-02;buy;X;1,5;10,25;;;;\r\n").encode())
        append_transaction(f, date="2025-02-03", type="buy", ticker="X", quantity=2.5, price=11.75, fee=1.0)
        raw = f.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"\xef\xbb\xbf") == 1
        assert raw.endswith(b"2025-02-03;buy;X;2,5;11,75;1,0;;;\r\n")
        assert b"\n" not in raw.replace(b"\r\n", b"")
        assert len(load_transactions(f)) == 2

    def test_lf_file_stays_lf_and_dot_decimals(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_bytes((HEADER + "\n2025-01-02,buy,X,1.5,10.25,,,,\n").encode())
        append_transaction(f, date="2025-02-03", type="buy", ticker="X", quantity=2.5, price=11.75)
        raw = f.read_bytes()
        assert b"\r" not in raw and raw.endswith(b"2025-02-03,buy,X,2.5,11.75,,,,\n")

    def test_comma_delimited_file_with_decimal_commas(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_bytes((HEADER + '\n2025-01-02,buy,X,"1,5","10,25",,,,\n').encode())
        append_transaction(f, date="2025-02-03", type="buy", ticker="X", quantity=2.5, price=11.75)
        assert f.read_bytes().endswith(b'2025-02-03,buy,X,"2,5","11,75",,,,\n')
        assert load_transactions(f)[1].price == 11.75

    def test_missing_final_newline_uses_file_eol(self, tmp_path):
        f = tmp_path / "a.csv"
        f.write_bytes((HEADER + "\n2025-01-02,buy,X,1,10,,,,").encode())
        append_transaction(f, date="2025-02-03", type="buy", ticker="X", quantity=2, price=11)
        assert b"\r" not in f.read_bytes() and len(load_transactions(f)) == 2


# ---------------------------------------------------------------- 6 validation
class TestValidation:
    @pytest.mark.parametrize("kw,msg", [
        (dict(date="1949-12-31"), "out of range"),
        (dict(date="2101-01-01"), "out of range"),
        (dict(currency="EURO"), "3-letter"),
        (dict(currency="E1R"), "3-letter"),
        (dict(price="0"), "price must be > 0"),
        (dict(price="-1"), "price must be > 0"),
        (dict(type="sell", price="0"), "price must be > 0"),
        (dict(quantity="1e12"), "unrealistically large"),
        (dict(type="dividend", amount="0", quantity="", price=""), "dividend amount must be > 0"),
        (dict(type="dividend", amount="-5", quantity="", price=""), "dividend amount must be > 0"),
    ])
    def test_rejects(self, kw, msg):
        with pytest.raises(LedgerError, match=msg):
            parse_rows([_row(**kw)])

    @pytest.mark.parametrize("ccy", ["EUR", "usd", "GBp", "GBX", "ZAc", "ILA"])
    def test_accepts_currency(self, ccy):
        assert parse_rows([_row(currency=ccy)])[0].currency

    def test_boundary_dates_ok(self):
        assert parse_rows([_row(date="1950-01-01"), _row(date="2100-12-31")])


# ---------------------------------------------------------------- 7 data errors
class TestDataErrors:
    def test_currency_unreachable(self, tmp_path, monkeypatch):
        class T:
            def __init__(self, s):
                raise RuntimeError("boom")
        import types
        monkeypatch.setattr(YahooProvider, "_yf", staticmethod(lambda: types.SimpleNamespace(Ticker=T)))
        with pytest.raises(DataError, match=r"could not reach Yahoo Finance for ABC \(check your internet connection"):
            YahooProvider(cache_dir=tmp_path).currency("ABC")

    def test_offline_no_data(self, tmp_path):
        yp = YahooProvider(cache_dir=tmp_path, offline=True)
        with pytest.raises(DataError, match="no saved data for ABC yet: run once with internet"):
            yp.currency("ABC")
        with pytest.raises(DataError, match="no saved data for ABC yet"):
            yp.close("ABC", pd.Timestamp("2025-01-01"), pd.Timestamp("2025-02-01"))

    def test_offline_fx_falls_through_to_missing_rate_message(self, tmp_path):
        yp = YahooProvider(cache_dir=tmp_path, offline=True)
        with pytest.raises(DataError, match=r"no exchange rate USD->EUR: this needs an internet connection to Yahoo Finance \(try again later\), or add the rate to the prices file"):
            fx_series(yp, "USD", "EUR", pd.Timestamp("2025-01-01"), pd.Timestamp("2025-02-01"))


# ---------------------------------------------------------------- 8 report
class TestReport:
    def _pf(self, as_of):
        idx = pd.date_range("2024-12-01", "2025-06-30", freq="D")
        sp = StaticProvider({"AAA": pd.Series(range(100, 100 + len(idx)), index=idx, dtype=float)}, {"AAA": "EUR"})
        txs = parse_rows([_row(date="2025-01-02", ticker="AAA", quantity="10", price="100")])
        return Portfolio(txs, sp, base="EUR", as_of=pd.Timestamp(as_of))

    def test_plain_words_block_first(self):
        text = render_text(self._pf("2025-06-30").report())
        lines = text.splitlines()
        assert lines[0] == "IN PLAIN WORDS"
        block = lines[1:lines.index("")]
        assert 3 <= len(block) <= 5
        assert "You put in 1,000.00 EUR" in block[0] and "gain" in block[0]
        assert any("Last month" in x for x in block) and any("Best holding" in x for x in block)

    def test_future_as_of_warns(self):
        pf = self._pf("2999-01-01")
        assert any("in the future" in w for w in pf.warnings)


# ------------------------------------------------- round 2 regressions
class TestRound2Core:
    @pytest.mark.parametrize("raw", ["1,5,5", "1,23,456", "12,34,5.6", "1.2.3", "1,234.5,6"])
    def test_num_rejects_bad_grouping(self, raw):
        with pytest.raises(LedgerError):
            parse_rows([_row(quantity=raw)])

    @pytest.mark.parametrize("raw,val", [("1,234,567", 1234567), ("1,234,567.5", 1234567.5),
                                         ("1.234.567,8", 1234567.8), ("1.234.567", 1234567), ("1,5", 1.5)])
    def test_num_accepts_proper_grouping(self, raw, val):
        assert parse_rows([_row(quantity=raw)])[0].quantity == pytest.approx(val)

    def test_ticker_normalised_and_validated(self):
        assert parse_rows([_row(ticker="vwce.de")])[0].ticker == "VWCE.DE"
        assert parse_rows([_row(ticker="^gspc")])[0].ticker == "^GSPC"
        for bad in ("A B", "A$", "X" * 21, "A/B"):
            with pytest.raises(LedgerError, match="ticker"):
                parse_rows([_row(ticker=bad)])

    def test_append_normalises_date(self, tmp_path):
        f = tmp_path / "t.csv"
        f.write_text(HEADER + "\n")
        append_transaction(f, date="03.07.2024", type="buy", ticker="aaa", quantity="1", price="2")
        assert "2024-07-03,buy,AAA" in f.read_text()

    def test_append_refuses_unreadable_or_unbalanced_file(self, tmp_path):
        f = tmp_path / "t.csv"
        f.write_bytes(HEADER.encode() + b"\n2024-01-02,buy,X,1,1,,,,\n\xff\xfe\n")
        with pytest.raises(LedgerError, match="UTF-8"):
            append_transaction(f, date="2024-02-01", type="buy", ticker="X", quantity="1", price="2")
        f.write_text(HEADER + '\n2024-01-02,buy,X,1,1,,"unclosed,,\n')
        before = f.read_bytes()
        with pytest.raises(LedgerError):
            append_transaction(f, date="2024-02-01", type="buy", ticker="X", quantity="1", price="2")
        assert f.read_bytes() == before

    def test_append_refuses_quote_swallowing_new_row(self, tmp_path):
        f = tmp_path / "t.csv"
        f.write_text(HEADER + '\n2024-01-02,buy,X,1,1,,,,\n2024-01-03,buy,Y,1,1,,,"note\n')
        before = f.read_bytes()
        with pytest.raises(LedgerError):
            append_transaction(f, date="2024-02-01", type="buy", ticker="X", quantity="1", price="2")
        assert f.read_bytes() == before

    def test_future_as_of_is_clamped_and_explained(self):
        today = pd.Timestamp.today().normalize()
        far = today + pd.Timedelta(days=400)
        idx = pd.date_range(today - pd.Timedelta(days=60), today, freq="D")
        p = StaticProvider({"AAA": pd.Series(10.0, index=idx)}, {"AAA": "EUR"})
        txs = parse_rows([_row(date=(today - pd.Timedelta(days=30)).strftime("%Y-%m-%d"), ticker="AAA")])
        r = Portfolio(txs, p, as_of=far).report()
        assert r.as_of == today
        assert f"as-of date is in the future, valued as of {today.date()}" in r.warnings
        assert " now: " in render_text(r)
        r2 = Portfolio(txs, p, as_of=today - pd.Timedelta(days=5)).report()
        assert f"on {r2.as_of.date()}:" in render_text(r2)

    def test_unknown_benchmark_gives_warning(self):
        p = _provider()
        txs = parse_rows([_row(date="2025-01-02", ticker="AAA", currency="EUR")])
        r = Portfolio(txs, p, as_of="2025-06-01", benchmarks=["ZZZZ"]).report()
        assert any("benchmark ZZZZ skipped" in w for w in r.warnings)
        assert "benchmark ZZZZ skipped" in render_text(r)


def test_semicolon_file_reads_three_decimal_comma_values(tmp_path):
    """Czech Excel writes 12,125 (= 12.125) in ';' files; that must load, not be called ambiguous."""
    from portfolio_tracker.transactions import load_transactions
    p = tmp_path / "cz.csv"
    p.write_bytes("﻿date;type;ticker;quantity;price;fee;currency;amount;fx_rate\r\n"
                  "02.01.2024;buy;VWCE.DE;3,125;12,125;1;;;\r\n".encode("utf-8"))
    (t,) = load_transactions(p)
    assert t.quantity == 3.125 and t.price == 12.125


def test_comma_file_still_rejects_ambiguous_value(tmp_path):
    import pytest
    from portfolio_tracker.transactions import LedgerError, load_transactions
    p = tmp_path / "en.csv"
    p.write_text('date,type,ticker,quantity,price,fee,currency,amount,fx_rate\n2024-01-02,buy,VWCE.DE,"1,234",100,,,,\n')
    with pytest.raises(LedgerError, match="ambiguous"):
        load_transactions(p)


# ---- savings plan in a currency other than the ticker's (bug: CZK plan on a EUR ETF)
def _czk_prices(tmp_path):
    p = tmp_path / "p.csv"
    p.write_text(
        "date,symbol,close,currency\n"
        "2025-01-06,NQSE.DE,12.5,EUR\n2025-01-13,NQSE.DE,13.0,EUR\n"
        "2025-01-06,EURCZK=X,25.0,CZK\n2025-01-13,EURCZK=X,25.0,CZK\n"
        "2025-01-06,CSPX.L,50000,GBp\n2025-01-13,CSPX.L,50000,GBp\n"
        "2025-01-06,GBPEUR=X,1.2,EUR\n2025-01-13,GBPEUR=X,1.2,EUR\n"
    )
    return p


def test_dca_converts_market_price_into_plan_currency(tmp_path):
    from portfolio_tracker import cli
    from portfolio_tracker.transactions import load_transactions
    prices = _czk_prices(tmp_path)
    tx = tmp_path / "t.csv"
    assert cli.main(["dca", str(tx), "NQSE.DE", "1000", "--currency", "CZK", "--start", "2025-01-06",
                     "--end", "2025-01-13", "--prices", str(prices)]) == 0
    a, b = load_transactions(tx)
    assert a.currency == "CZK" and a.price == 312.5          # 12.5 EUR x 25 CZK/EUR
    assert a.quantity == round(1000 / 312.5, 4)              # 3.2 units, not 80
    assert b.price == 325.0


def test_dca_scales_pence_tickers(tmp_path):
    from portfolio_tracker import cli
    from portfolio_tracker.transactions import load_transactions
    prices = _czk_prices(tmp_path)
    tx = tmp_path / "t.csv"
    assert cli.main(["dca", str(tx), "CSPX.L", "100", "--start", "2025-01-06", "--end", "2025-01-06",
                     "--prices", str(prices)]) == 0
    (a,) = load_transactions(tx)
    assert a.currency == "GBP" and a.price == 500.0 and a.quantity == 0.2


def test_dca_czk_plan_report_matches_eur_plan(tmp_path):
    from portfolio_tracker import cli
    from portfolio_tracker.analytics import Portfolio
    from portfolio_tracker.data import StaticProvider
    from portfolio_tracker.transactions import load_transactions
    prices = _czk_prices(tmp_path)
    tx = tmp_path / "t.csv"
    cli.main(["dca", str(tx), "NQSE.DE", "1000", "--currency", "CZK", "--start", "2025-01-06",
              "--end", "2025-01-13", "--prices", str(prices)])
    r = Portfolio(load_transactions(tx), StaticProvider.from_csv(prices), base="EUR", as_of="2025-01-13").report()
    assert not r.problems
    assert r.total_bought == pytest.approx(80.0, abs=0.01)   # 2 x 1000 CZK at 25 CZK/EUR
    assert r.simple_return == pytest.approx((3.2 + 3.0769) * 13.0 / 80.0 - 1, abs=1e-3)


def test_wrong_currency_column_is_reported_as_data_problem(tmp_path):
    from portfolio_tracker.analytics import Portfolio
    from portfolio_tracker.data import StaticProvider
    from portfolio_tracker.report import render_text
    from portfolio_tracker.transactions import parse_rows
    prices = _czk_prices(tmp_path)
    txs = parse_rows([dict(date="2025-01-06", type="buy", ticker="NQSE.DE", quantity="80", price="12.5",
                           currency="CZK")])  # EUR price labelled CZK: the old dca bug
    r = Portfolio(txs, StaticProvider.from_csv(prices), base="EUR", as_of="2025-01-13").report()
    assert len(r.problems) == 1 and "1 of 1 trade prices" in r.problems[0]
    text = render_text(r)
    assert text.startswith("!!! DATA PROBLEM")


def test_fx_split_price_and_currency_effect(tmp_path):
    """EUR asset bought with CZK, report in CZK: P/L splits into price effect + FX effect exactly."""
    from portfolio_tracker.analytics import Portfolio
    from portfolio_tracker.data import StaticProvider
    from portfolio_tracker.transactions import parse_rows
    p = tmp_path / "p.csv"
    p.write_text("date,symbol,close,currency\n"
                 "2025-01-06,AAA.DE,10,EUR\n2025-02-06,AAA.DE,12,EUR\n"
                 "2025-01-06,EURCZK=X,25,CZK\n2025-02-06,EURCZK=X,24,CZK\n")
    txs = parse_rows([dict(date="2025-01-06", type="buy", ticker="AAA.DE", quantity="4", price="250",
                           currency="CZK")])  # 4 units at 10 EUR = 1000 CZK
    prov = StaticProvider.from_csv(p)
    r = Portfolio(txs, prov, base="CZK", as_of="2025-02-06").report()
    # value 4*12*24 = 1152 CZK, P/L 152; price effect 4*(12-10)*24 = 192; FX effect 40 EUR*(24-25) = -40
    assert r.total_pl == pytest.approx(152)
    assert r.price_effect == pytest.approx(192)
    assert r.fx_effect == pytest.approx(-40)
    assert r.holdings[0].fx_effect == pytest.approx(-40)
    e = Portfolio(txs, prov, base="EUR", as_of="2025-02-06").report()
    assert e.fx_effect == pytest.approx(0) and e.payment_currencies == ["CZK"]
