#!/usr/bin/env python3
"""Candidate A: plain server-rendered local web UI for portfolio_tracker.

Standard library only (plus the existing package). No JavaScript.

    python ui_candidates/ui_a.py [--port N] [--no-browser]
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import html
import io
import json
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from portfolio_tracker.analytics import Portfolio  # noqa: E402
from portfolio_tracker.cli import cmd_dca  # noqa: E402
from portfolio_tracker.data import DataError, StaticProvider, YahooProvider  # noqa: E402
from portfolio_tracker.report import render_json, render_text  # noqa: E402
from portfolio_tracker.transactions import LedgerError, append_transaction, load_transactions  # noqa: E402

SETTINGS_FILE = Path.home() / ".portfolio_tracker_ui.json"
DEFAULT_CSV = str(Path.home() / "Desktop" / "portfolio.csv")
E = html.escape

DEFAULTS = {
    "path": DEFAULT_CSV,
    "base": "EUR", "as_of": "", "benchmarks": "", "top": "5", "offline": False,
    "no_split_adjust": False, "prices": "", "cache_dir": "",
    "dca_ticker": "", "dca_amount": "", "dca_start": "", "dca_end": "", "dca_mode": "weekday",
    "dca_weekday": "mon", "dca_monthly": "1", "dca_fee": "0", "dca_currency": "", "dca_decimals": "4",
    "dca_prices": "", "dca_offline": False,
}
LOCK = threading.Lock()
STATE = {"message": None, "report_cache": None}


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            s.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:
        pass
    return s


def save_settings(updates: dict) -> None:
    s = load_settings()
    s.update({k: v for k, v in updates.items() if k in DEFAULTS})
    try:
        SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")
    except Exception:
        pass  # not being able to remember settings is not fatal


def fpath(raw: str) -> Path:
    raw = (raw or "").strip()
    if not raw:
        raise LedgerError("please enter a file path")
    return Path(raw).expanduser()


def prices_path(raw: str) -> str:
    p = fpath(raw)
    if not p.is_file():
        raise LedgerError(f"prices file not found: {p}")
    return str(p)


# ---------------------------------------------------------------- raw CSV access (view / delete)
def read_raw(path: Path):
    """Return (delimiter, header, rows) with rows = list of lists (blank lines dropped)."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delim = ","
        allrows = [r for r in csv.reader(f, delimiter=delim) if any(c.strip() for c in r)]
    if not allrows:
        return delim, [], []
    return delim, allrows[0], allrows[1:]


def fingerprint(row) -> str:
    return hashlib.sha1("\x1f".join(row).encode("utf-8")).hexdigest()[:12]


def delete_row(path: Path, idx: int, fp: str) -> str:
    delim, header, rows = read_raw(path)
    if not (0 <= idx < len(rows)) or fingerprint(rows[idx]) != fp:
        raise LedgerError("that row has changed since the page was loaded; the page has been refreshed, please try again")
    gone = rows.pop(idx)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delim)
        w.writerow(header)
        w.writerows(rows)
    tmp.replace(path)
    return "deleted row: " + delim.join(gone)


# ---------------------------------------------------------------- actions
def num(form: dict, name: str, label: str, required=False, default=None):
    raw = form.get(name, "").strip()
    if raw == "":
        if required:
            raise LedgerError(f"{label} is required")
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        raise LedgerError(f"{label} must be a number, got {raw!r}")


def do_add(form: dict) -> str:
    path = fpath(form.get("path", ""))
    typ = form.get("type", "")
    if typ not in ("buy", "sell", "dividend"):
        raise LedgerError("choose buy, sell or dividend")
    ticker = form.get("ticker", "").strip().upper()
    if not ticker:
        raise LedgerError("ticker is required")
    date = form.get("date", "").strip() or pd.Timestamp.today().strftime("%Y-%m-%d")
    fee = num(form, "fee", "fee", default=0.0)
    cur = form.get("currency", "").strip() or None
    fx = num(form, "fx_rate", "fx rate")
    if typ == "dividend":
        amount = num(form, "price", "amount received", required=True)
        qty = price = None
    else:
        qty = num(form, "quantity", "quantity", required=True)
        price = num(form, "price", "price", required=True)
        amount = None
    append_transaction(path, date=date, type=typ, ticker=ticker, quantity=qty, price=price,
                       amount=amount, fee=fee, currency=cur, fx_rate=fx)
    save_settings({"path": str(path)})
    return f"added {typ} {ticker} on {date}"


def do_dca(form: dict) -> str:
    path = fpath(form.get("path", ""))
    ticker = form.get("dca_ticker", "").strip().upper()
    if not ticker:
        raise LedgerError("ticker is required")
    amount = num(form, "dca_amount", "amount per purchase", required=True)
    start = form.get("dca_start", "").strip()
    if not start:
        raise LedgerError("start date is required")
    mode = form.get("dca_mode", "weekday")
    monthly = None
    if mode == "monthly":
        try:
            monthly = int(form.get("dca_monthly", "").strip())
        except ValueError:
            raise LedgerError("day of month must be a whole number 1-31")
        if not 1 <= monthly <= 31:
            raise LedgerError("day of month must be between 1 and 31")
    weekday = form.get("dca_weekday", "mon")
    if weekday not in ("mon", "tue", "wed", "thu", "fri"):
        raise LedgerError("invalid weekday")
    try:
        decimals = int(form.get("dca_decimals", "").strip() or "4")
    except ValueError:
        raise LedgerError("decimals must be a whole number")
    try:
        pd.Timestamp(start)
        if form.get("dca_end", "").strip():
            pd.Timestamp(form["dca_end"].strip())
    except Exception:
        raise LedgerError("dates must look like 2024-01-31")
    import argparse as ap
    args = ap.Namespace(
        transactions=str(path), ticker=ticker, amount=amount, start=start,
        end=form.get("dca_end", "").strip() or None, weekday=weekday, monthly=monthly,
        fee=num(form, "dca_fee", "fee", default=0.0), currency=form.get("dca_currency", "").strip() or None,
        decimals=decimals, prices=form.get("dca_prices", "").strip() or None,
        cache_dir=None, offline=bool(form.get("dca_offline")))
    if args.prices:
        args.prices = prices_path(args.prices)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_dca(args)
    save_settings({"path": str(path)})
    return buf.getvalue().strip() or "done"


REPORT_FIELDS = ["path", "base", "as_of", "benchmarks", "top", "offline", "no_split_adjust", "prices", "cache_dir"]


def report_opts(form: dict) -> dict:
    o = {k: form.get(k, "").strip() for k in REPORT_FIELDS}
    o["offline"] = "1" if form.get("offline") else ""
    o["no_split_adjust"] = "1" if form.get("no_split_adjust") else ""
    return o


def compute_report(o: dict):
    """Return (text, json, daily_csv). Caches the last result so downloads are instant."""
    key = json.dumps(o, sort_keys=True)
    with LOCK:
        c = STATE["report_cache"]
        if c and c[0] == key:
            return c[1]
    path = fpath(o["path"])
    txs = load_transactions(path)
    if o["prices"]:
        prov = StaticProvider.from_csv(prices_path(o["prices"]))
    else:
        prov = YahooProvider(cache_dir=o["cache_dir"] or None, offline=bool(o["offline"]))
    try:
        top = int(o["top"] or 5)
    except ValueError:
        raise LedgerError("top N must be a whole number")
    try:
        as_of = pd.Timestamp(o["as_of"]) if o["as_of"] else None
    except Exception:
        raise LedgerError("as-of date must look like 2025-06-30")
    bms = [b.strip() for b in o["benchmarks"].replace(";", ",").replace("\n", ",").split(",") if b.strip()]
    pf = Portfolio(txs, prov, base=(o["base"] or "EUR").upper(), as_of=as_of, benchmarks=bms,
                   split_adjust=not o["no_split_adjust"])
    rep = pf.report(top_n=top)
    result = (render_text(rep, top_n=top), render_json(rep), rep.daily.to_csv(index_label="date"))
    with LOCK:
        STATE["report_cache"] = (key, result)
    return result


def example_opts() -> dict:
    return {"path": str(ROOT / "examples" / "transactions_example.csv"), "base": "EUR", "as_of": "2026-11-09",
            "benchmarks": "VWCE.DE", "top": "5", "offline": "", "no_split_adjust": "",
            "prices": str(ROOT / "examples" / "prices_synthetic.csv"), "cache_dir": ""}


# ---------------------------------------------------------------- HTML
def field(label, name, value="", size=20, hint="", typ="text"):
    return (f'<p><label>{E(label)}: <input type="{typ}" name="{name}" value="{E(str(value))}" size="{size}"></label>'
            f' <small>{E(hint)}</small></p>')


def check(label, name, on):
    return f'<p><label><input type="checkbox" name="{name}" value="1"{" checked" if on else ""}> {E(label)}</label></p>'


def render_page(s: dict, message=None, report=None) -> str:
    out = []
    a = out.append
    a("""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Portfolio tracker</title>
<style>body{font-family:sans-serif;max-width:60em;margin:1em auto;padding:0 1em}
table{border-collapse:collapse}td,th{border:1px solid #999;padding:2px 6px;font-size:90%}
.err{background:#fdd;border:2px solid #c00;padding:.5em;white-space:pre-wrap}
.ok{background:#dfd;border:2px solid #080;padding:.5em;white-space:pre-wrap}
pre{background:#f4f4f4;padding:.5em;overflow:auto}
fieldset{margin:1em 0}small{color:#555}</style></head><body>
<h1>Portfolio tracker</h1>
<p><a href="#file">1 File</a> | <a href="#transactions">2 Transactions</a> | <a href="#add">3 Add</a> |
<a href="#dca">4 Savings plan</a> | <a href="#report">5 Report</a></p>""")
    if message:
        kind, text = message
        a(f'<div class="{"err" if kind == "error" else "ok"}"><b>{"Error" if kind == "error" else "OK"}:</b> {E(text)}</div>')

    # 1 file
    a(f'<h2 id="file">1. Transactions file</h2><form method="post" action="/setfile">'
      f'<p>Path to the CSV file (it will be created when you add the first transaction):</p>'
      f'<p><input name="path" value="{E(s["path"])}" size="70"> <button>Use this file</button></p></form>')
    p = Path(s["path"]).expanduser() if s["path"].strip() else None
    a(f'<p>Current file: <code>{E(str(p))}</code> - {"exists" if p and p.exists() else "does not exist yet"}</p>')

    # 2 table
    a('<h2 id="transactions">2. Transactions</h2>')
    if p and p.exists():
        try:
            _, header, rows = read_raw(p)
        except Exception as e:
            header, rows = [], []
            a(f'<div class="err">Cannot read file: {E(str(e))}</div>')
        a(f'<p>{len(rows)} rows. Deleting a row cannot be undone.</p><table><tr><th>#</th>'
          + "".join(f"<th>{E(h)}</th>" for h in header) + "<th></th></tr>")
        for i, r in enumerate(rows):
            a(f"<tr><td>{i + 1}</td>" + "".join(f"<td>{E(c)}</td>" for c in r)
              + f'<td><form method="post" action="/delete"><input type="hidden" name="path" value="{E(str(p))}">'
                f'<input type="hidden" name="idx" value="{i}"><input type="hidden" name="fp" value="{fingerprint(r)}">'
                f'<button>Delete</button></form></td></tr>')
        a("</table>")
    else:
        a("<p>No file yet. Add a transaction or a savings plan below to create it.</p>")

    # 3 add
    a(f'<h2 id="add">3. Add a transaction</h2><form method="post" action="/add">'
      f'<input type="hidden" name="path" value="{E(s["path"])}">'
      '<p><label>Type: <select name="type"><option>buy</option><option>sell</option><option>dividend</option></select></label></p>'
      + field("Date", "date", "", 12, "YYYY-MM-DD or DD.MM.YYYY; empty = today")
      + field("Ticker", "ticker", "", 12, "Yahoo symbol, e.g. VWCE.DE, AAPL")
      + field("Quantity", "quantity", "", 10, "buy/sell only")
      + field("Price per unit, or total amount received for a dividend", "price", "", 10)
      + field("Fee", "fee", "", 8, "optional")
      + field("Currency", "currency", "", 6, "optional, e.g. USD; empty = the ticker's own currency; GBX for pence")
      + field("FX rate", "fx_rate", "", 8, "optional: base currency per 1 unit of currency, as your broker charged")
      + '<p><button>Add transaction</button></p></form>')

    # 4 dca
    wd = "".join(f'<option value="{v}"{" selected" if s["dca_weekday"] == v else ""}>{n}</option>'
                 for v, n in [("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"), ("fri", "Friday")])
    a(f'<h2 id="dca">4. Savings plan (DCA)</h2><form method="post" action="/dca">'
      f'<input type="hidden" name="path" value="{E(s["path"])}">'
      '<p>Adds one buy for every plan date, priced at the market close (a holiday moves to the next trading day). '
      'Running it twice adds the buys twice. <b>Downloading prices from Yahoo can take a minute; please wait.</b></p>'
      + field("Ticker", "dca_ticker", s["dca_ticker"], 12)
      + field("Amount per purchase (fee included)", "dca_amount", s["dca_amount"], 10)
      + field("Start date", "dca_start", s["dca_start"], 12, "YYYY-MM-DD")
      + field("End date", "dca_end", s["dca_end"], 12, "optional, default today")
      + f'<p><label><input type="radio" name="dca_mode" value="weekday"{" checked" if s["dca_mode"] != "monthly" else ""}> Every week on '
        f'<select name="dca_weekday">{wd}</select></label><br>'
        f'<label><input type="radio" name="dca_mode" value="monthly"{" checked" if s["dca_mode"] == "monthly" else ""}> Every month on day '
        f'<input name="dca_monthly" value="{E(s["dca_monthly"])}" size="3"></label></p>'
      + field("Fee per purchase", "dca_fee", s["dca_fee"], 8, "taken out of the amount")
      + field("Currency", "dca_currency", s["dca_currency"], 6, "optional")
      + field("Decimals for units", "dca_decimals", s["dca_decimals"], 4)
      + field("Offline prices CSV", "dca_prices", s["dca_prices"], 50, "optional, instead of Yahoo")
      + check("Offline (use cached Yahoo prices only)", "dca_offline", s["dca_offline"])
      + '<p><button>Create savings plan buys</button></p></form>')

    # 5 report
    a('<h2 id="report">5. Report</h2>'
      '<form method="post" action="/report"><input type="hidden" name="path" value="' + E(s["path"]) + '">'
      '<p><b>Downloading prices from Yahoo can take a minute; please wait after pressing the button.</b></p>'
      + field("Base currency", "base", s["base"], 6)
      + field("As-of date", "as_of", s["as_of"], 12, "optional, YYYY-MM-DD; empty = today")
      + field("Benchmarks", "benchmarks", s["benchmarks"], 40, "optional, separated by commas, e.g. VWCE.DE, SPY")
      + field("Show top N movers", "top", s["top"], 4)
      + check("Offline (cached prices only, no internet)", "offline", s["offline"])
      + check("No split adjust (quantities already adjusted for splits)", "no_split_adjust", s["no_split_adjust"])
      + field("Offline prices CSV", "prices", s["prices"], 50, "optional, used instead of Yahoo")
      + field("Cache directory", "cache_dir", s["cache_dir"], 50, "optional")
      + '<p><button>Show report</button> '
        '<button formaction="/download/json">Download JSON</button> '
        '<button formaction="/download/csv">Download daily CSV</button></p></form>'
      '<form method="post" action="/example"><p><button>Load example (example transactions + synthetic prices, as of 2026-11-09)</button></p></form>')
    if report:
        title, text, opts = report
        q = urlencode(opts)
        a(f'<h3>{E(title)}</h3><p><a href="/download/json?{q}">Download JSON</a> | '
          f'<a href="/download/csv?{q}">Download daily CSV</a></p><pre>{E(text)}</pre>')
    a("</body></html>")
    return "".join(out)


# ---------------------------------------------------------------- server
def friendly(e: Exception) -> str:
    if isinstance(e, (LedgerError, DataError)):
        return str(e)
    sys.stderr.write(traceback.format_exc())
    return f"Unexpected problem ({type(e).__name__}): {e}"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body: bytes, ctype="text/html; charset=utf-8", status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def page(self, message=None, report=None, status=200):
        self.send(render_page(load_settings(), message, report).encode("utf-8"), status=status)

    def redirect(self, frag=""):
        self.send_response(303)
        self.send_header("Location", "/#" + frag if frag else "/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def form(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def download(self, kind: str, opts: dict):
        try:
            _, js, csv_text = compute_report(opts)
        except Exception as e:
            return self.page(("error", friendly(e)), status=200)
        if kind == "json":
            self.send(js.encode("utf-8"), "application/json", extra={"Content-Disposition": 'attachment; filename="report.json"'})
        else:
            self.send(csv_text.encode("utf-8"), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="daily.csv"'})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            msg, STATE["message"] = STATE["message"], None
            return self.page(msg)
        if u.path in ("/download/json", "/download/csv"):
            q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
            o = {k: q.get(k, "") for k in REPORT_FIELDS}
            return self.download(u.path.rsplit("/", 1)[1], o)
        self.send(b"not found", "text/plain", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        form = self.form()
        try:
            if path == "/setfile":
                p = fpath(form.get("path", ""))
                save_settings({"path": str(p)})
                STATE["message"] = ("ok", f"using {p}")
                return self.redirect("file")
            if path in ("/add", "/dca", "/delete"):
                if form.get("path"):
                    save_settings({"path": form["path"]})
            if path == "/add":
                s = do_add(form)
                STATE["message"] = ("ok", s)
                return self.redirect("transactions")
            if path == "/dca":
                keep = {k: form.get(k, "") for k in DEFAULTS if k.startswith("dca_") and k != "dca_offline"}
                keep["dca_offline"] = bool(form.get("dca_offline"))
                save_settings(keep)
                s = do_dca(form)
                STATE["message"] = ("ok", s)
                return self.redirect("transactions")
            if path == "/delete":
                try:
                    idx = int(form.get("idx", ""))
                except ValueError:
                    raise LedgerError("bad row number")
                STATE["message"] = ("ok", delete_row(fpath(form.get("path", "")), idx, form.get("fp", "")))
                return self.redirect("transactions")
            if path in ("/report", "/download/json", "/download/csv"):
                o = report_opts(form)
                save_settings({k: (bool(o[k]) if k in ("offline", "no_split_adjust") else o[k]) for k in REPORT_FIELDS})
                if path != "/report":
                    return self.download(path.rsplit("/", 1)[1], o)
                text, _, _ = compute_report(o)
                return self.page(None, ("Report", text, o))
            if path == "/example":
                o = example_opts()
                text, _, _ = compute_report(o)
                return self.page(None, ("Example report (example transactions, synthetic prices)", text, o))
            self.send(b"not found", "text/plain", 404)
        except Exception as e:
            if path in ("/report", "/example"):
                return self.page(("error", friendly(e)))
            STATE["message"] = ("error", friendly(e))
            self.redirect({"/add": "add", "/dca": "dca", "/setfile": "file"}.get(path, "transactions"))


def make_server(port: int) -> HTTPServer:
    try:
        return HTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return HTTPServer(("127.0.0.1", 0), Handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Plain local web UI for the portfolio tracker")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    srv = make_server(args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Portfolio tracker UI: {url}\nPress Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
