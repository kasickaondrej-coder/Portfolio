"""Plain local web UI for the portfolio tracker (standard library only).

    python -m portfolio_tracker ui        (or: python -m portfolio_tracker.ui)
    options: --port N (default 8765), --no-browser

Pages are rendered on the server. Every form POSTs and then redirects
(POST/redirect/GET), so reloading a page never repeats an action. Before every
write to the transactions file a timestamped copy is saved in a hidden folder
next to it (".portfolio_backups"), and the Transactions page can undo the last
change. The only
JavaScript is a delete confirm() and a "please wait" banner that also blocks
double submits. The server listens on 127.0.0.1 only and rejects requests from
other web sites (Host and Origin checks).
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import html
import io
import json
import logging
import os
import secrets
import re
import tempfile
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
from urllib.request import urlopen

import pandas as pd

from . import cli
from .analytics import Portfolio
from .data import DataError, StaticProvider, YahooProvider
from .report import render_json, render_text
from .transactions import (CSV_COLUMNS, LedgerError, _date, _num, append_transaction, clean_ticker,
                           load_transactions, parse_rows)

ROOT = Path(__file__).resolve().parent.parent

def settings_path() -> Path:
    return Path.home() / ".portfolio_tracker_ui.json"


def default_csv() -> str:
    return str(Path.home() / "Desktop" / "portfolio.csv")

EXAMPLE_TX = ROOT / "examples" / "transactions_example.csv"
EXAMPLE_PRICES = ROOT / "examples" / "prices_synthetic.csv"

LOCK = threading.RLock()  # serialises file writes and settings
RESULTS: "OrderedDict[str, dict]" = OrderedDict()  # report results kept in memory
FLASH: "OrderedDict[str, dict]" = OrderedDict()  # one-shot messages + form values
PENDING: "OrderedDict[str, dict]" = OrderedDict()  # savings plans waiting for "add anyway" (used once)

# ----------------------------------------------------------------- settings


def _clean_form_dict(d) -> dict:
    """Keep only text values of a saved form (anything else in a damaged settings file is ignored)."""
    if not isinstance(d, dict):
        return {}
    return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str) and _safe_text(v)}


def _safe_text(v: str) -> bool:
    """False for text with NUL or characters that cannot be written as UTF-8 (lone surrogates)."""
    if "\x00" in v:
        return False
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def load_settings() -> dict:
    """Read the settings file. Damaged or wrongly typed parts are ignored (defaults are used instead)."""
    try:
        d = json.loads(settings_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    if isinstance(d.get("csv_path"), str) and d["csv_path"].strip() and _safe_text(d["csv_path"]):
        out["csv_path"] = d["csv_path"]
    for k in ("dca", "report"):
        if isinstance(d.get(k), dict):
            out[k] = _clean_form_dict(d[k])
    return out


def save_settings(**updates) -> None:
    with LOCK:
        s = load_settings()
        s.update(updates)
        try:
            settings_path().write_text(json.dumps(s, indent=2), encoding="utf-8")
        except OSError:
            pass  # settings are a convenience only


def current_path() -> str:
    return load_settings().get("csv_path") or default_csv()


_QUOTES = "\"'\u201c\u201d\u2018\u2019`"


def expand(p: str) -> str:
    """Turn what a person typed or pasted into an absolute path.

    Surrounding quotes and a file:// prefix are removed, ~ is expanded and a relative path is taken
    relative to the user's home folder (never to the folder the server happens to run in).
    """
    p = (p or "").strip()
    while len(p) >= 2 and p[0] in _QUOTES and p[-1] in _QUOTES:
        p = p[1:-1].strip()
    if p.lower().startswith("file://"):
        p = p[7:]
        if not p.startswith("/"):  # file://localhost/Users/x -> /Users/x
            p = p[p.find("/"):] if "/" in p else ""
        p = unquote(p)
    p = os.path.expanduser(p)
    if "\x00" in p:
        raise UserError("the file path contains an invalid character")
    if not os.path.isabs(p):
        p = os.path.join(str(Path.home()), p)
    return str(Path(os.path.normpath(p)))


# ------------------------------------------------------------------ helpers


def esc(x) -> str:
    return html.escape("" if x is None else str(x), quote=True)


class UserError(Exception):
    """Bad input from the form."""


def num(raw: str, name: str, default=None, kind=float):
    raw = (raw or "").strip()
    if raw == "":
        if default is None:
            raise UserError(f"{name} is required")
        return kind(default)
    if kind is float:
        try:
            return _num(raw, name, 0)
        except LedgerError as e:
            raise UserError(str(e)) from None
    try:
        return int(raw)
    except ValueError:
        raise UserError(f"{name}: {raw!r} is not a valid whole number")


def check_date(raw: str, name: str, required=True):
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise UserError(f"{name} is required (format YYYY-MM-DD)")
        return None
    try:
        pd.Timestamp(raw)
    except Exception:
        raise UserError(f"{name}: {raw!r} is not a date, use YYYY-MM-DD")
    if len(raw) != 10 or raw[4] != "-" or raw[7] != "-":
        raise UserError(f"{name}: {raw!r} is not a date, use YYYY-MM-DD")
    return raw


def friendly(e: BaseException) -> str:
    if isinstance(e, (LedgerError, DataError, UserError)):
        return str(e)
    if isinstance(e, UnicodeDecodeError):
        return ("The file could not be read as text. Save it as CSV (UTF-8) from your spreadsheet program "
                "and try again. Nothing was changed.")
    if isinstance(e, PermissionError):
        return f"No permission to use {e.filename or 'that file or folder'}. Choose a file in a folder you own. Nothing was changed."
    if isinstance(e, FileNotFoundError):
        return f"Could not find {e.filename or 'the file or folder'}. Check the path on the Transactions page. Nothing was changed."
    if isinstance(e, OSError):
        what = f" ({e.filename})" if e.filename else ""
        return f"The file or folder could not be used{what}: {e.strerror or e}. Nothing was changed."
    if isinstance(e, ValueError):
        return f"Something in the file or the form is not valid: {e}. Nothing was changed."
    logging.getLogger("portfolio_tracker.ui").error("unexpected error:\n%s", traceback.format_exc())
    return f"Unexpected error ({type(e).__name__}): {e}. Nothing was changed unless stated above."


def flash(kind: str, msg: str, values: dict | None = None) -> str:
    tok = secrets.token_urlsafe(8)
    FLASH[tok] = {"kind": kind, "msg": msg, "values": values or {}}
    while len(FLASH) > 50:
        FLASH.popitem(last=False)
    return tok


def store_result(res: dict) -> str:
    rid = secrets.token_urlsafe(8)
    RESULTS[rid] = res
    while len(RESULTS) > 10:
        RESULTS.popitem(last=False)
    return rid


# ------------------------------------------------------------- CSV handling


def _parse(text: str):
    """Split CSV text (BOM already removed) into physical lines and records.

    Returns (delimiter, lines, [(first_line, last_line, row)]). Lines keep their own line endings, so
    joining them gives back exactly the original text. A quoted field may span several lines.
    """
    lines = io.StringIO(text, newline="").readlines()
    try:
        delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    except csv.Error:
        delim = ","
    r = csv.reader(iter(lines), delimiter=delim)
    recs, prev = [], 0
    for row in r:
        recs.append((prev + 1, r.line_num, row))
        prev = r.line_num
    return delim, lines, recs


def _read_text(path: str) -> str:
    return Path(path).read_bytes().decode("utf-8").removeprefix("\ufeff")


def read_raw(path: str):
    """Return (delimiter, [(line_num, row_list)]) for the raw CSV file (line_num = last line of the row)."""
    delim, _, recs = _parse(_read_text(path))
    return delim, [(end, row) for _, end, row in recs]


def sig(row: list, path: str = "") -> str:
    """Fingerprint of one row in one file; a page for another file or an edited row no longer matches."""
    return hashlib.sha1("\x1f".join([str(path)] + list(row)).encode("utf-8")).hexdigest()[:12]


# ------------------------------------------------------------------ backups

BACKUP_DIR = ".portfolio_backups"
BACKUPS_KEPT = 20


def backup_dir(path) -> Path:
    return Path(path).parent / BACKUP_DIR


def _backup_key(path, f: Path):
    m = re.fullmatch(re.escape(Path(path).name) + r"\.(\d{8}-\d{6})(?:-(\d+))?\.csv", f.name)
    return (m.group(1), int(m.group(2) or 0)) if m else None


def list_backups(path) -> list:
    """Backups of this file, oldest first."""
    d = backup_dir(path)
    try:
        found = [(k, f) for f in d.iterdir() if f.is_file() and (k := _backup_key(path, f))]
    except OSError:
        return []
    return [f for _, f in sorted(found)]


def make_backup(path):
    """Copy the file into the hidden backup folder next to it; None if there is no file yet."""
    path = Path(path)
    if not path.is_file():
        return None
    d = backup_dir(path)
    d.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    # several writes within one second: number them after the highest one already there, so order is kept
    used = [k[1] for f in list_backups(path) if (k := _backup_key(path, f))[0] == stamp]
    dest = d / (f"{path.name}.{stamp}.csv" if not used else f"{path.name}.{stamp}-{max(used) + 1}.csv")
    shutil.copy2(path, dest)
    return dest


def prune_backups(path) -> None:
    for old in list_backups(path)[:-BACKUPS_KEPT]:
        with contextlib.suppress(OSError):
            old.unlink()


@contextlib.contextmanager
def backed_up(path):
    """Save a copy before a write. If the write fails without changing the file, the copy is dropped again."""
    b = make_backup(path)
    try:
        yield
    finally:
        if b is not None:
            try:
                if Path(path).is_file() and Path(path).read_bytes() == b.read_bytes():
                    b.unlink()
                else:
                    prune_backups(path)
            except OSError:
                pass


def write_bytes_atomic(path: str, data: bytes) -> None:
    """Replace the file's content atomically. A symlink stays a link (its target is updated); the temp file is
    unique and lives next to the real file."""
    real = os.path.realpath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real) or ".", prefix=os.path.basename(real) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(real):
            shutil.copymode(real, tmp)
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


LAST_WRITE: dict = {}  # file path -> sha1 of the content after the app's last write (in memory only)


def _file_hash(path) -> str:
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def note_write(path) -> None:
    LAST_WRITE[os.path.realpath(path)] = _file_hash(path)


def modified_outside(path) -> bool:
    """True if the file differs from what the app wrote last (only known after a write in this run)."""
    h = LAST_WRITE.get(os.path.realpath(path))
    return h is not None and h != _file_hash(path)


def pretty_stamp(stamp: str) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"


def undo_last_change(path: str, expected: str = "", shown_path: str = "") -> str:
    """Restore the newest backup, then move that backup to .portfolio_backups/undone/ so repeated undo goes
    back further. The replaced file is kept in undone/ too, so nothing is lost."""
    if shown_path and shown_path != path:
        raise LedgerError("a different file is selected now; reload the page and try again")
    backups = list_backups(path)
    if not backups:
        raise UserError("There is no earlier version to go back to yet.")
    newest = backups[-1]
    if expected and newest.name != expected:
        raise LedgerError("the file changed since this page was loaded; reload the page and try again")
    data = newest.read_bytes()
    undone = backup_dir(path) / "undone"
    undone.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if Path(path).is_file():
        shutil.copy2(path, undone / f"{Path(path).name}.replaced-{stamp}-{secrets.token_hex(3)}.csv")
    write_bytes_atomic(path, data)
    shutil.move(str(newest), str(undone / f"{newest.name}.{secrets.token_hex(3)}"))
    note_write(path)
    left = len(list_backups(path))
    return (f"restored the version saved at {pretty_stamp(_backup_key(path, newest)[0])}; "
            f"{left} more undo step(s) available. The replaced version is kept in {BACKUP_DIR}/undone")


def delete_line(path: str, line: int, signature: str) -> str:
    """Remove exactly the bytes of one row. BOM, line endings and every other byte stay as they were."""
    data = Path(path).read_bytes()
    text = data.decode("utf-8")
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    delim, lines, recs = _parse(text[len(bom):])
    if not recs:
        raise LedgerError("file is empty")
    hit = [i for i, (_, end, _) in enumerate(recs) if end == line and i > 0]
    if not hit or sig(recs[hit[0]][2], path) != signature:
        raise LedgerError("the file changed since this page was loaded; reload the page and try again")
    first, last, row = recs[hit[0]]
    new_text = bom + "".join(lines[:first - 1] + lines[last:])
    with backed_up(path):
        write_bytes_atomic(path, new_text.encode("utf-8"))
    note_write(path)
    return " | ".join(row)


# --------------------------------------------------------------- page layout

NAV = [("/", "Transactions"), ("/add", "Add"), ("/dca", "Savings plan"), ("/report", "Report"), ("/example", "Example")]


def layout(title: str, body: str, active: str, fl: dict | None = None) -> bytes:
    menu = " | ".join(
        f"<b>{esc(t)}</b>" if u == active else f'<a href="{u}">{esc(t)}</a>' for u, t in NAV)
    msg = ""
    if fl:
        cls = "ok" if fl["kind"] == "ok" else "err"
        label = "Done: " if fl["kind"] == "ok" else "Problem: "
        msg = f'<div class="{cls}"><b>{label}</b>{esc(fl["msg"])}</div>'
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{esc(title)} - Portfolio tracker</title>
<style>
body{{font-family:sans-serif;max-width:1000px;margin:1em auto;padding:0 1em;line-height:1.4}}
nav{{padding:.5em;background:#eee;border:1px solid #bbb}}
.ok{{background:#dfd;border:1px solid #6a6;padding:.5em;margin:1em 0}}
.err{{background:#fdd;border:1px solid #c66;padding:.5em;margin:1em 0}}
.warn{{background:#ffd;border:1px solid #cc6;padding:.5em;margin:1em 0}}
table{{border-collapse:collapse}} td,th{{border:1px solid #999;padding:2px 6px;text-align:right}}
td.l,th.l{{text-align:left}}
label{{display:block;margin:.4em 0}} input[type=text]{{padding:2px}}
pre{{background:#f6f6f6;border:1px solid #bbb;padding:.5em;overflow:auto}}
small{{color:#555}} fieldset{{margin:1em 0}}
#busy{{display:none;position:fixed;top:0;left:0;right:0;background:#ffd;border-bottom:2px solid #c90;padding:.6em;text-align:center}}
</style></head><body>
<div id="busy"><b>Working, please wait...</b> Downloading prices can take a minute. Do not click again.</div>
<nav>{menu}</nav>
<p><small>File in use: <code>{esc(current_path())}</code></small></p>
<h1>{esc(title)}</h1>
{msg}
{body}
<script>
// Show "please wait" and ignore a second click while a form is being processed.
document.addEventListener("submit", function (e) {{
  var f = e.target;
  if (e.defaultPrevented) return;
  if (f.dataset.sent) {{ e.preventDefault(); return; }}
  f.dataset.sent = "1";
  document.getElementById("busy").style.display = "block";
}});
window.addEventListener("pageshow", function () {{  // back button: reset
  document.getElementById("busy").style.display = "none";
  document.querySelectorAll("form").forEach(function (f) {{ delete f.dataset.sent; }});
}});
</script>
</body></html>"""
    return page.encode("utf-8")


def field(label, name, value="", size=20, hint="", typ="text"):
    return (f'<label>{esc(label)}: <input type="{typ}" name="{name}" value="{esc(value)}" size="{size}"> '
            f'<small>{esc(hint)}</small></label>')


def checkbox(label, name, checked, hint=""):
    return (f'<label><input type="checkbox" name="{name}" value="1"{" checked" if checked else ""}> {esc(label)} '
            f'<small>{esc(hint)}</small></label>')


# -------------------------------------------------------------------- pages


def page_transactions(fl, q) -> bytes:
    path = current_path()
    browse = ""
    if sys.platform == "darwin":
        browse = ('<form method="post" action="/browse"><p>Or pick with Finder: '
                  '<button name="mode" value="open">Choose an existing file...</button> '
                  '<button name="mode" value="new">Make a new file...</button></p></form>')
    body = f"""
<form method="post" action="/setfile">
{field("CSV file path", "path", path, 60, "full path, e.g. /Users/you/Desktop/portfolio.csv (~ is allowed)")}
<button name="action" value="use">Use this file</button>
<button name="action" value="create">Create new empty file here</button>
</form>
{browse}
"""
    try:
        p = Path(expand(path))
        if not p.exists():
            body += ('<div class="warn">This file does not exist yet. Press "Create new empty file here" first; '
                     'Add and Savings plan need an existing file.</div>')
        elif p.is_dir():
            body += '<div class="err">That path is a folder, not a file. Add a file name, e.g. portfolio.csv.</div>'
        else:
            body += table_html(p)
        body += undo_html(p)
    except (OSError, ValueError) as e:
        body += f'<div class="err">{esc(friendly(e))}</div>'
    return layout("Transactions", body, "/", fl)


def undo_html(p: Path) -> str:
    backups = list_backups(p)
    if not backups:
        return ""
    newest = backups[-1]
    stamp = esc(pretty_stamp(_backup_key(p, newest)[0]))
    warn = ""
    if modified_outside(p):
        warn = (" WARNING: the file was changed outside this app after its last write; those changes "
                "will be replaced (the current file is kept in the undone folder).")
    n = len(backups)
    return (f'<form method="post" action="/undo" onsubmit="return confirm(\'Go back to the version saved at '
            f'{stamp}?{esc(warn)} The current file is kept, and you can undo again to go back further.\')">'
            f'<input type="hidden" name="backup" value="{esc(newest.name)}">'
            f'<input type="hidden" name="path" value="{esc(str(p))}">'
            f'<p><button>Undo last change</button> <small>{n} undo step(s) available. Puts the file back as it was '
            f'before the last add, savings plan or delete (saved {stamp}); each further click goes back one more step. '
            f'Copies are kept in the hidden folder <code>{esc(BACKUP_DIR)}</code> next to the file '
            f'(the last {BACKUPS_KEPT}).</small></p>'
            + (f'<div class="warn">{esc(warn.strip())}</div>' if warn else "") + '</form>')


def table_html(p: Path) -> str:
    out = []
    try:
        delim, raw = read_raw(str(p))
    except Exception as e:
        return f'<div class="err">Cannot read the file: {esc(friendly(e))}</div>'
    sigs = {ln: sig(row, str(p)) for ln, row in raw[1:]}
    if not any(any(c.strip() for c in row) for _, row in raw[1:]):
        return "<p>No transactions yet. Add some on the <a href=\"/add\">Add</a> or <a href=\"/dca\">Savings plan</a> page.</p>"
    try:
        txs = load_transactions(p)
    except LedgerError as e:
        out.append(f'<div class="err">The file has a problem: {esc(e)}<br>Fix or delete the bad row below '
                   f'(or edit the file in a spreadsheet program).</div>')
        out.append("<h2>Raw rows</h2><table><tr><th>line in file</th>"
                   + "".join(f"<th>{esc(h)}</th>" for h in (raw[0][1] if raw else [])) + "<th></th></tr>")
        for ln, row in raw[1:]:
            out.append(f"<tr><td>{ln}</td>" + "".join(f"<td class='l'>{esc(c)}</td>" for c in row)
                       + f"<td>{del_form(ln, sigs[ln])}</td></tr>")
        out.append("</table>")
        return "\n".join(out)
    except Exception as e:
        return f'<div class="err">{esc(friendly(e))}</div>'
    out.append(f"<p>{len(txs)} transactions (sorted by date). Every change keeps a copy of the previous file, "
               f"so you can use <b>Undo last change</b> below the table.</p>")
    out.append("<table><tr><th>date</th><th class='l'>type</th><th class='l'>ticker</th><th>quantity</th><th>price</th>"
               "<th>fee</th><th>currency</th><th>amount</th><th>fx_rate</th><th></th></tr>")

    def n(x, blank_zero=True):
        return "" if x is None or (blank_zero and x == 0) else f"{x:g}"
    for t in txs:
        out.append(
            f"<tr><td>{t.date.date()}</td><td class='l'>{esc(t.type)}</td><td class='l'>{esc(t.ticker)}</td>"
            f"<td>{n(t.quantity)}</td><td>{n(t.price)}</td><td>{n(t.fee)}</td><td>{esc(t.currency or '')}</td>"
            f"<td>{n(t.amount)}</td><td>{n(t.fx_rate)}</td>"
            f"<td>{del_form(t.line, sigs.get(t.line, ''))}</td></tr>")
    out.append("</table>")
    return "\n".join(out)


def del_form(line, sg):
    return (f'<form method="post" action="/delete" style="margin:0" onsubmit="return confirm(\'Delete this row?\')">'
            f'<input type="hidden" name="line" value="{line}"><input type="hidden" name="sig" value="{esc(sg)}">'
            f'<button>Delete</button></form>')


def page_add(fl, q) -> bytes:
    v = (fl or {}).get("values", {})
    g = lambda k, d="": v.get(k, d)  # noqa: E731
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    typ = g("type", "buy")
    opts = "".join(f'<option value="{t}"{" selected" if t == typ else ""}>{t}</option>' for t in ("buy", "sell", "dividend"))
    body = f"""
<p>Adds one row at the end of the file above. Decimal comma or point both work (118,40 or 118.40).</p>
<form method="post" action="/add">
<label>Type: <select name="type">{opts}</select></label>
{field("Date", "date", g("date", today), 12, "YYYY-MM-DD or DD.MM.YYYY")}
{field("Ticker", "ticker", g("ticker"), 14, "Yahoo symbol, e.g. VWCE.DE, AAPL, CSPX.L")}
<fieldset><legend>For buy / sell</legend>
{field("Quantity", "quantity", g("quantity"), 12, "units traded")}
{field("Price per unit", "price", g("price"), 12, "in the currency below")}
</fieldset>
<fieldset><legend>For dividend</legend>
{field("Amount received", "amount", g("amount"), 12, "total cash after tax")}
</fieldset>
<fieldset><legend>Optional</legend>
{field("Fee", "fee", g("fee"), 10, "in the currency below")}
{field("Currency", "currency", g("currency"), 8, "empty = the ticker's own currency; GBX for pence")}
{field("FX rate", "fx_rate", g("fx_rate"), 10, "base currency per 1 unit of currency, as charged by broker")}
</fieldset>
{checkbox("save anyway", "force_hold", False, "only for a sell above the quantity held (e.g. after a stock split)")}
<button>Add transaction</button>
</form>"""
    return layout("Add transaction", body, "/add", fl)


def page_dca(fl, q) -> bytes:
    v = (fl or {}).get("values", {})
    s = load_settings().get("dca", {})
    g = lambda k, d="": v.get(k, s.get(k, d))  # noqa: E731
    wd = g("weekday", "mon")
    wdopts = "".join(f'<option value="{w}"{" selected" if w == wd else ""}>{w}</option>' for w in cli.WEEKDAYS)
    mode = g("mode", "weekly")
    body = f"""
<p>Writes one buy per plan date into the file above, priced at the market close (a holiday moves the buy to the next
trading day). The fee is taken out of the amount. <b>Run each plan once</b>; the page warns you if buys of that ticker
already exist in the date range.</p>
<form method="post" action="/dca">
{field("Ticker", "ticker", g("ticker"), 14, "e.g. NQSE.DE")}
{field("Amount per purchase", "amount", g("amount"), 10, "fee included")}
{field("Start date", "start", g("start"), 12, "YYYY-MM-DD")}
{field("End date", "end", g("end"), 12, "empty = today")}
<fieldset><legend>When</legend>
<label><input type="radio" name="mode" value="weekly"{" checked" if mode != "monthly" else ""}> Every week on
<select name="weekday">{wdopts}</select>
<small>only trading weekdays (Monday to Friday) exist; there is no Saturday or Sunday</small></label>
<label><input type="radio" name="mode" value="monthly"{" checked" if mode == "monthly" else ""}> Every month on day
<input type="text" name="monthly" value="{esc(g('monthly', '1'))}" size="3"> (1-31)
<small>if that day is a weekend or holiday, the buy happens on the next trading day</small></label>
</fieldset>
{field("Fee per purchase", "fee", g("fee", "0"), 8, "money paid to the broker per buy; 0 if none")}
{field("Currency", "currency", g("currency"), 8, "leave empty unless you pay in another currency than the ticker's own")}
{field("Decimals for units", "decimals", g("decimals", "4"), 4, "how many decimals the bought quantity gets (4 is fine)")}
<fieldset><legend>Prices source (optional)</legend>
{field("Offline prices CSV", "prices", g("prices"), 50, "leave empty unless you have your own prices file; empty = download from Yahoo")}
{field("Cache folder", "cache_dir", g("cache_dir"), 50, "leave empty unless you want downloaded prices stored elsewhere")}
{checkbox("Offline: cached prices only", "offline", g("offline") == "1", "tick only if you have no internet; works only for prices downloaded before")}
</fieldset>
<button>Create savings plan</button>
</form>"""
    return layout("Savings plan", body, "/dca", fl)


def page_dca_warn(fl, q) -> bytes:
    """GET page for a pending savings plan. Reloading it is harmless: nothing is written or used up."""
    tok = q.get("t", [""])[0]
    pend = PENDING.get(tok)
    if not pend:
        return layout("Savings plan: possible duplicates",
                      '<div class="err">This warning is no longer available (it was already used, or the app was '
                      'restarted). Nothing was written. <a href="/dca">Back to the savings plan form</a></div>', "/dca")
    dups, form = pend["dups"], pend["form"]
    rows = "".join(f"<tr><td>{esc(d)}</td><td>{esc(qty)}</td><td>{esc(pr)}</td></tr>" for d, qty, pr in dups[:30])
    more = f"<p>... and {len(dups) - 30} more.</p>" if len(dups) > 30 else ""
    body = f"""
<div class="warn"><b>Warning:</b> the file already has {len(dups)} buy(s) of {esc(form['ticker'].upper())} in this date
range (plus 6 days for holiday shifts). Running the plan would probably duplicate them. Nothing has been written.</div>
<table><tr><th>date</th><th>quantity</th><th>price</th></tr>{rows}</table>{more}
<form method="post" action="/dca/confirm" style="display:inline">
<input type="hidden" name="t" value="{esc(tok)}"><button>Add the plan anyway</button></form>
<a href="/dca">Cancel (go back to the form)</a>"""
    return layout("Savings plan: possible duplicates", body, "/dca")


def report_form(action, v, s):
    g = lambda k, d="": v.get(k, s.get(k, d))  # noqa: E731
    return f"""
<form method="post" action="{action}">
{field("Show results in currency", "base", g("base", "EUR"), 6, "the currency all totals are converted to, e.g. EUR or USD")}
{field("As of date", "as_of", g("as_of"), 12, "YYYY-MM-DD; leave empty for today (a future date has no prices yet)")}
{field("Benchmarks", "benchmarks", g("benchmarks"), 40, "optional: funds to compare with, separated by comma or space, e.g. VWCE.DE SPY")}
{field("How many best and worst holdings to list", "top", g("top", "5"), 4, "a whole number, 5 is a good start")}
{checkbox("Offline: cached prices only (no internet)", "offline", g("offline") == "1", "tick only if you have no internet; works only for prices downloaded before")}
{checkbox("Quantities already split-adjusted (turn off split adjustment)", "no_split_adjust", g("no_split_adjust") == "1", "leave unticked unless you already corrected your quantities for stock splits")}
{field("Offline prices CSV", "prices", g("prices"), 50, "leave empty unless you have your own prices file; empty = download from Yahoo")}
{field("Cache folder", "cache_dir", g("cache_dir"), 50, "leave empty unless you want downloaded prices stored somewhere else (default: ~/.cache/portfolio_tracker)")}
<button>Run report</button>
</form>"""


def result_html(rid: str | None) -> str:
    res = RESULTS.get(rid or "")
    if not res:
        return ""
    notes = "".join(f'<div class="warn">{esc(n)}</div>' for n in res.get("notes", []))
    return (f"<h2>Result</h2>{notes}<p><small>{esc(res['desc'])}</small></p>"
            f'<p><a href="/download/json?r={quote(rid)}">Download JSON</a> | '
            f'<a href="/download/daily?r={quote(rid)}">Download daily CSV</a></p>'
            f"<pre>{esc(res['text'])}</pre>")


LEGEND = """
<h2>What the words mean</h2>
<ul>
<li><b>XIRR</b>: the yearly return on the money you actually put in, taking into account <i>when</i> you added it.</li>
<li><b>TWR</b> (time-weighted return): how the investments themselves performed, no matter when you added or took out money.</li>
<li><b>MWR</b> (money-weighted return): the return on your own money, so it depends on when you added or took out money.
 If it is below the TWR, your timing cost you a little.</li>
<li><b>Drawdown</b>: the biggest fall from a high point to the lowest point after it; it shows how bad the worst dip was.</li>
<li><b>UP / DOWN</b>: the holdings that gained the most (UP) and lost the most (DOWN) money.</li>
</ul>"""


def page_report(fl, q) -> bytes:
    v = (fl or {}).get("values", {})
    body = "<p>Uses the file shown above.</p>" + report_form("/report", v, load_settings().get("report", {}))
    body += result_html(q.get("r", [None])[0]) + LEGEND
    return layout("Report", body, "/report", fl)


def page_example(fl, q) -> bytes:
    body = f"""
<p>Runs a report on the built-in example (<code>{esc(EXAMPLE_TX)}</code>: 40 EUR of NQSE.DE every Monday for two
years, synthetic prices, as of 2026-11-09, benchmark VWCE.DE). It does not touch your own file or settings and needs no
internet.</p>
<form method="post" action="/example"><button>Run the example report</button></form>"""
    body += result_html(q.get("r", [None])[0]) + LEGEND
    return layout("Example", body, "/example", fl)


# ------------------------------------------------------------------ actions


def make_provider(prices: str, cache_dir: str, offline: bool):
    if prices.strip():
        pp = expand(prices)
        if not Path(pp).is_file():
            raise UserError(f"prices file not found: {pp}")
        return StaticProvider.from_csv(pp)
    return YahooProvider(cache_dir=expand(cache_dir) if cache_dir.strip() else None, offline=offline)


def run_report(tx_path: str, base: str, as_of: str, benchmarks: list, top: int, provider, no_split: bool) -> dict:
    txs = load_transactions(tx_path)
    pf = Portfolio(txs, provider, base=base, as_of=pd.Timestamp(as_of) if as_of else None,
                   benchmarks=benchmarks, split_adjust=not no_split)
    rep = pf.report(top_n=top)
    return {
        "text": render_text(rep, top_n=top),
        "json": render_json(rep),
        "daily": rep.daily.to_csv(index_label="date"),
        "desc": f"{tx_path}, base {base}, as of {as_of or 'today'}",
        "notes": list(rep.warnings),
    }


def do_report(f: dict):
    base = (f.get("base") or "EUR").strip().upper() or "EUR"
    as_of = check_date(f.get("as_of", ""), "As of date", required=False)
    top = num(f.get("top", ""), "How many best and worst holdings to list", default=5.0, kind=int)
    if top < 1:
        raise UserError("How many best and worst holdings to list must be at least 1")
    bms = [b.strip() for b in (f.get("benchmarks") or "").replace(",", " ").split() if b.strip()]
    keep = {k: f.get(k, "") for k in ("base", "as_of", "benchmarks", "top", "offline", "no_split_adjust", "prices", "cache_dir")}
    p = Path(expand(current_path()))
    if not p.is_file():
        raise UserError(f"transactions file not found: {p}. Choose a file on the Transactions page.")
    provider = make_provider(f.get("prices", ""), f.get("cache_dir", ""), f.get("offline") == "1")
    res = run_report(str(p), base, as_of, bms, top, provider, f.get("no_split_adjust") == "1")
    save_settings(report=keep)
    return store_result(res)


def do_example():
    res = run_report(str(EXAMPLE_TX), "EUR", "2026-11-09", ["VWCE.DE"], 5,
                     StaticProvider.from_csv(str(EXAMPLE_PRICES)), False)
    res["desc"] = "Example: examples/transactions_example.csv with examples/prices_synthetic.csv, as of 2026-11-09"
    return store_result(res)


def require_file(path: str) -> None:
    if not os.path.isfile(path):
        raise UserError(f"file not found: {path}. Choose it again or create a new one on the Transactions page.")


def held_on(path: str, ticker: str, date: pd.Timestamp) -> float:
    """Quantity of ticker held at the end of `date` according to the file (quantities as entered)."""
    if os.path.getsize(path) == 0:
        return 0.0
    q = 0.0
    for t in load_transactions(path, allow_empty=True):
        if t.ticker == ticker and t.date <= date:
            q += t.quantity if t.type == "buy" else -t.quantity if t.type == "sell" else 0.0
    return q


def do_add(f: dict) -> str:
    typ = f.get("type", "")
    if typ not in ("buy", "sell", "dividend"):
        raise UserError("choose a type")
    date = (f.get("date") or "").strip() or pd.Timestamp.today().strftime("%Y-%m-%d")
    g = lambda k: (f.get(k) or "").strip() or None  # noqa: E731
    try:
        ticker = clean_ticker(f.get("ticker"))
        day = _date(date, 0)
    except LedgerError as e:
        raise UserError(str(e)) from None
    date = day.strftime("%Y-%m-%d")
    if typ == "dividend":
        if not g("amount"):
            raise UserError("a dividend needs the Amount received")
        kw = dict(quantity=None, price=None, amount=g("amount"))
    else:
        if not g("quantity") or not g("price"):
            raise UserError(f"a {typ} needs Quantity and Price per unit")
        kw = dict(quantity=g("quantity"), price=g("price"), amount=None)
    path = expand(current_path())
    with LOCK:
        require_file(path)
        row = dict(date=date, type=typ, ticker=ticker, fee=g("fee"), currency=g("currency"), fx_rate=g("fx_rate"), **kw)
        if typ == "sell" and f.get("force_hold") != "1":
            try:
                tx = parse_rows([{c: "" if row.get(c) is None else str(row.get(c)) for c in CSV_COLUMNS}], lines=[0])[0]
            except LedgerError as e:
                raise UserError(re.sub(r"^line \d+: ", "", str(e))) from None
            held = held_on(path, ticker, day)
            if tx.quantity > held + 1e-9:
                raise UserError(f"you would sell {tx.quantity:g} but only hold {max(held, 0):g} on that date. "
                                "If a stock split happened, tick 'save anyway'")
        with backed_up(path):
            append_transaction(path, **row)
        note_write(path)
    msg = f"added {typ} {ticker} on {date}"
    if day > pd.Timestamp.today().normalize():
        msg += f". Note: {date} is in the future, so it is ignored in reports until then"
    return msg


def do_dca(f: dict) -> str | list:
    """Returns a message on success, or a list of duplicate rows (needs confirmation)."""
    try:
        ticker = clean_ticker(f.get("ticker"))
    except LedgerError as e:
        raise UserError(str(e)) from None
    amount = num(f.get("amount", ""), "Amount")
    start = check_date(f.get("start", ""), "Start date")
    end = check_date(f.get("end", ""), "End date", required=False)
    fee = num(f.get("fee", ""), "Fee", default=0.0)
    decimals = num(f.get("decimals", ""), "Decimals", default=4.0, kind=int)
    monthly = None
    if f.get("mode") == "monthly":
        monthly = num(f.get("monthly", ""), "Day of month", kind=int)
        if not 1 <= monthly <= 31:
            raise UserError("Day of month must be between 1 and 31")
    if not 0 <= decimals <= 10:
        raise UserError("Decimals must be between 0 and 10")
    if end and pd.Timestamp(end) < pd.Timestamp(start):
        raise UserError("End date is before the start date")
    path = expand(current_path())
    require_file(path)
    if f.get("force") != "1" and Path(path).is_file():
        lo = pd.Timestamp(start)
        hi = (pd.Timestamp(end) if end else pd.Timestamp.today().normalize()) + pd.Timedelta(days=6)
        try:
            dups = [(str(t.date.date()), f"{t.quantity:g}", f"{t.price:g}") for t in load_transactions(path)
                    if t.type == "buy" and t.ticker == ticker and lo <= t.date <= hi]
        except LedgerError:
            dups = []  # a broken file is reported by the append itself
        if dups:
            return dups
    provider = make_provider(f.get("prices", ""), f.get("cache_dir", ""), f.get("offline") == "1")
    args = argparse.Namespace(transactions=path, ticker=ticker, amount=amount, start=start, end=end,
                         weekday=f.get("weekday", "mon") if f.get("weekday") in cli.WEEKDAYS else "mon",
                         monthly=monthly, fee=fee, currency=(f.get("currency") or "").strip() or None,
                         decimals=decimals, prices=None, cache_dir=None, offline=False)
    buf = io.StringIO()
    with LOCK, backed_up(path), contextlib.redirect_stdout(buf):
        cli.cmd_dca(args, provider=provider)
    note_write(path)
    save_settings(dca={k: f.get(k, "") for k in ("ticker", "amount", "start", "end", "mode", "weekday", "monthly",
                                                 "fee", "currency", "decimals", "prices", "cache_dir", "offline")})
    return buf.getvalue().strip() or "plan created"


def do_setfile(f: dict) -> str:
    raw = (f.get("path") or "").strip()
    if not raw:
        raise UserError("enter a file path")
    p = Path(expand(raw))
    if p.is_dir():
        raise UserError(f"{p} is a folder; add a file name such as portfolio.csv")
    if not p.parent.is_dir():
        raise UserError(f"the folder {p.parent} does not exist. Check the path, or create the folder first.")
    if f.get("action") == "create":
        if p.exists() and p.stat().st_size > 0:
            raise UserError(f"{p} already exists and is not empty; it was not changed. Use 'Use this file'.")
        p.write_text(",".join(CSV_COLUMNS) + "\n", encoding="utf-8")
        save_settings(csv_path=str(p))
        return f"created {p} and selected it"
    save_settings(csv_path=str(p))
    return f"now using {p}" + ("" if p.exists() else " (file does not exist yet)")


def do_browse(mode: str) -> str:
    """macOS only: let the user pick the file in a Finder dialog."""
    if mode == "new":
        pick = ('choose file name with prompt "Name your new transactions file" '
                'default name "portfolio.csv" default location (path to desktop folder)')
    else:
        pick = 'choose file with prompt "Choose your transactions CSV file"'
    script = f"activate\nPOSIX path of ({pick})"  # activate: bring the dialog to the front
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
    if out.returncode != 0 or not out.stdout.strip():
        raise UserError("no file chosen")
    p = Path(out.stdout.strip())
    if mode == "new" and not p.exists():
        p.write_text(",".join(CSV_COLUMNS) + "\n", encoding="utf-8")
        save_settings(csv_path=str(p))
        return f"created {p} and selected it"
    save_settings(csv_path=str(p))
    return f"now using {p}"


# ------------------------------------------------------------------- server


class Server(ThreadingHTTPServer):
    request_queue_size = 64  # a browser may open several connections at once


MAX_BODY = 1_000_000


class Handler(BaseHTTPRequestHandler):
    server_version = "PortfolioUI"

    def log_message(self, fmt, *a):
        pass

    def send(self, body: bytes, status=200, ctype="text/html; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, url):
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def allowed(self) -> tuple:
        port = self.server.server_address[1]
        return f"127.0.0.1:{port}", f"localhost:{port}"

    def host_ok(self) -> bool:
        return (self.headers.get("Host") or "") in self.allowed()

    def do_GET(self):
        if not self.host_ok():
            return self.send(b"bad host", 403, "text/plain")
        u = urlparse(self.path)
        q = parse_qs(u.query)
        fl = FLASH.pop(q.get("f", [""])[0], None)
        try:
            pages = {"/": page_transactions, "/add": page_add, "/dca": page_dca,
                     "/report": page_report, "/example": page_example, "/dca/warn": page_dca_warn}
            if u.path in pages:
                return self.send(pages[u.path](fl, q))
            if u.path in ("/download/json", "/download/daily"):
                res = RESULTS.get(q.get("r", [""])[0])
                if not res:
                    return self.send(layout("Not found", "<p>That report is no longer available. Run it again.</p>", ""), 404)
                if u.path.endswith("json"):
                    return self.send(res["json"].encode(), ctype="application/json",
                                     extra={"Content-Disposition": 'attachment; filename="report.json"'})
                return self.send(res["daily"].encode(), ctype="text/csv",
                                 extra={"Content-Disposition": 'attachment; filename="daily.csv"'})
            return self.send(layout("Not found", '<p>No such page. <a href="/">Home</a></p>', ""), 404)
        except Exception as e:
            self.send(layout("Error", f'<div class="err">{esc(friendly(e))}</div>', ""), 500)

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.host_ok() or (origin and urlparse(origin).netloc not in self.allowed()):
            return self.send(b"forbidden", 403, "text/plain")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0:
                raise ValueError
        except ValueError:
            return self.send(b"bad request: invalid Content-Length", 400, "text/plain")
        if n > MAX_BODY:
            return self.send(b"request too large", 413, "text/plain")
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True).items()}
        path = urlparse(self.path).path
        back = {"/setfile": "/", "/delete": "/", "/browse": "/", "/add": "/add", "/dca": "/dca",
                "/report": "/report", "/example": "/example", "/dca/confirm": "/dca", "/undo": "/"}.get(path)
        if back is None:
            return self.send(b"not found", 404, "text/plain")
        try:
            with LOCK:
                if path == "/setfile":
                    tok = flash("ok", do_setfile(form))
                elif path == "/browse":
                    tok = flash("ok", do_browse(form.get("mode", "open")))
                elif path == "/delete":
                    p = expand(current_path())
                    try:
                        line = int(form.get("line", "0") or 0)
                    except ValueError:
                        raise UserError("that row number is not valid; reload the page and try again")
                    removed = delete_line(p, line, form.get("sig", ""))
                    tok = flash("ok", f"deleted row: {removed}. You can undo this on this page.")
                elif path == "/undo":
                    tok = flash("ok", undo_last_change(expand(current_path()), form.get("backup", ""), form.get("path", "")))
                elif path == "/add":
                    tok = flash("ok", do_add(form))
                elif path == "/dca":
                    r = do_dca(form)
                    if isinstance(r, list):  # warn, write nothing; POST/redirect/GET so a reload does not re-POST
                        pt = secrets.token_urlsafe(12)
                        PENDING[pt] = {"dups": r, "path": expand(current_path()), "hash": _file_hash(expand(current_path())),
                                       "form": {k: v for k, v in form.items() if k != "force"}}
                        while len(PENDING) > 20:
                            PENDING.popitem(last=False)
                        return self.redirect(f"/dca/warn?{urlencode({'t': pt})}")
                    tok = flash("ok", r)
                elif path == "/dca/confirm":
                    pend = PENDING.pop(form.get("t", ""), None)  # usable once
                    if not pend:
                        raise UserError("this savings plan was already added or the warning expired; nothing was written. "
                                        "Check the Transactions page, then fill in the form again if needed")
                    form = dict(pend["form"])
                    if pend["path"] != expand(current_path()):
                        raise LedgerError("a different file is selected now; nothing was written")
                    if pend.get("hash") != _file_hash(pend["path"]):
                        raise LedgerError("the file changed, run the plan again. Nothing was written")
                    tok = flash("ok", do_dca({**form, "force": "1"}))
                elif path == "/report":
                    rid = do_report(form)
                    return self.redirect(f"/report?r={quote(rid)}")
                else:  # /example
                    rid = do_example()
                    return self.redirect(f"/example?r={quote(rid)}")
        except Exception as e:
            msg = friendly(e)
            tok = flash("err", msg, form if path in ("/add", "/dca", "/dca/confirm", "/report") else None)
        self.redirect(f"{back}?{urlencode({'f': tok})}")


def already_running(port: int) -> bool:
    """True if this UI is already serving on that port (e.g. the launcher was double-clicked twice)."""
    try:
        with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            return r.headers.get("Server", "").startswith("PortfolioUI")
    except Exception:
        return False


def make_server(port: int) -> ThreadingHTTPServer:
    """Bind to 127.0.0.1:port, or to a free port if that one is taken. port=0 always picks a free port."""
    try:
        return Server(("127.0.0.1", port), Handler)
    except OSError:
        print(f"Port {port} is busy, picking a free one.")
        return Server(("127.0.0.1", 0), Handler)


def serve(port: int = 8765, open_browser: bool = True) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    if port and already_running(port):
        url = f"http://127.0.0.1:{port}/"
        print(f"Portfolio tracker is already running at {url}; opening it.")
        if open_browser:
            webbrowser.open(url)
        return 0
    srv = make_server(port)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Portfolio tracker is running at {url}\n"
          "Keep this window open while you use it. To stop, close this window (or press Ctrl+C).", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m portfolio_tracker.ui", description="Portfolio tracker local web page")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="do not open the browser")
    a = ap.parse_args(argv)
    return serve(a.port, open_browser=not a.no_browser)


if __name__ == "__main__":
    sys.exit(main())
