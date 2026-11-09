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
        with pytest.raises(DataError, match=r"no exchange rate USD->EUR available \(needs internet, or add it to the prices file\)"):
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
