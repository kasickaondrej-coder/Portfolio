"""Command line interface.

    python -m portfolio_tracker report transactions.csv --base EUR --benchmark VWCE.DE
    python -m portfolio_tracker add transactions.csv buy VWCE.DE 2.5 118.40 --date 2025-03-03 --fee 1
    python -m portfolio_tracker dca transactions.csv NQSE.DE 40 --start 2024-11-11 --weekday mon
    python -m portfolio_tracker ui      (local web page in the browser, no commands needed)
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from .analytics import Portfolio
from .data import DataError, StaticProvider, YahooProvider
from .report import render_json, render_text
from .transactions import LedgerError, append_transaction, load_transactions


def _provider(args):
    if args.prices:
        return StaticProvider.from_csv(args.prices)
    return YahooProvider(cache_dir=args.cache_dir, offline=args.offline)


def cmd_report(args) -> int:
    txs = load_transactions(args.transactions)
    pf = Portfolio(txs, _provider(args), base=args.base, as_of=pd.Timestamp(args.as_of) if args.as_of else None,
                   benchmarks=args.benchmark, split_adjust=not args.no_split_adjust)
    rep = pf.report(top_n=args.top)
    print(render_json(rep) if args.json else render_text(rep, top_n=args.top))
    if args.export_daily:
        rep.daily.to_csv(args.export_daily, index_label="date")
    return 0


def cmd_add(args) -> int:
    append_transaction(
        args.transactions, date=args.date, type=args.type, ticker=args.ticker,
        quantity=args.quantity if args.type != "dividend" else None,
        price=args.price if args.type != "dividend" else None,
        amount=args.price if args.type == "dividend" else None,
        fee=args.fee, currency=args.currency, fx_rate=args.fx_rate,
    )
    print(f"added {args.type} {args.ticker} on {args.date}")
    return 0


WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4}


def cmd_dca(args, provider=None) -> int:
    """Append a regular savings plan (fixed amount on a fixed weekday or day of month) using market closes."""
    provider = provider or _provider(args)
    start = pd.Timestamp(args.start).normalize()
    end = pd.Timestamp(args.end).normalize() if args.end else pd.Timestamp.today().normalize()
    if args.monthly:
        dates = [d for d in pd.date_range(start, end, freq="D") if d.day == args.monthly]
    else:
        dates = list(pd.date_range(start, end, freq="W-" + args.weekday.upper()))
    if not dates:
        raise LedgerError("no plan dates in that range")
    closes = provider.close(args.ticker, start - pd.Timedelta(days=7), end + pd.Timedelta(days=7))
    closes = closes[closes > 0]
    if closes.empty:
        raise DataError(f"no prices for {args.ticker}")
    invest = args.amount - args.fee
    if invest <= 0:
        raise LedgerError("amount must be larger than the fee")
    added = 0
    for d in dates:
        # holiday or weekend: execute on the next trading day with a price (within 6 days)
        nxt = closes.loc[d: d + pd.Timedelta(days=6)]
        if nxt.empty:
            print(f"skipped {d.date()}: no price yet")
            continue
        trade_day, price = nxt.index[0], float(nxt.iloc[0])
        append_transaction(
            args.transactions, date=trade_day.strftime("%Y-%m-%d"), type="buy", ticker=args.ticker.upper(),
            quantity=round(invest / price, args.decimals), price=round(price, 4), fee=args.fee or None,
            currency=args.currency,
        )
        added += 1
    print(f"added {added} buys of {args.ticker.upper()} ({args.amount:g} each) to {args.transactions}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="portfolio_tracker", description="DCA aware portfolio tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("report", help="compute returns from a transactions CSV")
    r.add_argument("transactions")
    r.add_argument("--base", default="EUR", help="base currency for all figures (default EUR)")
    r.add_argument("--as-of", help="valuation date YYYY-MM-DD (default today)")
    r.add_argument("--benchmark", action="append", default=[], help="benchmark symbol, repeatable (e.g. VWCE.DE, SPY)")
    r.add_argument("--prices", help="offline prices CSV (date,symbol,close[,currency,adj_close,split]) instead of Yahoo")
    r.add_argument("--cache-dir", help="price cache directory (default ~/.cache/portfolio_tracker)")
    r.add_argument("--offline", action="store_true", help="use cached Yahoo data only")
    r.add_argument("--no-split-adjust", action="store_true", help="quantities in the CSV are already split adjusted")
    r.add_argument("--top", type=int, default=5, help="number of movers to show")
    r.add_argument("--json", action="store_true", help="JSON output")
    r.add_argument("--export-daily", help="write daily value / flow / TWR index CSV here")
    r.set_defaults(func=cmd_report)

    a = sub.add_parser("add", help="append a transaction to the CSV")
    a.add_argument("transactions")
    a.add_argument("type", choices=["buy", "sell", "dividend"])
    a.add_argument("ticker")
    a.add_argument("quantity", type=float, help="units (use 0 for dividends)")
    a.add_argument("price", type=float, help="price per unit; for dividends the total amount received")
    a.add_argument("--date", default=pd.Timestamp.today().strftime("%Y-%m-%d"))
    a.add_argument("--fee", type=float, default=0.0)
    a.add_argument("--currency")
    a.add_argument("--fx-rate", type=float)
    a.set_defaults(func=cmd_add)

    d = sub.add_parser("dca", help="append a regular savings plan (e.g. 40 EUR every Monday) priced at market closes")
    d.add_argument("transactions")
    d.add_argument("ticker")
    d.add_argument("amount", type=float, help="money spent per purchase, fee included")
    d.add_argument("--start", required=True, help="first plan date YYYY-MM-DD")
    d.add_argument("--end", help="last plan date (default today)")
    d.add_argument("--weekday", choices=sorted(WEEKDAYS, key=WEEKDAYS.get), default="mon", help="weekly plan day (default mon)")
    d.add_argument("--monthly", type=int, metavar="DAY", help="monthly plan on this day of month instead of weekly")
    d.add_argument("--fee", type=float, default=0.0, help="fee per purchase, taken out of the amount")
    d.add_argument("--currency", help="currency of the amount/price if not the ticker's own")
    d.add_argument("--decimals", type=int, default=4, help="decimals for fractional units (default 4)")
    d.add_argument("--prices", help="offline prices CSV instead of Yahoo")
    d.add_argument("--cache-dir")
    d.add_argument("--offline", action="store_true")
    d.set_defaults(func=cmd_dca)

    u = sub.add_parser("ui", help="open the local web page (all of the above without typing commands)")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--no-browser", action="store_true", help="do not open the browser")
    u.set_defaults(func=cmd_ui)
    return ap


def cmd_ui(args) -> int:
    from .ui import serve
    return serve(args.port, open_browser=not args.no_browser)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (LedgerError, DataError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
