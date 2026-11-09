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
import io
import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
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
_MINOR = {"GBX": ("GBP", 0.01), "ZAC": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}


def _at(line: int) -> str:
    """Prefix for error messages; empty when the value does not come from a file line."""
    return f"line {line}: " if line else ""


def _num(raw: Optional[str], name: str, line: int, default: Optional[float] = 0.0,
         decimal_comma: bool = False) -> Optional[float]:
    """Parse 1234.5, 1234,5, 1,234.50, 1.234,50, 1 234,5. Rejects ambiguous 1,234 and nan/inf.

    decimal_comma: the value comes from a file whose separator is not a comma (e.g. Czech
    Excel ';' files). There a single comma can only be the decimal mark, so 12,125 = 12.125.
    """
    if raw is None or str(raw).strip() == "":
        return default
    txt = str(raw).strip().replace("\u00a0", "").replace(" ", "").replace("'", "")
    bad = LedgerError(f"{_at(line)}{name}={raw!r} is not a valid number (check the thousands and decimal separators)")
    if "," in txt and "." in txt:
        if txt.rfind(",") > txt.rfind("."):  # 1.234.567,8
            if not re.fullmatch(r"-?\d{1,3}(\.\d{3})+,\d+", txt):
                raise bad
            txt = txt.replace(".", "").replace(",", ".")
        else:  # 1,234,567.8
            if not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", txt):
                raise bad
            txt = txt.replace(",", "")
    elif txt.count(",") > 1:
        if not re.fullmatch(r"-?\d{1,3}(,\d{3})+", txt):
            raise bad
        txt = txt.replace(",", "")
    elif txt.count(".") > 1:
        if not re.fullmatch(r"-?\d{1,3}(\.\d{3})+", txt):
            raise bad
        txt = txt.replace(".", "")
    elif "," in txt:
        if _AMBIGUOUS.match(txt) and not decimal_comma:
            raise LedgerError(
                f"{_at(line)}{name}={raw!r} is ambiguous (thousands or decimal separator?); "
                "write 1234 or 1.234 / 1234,0"
            )
        txt = txt.replace(",", ".")
    try:
        val = float(txt)
    except ValueError as e:
        raise LedgerError(f"{_at(line)}{name}={raw!r} is not a number") from e
    if not math.isfinite(val):
        raise LedgerError(f"{_at(line)}{name}={raw!r} is not a finite number")
    return val


DATE_MIN, DATE_MAX = pd.Timestamp("1950-01-01"), pd.Timestamp("2100-12-31")
MAX_QUANTITY = 1e12


def _date(raw: Optional[str], line: int) -> pd.Timestamp:
    txt = (raw or "").strip()
    if not txt:
        raise LedgerError(f"{_at(line)}missing date")
    try:
        if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", txt):
            d = pd.Timestamp(txt)
        elif re.match(r"^\d{1,2}\.\d{1,2}\.\d{4}$", txt):
            d = pd.to_datetime(txt, format="%d.%m.%Y")
        else:
            raise ValueError
    except (ValueError, TypeError) as e:
        raise LedgerError(f"{_at(line)}bad date {txt!r}, use YYYY-MM-DD (or DD.MM.YYYY)") from e
    if pd.isna(d):
        raise LedgerError(f"{_at(line)}bad date {txt!r}")
    d = d.normalize()
    if not DATE_MIN <= d <= DATE_MAX:
        raise LedgerError(f"{_at(line)}date {txt!r} is out of range (1950-01-01 to 2100-12-31)")
    return d


_TICKER = re.compile(r"[A-Z0-9.\-=^&]{1,20}")


def clean_ticker(raw: Optional[str], line: int = 0) -> str:
    """Upper-case ticker; only letters, digits and . - = ^ & (at most 20 characters) are accepted."""
    t = (raw or "").strip().upper()
    if not t:
        raise LedgerError(f"{_at(line)}missing ticker")
    if not _TICKER.fullmatch(t):
        raise LedgerError(f"{_at(line)}ticker {raw!r} is not valid: use 1 to 20 letters, digits or . - = ^ & "
                          "(no spaces), e.g. VWCE.DE")
    return t


def parse_rows(rows: list[dict], start_line: int = 2, lines: Optional[list[int]] = None,
               decimal_comma: bool = False) -> list[Transaction]:
    txs: list[Transaction] = []
    for i, row in enumerate(rows):
        line = lines[i] if lines else start_line + i
        row = {(k or "").strip().lower(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
        if not any(row.values()):
            continue
        d = _date(row.get("date"), line)
        t = (row.get("type") or "").lower()
        if t not in VALID_TYPES:
            raise LedgerError(f"{_at(line)}type must be one of {sorted(VALID_TYPES)}, got {t!r}")
        ticker = clean_ticker(row.get("ticker"), line)
        raw_ccy = (row.get("currency") or "").strip()
        if raw_ccy and not re.fullmatch(r"[A-Za-z]{3}", raw_ccy):
            raise LedgerError(f"{_at(line)}currency {raw_ccy!r} must be a 3-letter code such as EUR, USD or GBP")
        scale = 1.0
        if raw_ccy == "GBp" or raw_ccy.upper() in _MINOR:
            raw_ccy, scale = _MINOR.get(raw_ccy.upper(), ("GBP", 0.01))
        ccy = raw_ccy.upper() or None
        tx = Transaction(
            date=d,
            type=t,
            ticker=ticker,
            quantity=_num(row.get("quantity"), "quantity", line, decimal_comma=decimal_comma),
            price=_num(row.get("price"), "price", line, decimal_comma=decimal_comma),
            fee=_num(row.get("fee"), "fee", line, decimal_comma=decimal_comma),
            currency=ccy,
            amount=_num(row.get("amount"), "amount", line, decimal_comma=decimal_comma),
            fx_rate=_num(row.get("fx_rate"), "fx_rate", line, default=None, decimal_comma=decimal_comma),
            line=line,
        )
        if scale != 1.0:  # prices entered in pence: convert to pounds
            tx.price *= scale
            tx.fee *= scale
            tx.amount *= scale
        if t in ("buy", "sell"):
            if tx.quantity <= 0:
                raise LedgerError(f"{_at(line)}quantity must be > 0")
            if tx.quantity >= MAX_QUANTITY:
                raise LedgerError(f"{_at(line)}quantity {tx.quantity:g} is unrealistically large (must be below 1e12)")
            if tx.price <= 0:
                raise LedgerError(f"{_at(line)}price must be > 0")
        if t == "dividend" and tx.amount == 0 and tx.quantity > 0 and tx.price > 0:
            tx.amount = tx.quantity * tx.price  # per-share form: quantity x dividend per share
        if t == "dividend" and tx.amount <= 0:
            raise LedgerError(f"{_at(line)}dividend amount must be > 0")
        if tx.fee < 0:
            raise LedgerError(f"{_at(line)}fee must be >= 0")
        if tx.fx_rate is not None and tx.fx_rate <= 0:
            raise LedgerError(f"{_at(line)}fx_rate must be > 0")
        txs.append(tx)
    # stable: by date, buys before sells/dividends on the same day
    order = {"buy": 0, "sell": 1, "dividend": 2}
    txs.sort(key=lambda x: (x.date, order[x.type], x.line))
    return txs


def parse_text(text: str, allow_empty: bool = False) -> list[Transaction]:
    """Parse CSV text (BOM already removed)."""
    f = io.StringIO(text, newline="")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(f, dialect=dialect)
    rows, lines = [], []
    for row in reader:
        rows.append(row)
        lines.append(reader.line_num)
    txs = parse_rows(rows, lines=lines, decimal_comma=dialect.delimiter != ",")
    if not txs and not allow_empty:
        raise LedgerError("no transactions in the file")
    return txs


def load_transactions(path: str | Path, allow_empty: bool = False) -> list[Transaction]:
    path = Path(path)
    if not path.exists():
        raise LedgerError(f"transactions file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        text = f.read()
    try:
        return parse_text(text, allow_empty)
    except LedgerError as e:
        if str(e) == "no transactions in the file":
            raise LedgerError(f"no transactions in {path}") from None
        raise


_NUMERIC = ("quantity", "price", "fee", "amount", "fx_rate")
_PLAIN = re.compile(r"^-?\d+(\.\d+)?$")


def _fmt_value(v, name: str, decimal_comma: bool) -> str:
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return ""
    if name in _NUMERIC:
        if isinstance(v, str):
            txt = v.strip()
            if not _PLAIN.match(txt):
                txt = format(Decimal(str(_num(txt, name, 0))), "f")
        else:
            txt = format(Decimal(str(float(v))), "f")
        if not decimal_comma:
            return txt
        txt = txt.replace(".", ",")
        # 12,125 would read back as ambiguous (thousands or decimals?); 12,1250 is the same value and is not
        return txt + "0" if _AMBIGUOUS.match(txt) else txt
    return str(v)


def _file_style(path: Path) -> dict:
    """Delimiter, decimal comma, line ending, header and trailing-newline state of an existing ledger."""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        delimiter = ","
    eol = "\r\n" if b"\r\n" in raw else "\n"
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    header = rows[0] if rows else []
    cols = [h.strip().lower() for h in header] or list(CSV_COLUMNS)
    decimal_comma = delimiter == ";"
    if not decimal_comma:
        for r in rows[1:]:
            for c, v in zip(cols, r):
                if c in _NUMERIC and "," in v:
                    decimal_comma = True
    return {"delimiter": delimiter, "decimal_comma": decimal_comma, "eol": eol, "cols": cols,
            "needs_newline": bool(raw) and raw[-1:] not in (b"\n", b"\r")}


def _decode_existing(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8").removeprefix("\ufeff")
    except UnicodeDecodeError:
        raise LedgerError(f"{path} cannot be read as UTF-8 text; save it as CSV (UTF-8) first. Nothing was written.") from None


def append_transactions(path: str | Path, rows_in: list[dict]) -> None:
    """Validate every row, then append them all in a single write (all or nothing).

    An existing file must be readable (UTF-8, valid rows) and must still parse, with exactly the new rows
    added, after appending; otherwise nothing is written.
    """
    path = Path(path)
    parsed = []
    for fields in rows_in:
        row = {c: "" if fields.get(c) is None else str(fields.get(c)) for c in CSV_COLUMNS}
        try:
            tx = parse_rows([row], lines=[0])[0]  # raises on invalid input
        except LedgerError as e:
            raise LedgerError(re.sub(r"^line \d+: ", "", str(e))) from None
        parsed.append(tx)
    new = not path.exists() or path.stat().st_size == 0
    existing_text = ""
    if new:
        style = {"delimiter": ",", "decimal_comma": False, "eol": "\r\n", "cols": list(CSV_COLUMNS), "needs_newline": False}
    else:
        existing_text = _decode_existing(path)
        try:
            before = len(parse_text(existing_text, allow_empty=True))
        except LedgerError as e:
            raise LedgerError(f"{path} has a problem, so nothing was added: {e}") from None
        style = _file_style(path)
    cols = style["cols"]
    for fields in rows_in:
        missing = [c for c in CSV_COLUMNS if fields.get(c) not in (None, "") and c not in cols]
        if missing:
            raise LedgerError(f"{path}: header has no column for {missing}; add the column(s) first")
    buf = io.StringIO(newline="")
    if style["needs_newline"]:
        buf.write(style["eol"])
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", delimiter=style["delimiter"],
                       lineterminator=style["eol"])
    if new:
        w.writeheader()
    for fields, tx in zip(rows_in, parsed):
        fields = {**fields, "date": tx.date.strftime("%Y-%m-%d"), "ticker": tx.ticker}  # always YYYY-MM-DD
        w.writerow({c: _fmt_value(fields.get(c), c, style["decimal_comma"]) for c in cols})
    if not new:
        try:
            after = len(parse_text(existing_text + buf.getvalue(), allow_empty=True))
        except LedgerError:
            after = -1
        if after != before + len(rows_in):
            raise LedgerError(f"{path} is not in a shape new rows can be added to safely (for example an unclosed "
                              "quote); nothing was added. Fix the file first.")
    with path.open("ab") as f:  # appending keeps an existing BOM untouched
        f.write(buf.getvalue().encode("utf-8"))


def append_transaction(path: str | Path, **fields) -> None:
    """Append one row, creating the file with a header if needed. Validates first."""
    append_transactions(path, [fields])
