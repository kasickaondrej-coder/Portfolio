"""Market data providers.

Every provider returns:
    close(symbol, start, end, adjusted=False) -> pd.Series   naive daily DatetimeIndex, float, may have gaps
    currency(symbol) -> (iso_code, scale)                     multiply raw prices by scale (GBp -> GBP, 0.01)
    splits(symbol) -> pd.Series                               split ratio by date (4.0 = 4-for-1)

`fx_series` builds a currency->base conversion series on top of any provider.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional, Protocol

import pandas as pd

log = logging.getLogger(__name__)

# minor-unit quotes used by Yahoo: code -> (major code, scale)
MINOR_UNITS = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ZAc": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}


def normalise_currency(code: Optional[str]) -> Optional[tuple[str, float]]:
    if not code:
        return None
    sub = MINOR_UNITS.get(code)
    if sub:
        return sub
    return code.upper(), 1.0


class DataError(RuntimeError):
    pass


class Provider(Protocol):
    def close(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp, adjusted: bool = False) -> pd.Series: ...
    def currency(self, symbol: str) -> tuple[str, float]: ...
    def splits(self, symbol: str) -> pd.Series: ...


def _clean_index(s: pd.Series) -> pd.Series:
    s = s.copy()
    idx = pd.to_datetime(s.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = idx.normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return pd.to_numeric(s, errors="coerce").dropna().astype(float)


class StaticProvider:
    """In-memory provider. Used for tests and for the --prices CSV option."""

    def __init__(
        self,
        prices: dict[str, pd.Series],
        currencies: Optional[dict[str, str]] = None,
        splits: Optional[dict[str, pd.Series]] = None,
        adjusted: Optional[dict[str, pd.Series]] = None,
    ):
        self._p = {k.upper(): _clean_index(v) for k, v in prices.items()}
        self._adj = {k.upper(): _clean_index(v) for k, v in (adjusted or {}).items()}
        self._c = {k.upper(): v for k, v in (currencies or {}).items()}
        self._s = {k.upper(): v for k, v in (splits or {}).items()}

    def close(self, symbol, start, end, adjusted=False):
        src = self._adj if adjusted and symbol.upper() in self._adj else self._p
        s = src.get(symbol.upper())
        if s is None:
            return pd.Series(dtype=float)
        return s.loc[pd.Timestamp(start): pd.Timestamp(end)]

    def currency(self, symbol):
        c = normalise_currency(self._c.get(symbol.upper()))
        if c is None:
            raise DataError(f"no currency known for {symbol}; add it to the prices file or the transaction")
        return c

    def splits(self, symbol):
        s = self._s.get(symbol.upper())
        return pd.Series(dtype=float) if s is None else _clean_index(s)

    @classmethod
    def from_csv(cls, path: str | Path) -> "StaticProvider":
        """Long format CSV: date,symbol,close[,currency][,adj_close][,split].

        FX rates are ordinary symbols such as USDEUR=X (EUR per 1 USD).
        """
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        need = {"date", "symbol", "close"}
        if not need.issubset(df.columns):
            raise DataError(f"{path}: needs columns {sorted(need)}")
        df["date"] = pd.to_datetime(df["date"])
        df["symbol"] = df["symbol"].str.strip().str.upper()
        prices, adj, ccys, spl = {}, {}, {}, {}
        for sym, g in df.groupby("symbol"):
            g = g.set_index("date").sort_index()
            prices[sym] = g["close"]
            if "adj_close" in g and g["adj_close"].notna().any():
                adj[sym] = g["adj_close"].fillna(g["close"])
            if "currency" in g:
                c = g["currency"].dropna()
                if not c.empty:
                    ccys[sym] = str(c.iloc[0]).strip()
            if "split" in g:
                sp = g["split"].dropna()
                sp = sp[sp != 0]
                if not sp.empty:
                    spl[sym] = sp
        # currency codes like GBp are case sensitive: keep as written
        return cls(prices, ccys, spl, adj)


class YahooProvider:
    """Yahoo Finance via yfinance, with a CSV cache per symbol.

    Close is split adjusted, not dividend adjusted (auto_adjust=False).
    """

    def __init__(self, cache_dir: Optional[str | Path] = None, refresh_days: int = 5, offline: bool = False):
        self.cache = Path(cache_dir or os.environ.get("PORTFOLIO_CACHE", Path.home() / ".cache" / "portfolio_tracker"))
        self.cache.mkdir(parents=True, exist_ok=True)
        self.refresh_days = refresh_days
        self.offline = offline
        self._ccy: dict[str, tuple[str, float]] = {}
        self._splits: dict[str, pd.Series] = {}
        self._mem: dict[tuple[str, bool], pd.Series] = {}

    @staticmethod
    def _yf():
        try:
            import yfinance as yf  # noqa: WPS433
        except ImportError as e:  # pragma: no cover
            raise DataError("yfinance is not installed: pip install yfinance") from e
        return yf

    def _path(self, symbol: str, kind: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-=" else "_" for ch in symbol)
        return self.cache / f"{safe}.{kind}.csv"

    def _read(self, symbol: str, kind: str) -> pd.Series:
        p = self._path(symbol, kind)
        if not p.exists():
            return pd.Series(dtype=float)
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        return _clean_index(df.iloc[:, 0]) if not df.empty else pd.Series(dtype=float)

    def _download(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp, adjusted: bool) -> pd.Series:
        yf = self._yf()
        last_err = None
        for attempt in range(4):
            try:
                raw = yf.download(
                    symbol, start=start.strftime("%Y-%m-%d"), end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                    auto_adjust=False, actions=False, progress=False, threads=False, multi_level_index=True,
                )
                if raw is None or raw.empty:
                    return pd.Series(dtype=float)
                col = "Adj Close" if adjusted else "Close"
                s = raw[col]
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0]
                return _clean_index(s)
            except Exception as e:  # rate limits, network errors
                last_err = e
                time.sleep(2 ** (attempt + 1))
        raise DataError(f"download failed for {symbol}: {last_err}")

    def close(self, symbol, start, end, adjusted=False):
        start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        key = (symbol, adjusted)
        kind = "adj" if adjusted else "close"
        cached = self._mem.get(key)
        if cached is None:
            cached = self._read(symbol, kind)
        today = pd.Timestamp.today().normalize()
        want_end = min(end, today)
        stale = (
            cached.empty
            or cached.index.min() > start + pd.Timedelta(days=7)
            or cached.index.max() < want_end - pd.Timedelta(days=self.refresh_days)
        )
        if stale and not self.offline:
            if cached.empty or cached.index.min() > start + pd.Timedelta(days=7):
                fetch_start = start  # cache does not reach back far enough: refetch everything
            else:
                fetch_start = cached.index.max() - pd.Timedelta(days=self.refresh_days)
            new = self._download(symbol, fetch_start, want_end, adjusted)
            if not new.empty:
                merged = pd.concat([cached, new])
                cached = merged[~merged.index.duplicated(keep="last")].sort_index()
                cached.rename("close").to_frame().to_csv(self._path(symbol, kind))
            elif cached.empty:
                log.warning("no price data for %s", symbol)
        elif stale:
            log.warning("offline: using cached data for %s (may be stale)", symbol)
        self._mem[key] = cached
        return cached.loc[start:end]

    def currency(self, symbol):
        if symbol in self._ccy:
            return self._ccy[symbol]
        p = self.cache / "currencies.csv"
        table = {}
        if p.exists():
            table = pd.read_csv(p, index_col=0).iloc[:, 0].astype(str).to_dict()
        code = table.get(symbol)
        if not code and not self.offline:
            try:
                code = self._yf().Ticker(symbol).fast_info["currency"]
            except Exception as e:
                raise DataError(f"could not get currency for {symbol}: {e}") from e
            if code:
                table[symbol] = code
                pd.Series(table, name="currency").to_csv(p)
        res = normalise_currency(code)
        if res is None:
            raise DataError(f"no currency for {symbol}; set the currency column in the transactions file")
        self._ccy[symbol] = res
        return res

    def splits(self, symbol):
        if symbol in self._splits:
            return self._splits[symbol]
        p = self._path(symbol, "splits")
        if p.exists() and (time.time() - p.stat().st_mtime < 86400 or self.offline):
            s = self._read(symbol, "splits")
        elif self.offline:
            s = pd.Series(dtype=float)
        else:
            try:
                s = self._yf().Ticker(symbol).splits
                s = _clean_index(s) if s is not None and len(s) else pd.Series(dtype=float)
            except Exception as e:
                log.warning("could not fetch splits for %s: %s", symbol, e)
                s = pd.Series(dtype=float)
            s.rename("split").to_frame().to_csv(p)
        s = s[s > 0]
        self._splits[symbol] = s
        return s


def fx_series(provider: Provider, ccy: str, base: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Units of `base` per 1 unit of `ccy`, daily close, may have gaps. 1.0 if same currency."""
    if ccy == base:
        return pd.Series(1.0, index=pd.date_range(start, end, freq="D"))
    direct = provider.close(f"{ccy}{base}=X", start, end)
    if not direct.empty:
        return direct
    inverse = provider.close(f"{base}{ccy}=X", start, end)
    if not inverse.empty:
        return 1.0 / inverse
    if ccy != "USD" and base != "USD":  # cross via USD
        a = fx_series(provider, ccy, "USD", start, end)
        b = fx_series(provider, "USD", base, start, end)
        cal = a.index.union(b.index)
        return (a.reindex(cal).ffill() * b.reindex(cal).ffill()).dropna()
    raise DataError(f"no FX data for {ccy}->{base}")
