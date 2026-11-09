"""I/O tests: transaction loading/appending, static and Yahoo providers, CLI end to end.

Yahoo is never reached: yfinance.download and yfinance.Ticker are monkeypatched.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yfinance

from portfolio_tracker import cli
from portfolio_tracker.data import DataError, StaticProvider, YahooProvider, fx_series
from portfolio_tracker.transactions import LedgerError, append_transaction, load_transactions, parse_rows

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_TX = REPO / "examples" / "transactions_example.csv"
EXAMPLE_PRICES = REPO / "examples" / "prices_synthetic.csv"

HEADER = "date,type,ticker,quantity,price,fee,currency,amount,fx_rate"


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.write_bytes(text.encode(encoding))
    return path


def run_cli(*args: str, cwd: Path = REPO) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "portfolio_tracker", *args],
        cwd=cwd, capture_output=True, text=True, timeout=300,
    )


# ============================================================ load_transactions
class TestLoadTransactions:
    def test_comma_separated(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,2.5,105.10,1.00,,,\n")
        txs = load_transactions(p)
        assert len(txs) == 1
        t = txs[0]
        assert (t.type, t.ticker, t.quantity, t.price, t.fee) == ("buy", "VWCE.DE", 2.5, 105.10, 1.0)
        assert t.date == pd.Timestamp("2024-01-02")

    def test_semicolon_separated(self, tmp_path):
        p = write(tmp_path / "t.csv", "date;type;ticker;quantity;price;fee;currency;amount;fx_rate\n"
                                       "2024-01-02;buy;VWCE.DE;2.5;105.10;1.00;;;\n")
        txs = load_transactions(p)
        assert [(t.ticker, t.quantity, t.price) for t in txs] == [("VWCE.DE", 2.5, 105.10)]

    def test_tab_separated(self, tmp_path):
        p = write(tmp_path / "t.tsv", "date\ttype\tticker\tquantity\tprice\tfee\tcurrency\tamount\tfx_rate\n"
                                       "2024-01-02\tbuy\tAAPL\t3\t185.20\t1.5\tUSD\t\t0.9215\n")
        txs = load_transactions(p)
        assert len(txs) == 1
        assert txs[0].ticker == "AAPL"
        assert txs[0].currency == "USD"
        assert txs[0].fx_rate == pytest.approx(0.9215)

    def test_utf8_bom_header(self, tmp_path):
        p = write(tmp_path / "t.csv", f"﻿{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n", encoding="utf-8")
        txs = load_transactions(p)
        assert len(txs) == 1 and txs[0].ticker == "VWCE.DE"

    def test_decimal_comma_with_semicolon_separator(self, tmp_path):
        p = write(tmp_path / "t.csv", "date;type;ticker;quantity;price;fee;currency;amount;fx_rate\n"
                                       "2024-01-02;buy;VWCE.DE;2,5;105,10;1,00;;;\n"
                                       "2024-08-15;dividend;AAPL;;;;USD;2,40;\n")
        txs = load_transactions(p)
        buy, div = txs
        assert buy.quantity == pytest.approx(2.5)
        assert buy.price == pytest.approx(105.10)
        assert buy.fee == pytest.approx(1.0)
        assert div.amount == pytest.approx(2.40)

    def test_blank_lines_are_skipped(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n\n2024-01-02,buy,VWCE.DE,1,100,,,,\n\n\n"
                                       "2024-01-03,buy,VWCE.DE,1,101,,,,\n\n")
        txs = load_transactions(p)
        assert [t.price for t in txs] == [100.0, 101.0]

    def test_bad_date_reports_line(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n"
                                       "2024-13-45,buy,VWCE.DE,1,100,,,,\n")
        with pytest.raises(LedgerError, match=r"line 3"):
            load_transactions(p)

    def test_empty_date_raises_ledger_error(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n,buy,VWCE.DE,1,100,,,,\n")
        with pytest.raises(LedgerError, match=r"line 3.*date"):
            load_transactions(p)

    def test_bad_type_reports_line(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,transfer,VWCE.DE,1,100,,,,\n")
        with pytest.raises(LedgerError, match=r"line 2.*type"):
            load_transactions(p)

    def test_missing_ticker_reports_line(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n"
                                       "2024-01-03,buy,,1,100,,,,\n")
        with pytest.raises(LedgerError, match=r"line 3.*ticker"):
            load_transactions(p)

    def test_negative_quantity_reports_line(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,-1,100,,,,\n")
        with pytest.raises(LedgerError, match=r"line 2.*quantity"):
            load_transactions(p)

    def test_line_number_after_blank_line_is_physical(self, tmp_path):
        # physical lines: 1 header, 2 row, 3 blank, 4 row, 5 bad type
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n\n"
                                       "2024-01-03,buy,VWCE.DE,1,100,,,,\n"
                                       "2024-01-04,bogus,VWCE.DE,1,100,,,,\n")
        with pytest.raises(LedgerError, match=r"line 5"):
            load_transactions(p)

    def test_per_share_dividend_becomes_amount(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-08-15,dividend,AAPL,10,0.5,,USD,,\n")
        (t,) = load_transactions(p)
        assert t.amount == pytest.approx(5.0)

    def test_explicit_dividend_amount_is_kept(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-08-15,dividend,AAPL,,,,USD,2.40,\n")
        (t,) = load_transactions(p)
        assert t.amount == pytest.approx(2.40)

    def test_lowercase_ticker_is_uppercased(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,vwce.de,1,100,,,,\n")
        (t,) = load_transactions(p)
        assert t.ticker == "VWCE.DE"

    def test_same_day_buy_sorted_before_sell(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n"
                                       "2025-06-02,sell,AAPL,1,201,,,,\n"
                                       "2025-06-02,buy,AAPL,3,190,,,,\n")
        txs = load_transactions(p)
        assert [t.type for t in txs] == ["buy", "sell"]

    def test_rows_sorted_by_date(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-02-01,buy,A,1,1,,,,\n2024-01-01,buy,A,1,1,,,,\n")
        assert [t.date.date().isoformat() for t in load_transactions(p)] == ["2024-01-01", "2024-02-01"]

    def test_header_only_raises(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n")
        with pytest.raises(LedgerError, match="no transactions"):
            load_transactions(p)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(LedgerError, match="not found"):
            load_transactions(tmp_path / "nope.csv")

    def test_example_file_loads(self):
        txs = load_transactions(EXAMPLE_TX)
        assert len(txs) == 38  # 39 non-empty lines minus header
        assert {"buy", "sell", "dividend"} == {t.type for t in txs}


# ============================================================ append_transaction
class TestAppendTransaction:
    def test_creates_file_with_header(self, tmp_path):
        p = tmp_path / "new.csv"
        append_transaction(p, date="2024-01-02", type="buy", ticker="VWCE.DE", quantity="2.5", price="105.1", fee="1")
        lines = p.read_text().splitlines()
        assert lines[0] == HEADER
        assert len(lines) == 2
        (t,) = load_transactions(p)
        assert (t.ticker, t.quantity, t.price, t.fee) == ("VWCE.DE", 2.5, 105.1, 1.0)

    def test_appends_to_file_without_trailing_newline(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,")  # no final newline
        append_transaction(p, date="2024-02-01", type="sell", ticker="VWCE.DE", quantity="0.5", price="110")
        txs = load_transactions(p)
        assert len(txs) == 2
        assert [(t.type, t.date.date().isoformat(), t.quantity, t.price) for t in txs] == [
            ("buy", "2024-01-02", 1.0, 100.0),
            ("sell", "2024-02-01", 0.5, 110.0),
        ]

    def test_appends_to_file_with_trailing_newline(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n")
        append_transaction(p, date="2024-02-01", type="buy", ticker="VWCE.DE", quantity="1", price="101")
        txs = load_transactions(p)
        assert len(txs) == 2
        assert p.read_text().count(HEADER) == 1

    def test_dividend_row(self, tmp_path):
        p = tmp_path / "d.csv"
        append_transaction(p, date="2024-08-15", type="dividend", ticker="AAPL", amount="2.4", currency="USD")
        (t,) = load_transactions(p)
        assert t.type == "dividend" and t.amount == pytest.approx(2.4) and t.currency == "USD"

    def test_invalid_row_rejected_without_writing(self, tmp_path):
        p = write(tmp_path / "t.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n")
        before = p.read_bytes()
        with pytest.raises(LedgerError):
            append_transaction(p, date="2024-02-01", type="buy", ticker="VWCE.DE", quantity="-1", price="100")
        assert p.read_bytes() == before

    def test_invalid_row_on_new_file_creates_nothing(self, tmp_path):
        p = tmp_path / "new.csv"
        with pytest.raises(LedgerError):
            append_transaction(p, date="not-a-date", type="buy", ticker="X", quantity="1", price="1")
        assert not p.exists()

    def test_append_to_semicolon_file_keeps_separator(self, tmp_path):
        # README: separators are sniffed on load, so an appended row should match the file's separator
        p = write(tmp_path / "t.csv", "date;type;ticker;quantity;price;fee;currency;amount;fx_rate\n"
                                       "2024-01-02;buy;VWCE.DE;1;100;;;;\n")
        append_transaction(p, date="2024-02-01", type="buy", ticker="VWCE.DE", quantity="1", price="101")
        txs = load_transactions(p)
        assert [t.price for t in txs] == [100.0, 101.0]


# ============================================================ StaticProvider
def _prices_csv(tmp_path: Path, text: str) -> Path:
    return write(tmp_path / "prices.csv", text)


class TestStaticProviderFromCsv:
    def test_currency_adj_close_and_split_columns(self, tmp_path):
        p = _prices_csv(tmp_path, "date,symbol,close,currency,adj_close,split\n"
                                  "2024-01-02,AAPL,100,USD,95,\n"
                                  "2024-01-03,AAPL,50,USD,47.5,2\n"
                                  "2024-01-04,AAPL,51,USD,48,\n"
                                  "2024-01-02,VWCE.DE,105,EUR,,\n")
        sp = StaticProvider.from_csv(p)
        assert sp.currency("AAPL") == ("USD", 1.0)
        assert sp.currency("VWCE.DE") == ("EUR", 1.0)

        raw = sp.close("AAPL", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-05"))
        adj = sp.close("AAPL", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-05"), adjusted=True)
        assert list(raw) == [100.0, 50.0, 51.0]
        assert list(adj) == [95.0, 47.5, 48.0]

        spl = sp.splits("AAPL")
        assert len(spl) == 1
        assert spl.iloc[0] == 2.0
        assert spl.index[0] == pd.Timestamp("2024-01-03")
        assert sp.splits("VWCE.DE").empty

    def test_adjusted_falls_back_to_close_when_no_adj_column(self, tmp_path):
        p = _prices_csv(tmp_path, "date,symbol,close,currency\n2024-01-02,VWCE.DE,105,EUR\n")
        sp = StaticProvider.from_csv(p)
        s = sp.close("VWCE.DE", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-31"), adjusted=True)
        assert list(s) == [105.0]

    def test_gbp_pence_currency_kept_case_sensitive(self, tmp_path):
        p = _prices_csv(tmp_path, "date,symbol,close,currency\n"
                                  "2024-01-02,CSPX.L,800,GBp\n"
                                  "2024-01-02,VUSA.L,80,GBP\n"
                                  "2024-01-02,IUKD.L,700,GBX\n")
        sp = StaticProvider.from_csv(p)
        assert sp.currency("CSPX.L") == pytest.approx(("GBP", 0.01))
        assert sp.currency("VUSA.L") == ("GBP", 1.0)
        assert sp.currency("IUKD.L") == pytest.approx(("GBP", 0.01))

    def test_missing_required_columns_raise(self, tmp_path):
        p = _prices_csv(tmp_path, "date,symbol\n2024-01-02,AAPL\n")
        with pytest.raises(DataError, match="needs columns"):
            StaticProvider.from_csv(p)

    def test_unknown_currency_raises(self, tmp_path):
        p = _prices_csv(tmp_path, "date,symbol,close\n2024-01-02,AAPL,100\n")
        sp = StaticProvider.from_csv(p)
        with pytest.raises(DataError, match="no currency"):
            sp.currency("AAPL")


def _series(values, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="D")
    return pd.Series(values, index=idx, dtype=float)


class TestFxSeries:
    def test_same_currency_is_one(self):
        sp = StaticProvider({})
        s = fx_series(sp, "EUR", "EUR", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-03"))
        assert list(s) == [1.0, 1.0, 1.0]

    def test_direct_pair(self):
        sp = StaticProvider({"USDEUR=X": _series([0.90, 0.91, 0.92])})
        s = fx_series(sp, "USD", "EUR", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-03"))
        assert list(s) == pytest.approx([0.90, 0.91, 0.92])

    def test_inverse_pair(self):
        sp = StaticProvider({"EURUSD=X": _series([1.25, 1.10, 2.0])})
        s = fx_series(sp, "USD", "EUR", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-03"))
        assert list(s) == pytest.approx([1 / 1.25, 1 / 1.10, 0.5])

    def test_cross_via_usd(self):
        sp = StaticProvider({
            "GBPUSD=X": _series([1.25, 1.25, 1.25]),
            "USDEUR=X": _series([0.90, 0.90, 0.90]),
        })
        s = fx_series(sp, "GBP", "EUR", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-03"))
        assert list(s) == pytest.approx([1.125, 1.125, 1.125])

    def test_no_fx_data_raises(self):
        sp = StaticProvider({})
        with pytest.raises(DataError, match="no FX data"):
            fx_series(sp, "JPY", "EUR", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-03"))


# ============================================================ YahooProvider
def _yf_frame(dates, closes, adj_closes, ticker="X"):
    idx = pd.DatetimeIndex(dates)
    cols = pd.MultiIndex.from_tuples(
        [("Close", ticker), ("Adj Close", ticker), ("High", ticker), ("Low", ticker), ("Open", ticker), ("Volume", ticker)],
        names=["Price", "Ticker"],
    )
    data = np.column_stack([closes, adj_closes, closes, closes, closes, np.ones(len(closes))])
    return pd.DataFrame(data, index=idx, columns=cols)


@pytest.fixture
def fake_download(monkeypatch):
    """Replaces yfinance.download. Records calls; returns a frame built by `set_frame`."""
    state = {"calls": [], "frame": None, "raise_times": 0}

    def fake(*args, **kwargs):
        state["calls"].append({"args": args, "kwargs": kwargs})
        if state["raise_times"] > 0:
            state["raise_times"] -= 1
            raise ConnectionError("simulated network failure")
        f = state["frame"]
        return f() if callable(f) else f

    monkeypatch.setattr(yfinance, "download", fake)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # skip retry back-off
    state["fake"] = fake
    return state


def _business_days(start, end):
    return pd.bdate_range(start, end)


class TestYahooProvider:
    def test_close_extracted_not_adjusted(self, tmp_path, fake_download):
        days = _business_days("2025-01-01", "2025-01-31")
        fake_download["frame"] = _yf_frame(days, np.arange(len(days)) + 100.0, np.arange(len(days)) + 90.0)
        yp = YahooProvider(cache_dir=tmp_path)
        s = yp.close("VWCE.DE", pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-10"))
        expected = {d: 100.0 + i for i, d in enumerate(days)}
        assert len(s) == 5  # Mon 6th .. Fri 10th
        for d, v in s.items():
            assert v == pytest.approx(expected[d])
        assert s.index.min() == pd.Timestamp("2025-01-06")
        assert s.index.max() == pd.Timestamp("2025-01-10")

    def test_adjusted_flag_uses_adj_close(self, tmp_path, fake_download):
        days = _business_days("2025-01-01", "2025-01-31")
        fake_download["frame"] = _yf_frame(days, np.full(len(days), 100.0), np.full(len(days), 90.0))
        yp = YahooProvider(cache_dir=tmp_path)
        raw = yp.close("VWCE.DE", pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-10"))
        adj = yp.close("VWCE.DE", pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-10"), adjusted=True)
        assert raw.iloc[0] == 100.0
        assert adj.iloc[0] == 90.0

    def test_download_arguments(self, tmp_path, fake_download):
        days = _business_days("2025-01-01", "2025-01-31")
        fake_download["frame"] = _yf_frame(days, np.full(len(days), 1.0), np.full(len(days), 1.0))
        yp = YahooProvider(cache_dir=tmp_path)
        yp.close("VWCE.DE", pd.Timestamp("2025-01-06"), pd.Timestamp("2025-01-10"))
        call = fake_download["calls"][0]
        assert call["args"][0] == "VWCE.DE"
        assert call["kwargs"]["auto_adjust"] is False
        assert call["kwargs"]["start"] == "2025-01-06"
        assert call["kwargs"]["end"] == "2025-01-11"  # end is exclusive in yfinance

    def test_second_call_with_fresh_cache_does_not_download(self, tmp_path, fake_download):
        days = _business_days("2025-01-01", "2025-10-31")
        fake_download["frame"] = _yf_frame(days, np.linspace(50, 60, len(days)), np.linspace(50, 60, len(days)))
        end = pd.Timestamp("2025-10-31")
        first = YahooProvider(cache_dir=tmp_path).close("VWCE.DE", pd.Timestamp("2025-02-01"), end)
        assert len(fake_download["calls"]) == 1
        # new instance: nothing in memory, must come from the CSV cache
        second = YahooProvider(cache_dir=tmp_path).close("VWCE.DE", pd.Timestamp("2025-02-01"), end)
        assert len(fake_download["calls"]) == 1
        pd.testing.assert_series_equal(first, second, check_names=False, check_freq=False)

    def test_cache_file_written(self, tmp_path, fake_download):
        days = _business_days("2025-01-01", "2025-10-31")
        fake_download["frame"] = _yf_frame(days, np.full(len(days), 70.0), np.full(len(days), 70.0))
        YahooProvider(cache_dir=tmp_path).close("VWCE.DE", pd.Timestamp("2025-02-01"), pd.Timestamp("2025-10-31"))
        assert (tmp_path / "VWCE.DE.close.csv").exists()

    def test_offline_uses_cache_without_download(self, tmp_path, fake_download):
        days = _business_days("2025-01-01", "2025-10-31")
        fake_download["frame"] = _yf_frame(days, np.full(len(days), 70.0), np.full(len(days), 70.0))
        YahooProvider(cache_dir=tmp_path).close("VWCE.DE", pd.Timestamp("2025-02-01"), pd.Timestamp("2025-10-31"))
        n = len(fake_download["calls"])

        def boom(*a, **k):
            raise AssertionError("download must not be called in offline mode")

        fake_download["frame"] = boom
        offline = YahooProvider(cache_dir=tmp_path, offline=True)
        s = offline.close("VWCE.DE", pd.Timestamp("2025-02-03"), pd.Timestamp("2025-02-07"))
        assert len(fake_download["calls"]) == n
        assert not s.empty and set(s) == {70.0}

    def test_offline_without_cache_is_empty(self, tmp_path, fake_download):
        yp = YahooProvider(cache_dir=tmp_path, offline=True)
        s = yp.close("NOPE", pd.Timestamp("2025-02-03"), pd.Timestamp("2025-02-07"))
        assert s.empty
        assert fake_download["calls"] == []

    def test_empty_frame_handled(self, tmp_path, fake_download):
        fake_download["frame"] = pd.DataFrame()
        yp = YahooProvider(cache_dir=tmp_path)
        s = yp.close("NOPE", pd.Timestamp("2025-02-03"), pd.Timestamp("2025-02-07"))
        assert isinstance(s, pd.Series)
        assert s.empty

    def test_retries_then_succeeds(self, tmp_path, fake_download):
        days = _business_days("2025-01-01", "2025-10-31")
        fake_download["frame"] = _yf_frame(days, np.full(len(days), 5.0), np.full(len(days), 5.0))
        fake_download["raise_times"] = 2
        s = YahooProvider(cache_dir=tmp_path).close("VWCE.DE", pd.Timestamp("2025-02-01"), pd.Timestamp("2025-10-31"))
        assert len(fake_download["calls"]) == 3
        assert set(s) == {5.0}

    def test_persistent_failure_raises_data_error(self, tmp_path, fake_download):
        fake_download["raise_times"] = 100
        with pytest.raises(DataError, match="download failed"):
            YahooProvider(cache_dir=tmp_path).close("VWCE.DE", pd.Timestamp("2025-02-01"), pd.Timestamp("2025-10-31"))

    def test_currency_from_fast_info_and_cached(self, tmp_path, monkeypatch):
        calls = []

        class FakeTicker:
            def __init__(self, symbol):
                calls.append(symbol)
                self.fast_info = {"currency": "GBp"}

        monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
        yp = YahooProvider(cache_dir=tmp_path)
        assert yp.currency("CSPX.L") == pytest.approx(("GBP", 0.01))
        assert calls == ["CSPX.L"]
        # cached on disk: a new instance must not ask Yahoo again
        assert YahooProvider(cache_dir=tmp_path).currency("CSPX.L") == pytest.approx(("GBP", 0.01))
        assert calls == ["CSPX.L"]

    def test_currency_usd(self, tmp_path, monkeypatch):
        class FakeTicker:
            def __init__(self, symbol):
                self.fast_info = {"currency": "USD"}

        monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
        assert YahooProvider(cache_dir=tmp_path).currency("AAPL") == ("USD", 1.0)

    def test_splits_from_ticker(self, tmp_path, monkeypatch):
        class FakeTicker:
            def __init__(self, symbol):
                idx = pd.DatetimeIndex(["2020-08-31"], tz="America/New_York")
                self.splits = pd.Series([4.0], index=idx)

        monkeypatch.setattr(yfinance, "Ticker", FakeTicker)
        sp = YahooProvider(cache_dir=tmp_path).splits("AAPL")
        assert list(sp) == [4.0]
        assert sp.index[0] == pd.Timestamp("2020-08-31")

    def test_failed_splits_fetch_is_not_cached(self, tmp_path, monkeypatch):
        """Expected: a transient failure returns no splits but does not poison the cache for 24h."""
        class Broken:
            def __init__(self, symbol):
                raise ConnectionError("simulated")

        class Working:
            def __init__(self, symbol):
                self.splits = pd.Series([2.0], index=pd.DatetimeIndex(["2021-06-01"]))

        monkeypatch.setattr(yfinance, "Ticker", Broken)
        assert YahooProvider(cache_dir=tmp_path).splits("XYZ").empty
        monkeypatch.setattr(yfinance, "Ticker", Working)
        sp = YahooProvider(cache_dir=tmp_path).splits("XYZ")
        assert list(sp) == [2.0]


# ============================================================ CLI end to end
class TestCliEndToEnd:
    def test_report_text_exit_zero(self):
        r = run_cli("report", "examples/transactions_example.csv", "--prices", "examples/prices_synthetic.csv",
                    "--as-of", "2025-10-31", "--benchmark", "VWCE.DE")
        assert r.returncode == 0, r.stderr
        assert "XIRR" in r.stdout
        assert r.stderr.strip() == "" or "error" not in r.stderr.lower()

    def test_report_json_parses(self):
        r = run_cli("report", "examples/transactions_example.csv", "--prices", "examples/prices_synthetic.csv",
                    "--as-of", "2025-10-31", "--benchmark", "VWCE.DE", "--json")
        assert r.returncode == 0, r.stderr
        data = json.loads(r.stdout)
        for key in ("xirr", "periods", "holdings"):
            assert key in data
        assert isinstance(data["holdings"], list) and data["holdings"]
        assert isinstance(data["periods"], list) and data["periods"]
        assert {h["ticker"] for h in data["holdings"]} >= {"VWCE.DE", "AAPL"}

    def test_export_daily_writes_csv(self, tmp_path):
        out = tmp_path / "daily.csv"
        r = run_cli("report", "examples/transactions_example.csv", "--prices", "examples/prices_synthetic.csv",
                    "--as-of", "2025-10-31", "--export-daily", str(out))
        assert r.returncode == 0, r.stderr
        df = pd.read_csv(out)
        assert len(df) > 100
        assert "date" in df.columns
        assert pd.to_datetime(df["date"]).max() <= pd.Timestamp("2025-10-31")

    def test_add_subcommand_appends_row(self, tmp_path):
        tx = tmp_path / "my.csv"
        tx.write_text(EXAMPLE_TX.read_text())
        before = len(load_transactions(tx))
        r = run_cli("add", str(tx), "buy", "VWCE.DE", "1.5", "110.25", "--date", "2025-11-03", "--fee", "1")
        assert r.returncode == 0, r.stderr
        txs = load_transactions(tx)
        assert len(txs) == before + 1
        added = txs[-1]
        assert (added.type, added.ticker, added.quantity, added.price, added.fee) == ("buy", "VWCE.DE", 1.5, 110.25, 1.0)
        assert added.date == pd.Timestamp("2025-11-03")

    def test_add_dividend_subcommand(self, tmp_path):
        tx = tmp_path / "new.csv"
        r = run_cli("add", str(tx), "dividend", "AAPL", "0", "2.40", "--date", "2024-08-15", "--currency", "USD")
        assert r.returncode == 0, r.stderr
        (t,) = load_transactions(tx)
        assert t.type == "dividend" and t.amount == pytest.approx(2.4) and t.currency == "USD"

    def test_bad_file_exit_2_with_message(self, tmp_path):
        bad = write(tmp_path / "bad.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,1,100,,,,\n2024-99-99,buy,VWCE.DE,1,100,,,,\n")
        r = run_cli("report", str(bad), "--prices", str(EXAMPLE_PRICES), "--as-of", "2025-10-31")
        assert r.returncode == 2
        assert "error:" in r.stderr
        assert "line 3" in r.stderr
        assert "Traceback" not in r.stderr

    def test_missing_file_exit_2(self, tmp_path):
        r = run_cli("report", str(tmp_path / "missing.csv"), "--prices", str(EXAMPLE_PRICES))
        assert r.returncode == 2
        assert "error:" in r.stderr

    def test_main_returns_2_on_bad_file_with_capsys(self, tmp_path, capsys):
        bad = write(tmp_path / "bad.csv", f"{HEADER}\n2024-01-02,buy,VWCE.DE,-2,100,,,,\n")
        code = cli.main(["report", str(bad), "--prices", str(EXAMPLE_PRICES)])
        captured = capsys.readouterr()
        assert code == 2
        assert "error:" in captured.err
        assert "quantity" in captured.err
