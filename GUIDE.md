# User guide

## Using the app without commands

You can do everything in this guide from a simple page in your web browser (Safari or Chrome), without typing commands.

1. In Finder, open the `Portfolio` folder and double-click **`Start Portfolio Tracker.command`**.
2. The first time, macOS may refuse to open it ("cannot be opened because it is from an unidentified developer"). Right-click the file, choose **Open**, then **Open** again. You only do this once.
3. The first start takes a few minutes while it installs what it needs (internet required). After that it starts in a few seconds.
4. Your browser opens the page. A Terminal window stays open next to it: **close that window to stop the app**. If you close only the browser tab, double-click the file again to get it back.

The menu at the top has these pages:

| Page | What it does |
|---|---|
| Transactions | Choose your transactions file (type or paste the path; quotes, `file://` and `~` are fine, and a path without a leading `/` starts in your home folder), or create a new empty one. Shows all rows, sorted by date. **Delete** removes one row after asking and touches nothing else in the file. **Undo last change** puts the file back as it was before the last add, savings plan or delete (it asks first). Press it again to step back one more change; the page shows how many steps are available |
| Add | Add one buy, sell or dividend, with all the columns of section 4 (fee, currency, FX rate are optional) |
| Savings plan | Writes all the buys of a regular plan (every week on a weekday, or every month on a day) at market closing prices, like the `dca` command in section 8. If buys of that ticker already exist in that period, it shows a warning page first and writes nothing until you press "Add the plan anyway" (reloading that page is harmless; the button works once). Only trading weekdays (Monday to Friday) can be chosen |
| Report | Runs the report on your file with every option of section 9: base currency, as-of date, one or more benchmarks (separate them with commas), top movers, offline, already split-adjusted quantities, offline prices file, cache folder. Every field has a short hint; leave the price file and cache folder empty unless you know you need them. A future as-of date is replaced by today, with a note. A short legend explains XIRR, TWR, MWR, drawdown and UP/DOWN. Below the report are links to download it as JSON and the daily values as CSV (for Excel) |
| Example | Runs the built-in example report. It does not touch your own file and needs no internet |

- The file in use is shown at the top of every page, and the app remembers it next time.
- After pressing a button, a yellow "Working, please wait" bar appears. Downloading prices from Yahoo can take a minute; do not click again.
- Errors are shown in a red box saying what to fix. A form with a mistake writes nothing to your file.
- Reloading the page never adds a transaction twice.
- Before every change (add, savings plan, delete, undo) a dated copy of your file is saved in a hidden folder called `.portfolio_backups` next to it. The last 20 copies are kept. **Undo last change** on the Transactions page restores the newest one; pressing it again steps further back. Undone versions are kept in `.portfolio_backups/undone`, so nothing is ever lost. This is also how you take back a savings plan you ran twice by mistake.
- A path to a folder that does not exist is refused with a message, and file problems (no permission, a file that is not UTF-8 text) are explained in plain words instead of a technical error.
- From the terminal the same page starts with `python -m portfolio_tracker ui`.

The rest of this guide explains the same features as terminal commands, and what the numbers mean.

## 1. Start a session (every time)

```
cd ~/Desktop/Claude_code/Agent_C/Portfolio
source .venv/bin/activate
```

When it works, the prompt starts with `(.venv)`. Every command below assumes you are in this folder with `(.venv)` active.

## 2. Yahoo Finance prices

There is nothing to switch on: no account and no API key. Run `report` without `--prices` and it downloads prices from Yahoo automatically.

- `--prices file.csv` replaces Yahoo with a local price file. Only the example uses it.
- Check that Yahoo is reachable from your computer:

  ```
  python -c "import yfinance as yf; print(yf.download('VWCE.DE', period='5d', progress=False))"
  ```

  A table of recent prices means it works. An empty table or an error means Yahoo is blocked or rate limited. Wait a few minutes and try again.
- Downloaded prices are cached in `~/.cache/portfolio_tracker` and refreshed at most once a day.
- `--offline` uses only the cache, with no internet.
- To force a full re-download, delete the cache: `rm -rf ~/.cache/portfolio_tracker`

## 3. Finding the right ticker

Search the instrument on https://finance.yahoo.com and use the symbol exactly as Yahoo shows it. The suffix tells Yahoo which exchange the price comes from:

| Exchange | Suffix | Example |
|---|---|---|
| Xetra (Germany, EUR) | `.DE` | `VWCE.DE`, `SXR8.DE`, `EUNL.DE` |
| Amsterdam (EUR) | `.AS` | `IWDA.AS`, `VUSA.AS` |
| Milan (EUR) | `.MI` | `SWDA.MI` |
| London (GBP or pence) | `.L` | `CSPX.L`, `VWRL.L` |
| Prague (CZK) | `.PR` | `CEZ.PR`, `KOMB.PR` |
| US stocks and ETFs | none | `AAPL`, `MSFT`, `SPY`, `VOO` |
| Crypto | `-USD` | `BTC-USD` |

Use the listing you actually bought, because the same ETF trades on several exchanges in different currencies. For example, if your broker bought VWCE on Xetra, use `VWCE.DE`.

## 4. Transactions file

Create `my.csv` in the `Portfolio` folder. You can write it in Excel or Numbers and choose File > Export > CSV, or in any text editor. The first line must be the header:

```
date,type,ticker,quantity,price,fee,currency,amount,fx_rate
```

One line per transaction. Empty columns are fine.

| Column | Required | Meaning |
|---|---|---|
| date | yes | `2025-03-03` or `03.03.2025` |
| type | yes | `buy`, `sell` or `dividend` |
| ticker | yes | Yahoo symbol, see section 3 |
| quantity | buy/sell | units bought or sold (fractions OK, e.g. `1.8342`) |
| price | buy/sell | price per unit you paid or received |
| fee | no | broker fee for that transaction |
| currency | no | currency of price, fee and amount. Empty means the instrument's own trading currency |
| amount | dividends | total cash received, after tax |
| fx_rate | no | the exchange rate your broker actually used, see section 6 |

Excel set to Czech may save with `;` separators and decimal commas (`118,40`). Both are accepted.

## 5. Multiple instruments

Put every instrument in the same file, one row per transaction. Order does not matter, because the file is sorted by date automatically.

```
date,type,ticker,quantity,price,fee,currency,amount,fx_rate
2024-01-02,buy,VWCE.DE,2.5,105.10,1.00,,,
2024-01-02,buy,AAPL,1,185.20,1.50,,,
2024-02-01,buy,VWCE.DE,1.9,108.40,1.00,,,
2024-02-01,buy,CSPX.L,0.5,480.20,0,,,
2024-03-01,buy,VWCE.DE,1.8,110.95,1.00,,,
2024-03-04,buy,CEZ.PR,10,850,40,,,
2024-08-15,dividend,AAPL,,,,USD,1.20,
2025-06-02,sell,AAPL,0.5,201.00,1.50,,,
```

Each holding is shown separately, and all of them are combined into one portfolio result. The "biggest movers" sections rank them against each other.

## 6. Currencies

- **Base currency:** `--base` sets the currency every result is shown in, e.g. `--base EUR` or `--base CZK`.
- **Converting prices:** each instrument is valued in its trading currency and converted to the base currency with Yahoo FX rates.
- **Paying in a different currency:** if you paid in a different currency from the price you entered, set the `currency` column to the currency of the `price` you typed.
- **Your broker's exchange rate:** to use the rate your broker actually charged instead of the market rate, fill `fx_rate`. It is the number of base currency units per 1 unit of `currency`. Example with `--base EUR`, buying a USD stock at a rate of 0.92 EUR per USD:

  ```
  2024-02-01,buy,AAPL,3,185.20,1.50,USD,,0.92
  ```

  If you enter it upside down (1.087 instead of 0.92), the report warns you.
- **London pence:** London prices may be quoted in pence (GBp). Enter your price in pounds (`480.20`), or in pence with `GBX` in the currency column (`48020`, `GBX`).

## 7. Dividends, sells, fees

- **Dividend:** one row with `type` dividend, the ticker, the `amount` received (after tax), and the currency.

  ```
  2024-08-15,dividend,VWRL.L,,,,USD,12.40,
  ```

  Accumulating ETFs such as VWCE, CSPX and SWDA pay no cash dividends, so you do not enter anything for them.
- **Sell:** same as a buy with `type` sell. Selling more than you hold is rejected with an error.
- **Fees:** added to your cost on buys and subtracted from proceeds on sells. They are included in all returns.
- **Stock splits:** handled automatically. Enter the quantity exactly as you traded it at the time.

## 8. Adding transactions from the terminal

Instead of editing the file you can append rows:

```
python -m portfolio_tracker add my.csv buy VWCE.DE 1.75 118.40 --date 2025-03-03 --fee 1
python -m portfolio_tracker add my.csv sell AAPL 2 201.00 --date 2025-06-02 --fee 1.5
python -m portfolio_tracker add my.csv dividend VWRL.L 0 12.40 --date 2025-07-01 --currency USD
python -m portfolio_tracker add my.csv buy AAPL 1 190 --date 2025-04-01 --currency USD --fx-rate 0.91
```

- Argument order: `type ticker quantity price`.
- For a dividend, put `0` as the quantity and the total amount as the price.
- Without `--date`, today's date is used.
- The file is created if it does not exist.

### Savings plans in one command

For a fixed amount on a fixed day, `dca` writes all the buys for you. It uses each day's real Yahoo closing price, and if the plan day is a holiday the buy moves to the next trading day.

```
# 40 EUR of NQSE.DE every Monday since 11 Nov 2024 (until today)
python -m portfolio_tracker dca my.csv NQSE.DE 40 --start 2024-11-11 --weekday mon

# 200 EUR of VWCE.DE on the 15th of every month, 1 EUR fee included in the 200
python -m portfolio_tracker dca my.csv VWCE.DE 200 --start 2024-01-01 --monthly 15 --fee 1
```

- `--end YYYY-MM-DD` stops the plan at that date instead of today.
- `--weekday` takes `mon` to `fri`.
- Quantities are the amount (minus the fee) divided by the closing price, to 4 decimals. Change that with `--decimals`.
- These are estimates. Your broker's real execution prices differ slightly, so for exact numbers enter the trades from your broker's statement.
- Running the command twice adds the buys twice, so run it once per plan.

## 9. Running the report

```
python -m portfolio_tracker report my.csv --base EUR --benchmark VWCE.DE
```

| Option | What it does |
|---|---|
| `--base CZK` | show everything in CZK (default EUR) |
| `--benchmark VWCE.DE` | compare with a benchmark bought with the same money on the same dates. Repeat to add more: `--benchmark VWCE.DE --benchmark SPY` |
| `--as-of 2025-06-30` | value the portfolio as of a past date |
| `--top 10` | show up to 10 gainers and 10 losers (default 5) |
| `--export-daily daily.csv` | save daily value, flows and return index for charts in Excel |
| `--json` | machine readable output |
| `> report.txt` | save the report to a text file, e.g. `python -m portfolio_tracker report my.csv > report.txt` |
| `--offline` | no internet, cached prices only |
| `--no-split-adjust` | use only if you entered quantities already adjusted for later splits |

## 10. Reading the report

| Line | Meaning |
|---|---|
| Total P/L | value now + money received from sales and dividends - everything you paid in |
| Total return | Total P/L / everything you paid in |
| XIRR | **Your actual annual return.** It takes into account when each DCA payment went in. This is the main number |
| TWR | How the investments themselves performed, ignoring the timing of your payments. Compare this with funds or indices |
| XIRR below TWR | Your buys were timed unluckily, for example large buys just before a drop |
| Period P/L | Gain in that period. New money you added is not counted as gain |
| Biggest movers | Which holdings added or removed the most money. "Contribution" is that holding's share of the portfolio return, and all shares add up to the total |
| Benchmark | What the same payments would be worth in the benchmark. A positive "Your XIRR - bm" means you beat it |

Annualised numbers for a history shorter than 1 year are extrapolations and are flagged in the report. Use the cumulative numbers instead.

## 11. Getting updates

```
cd ~/Desktop/Claude_code/Agent_C/Portfolio
git pull
source .venv/bin/activate
pip install -r requirements.txt
```

## 12. Troubleshooting

| Problem | Fix |
|---|---|
| `command not found: python` | Run `source .venv/bin/activate` first |
| `No module named portfolio_tracker` | You are not in the `Portfolio` folder; `cd` into it |
| `error: line 5: ...` | Fix that line in your CSV. The message says what is wrong |
| `no price data for XYZ` | Wrong ticker or suffix. Search it on finance.yahoo.com |
| Warning: price is 100x the market price | Pence and pounds are mixed up. See section 6 |
| `refresh failed ... using cached prices` | Yahoo was unreachable or rate limited. Try again later |
| Folder name has a space | Use quotes: `cd "Agent C"` |
