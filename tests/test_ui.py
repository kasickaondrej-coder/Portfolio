"""Local web UI: start the server on a free port in a thread and drive it with urllib.

No network: reports and savings plans use the example price file.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pytest

from portfolio_tracker import cli, ui
from portfolio_tracker.transactions import load_transactions

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_PRICES = str(REPO / "examples" / "prices_synthetic.csv")
HEADER = "date,type,ticker,quantity,price,fee,currency,amount,fx_rate"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # settings file and default CSV live here
    srv = ui.make_server(0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url: str, headers: dict | None = None) -> tuple[int, str, dict]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)


def post(url: str, data: dict, headers: dict | None = None) -> tuple[int, str, str]:
    """POST a form; redirects are followed. Returns (status, body, final url)."""
    req = urllib.request.Request(url, data=urlencode(data).encode(), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8"), r.url
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), url


def use_file(base: str, path: Path, create: bool = True) -> str:
    return post(base + "/setfile", {"path": str(path), "action": "create" if create else "use"})[1]


def test_all_pages_render(server):
    for page in ("/", "/add", "/dca", "/report", "/example"):
        status, body, headers = get(server + page)
        assert status == 200 and "<nav>" in body, page
        assert headers["Cache-Control"] == "no-store"
    assert get(server + "/nope")[0] == 404


def test_create_file_and_refuse_overwrite(server, tmp_path):
    f = tmp_path / "my.csv"
    body = use_file(server, f)
    assert "Done:" in body and f.read_text().strip() == HEADER
    assert "No transactions yet" in body
    f.write_text(HEADER + "\n2024-01-02,buy,X,1,1,,,,\n")
    body = use_file(server, f)  # "create" on a non-empty file must not touch it
    assert "already exists" in body and "2024-01-02" in f.read_text()
    assert json.loads((tmp_path / ".portfolio_tracker_ui.json").read_text())["csv_path"] == str(f)


def test_add_buy_dividend_sell_and_errors(server, tmp_path):
    f = tmp_path / "my.csv"
    use_file(server, f)
    status, body, url = post(server + "/add", {"type": "buy", "date": "2025-01-06", "ticker": "NQSE.DE",
                                               "quantity": "10", "price": "12,5", "fee": "1", "currency": "EUR",
                                               "fx_rate": "1"})
    assert status == 200 and "added buy NQSE.DE" in body and url.startswith(server + "/add?f=")
    post(server + "/add", {"type": "dividend", "date": "2025-06-01", "ticker": "NQSE.DE", "amount": "3.2"})
    post(server + "/add", {"type": "sell", "date": "2025-07-01", "ticker": "NQSE.DE", "quantity": "2", "price": "13"})
    txs = load_transactions(f)
    assert [t.type for t in txs] == ["buy", "dividend", "sell"]
    assert txs[0].price == 12.5 and txs[0].fee == 1 and txs[0].fx_rate == 1 and txs[1].amount == 3.2

    before = f.read_text()
    _, body, _ = post(server + "/add", {"type": "buy", "ticker": "AAPL", "quantity": "5", "price": ""})
    assert "Problem:" in body and "Quantity and Price" in body
    assert 'value="5"' in body  # the form keeps what was typed
    _, body, _ = post(server + "/add", {"type": "buy", "date": "31/31/2025", "ticker": "X", "quantity": "1",
                                        "price": "1"})
    assert "Problem:" in body and f.read_text() == before


def test_savings_plan_and_duplicate_guard(server, tmp_path):
    f = tmp_path / "my.csv"
    use_file(server, f)
    form = {"ticker": "VWCE.DE", "amount": "100", "start": "2025-01-01", "end": "2025-06-30", "mode": "monthly",
            "monthly": "15", "weekday": "mon", "fee": "1", "currency": "", "decimals": "", "prices": EXAMPLE_PRICES,
            "cache_dir": "", "offline": ""}
    _, body, _ = post(server + "/dca", form)
    assert "added 6 buys of VWCE.DE" in body
    txs = load_transactions(f)
    assert len(txs) == 6 and all(t.fee == 1 for t in txs)
    assert all(t.date.day >= 15 for t in txs)

    _, body, _ = post(server + "/dca", form)  # same plan again: warning, nothing written
    assert "possible duplicates" in body and len(load_transactions(f)) == 6
    _, body, _ = post(server + "/dca", {**form, "force": "1"})
    assert "added 6 buys" in body and len(load_transactions(f)) == 12

    weekly = {**form, "ticker": "NQSE.DE", "amount": "40", "start": "2026-10-01", "end": "2026-10-31", "mode": "weekly",
              "weekday": "wed", "fee": "", "decimals": "3"}
    _, body, _ = post(server + "/dca", weekly)
    assert "added 4 buys of NQSE.DE" in body  # Wednesdays 2026-10-07 .. 2026-10-28
    assert {t.date.weekday() for t in load_transactions(f) if t.ticker == "NQSE.DE"} == {2}

    _, body, _ = post(server + "/dca", {**form, "start": "", "ticker": "NEW"})
    assert "Problem:" in body and "Start date is required" in body


def test_delete_checks_signature_and_keeps_backup(server, tmp_path):
    f = tmp_path / "my.csv"
    f.write_text(HEADER + "\n2024-01-02,buy,AAA,1,10,,,,\n2024-02-02,buy,BBB,2,20,,,,\n")
    use_file(server, f, create=False)
    _, page, _ = get(server + "/")
    assert "AAA" in page and "BBB" in page
    delim, raw = ui.read_raw(str(f))
    line, row = raw[2]  # the BBB row
    original = f.read_text()

    _, body, _ = post(server + "/delete", {"line": str(line), "sig": "wrong"})
    assert "file changed" in body and f.read_text() == original

    _, body, _ = post(server + "/delete", {"line": str(line), "sig": ui.sig(row)})
    assert "deleted row" in body
    assert "BBB" not in f.read_text() and "AAA" in f.read_text()
    assert (tmp_path / "my.csv.bak").read_text() == original


def test_report_with_all_options_and_downloads(server, tmp_path):
    f = tmp_path / "my.csv"
    use_file(server, f)
    post(server + "/dca", {"ticker": "NQSE.DE", "amount": "40", "start": "2025-01-06", "end": "2026-11-09",
                           "mode": "weekly", "weekday": "mon", "prices": EXAMPLE_PRICES})
    _, body, url = post(server + "/report", {"base": "eur", "as_of": "2026-06-30", "benchmarks": "VWCE.DE, NQSE.DE",
                                             "top": "3", "no_split_adjust": "1", "prices": EXAMPLE_PRICES,
                                             "cache_dir": str(tmp_path / "cache"), "offline": ""})
    assert url.startswith(server + "/report?r=")
    assert "PORTFOLIO REPORT" in body and "as of 2026-06-30" in body and "VWCE.DE" in body
    rid = re.search(r"/download/json\?r=([\w-]+)", body).group(1)

    status, js, headers = get(f"{server}/download/json?r={rid}")
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    data = json.loads(js)
    assert data["base"] == "EUR"
    status, daily, headers = get(f"{server}/download/daily?r={rid}")
    assert status == 200 and daily.startswith("date,") and "text/csv" in headers["Content-Type"]
    assert get(f"{server}/download/json?r=unknown")[0] == 404

    # reloading the result page shows it again without recomputing anything
    status, again, _ = get(url)
    assert status == 200 and "PORTFOLIO REPORT" in again

    _, body, _ = post(server + "/report", {"as_of": "2026-13-45", "prices": EXAMPLE_PRICES, "top": "5"})
    assert "Problem:" in body and "is not a date" in body
    _, body, _ = post(server + "/report", {"prices": str(tmp_path / "missing.csv")})
    assert "prices file not found" in body


def test_example_report(server):
    _, body, url = post(server + "/example", {})
    assert url.startswith(server + "/example?r=") and "PORTFOLIO REPORT" in body and "as of 2026-11-09" in body


def test_rejects_other_sites(server):
    status, _, _ = post(server + "/add", {"type": "buy", "ticker": "X"}, headers={"Origin": "http://evil.example"})
    assert status == 403
    assert get(server + "/", headers={"Host": "evil.example"})[0] == 403


def test_cli_has_ui_subcommand(monkeypatch):
    seen = {}
    monkeypatch.setattr(ui, "serve", lambda port, open_browser: seen.update(port=port, open=open_browser) or 0)
    assert cli.main(["ui", "--port", "9999", "--no-browser"]) == 0
    assert seen == {"port": 9999, "open": False}
