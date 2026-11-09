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


def _num(raw: Optional[str], name: str, line: int, default: Optional[float] = 0.0) -> Optional[float]:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip().replace(",", "."))
    except ValueError as e:
        raise LedgerError(f"line {line}: {name}={raw!r} is not a number") from e


def parse_rows(rows: list[dict], start_line: int = 2) -> list[Transaction]:
    txs: list[Transaction] = []
    for i, row in enumerate(rows):
        line = start_line + i
        row = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if not any(row.values()):
            continue
        try:
            d = pd.Timestamp(row.get("date", "")).normalize()
        except (ValueError, TypeError) as e:
            raise LedgerError(f"line {line}: bad date {row.get('date')!r}") from e
        if pd.isna(d):
            raise LedgerError(f"line {line}: missing date")
        t = (row.get("type") or "").lower()
        if t not in VALID_TYPES:
            raise LedgerError(f"line {line}: type must be one of {sorted(VALID_TYPES)}, got {t!r}")
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            raise LedgerError(f"line {line}: missing ticker")
        ccy = (row.get("currency") or "").strip().upper() or None
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
        rows = list(reader)
    txs = parse_rows(rows)
    if not txs:
        raise LedgerError(f"no transactions in {path}")
    return txs


def append_transaction(path: str | Path, **fields) -> None:
    """Append one row, creating the file with a header if needed. Validates first."""
    path = Path(path)
    row = {c: "" if fields.get(c) is None else str(fields.get(c)) for c in CSV_COLUMNS}
    parse_rows([row])  # raises on invalid input
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with path.open(newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        cols = [h.strip().lower() for h in header] or CSV_COLUMNS
        with path.open("rb") as fb:
            fb.seek(-1, 2)
            needs_newline = fb.read(1) not in (b"\n", b"\r")
    else:
        cols = CSV_COLUMNS
        needs_newline = False
    with path.open("a", newline="", encoding="utf-8") as f:
        if needs_newline:
            f.write("\r\n")
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)
