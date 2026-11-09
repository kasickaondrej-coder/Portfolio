"""Transaction ledger: loading, validation, appending.

CSV columns (header required, extra columns ignored):
    date        YYYY-MM-DD (trade date)
    type        buy | sell | dividend
    ticker      Yahoo Finance symbol, e.g. VWCE.DE, AAPL, CSPX.L
    quantity    shares/units (buy/sell). Ignored for dividends.
    price       price per unit in `currency` (buy/sell). Ignored for dividends.
    fee         optional, in `currency`. Added to cost on buys, deducted from proceeds on sells.
    currency    optional ISO code of price/fee/amount. Default: the ticker's trading currency
                (major unit, so GBP for pence-quoted London tickers).
    amount      dividends only: total net cash received, in `currency`.
    fx_rate     optional: units of base currency per 1 unit of `currency` actually applied
                by the broker. Overrides the market FX rate for this transaction.
"""
from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

VALID_TYPES = {"buy", "sell", "dividend"}
CSV_COLUMNS = ["date", "type", "ticker", "quantity", "price", "fee", "currency", "amount", "fx_rate"]


class LedgerError(ValueError):
    pass


@dataclass
class Transaction:
    date: pd.Timestamp
    type: str
    ticker: str
    quantity: float = 0.0
    price: float = 0.0
    fee: float = 0.0
    currency: Optional[str] = None
    amount: float = 0.0
    fx_rate: Optional[float] = None
    line: int = 0
    # split adjusted copies, filled by the engine
    adj_quantity: float = field(default=0.0, repr=False)
    adj_price: float = field(default=0.0, repr=False)

    @property
    def gross(self) -> float:
        """Trade value in transaction currency, excluding fees."""
        return self.quantity * self.price


_AMBIGUOUS = re.compile(r"^-?[1-9]\d{0,2},\d{3}$")
# minor currency units a user might type in the currency column -> (major code, scale)
_MINOR = {"GBX": ("GBP", 0.01), "GBP_PENCE": ("GBP", 0.01), "ZAC": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}


def _num(raw: Optional[str], name: str, line: int, default: Optional[float] = 0.0) -> Optional[float]:
    """Parse 1234.5, 1234,5, 1,234.50, 1.234,50, 1 234,5. Rejects ambiguous 1,234 and nan/inf."""
    if raw is None or str(raw).strip() == "":
        return default
    txt = str(raw).strip().replace("\u00a0", "").replace(" ", "").replace("'", "")
    if "," in txt and "." in txt:
        if txt.rfind(",") > txt.rfind("."):
            txt = txt.replace(".", "").replace(",", ".")
        else:
            txt = txt.replace(",", "")
    elif txt.count(",") > 1:
        txt = txt.replace(",", "")
    elif "," in txt:
        if _AMBIGUOUS.match(txt):
            raise LedgerError(
                f"line {line}: {name}={raw!r} is ambiguous (thousands or decimal separator?); "
                "write 1234 or 1.234 / 1234,0"
            )
        txt = txt.replace(",", ".")
    try:
        val = float(txt)
    except ValueError as e:
        raise LedgerError(f"line {line}: {name}={raw!r} is not a number") from e
    if not math.isfinite(val):
        raise LedgerError(f"line {line}: {name}={raw!r} is not a finite number")
    return val


def _date(raw: Optional[str], line: int) -> pd.Timestamp:
    txt = (raw or "").strip()
    if not txt:
        raise LedgerError(f"line {line}: missing date")
    try:
        if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", txt):
            d = pd.Timestamp(txt)
        elif re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}$", txt):
            d = pd.to_datetime(txt, format="%d.%m.%Y")
        else:
            raise ValueError
    except (ValueError, TypeError) as e:
        raise LedgerError(f"line {line}: bad date {txt!r}, use YYYY-MM-DD (or DD.MM.YYYY)") from e
    if pd.isna(d):
        raise LedgerError(f"line {line}: bad date {txt!r}")
    return d.normalize()


def parse_rows(rows: list[dict], start_line: int = 2, lines: Optional[list[int]] = None) -> list[Transaction]:
    txs: list[Transaction] = []
    for i, row in enumerate(rows):
        line = lines[i] if lines else start_line + i
        row = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if not any(row.values()):
            continue
        d = _date(row.get("date"), line)
        t = (row.get("type") or "").lower()
        if t not in VALID_TYPES:
            raise LedgerError(f"line {line}: type must be one of {sorted(VALID_TYPES)}, got {t!r}")
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            raise LedgerError(f"line {line}: missing ticker")
        raw_ccy = (row.get("currency") or "").strip()
        scale = 1.0
        if raw_ccy == "GBp" or raw_ccy.upper() in _MINOR:
            raw_ccy, scale = _MINOR.get(raw_ccy.upper(), ("GBP", 0.01))
        ccy = raw_ccy.upper() or None
        tx = Transaction(
            date=d,
            type=t,
            ticker=ticker,
            quantity=_num(row.get("quantity"), "quantity", line),
            price=_num(row.get("price"), "price", line),
            fee=_num(row.get("fee"), "fee", line),
            currency=ccy,
            amount=_num(row.get("amount"), "amount", line),
            fx_rate=_num(row.get("fx_rate"), "fx_rate", line, default=None),
            line=line,
        )
        if scale != 1.0:  # prices entered in pence: convert to pounds
            tx.price *= scale
            tx.fee *= scale
            tx.amount *= scale
        if t in ("buy", "sell"):
            if tx.quantity <= 0:
                raise LedgerError(f"line {line}: quantity must be > 0")
            if tx.price < 0:
                raise LedgerError(f"line {line}: price must be >= 0")
        if t == "dividend" and tx.amount == 0 and tx.quantity > 0 and tx.price > 0:
            tx.amount = tx.quantity * tx.price  # per-share form: quantity x dividend per share
        if tx.fee < 0:
            raise LedgerError(f"line {line}: fee must be >= 0")
        if tx.fx_rate is not None and tx.fx_rate <= 0:
            raise LedgerError(f"line {line}: fx_rate must be > 0")
        txs.append(tx)
    # stable: by date, buys before sells/dividends on the same day
    order = {"buy": 0, "sell": 1, "dividend": 2}
    txs.sort(key=lambda x: (x.date, order[x.type], x.line))
    return txs


def load_transactions(path: str | Path) -> list[Transaction]:
    path = Path(path)
    if not path.exists():
        raise LedgerError(f"transactions file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        rows, lines = [], []
        for row in reader:
            rows.append(row)
            lines.append(reader.line_num)
    txs = parse_rows(rows, lines=lines)
    if not txs:
        raise LedgerError(f"no transactions in {path}")
    return txs


def append_transaction(path: str | Path, **fields) -> None:
    """Append one row, creating the file with a header if needed. Validates first."""
    path = Path(path)
    row = {c: "" if fields.get(c) is None else str(fields.get(c)) for c in CSV_COLUMNS}
    parse_rows([row])  # raises on invalid input
    new = not path.exists() or path.stat().st_size == 0
    delimiter = ","
    if not new:
        with path.open(newline="", encoding="utf-8-sig") as f:
            sample = f.read(4096)
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
        header = next(csv.reader(sample.splitlines(), delimiter=delimiter), [])
        cols = [h.strip().lower() for h in header] or CSV_COLUMNS
        missing = [c for c in CSV_COLUMNS if row[c] != "" and c not in cols]
        if missing:
            raise LedgerError(f"{path}: header has no column for {missing}; add the column(s) first")
        with path.open("rb") as fb:
            fb.seek(-1, 2)
            needs_newline = fb.read(1) not in (b"\n", b"\r")
    else:
        cols = CSV_COLUMNS
        needs_newline = False
    with path.open("a", newline="", encoding="utf-8") as f:
        if needs_newline:
            f.write("\r\n")
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore", delimiter=delimiter)
        if new:
            w.writeheader()
        w.writerow(row)
