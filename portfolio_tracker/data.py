"""Market data providers.

Every provider returns:
    close(symbol, start, end, adjusted=False) -> pd.Series   naive daily DatetimeIndex, float, may have gaps
    currency(symbol) -> (iso_code, scale)                     multiply raw prices by scale (GBp -> GBP, 0.01)
    splits(symbol) -> pd.Series                               split ratio by date (4.0 = 4-for-1)

`fx_series` builds a currency->base conversion series on top of any provider.
"""
from __future__ import annotations

import json
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


def _unreachable(symbol: str) -> str:
    return f"could not reach Yahoo Finance for {symbol} (check your internet connection and try again)"


def _no_saved(symbol: str) -> str:
    return f"no saved data for {symbol} yet: run once with internet (untick Offline)"


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
        try:
            df = pd.read_csv(p, index_col=0, parse_dates=True)
        except Exception as e:  # corrupt cache: ignore it, it will be rebuilt
            log.warning("ignoring unreadable cache file %s: %s", p, e)
            return pd.Series(dtype=float)
        return _clean_index(df.iloc[:, 0]) if not df.empty else pd.Series(dtype=float)

    # cache metadata: earliest date requested from Yahoo and the day of the last refresh
    def _meta_path(self) -> Path:
        return self.cache / "meta.json"

    def _meta(self) -> dict:
        try:
            return json.loads(self._meta_path().read_text())
        except Exception:
            return {}

    def _set_meta(self, key: str, start: pd.Timestamp) -> None:
        m = self._meta()
        old = m.get(key, {}).get("from")
        frm = min(start, pd.Timestamp(old)) if old else start
        m[key] = {"from": frm.strftime("%Y-%m-%d"), "fetched": pd.Timestamp.today().strftime("%Y-%m-%d")}
        self._meta_path().write_text(json.dumps(m, indent=1, sort_keys=True))

    def _download(self, symbol: str, start: pd.Timestamp, end: pd.Timestamp, adjusted: bool) -> pd.Series:
        yf = self._yf()
        last_err = None
        for attempt in range(3):
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
                time.sleep(2 ** attempt)
        log.debug("yfinance error for %s: %s", symbol, last_err)
        raise DataError(_unreachable(symbol)) from last_err

    def close(self, symbol, start, end, adjusted=False):
        """Daily closes. Refreshed at most once per day. Because Yahoo rescales history
        after splits (Close) and dividends (Adj Close), an incremental refresh compares
        the overlapping rows with the cache and refetches the full range on mismatch."""
        start, end = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
        key = (symbol, adjusted)
        kind = "adj" if adjusted else "close"
        mkey = f"{symbol}|{kind}"
        if key in self._mem:
            cached, covered_from = self._mem[key]
            if covered_from <= start:
                return cached.loc[start:end]
        cached = self._read(symbol, kind)
        meta = self._meta().get(mkey, {})
        today = pd.Timestamp.today().normalize()
        covered_from = pd.Timestamp(meta["from"]) if meta.get("from") and not cached.empty else None
        fresh = (
            not cached.empty
            and covered_from is not None
            and covered_from <= start
            and meta.get("fetched") == today.strftime("%Y-%m-%d")
        )
        if not fresh and self.offline:
            if cached.empty:
                raise DataError(_no_saved(symbol))
            else:
                log.warning("offline: using cached prices for %s (last %s)", symbol, cached.index.max().date())
        elif not fresh:
            want_end = max(min(end, today), start)
            if cached.empty or covered_from is None or covered_from > start:
                new = self._download(symbol, start, want_end, adjusted)
                full = True
            else:
                inc_start = cached.index.max() - pd.Timedelta(days=self.refresh_days)
                new = self._download(symbol, inc_start, want_end, adjusted)
                full = False
                overlap = cached.index.intersection(new.index)
                if len(overlap) and ((new.loc[overlap] / cached.loc[overlap] - 1).abs() > 0.005).any():
                    log.info("%s: history rescaled by Yahoo (split/dividend), refetching", symbol)
                    new = self._download(symbol, covered_from, want_end, adjusted)
                    full = True
            if new.empty:
                if cached.empty:
                    log.warning("no price data for %s", symbol)
                else:
                    log.warning("refresh failed for %s, using cached prices up to %s", symbol, cached.index.max().date())
            else:
                if full:
                    cached = new
                else:
                    merged = pd.concat([cached, new])
                    cached = merged[~merged.index.duplicated(keep="last")].sort_index()
                cached.rename("close").to_frame().to_csv(self._path(symbol, kind))
                self._set_meta(mkey, start if covered_from is None else min(start, covered_from))
        self._mem[key] = (cached, start if covered_from is None else min(start, covered_from))
        return cached.loc[start:end]

    def currency(self, symbol):
        if symbol in self._ccy:
            return self._ccy[symbol]
        p = self.cache / "currencies.csv"
        table = {}
        if p.exists():
            try:
                table = pd.read_csv(p, index_col=0, keep_default_na=False, dtype=str).iloc[:, 0].to_dict()
            except Exception:
                table = {}
        code = table.get(symbol)
        if not code and not self.offline:
            try:
                code = self._yf().Ticker(symbol).fast_info["currency"]
            except Exception as e:
                log.debug("yfinance error for %s: %s", symbol, e)
                raise DataError(_unreachable(symbol)) from e
            if code:
                table[symbol] = code
                pd.Series(table, name="currency").to_csv(p)
        res = normalise_currency(code)
        if res is None and self.offline:
            raise DataError(_no_saved(symbol))
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
                s.rename("split").to_frame().to_csv(p)  # cache only successful lookups
            except Exception as e:
                log.warning("could not fetch splits for %s: %s", symbol, e)
                s = self._read(symbol, "splits")  # fall back to an older cache if any
        s = s[s > 0]
        self._splits[symbol] = s
        return s


def _fx_close(provider: Provider, symbol: str, start, end) -> pd.Series:
    try:
        s = provider.close(symbol, start, end)
    except DataError as e:
        if "could not reach" in str(e):
            raise
        return pd.Series(dtype=float)  # e.g. offline without saved data: try the other routes
    return s[s > 0]


def fx_series(provider: Provider, ccy: str, base: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    """Units of `base` per 1 unit of `ccy`, daily close, may have gaps. 1.0 if same currency."""
    if ccy == base:
        return pd.Series(1.0, index=pd.date_range(start, end, freq="D"))
    direct = _fx_close(provider, f"{ccy}{base}=X", start, end)
    if not direct.empty:
        return direct
    inverse = _fx_close(provider, f"{base}{ccy}=X", start, end)
    if not inverse.empty:
        return 1.0 / inverse
    if ccy != "USD" and base != "USD":  # cross via USD
        try:
            a = fx_series(provider, ccy, "USD", start, end)
            b = fx_series(provider, "USD", base, start, end)
        except DataError as e:
            if "could not reach" in str(e):
                raise
        else:
            cal = a.index.union(b.index)
            return (a.reindex(cal).ffill() * b.reindex(cal).ffill()).dropna()
    raise DataError(f"no exchange rate {ccy}->{base} available (needs internet, or add it to the prices file)")
