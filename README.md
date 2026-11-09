# Portfolio tracker (DCA aware, CLI)

Step by step usage: see [GUIDE.md](GUIDE.md).

## No-command app (macOS)

Double-click `Start Portfolio Tracker.command`. The first time it creates `.venv` and installs the requirements, then opens a local page in the browser (`http://127.0.0.1:8765`) for every command below: choose or create the transactions file, view and delete rows, add transactions, savings plans, reports with all options (JSON and daily CSV downloads) and the example. Close the Terminal window to stop it. The same page starts with:

```
python -m portfolio_tracker ui            # or: python -m portfolio_tracker.ui   (--port N, --no-browser)
```

It uses the standard library only, listens on 127.0.0.1 only, rejects requests from other sites, never repeats an action on reload, warns before a savings plan that would duplicate existing buys, and keeps `file.csv.bak` before deleting a row.

A portfolio tracker for dated buy/sell/dividend transactions in ETFs and stocks. It values the portfolio every day from Yahoo Finance prices, so the returns account for when each contribution was made.

## Install

```
pip install -r requirements.txt
```

## Input: transactions CSV

```
date,type,ticker,quantity,price,fee,currency,amount,fx_rate
2024-01-02,buy,VWCE.DE,2.5,105.10,1.00,,,
2024-02-01,buy,AAPL,3,185.20,1.50,USD,,0.9215
2024-08-15,dividend,AAPL,,,,USD,2.40,
2025-06-02,sell,AAPL,1,201.00,1.50,,,
```

| column   | meaning |
|----------|---------|
| date     | trade date, YYYY-MM-DD |
| type     | `buy`, `sell` or `dividend` |
| ticker   | Yahoo symbol (`VWCE.DE`, `AAPL`, `CSPX.L`, ...) |
| quantity | units bought or sold, as traded (before any later split) |
| price    | price per unit in `currency` |
| fee      | optional, in `currency`. Added to cost on buys and deducted from proceeds on sells |
| currency | optional. Defaults to the ticker's trading currency, in its major unit (GBP, not pence) |
| amount   | dividends: the total net cash received |
| fx_rate  | optional: base currency per 1 unit of `currency` that your broker actually charged |

- Separators: commas, semicolons or tabs.
- Rows can be in any order.
- Dates: `YYYY-MM-DD` or `DD.MM.YYYY`.
- Decimal comma: accepted (`118,40`), and so are `1.234,50` and `1,234.50`.
- Ambiguous values such as `1,234` are rejected, not guessed.
- Pence: put `GBX` (or `GBp`) in the currency column and the values are converted to GBP.
- Price check: a trade price more than 20x off the market price that day produces a warning, which usually means pence and pounds were mixed up.

To append a row from the command line:

```
python -m portfolio_tracker add my.csv buy VWCE.DE 2.5 105.10 --date 2024-01-02 --fee 1
python -m portfolio_tracker add my.csv dividend AAPL 0 2.40 --date 2024-08-15 --currency USD
```

To generate a regular savings plan from market closes, for example 40 EUR every Monday (a holiday moves to the next trading day):

```
python -m portfolio_tracker dca my.csv NQSE.DE 40 --start 2024-11-11 --weekday mon
python -m portfolio_tracker dca my.csv VWCE.DE 200 --start 2024-01-01 --monthly 15 --fee 1
```

## Report

```
python -m portfolio_tracker report my.csv --base EUR --benchmark VWCE.DE --benchmark SPY
```

Options: `--as-of YYYY-MM-DD`, `--json`, `--export-daily daily.csv`, `--top N`, `--offline` (use the cache only), `--prices file.csv` (offline price file instead of Yahoo), `--no-split-adjust`.

Try it without network access:

```
python -m portfolio_tracker report examples/transactions_example.csv \
    --prices examples/prices_synthetic.csv --as-of 2026-11-09 --benchmark VWCE.DE
```

The example is 2 years of buying NQSE.DE for 40 EUR every Monday (2024-11-11 to 2026-11-09). The example prices are synthetic random walks, not real market data. To build the same plan from real Yahoo prices, use the `dca` command.

## What it computes

All figures are in the base currency.

- **Total P/L** = market value + sale proceeds + dividends - everything bought (fees included), split into realized, unrealized and dividends. Cost basis is the average cost in the base currency, at the FX rate on each trade date.
- **Total return** = total P/L / total bought.
- **XIRR (money-weighted, annualised)**: the rate your own cash flows actually earned, including the timing of your DCA buys. This is the number to look at for "how well is my money doing".
- **TWR (time-weighted)**: the investments' own performance, independent of when money was added. Compare it with an index or fund. Daily chain-linked with flows at the end of the day: `r_t = (V_t + out_t - in_t) / V_{t-1} - 1`. On the first day, or when the day's buy is larger than the prior value, it uses `(V_t + out_t) / (V_{t-1} + in_t) - 1`.
- If XIRR is below TWR, your contributions were timed unfavourably, for example large buys just before a drop.
- **Period returns** for 1W, 1M, 3M, 6M, YTD, 1Y, 3Y, 5Y and ALL (since inception). A period that starts before your first transaction is left out. Each period shows:
  - absolute P/L = end value - start value - net money added, so contributions are not counted as gains
  - TWR
  - Modified Dietz money-weighted return
  - the benchmark TWR, if you gave one
- **Biggest movers (up and down)** for 1M, 1Y and all time:
  - each holding's P/L in the period
  - its additive contribution to the portfolio's money-weighted return (contributions add up to the portfolio return)
  - the holding's own TWR
- **Benchmark replay**: the same cash flows on the same dates go into the benchmark (dividend-adjusted close). It reports end value, P/L, XIRR, and the difference from your portfolio.
- **Max drawdown** of the TWR index, with peak, trough and recovery dates.
- **Per holding**: quantity, value, weight, average cost, cost basis, unrealized and realized P/L, dividends, P/L %, and the holding's own XIRR.
- **Warnings**: stale prices, missing data, transactions after the valuation date.

### Conventions

- Valuation uses Yahoo's `Close`, which is adjusted for splits but not for dividends. Dividends are counted only when you enter them as `dividend` rows. Accumulating ETFs need nothing extra.
- Splits are fetched from Yahoo, and quantities entered before a split are scaled automatically. Use `--no-split-adjust` if you already entered post-split quantities.
- Quantities are always shown in today's post-split units. Yahoo adjusts its whole price history for every split up to today, so this also applies with `--as-of`.
- Weekends and holidays use the last close (forward-filled).
- Trades are booked at your trade price. A day's TWR measures the move from the trade price to the close.
- For a history under 1 year, annualised figures are extrapolations and are flagged as such. The cumulative figures are the reliable ones.
- Yahoo data is cached and refreshed at most once a day. If Yahoo rescales history after a split, or after a dividend for the adjusted series, the whole range is downloaded again.
- If Yahoo is unreachable, prices come from the cache in `~/.cache/portfolio_tracker` (or `$PORTFOLIO_CACHE`). Without a cache, holdings are valued at the last transaction price, with a warning.

## Offline price file format

```
date,symbol,close,currency,adj_close,split
2024-01-02,VWCE.DE,105.1,EUR,,
2024-01-02,USDEUR=X,0.912,EUR,,
```

FX symbols use Yahoo's convention. `USDEUR=X` is the price of 1 USD in EUR. The inverse pair is also accepted.

## Tests

```
python -m pytest -q
```
