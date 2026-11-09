#!/usr/bin/env python3
"""Candidate C: plain single-page web UI for portfolio_tracker (standard library only).

One HTML page + vanilla JavaScript calling a JSON API on the same server.

    python ui_candidates/ui_c.py [--port N] [--no-browser]
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import sys
import tempfile
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from portfolio_tracker import cli  # noqa: E402
from portfolio_tracker.analytics import Portfolio  # noqa: E402
from portfolio_tracker.data import DataError  # noqa: E402
from portfolio_tracker.report import render_json, render_text  # noqa: E402
from portfolio_tracker.transactions import CSV_COLUMNS, LedgerError, append_transaction, load_transactions  # noqa: E402

SETTINGS_FILE = ".portfolio_tracker_ui.json"
LOCK = threading.Lock()  # one operation at a time (also protects redirect_stdout)


class UserError(Exception):
    pass


# ---------------------------------------------------------------- settings
def settings_path() -> Path:
    return Path.home() / SETTINGS_FILE


def load_settings() -> dict:
    defaults = {
        "file": str(Path.home() / "Desktop" / "portfolio.csv"), "base": "EUR", "as_of": "", "benchmarks": "",
        "top": "5", "offline": False, "no_split_adjust": False, "prices": "", "cache_dir": "",
    }
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update({k: v for k, v in data.items() if k in defaults})
    except (OSError, ValueError):
        pass
    return defaults


def save_settings(new: dict) -> None:
    cur = load_settings()
    cur.update({k: v for k, v in new.items() if k in cur})
    try:
        settings_path().write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except OSError:
        pass  # not fatal


# ---------------------------------------------------------------- helpers
def _s(d: dict, k: str) -> str:
    v = d.get(k)
    return "" if v is None else str(v).strip()


def _float(d: dict, k: str, label: str, default=None, required=False):
    t = _s(d, k)
    if t == "":
        if required:
            raise UserError(f"{label} is required.")
        return default
    try:
        return float(t.replace(",", "."))
    except ValueError:
        raise UserError(f"{label} must be a number, got {t!r}.")


def _int(d: dict, k: str, label: str, default=None):
    t = _s(d, k)
    if t == "":
        return default
    try:
        return int(t)
    except ValueError:
        raise UserError(f"{label} must be a whole number, got {t!r}.")


def _date(d: dict, k: str, label: str, required=False):
    t = _s(d, k)
    if not t:
        if required:
            raise UserError(f"{label} is required (YYYY-MM-DD).")
        return ""
    try:
        return pd.Timestamp(t).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        raise UserError(f"{label} must be a date like 2025-03-03, got {t!r}.")


def tx_path(d: dict) -> Path:
    t = _s(d, "file")
    if not t:
        raise UserError("Choose a transactions file first.")
    return Path(t).expanduser().resolve()


def opt_path(d: dict, k: str, label: str, must_exist=True):
    t = _s(d, k)
    if not t:
        return None
    p = Path(t).expanduser().resolve()
    if must_exist and not p.exists():
        raise UserError(f"{label} not found: {p}")
    return str(p)


def read_raw(path: Path):
    """Return (delimiter, header, rows) as raw strings, without validating."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            delim = csv.Sniffer().sniff(sample, delimiters=",;\t").delimiter
        except csv.Error:
            delim = ","
        rows = [r for r in csv.reader(f, delimiter=delim) if any(c.strip() for c in r)]
    if not rows:
        return delim, list(CSV_COLUMNS), []
    return delim, rows[0], rows[1:]


# ---------------------------------------------------------------- API actions
def api_state(d):
    return {"settings": load_settings(), "home": str(Path.home())}


def api_save_settings(d):
    save_settings(d)
    return {"ok": True}


def api_list(d):
    path = tx_path(d)
    save_settings({"file": _s(d, "file")})
    if not path.exists():
        return {"path": str(path), "exists": False, "header": CSV_COLUMNS, "rows": [], "problem": None}
    _, header, rows = read_raw(path)
    problem = None
    try:
        load_transactions(path)
    except (LedgerError, DataError) as e:
        problem = str(e)
    width = len(header)
    rows = [(r + [""] * width)[:width] for r in rows]
    return {"path": str(path), "exists": True, "header": header, "rows": rows, "problem": problem}


def api_delete(d):
    path = tx_path(d)
    if not path.exists():
        raise UserError(f"File not found: {path}")
    idx = _int(d, "index", "Row")
    expect = d.get("row")
    delim, header, rows = read_raw(path)
    if idx is None or not (0 <= idx < len(rows)):
        raise UserError("That row no longer exists. The table has been reloaded.")
    width = len(header)
    cur = (rows[idx] + [""] * width)[:width]
    if expect is not None and list(expect) != cur:
        raise UserError("The file changed since it was displayed. The table has been reloaded; try again.")
    del rows[idx]
    backup = path.with_name(path.name + ".bak")
    backup.write_bytes(path.read_bytes())
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_ui_", suffix=".csv")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delim)
        w.writerow(header)
        w.writerows(rows)
    os.replace(tmp, path)
    return {"message": f"Deleted row {idx + 1} ({', '.join(c for c in cur if c)}). Backup saved as {backup.name}."}


def api_add(d):
    path = tx_path(d)
    typ = _s(d, "type").lower()
    if typ not in ("buy", "sell", "dividend"):
        raise UserError("Type must be buy, sell or dividend.")
    ticker = _s(d, "ticker").upper()
    if not ticker:
        raise UserError("Ticker is required.")
    date = _date(d, "date", "Date") or pd.Timestamp.today().strftime("%Y-%m-%d")
    fee = _float(d, "fee", "Fee", 0.0)
    fx = _float(d, "fx_rate", "FX rate")
    ccy = _s(d, "currency") or None
    if typ == "dividend":
        amount = _float(d, "amount", "Dividend amount", required=True)
        qty = price = None
    else:
        qty = _float(d, "quantity", "Quantity", required=True)
        price = _float(d, "price", "Price", required=True)
        amount = None
    with LOCK:
        append_transaction(path, date=date, type=typ, ticker=ticker, quantity=qty, price=price,
                           amount=amount, fee=fee, currency=ccy, fx_rate=fx)
    save_settings({"file": _s(d, "file")})
    return {"message": f"Added {typ} {ticker} on {date} to {path}."}


def _ns(d, **extra):
    import argparse as ap
    return ap.Namespace(prices=opt_path(d, "prices", "Prices file"), cache_dir=opt_path(d, "cache_dir", "Cache folder", False),
                        offline=bool(d.get("offline")), **extra)


def api_dca(d):
    path = tx_path(d)
    ticker = _s(d, "ticker").upper()
    if not ticker:
        raise UserError("Ticker is required.")
    amount = _float(d, "amount", "Amount per purchase", required=True)
    if amount <= 0:
        raise UserError("Amount must be larger than 0.")
    start = _date(d, "start", "Start date", required=True)
    end = _date(d, "end", "End date") or None
    mode = _s(d, "mode") or "weekly"
    monthly = None
    weekday = _s(d, "weekday") or "mon"
    if mode == "monthly":
        monthly = _int(d, "monthly", "Day of month")
        if monthly is None or not 1 <= monthly <= 31:
            raise UserError("Day of month must be between 1 and 31.")
    elif weekday not in cli.WEEKDAYS:
        raise UserError("Weekday must be mon..fri.")
    fee = _float(d, "fee", "Fee", 0.0)
    decimals = _int(d, "decimals", "Decimals", 4)
    if fee < 0 or decimals < 0 or decimals > 12:
        raise UserError("Fee must be >= 0 and decimals between 0 and 12.")
    args = _ns(d, transactions=str(path), ticker=ticker, amount=amount, start=start, end=end,
               weekday=weekday, monthly=monthly, fee=fee, currency=_s(d, "currency") or None, decimals=decimals)
    buf = io.StringIO()
    with LOCK, contextlib.redirect_stdout(buf):
        cli.cmd_dca(args)
    save_settings({"file": _s(d, "file"), "prices": _s(d, "prices"), "cache_dir": _s(d, "cache_dir"),
                   "offline": bool(d.get("offline"))})
    return {"message": buf.getvalue().strip()}


def api_report(d):
    path = tx_path(d)
    base = (_s(d, "base") or "EUR").upper()
    as_of = _date(d, "as_of", "As-of date") or None
    top = _int(d, "top", "Top N", 5)
    if top < 1:
        raise UserError("Top N must be at least 1.")
    bench = [b.strip().upper() for b in _s(d, "benchmarks").replace(";", ",").replace(" ", ",").split(",") if b.strip()]
    args = _ns(d)
    with LOCK:
        txs = load_transactions(path)
        pf = Portfolio(txs, cli._provider(args), base=base, as_of=pd.Timestamp(as_of) if as_of else None,
                       benchmarks=bench, split_adjust=not d.get("no_split_adjust"))
        rep = pf.report(top_n=top)
    csv_buf = io.StringIO()
    rep.daily.to_csv(csv_buf, index_label="date")
    save_settings({"file": _s(d, "file"), "base": base, "as_of": _s(d, "as_of"), "benchmarks": ", ".join(bench),
                   "top": str(top), "offline": bool(d.get("offline")), "no_split_adjust": bool(d.get("no_split_adjust")),
                   "prices": _s(d, "prices"), "cache_dir": _s(d, "cache_dir")})
    return {"text": render_text(rep, top_n=top), "json": render_json(rep), "daily_csv": csv_buf.getvalue()}


def api_example(d):
    tx = ROOT / "examples" / "transactions_example.csv"
    pr = ROOT / "examples" / "prices_synthetic.csv"
    if not tx.exists() or not pr.exists():
        raise UserError(f"Example files not found in {ROOT / 'examples'}.")
    return api_report({"file": str(tx), "prices": str(pr), "as_of": "2026-11-09", "base": "EUR",
                       "benchmarks": "VWCE.DE", "top": "5"}) | {"example": True}


ROUTES = {"/api/state": api_state, "/api/settings": api_save_settings, "/api/list": api_list, "/api/delete": api_delete,
          "/api/add": api_add, "/api/dca": api_dca, "/api/report": api_report, "/api/example": api_example}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "PortfolioUI/1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"{}")
        if self.path in ("/", "/index.html"):
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        self._send(404, b'{"ok":false,"error":"not found"}')

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, b"{}")
        fn = ROUTES.get(self.path)
        if fn is None:
            return self._send(404, b'{"ok":false,"error":"not found"}')
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(data, dict):
                raise UserError("Bad request.")
            out = {"ok": True, **fn(data)}
        except UserError as e:
            out = {"ok": False, "kind": "Input problem", "error": str(e)}
        except LedgerError as e:
            out = {"ok": False, "kind": "Transactions problem", "error": str(e)}
        except DataError as e:
            out = {"ok": False, "kind": "Price data problem", "error": str(e)}
        except Exception as e:  # noqa: BLE001 - show anything readably
            out = {"ok": False, "kind": "Unexpected error", "error": f"{type(e).__name__}: {e}",
                   "detail": traceback.format_exc()}
        self._send(200, json.dumps(out).encode("utf-8"))


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Portfolio tracker</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:1000px;margin:0 auto;padding:10px;font-size:15px}
fieldset{margin:12px 0;padding:10px}legend{font-weight:bold}
label{display:inline-block;margin:3px 10px 3px 0}
input,select{font-size:15px;padding:3px}
button{font-size:15px;padding:5px 10px;margin:3px}
table{border-collapse:collapse}td,th{border:1px solid #999;padding:2px 6px;text-align:left}
pre{background:#f4f4f4;border:1px solid #bbb;padding:8px;overflow:auto;white-space:pre}
#status{position:sticky;top:0;padding:6px;border:2px solid #888;background:#fff;z-index:5}
#status.busy{background:#ffd;border-color:#c90}#status.err{background:#fdd;border-color:#c00}#status.ok{background:#dfd;border-color:#080}
.small{font-size:13px;color:#555}[hidden]{display:none !important}
</style></head><body>
<h1>Portfolio tracker</h1>
<div id="status">Ready.</div>
<pre id="errdetail" hidden></pre>

<fieldset><legend>1. Transactions file</legend>
<label>File path <input id="file" size="60"></label>
<button class="act" id="btnLoad">Load / create</button>
<div class="small" id="fileinfo">If the file does not exist it is created when you add the first transaction.</div>
</fieldset>

<fieldset><legend>2. Transactions</legend>
<div id="listmsg" class="small"></div>
<table id="tx"><thead id="txhead"></thead><tbody id="txbody"></tbody></table>
</fieldset>

<fieldset><legend>3. Add a transaction</legend>
<label>Type <select id="a_type"><option>buy</option><option>sell</option><option>dividend</option></select></label>
<label>Date <input id="a_date" type="date"></label>
<label>Ticker <input id="a_ticker" size="10" placeholder="VWCE.DE"></label>
<label id="l_qty">Quantity <input id="a_quantity" size="8"></label>
<label id="l_price">Price per unit <input id="a_price" size="8"></label>
<label id="l_amount">Dividend amount received <input id="a_amount" size="8"></label>
<label>Fee <input id="a_fee" size="6"></label>
<label>Currency <input id="a_currency" size="5" placeholder="optional"></label>
<label>FX rate <input id="a_fx_rate" size="6" placeholder="optional"></label>
<button class="act" id="btnAdd">Add transaction</button>
<div class="small">Currency empty = the ticker's own currency. FX rate = base currency per 1 unit of currency, optional.</div>
</fieldset>

<fieldset><legend>4. Savings plan (DCA)</legend>
<label>Ticker <input id="d_ticker" size="10"></label>
<label>Amount per purchase <input id="d_amount" size="8"></label>
<label>Start <input id="d_start" type="date"></label>
<label>End (empty = today) <input id="d_end" type="date"></label><br>
<label><input type="radio" name="d_mode" value="weekly" checked> Weekly on
<select id="d_weekday"><option value="mon">Monday</option><option value="tue">Tuesday</option><option value="wed">Wednesday</option><option value="thu">Thursday</option><option value="fri">Friday</option></select></label>
<label><input type="radio" name="d_mode" value="monthly"> Monthly on day <input id="d_monthly" size="3" value="1"></label><br>
<label>Fee per purchase <input id="d_fee" size="6"></label>
<label>Currency <input id="d_currency" size="5" placeholder="optional"></label>
<label>Decimals <input id="d_decimals" size="3" value="4"></label>
<button class="act" id="btnDca">Create plan purchases</button>
<div class="small">Uses the prices options below (offline, prices file, cache folder). Downloads from Yahoo can take a while.</div>
</fieldset>

<fieldset><legend>5. Report</legend>
<label>Base currency <input id="r_base" size="5"></label>
<label>As-of date (empty = today) <input id="r_as_of" type="date"></label>
<label>Top N movers <input id="r_top" size="3"></label><br>
<label>Benchmarks (comma separated) <input id="r_benchmarks" size="30" placeholder="VWCE.DE, SPY"></label><br>
<label><input type="checkbox" id="r_offline"> Offline (use cache only)</label>
<label><input type="checkbox" id="r_nosplit"> Quantities already split adjusted (no split adjust)</label><br>
<label>Offline prices CSV (optional) <input id="r_prices" size="50"></label><br>
<label>Cache folder (optional) <input id="r_cache" size="50"></label><br>
<button class="act" id="btnReport">Run report</button>
<button class="act" id="btnExample">One-click example report</button>
<span id="dl" hidden>
<button id="btnJson">Download JSON</button> <button id="btnCsv">Download daily CSV</button></span>
<pre id="reportout" hidden></pre>
</fieldset>

<script>
"use strict";
const $ = id => document.getElementById(id);
let lastReport = null, busy = false;

function setStatus(cls, text){const s=$("status");s.className=cls;s.textContent=text;}
function setBusy(b){busy=b;document.querySelectorAll("button.act, .rowdel").forEach(x=>x.disabled=b);}

async function call(path, body, label){
  if(busy) return null;
  setBusy(true); setStatus("busy","Working... "+label+" (please wait, do not click again)");
  $("errdetail").hidden=true;
  try{
    const r = await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body||{})});
    const j = await r.json();
    if(!j.ok){
      setStatus("err", j.kind+": "+j.error);
      if(j.detail){$("errdetail").textContent=j.detail;$("errdetail").hidden=false;}
      return null;
    }
    return j;
  }catch(e){
    setStatus("err","Cannot reach the local server: "+e+" (is the program still running?)");
    return null;
  }finally{ setBusy(false); }
}

function val(id){return $(id).value.trim();}
function common(){return {file:val("file"),prices:val("r_prices"),cache_dir:val("r_cache"),offline:$("r_offline").checked};}

async function loadList(label){
  const j = await call("/api/list",{file:val("file")},label||"loading file");
  if(!j) return false;
  $("fileinfo").textContent = j.exists ? "Using "+j.path : "File does not exist yet: "+j.path+" (it will be created when you add a transaction).";
  const head=$("txhead"), body=$("txbody");
  head.textContent=""; body.textContent="";
  const hr=document.createElement("tr");
  ["#"].concat(j.header).concat([""]).forEach(h=>{const th=document.createElement("th");th.textContent=h;hr.appendChild(th);});
  head.appendChild(hr);
  j.rows.forEach((row,i)=>{
    const tr=document.createElement("tr");
    const n=document.createElement("td");n.textContent=String(i+1);tr.appendChild(n);
    row.forEach(c=>{const td=document.createElement("td");td.textContent=c;tr.appendChild(td);});
    const td=document.createElement("td");const b=document.createElement("button");
    b.textContent="Delete";b.className="rowdel";b.disabled=busy;
    b.onclick=()=>delRow(i,row);td.appendChild(b);tr.appendChild(td);body.appendChild(tr);
  });
  let msg = j.rows.length+" transaction(s).";
  if(j.problem) msg += " WARNING, the file has a problem: "+j.problem;
  $("listmsg").textContent=msg;
  if(label) setStatus(j.problem?"err":"ok", msg);
  return true;
}

async function delRow(i,row){
  if(!confirm("Delete row "+(i+1)+": "+row.filter(x=>x).join(", ")+" ?")) return;
  const j = await call("/api/delete",{file:val("file"),index:i,row:row},"deleting row");
  if(j){ await loadList(); setStatus("ok",j.message); } else { await loadListQuiet(); }
}
async function loadListQuiet(){ // refresh table but keep the error message shown
  const keep=$("status").textContent, cls=$("status").className;
  await loadList(); setStatus(cls,keep);
}

function syncType(){
  const div=$("a_type").value==="dividend";
  $("l_qty").hidden=div;$("l_price").hidden=div;$("l_amount").hidden=!div;
}
$("a_type").onchange=syncType;

$("btnLoad").onclick=()=>loadList("loading file");

$("btnAdd").onclick=async()=>{
  const j=await call("/api/add",{file:val("file"),type:val("a_type"),date:val("a_date"),ticker:val("a_ticker"),
    quantity:val("a_quantity"),price:val("a_price"),amount:val("a_amount"),fee:val("a_fee"),
    currency:val("a_currency"),fx_rate:val("a_fx_rate")},"adding transaction");
  if(j){await loadList(); setStatus("ok",j.message);}
};

$("btnDca").onclick=async()=>{
  const c=common();
  const j=await call("/api/dca",Object.assign(c,{ticker:val("d_ticker"),amount:val("d_amount"),start:val("d_start"),
    end:val("d_end"),mode:document.querySelector("input[name=d_mode]:checked").value,weekday:val("d_weekday"),
    monthly:val("d_monthly"),fee:val("d_fee"),currency:val("d_currency"),decimals:val("d_decimals")}),
    "creating plan (downloading prices, this can take a minute)");
  if(j){await loadList(); setStatus("ok",j.message);}
};

function showReport(j){
  lastReport=j; $("reportout").textContent=j.text; $("reportout").hidden=false; $("dl").hidden=false;
}
$("btnReport").onclick=async()=>{
  const j=await call("/api/report",Object.assign(common(),{base:val("r_base"),as_of:val("r_as_of"),top:val("r_top"),
    benchmarks:val("r_benchmarks"),no_split_adjust:$("r_nosplit").checked}),"computing report (downloading prices, this can take a minute)");
  if(j){showReport(j);setStatus("ok","Report ready.");}
};
$("btnExample").onclick=async()=>{
  const j=await call("/api/example",{},"computing example report");
  if(j){showReport(j);setStatus("ok","Example report ready (examples/transactions_example.csv, as of 2026-11-09).");}
};

function download(name,text,type){
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([text],{type:type}));a.download=name;
  document.body.appendChild(a);a.click();a.remove();
}
$("btnJson").onclick=()=>lastReport&&download("portfolio_report.json",lastReport.json,"application/json");
$("btnCsv").onclick=()=>lastReport&&download("portfolio_daily.csv",lastReport.daily_csv,"text/csv");

(async function init(){
  $("a_date").value=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
  syncType();
  const j=await call("/api/state",{},"starting");
  if(!j) return;
  const s=j.settings;
  $("file").value=s.file;$("r_base").value=s.base;$("r_as_of").value=s.as_of;$("r_top").value=s.top;
  $("r_benchmarks").value=s.benchmarks;$("r_offline").checked=!!s.offline;$("r_nosplit").checked=!!s.no_split_adjust;
  $("r_prices").value=s.prices;$("r_cache").value=s.cache_dir;
  await loadList("loading file");
})();
</script></body></html>
"""


def make_server(port: int):
    try:
        return ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        return ThreadingHTTPServer(("127.0.0.1", 0), Handler)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Portfolio tracker web UI (candidate C)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    srv = make_server(args.port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Portfolio tracker UI running at {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
