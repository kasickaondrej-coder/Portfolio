"""Local web UI: start the server on a free port in a thread and drive it with urllib.

No network: reports and savings plans use the example price file.
"""
from __future__ import annotations

import html
import json
import re
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
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


def delete_form(base: str, f: Path, which: int):
    """(line, signature) of data row `which` (1 = first data row), as the page would send them."""
    _, raw = ui.read_raw(str(f))
    line, row = raw[which]
    return {"line": str(line), "sig": ui.sig(row, str(f))}


def test_delete_checks_signature_and_path(server, tmp_path):
    f = tmp_path / "my.csv"
    other = tmp_path / "other.csv"
    content = HEADER + "\n2024-01-02,buy,AAA,1,10,,,,\n2024-02-02,buy,BBB,2,20,,,,\n"
    f.write_text(content)
    other.write_text(content)
    use_file(server, f, create=False)
    _, page, _ = get(server + "/")
    assert "AAA" in page and "BBB" in page
    assert "file line" not in page  # internal detail, hidden from the table

    stale = delete_form(server, other, 2)  # a page that was built for another file
    _, body, _ = post(server + "/delete", stale)
    assert "file changed" in body and f.read_bytes() == content.encode()
    _, body, _ = post(server + "/delete", {**delete_form(server, f, 2), "sig": "wrong"})
    assert "file changed" in body and f.read_bytes() == content.encode()
    _, body, _ = post(server + "/delete", {"line": "abc", "sig": "x"})
    assert "Problem:" in body and f.read_bytes() == content.encode()

    _, body, _ = post(server + "/delete", delete_form(server, f, 2))
    assert "deleted row" in body
    assert "BBB" not in f.read_text() and "AAA" in f.read_text()


def test_delete_preserves_every_other_byte(server, tmp_path):
    f = tmp_path / "my.csv"
    # BOM, CRLF header, LF and CR line endings mixed, a quoted multi-line field, no final newline
    lines = [b"\xef\xbb\xbf" + HEADER.encode() + b"\r\n",
             b"2024-01-02,buy,AAA,1,10,,,,\r\n",
             b'2024-02-02,buy,"B\nB",2,20,,,,\n',
             b"2024-03-02,buy,CCC,3,30,,,,\r",
             b"2024-04-02,buy,DDD,4,40,,,,"]
    f.write_bytes(b"".join(lines))
    use_file(server, f, create=False)
    # delete the multi-line row (data row 2)
    _, body, _ = post(server + "/delete", delete_form(server, f, 2))
    assert "deleted row" in body
    assert f.read_bytes() == lines[0] + lines[1] + lines[3] + lines[4]
    # delete the last row, which has no line ending
    _, raw = ui.read_raw(str(f))
    n = len(raw) - 1
    post(server + "/delete", delete_form(server, f, n))
    assert f.read_bytes() == lines[0] + lines[1] + lines[3]
    # delete the first row
    post(server + "/delete", delete_form(server, f, 1))
    assert f.read_bytes() == lines[0] + lines[3]


def backups_of(f: Path):
    return ui.list_backups(f)


def test_backups_and_undo(server, tmp_path):
    f = tmp_path / "my.csv"
    use_file(server, f)
    assert 'action="/undo"' not in get(server + "/")[1]  # nothing to undo yet
    states = [f.read_bytes()]
    post(server + "/add", {"type": "buy", "date": "2025-01-06", "ticker": "AAA", "quantity": "1", "price": "10"})
    states.append(f.read_bytes())
    post(server + "/add", {"type": "buy", "date": "2025-01-07", "ticker": "BBB", "quantity": "1", "price": "10"})
    states.append(f.read_bytes())
    post(server + "/delete", delete_form(server, f, 1))
    bk = backups_of(f)
    assert len(bk) == 3 and bk[0].parent == tmp_path / ".portfolio_backups"
    assert re.fullmatch(r"my\.csv\.\d{8}-\d{6}(-\d+)?\.csv", bk[0].name)
    assert [b.read_bytes() for b in bk] == states  # every write saved the state before it

    # a failed add changes nothing and leaves no extra backup
    post(server + "/add", {"type": "buy", "ticker": "X", "quantity": "", "price": ""})
    assert len(backups_of(f)) == 3

    page = get(server + "/")[1]
    assert "Undo last change" in page and "confirm(" in page
    undo = dict(re.findall(r'name="(backup|path)" value="([^"]*)"', page))
    deleted_state = f.read_bytes()
    _, body, _ = post(server + "/undo", {"backup": "stale.csv", "path": undo["path"]})
    assert "changed since" in body and f.read_bytes() == deleted_state
    _, body, _ = post(server + "/undo", undo)
    assert "restored" in body and f.read_bytes() == states[2]
    assert backups_of(f)[-1].read_bytes() == deleted_state  # the undo itself can be undone
    # savings plan: undo removes an accidental duplicate plan
    form = {"ticker": "VWCE.DE", "amount": "100", "start": "2025-01-01", "end": "2025-03-30", "mode": "monthly",
            "monthly": "15", "prices": EXAMPLE_PRICES}
    before = f.read_bytes()
    post(server + "/dca", form)
    assert f.read_bytes() != before
    page = get(server + "/")[1]
    post(server + "/undo", dict(re.findall(r'name="(backup|path)" value="([^"]*)"', page)))
    assert f.read_bytes() == before


def test_backups_keep_last_20(server, tmp_path):
    f = tmp_path / "my.csv"
    use_file(server, f)
    for i in range(25):
        post(server + "/add", {"type": "buy", "date": "2025-01-06", "ticker": "AAA", "quantity": str(i + 1),
                               "price": "10"})
    bk = backups_of(f)
    assert len(bk) == 20
    assert bk[-1].read_bytes().count(b"\n") == 25  # newest = state before the 25th add (header + 24 rows)


def test_savings_plan_warning_is_post_redirect_get(server, tmp_path):
    f = tmp_path / "my.csv"
    use_file(server, f)
    form = {"ticker": "VWCE.DE", "amount": "100", "start": "2025-01-01", "end": "2025-03-30", "mode": "monthly",
            "monthly": "15", "prices": EXAMPLE_PRICES}
    post(server + "/dca", form)
    assert len(load_transactions(f)) == 3
    status, body, url = post(server + "/dca", form)
    assert status == 200 and "possible duplicates" in body
    assert "/dca/warn?t=" in url  # ended on a GET page, so a browser reload does not re-POST
    assert get(url)[1] == body and len(load_transactions(f)) == 3  # reload is harmless
    token = re.search(r'name="t" value="([^"]+)"', body).group(1)
    _, body, _ = post(server + "/dca/confirm", {"t": token})
    assert "added 3 buys" in body and len(load_transactions(f)) == 6
    _, body, _ = post(server + "/dca/confirm", {"t": token})  # the token works once only
    assert "already added" in body and len(load_transactions(f)) == 6
    assert "no longer available" in get(url)[1]
    _, body, _ = post(server + "/dca/confirm", {"t": "bogus"})
    assert "Problem:" in body and len(load_transactions(f)) == 6


def test_path_cleanup(server, tmp_path):
    home = tmp_path
    assert ui.expand('"/a/b.csv"') == "/a/b.csv"
    assert ui.expand("'/a/b.csv'  ") == "/a/b.csv"
    assert ui.expand("file:///a/my%20file.csv") == "/a/my file.csv"
    assert ui.expand("~/x.csv") == str(home / "x.csv")
    assert ui.expand("sub/x.csv") == str(home / "sub" / "x.csv")  # relative to home, not the server's cwd
    assert ui.expand("x.csv") == str(home / "x.csv")

    body = use_file(server, tmp_path / "nofolder" / "x.csv", create=False)
    assert "Problem:" in body and "does not exist" in body
    assert "nofolder" not in json.dumps(ui.load_settings())
    body = post(server + "/setfile", {"path": f'"file://{tmp_path}/ok.csv"', "action": "create"})[1]
    assert "Done:" in body and (tmp_path / "ok.csv").is_file()
    body = post(server + "/setfile", {"path": "rel.csv", "action": "create"})[1]
    assert (tmp_path / "rel.csv").is_file()


def test_os_errors_are_friendly(server, tmp_path):
    f = tmp_path / "my.csv"
    use_file(server, f)
    f.write_bytes(b"\xff\xfe\x00bad")  # not UTF-8
    body = get(server + "/")[1]
    assert "Unexpected error" not in body and "could not be read" in body
    _, body, _ = post(server + "/add", {"type": "buy", "ticker": "X", "quantity": "1", "price": "1"})
    assert "Unexpected error" not in body and "Problem:" in body
    # a file path that is under a regular file (OSError: not a directory) must not crash either
    ui.save_settings(csv_path=str(f / "inside.csv"))
    for page in ("/", "/report"):
        assert "Unexpected error" not in get(server + page)[1]
    _, body, _ = post(server + "/add", {"type": "buy", "ticker": "X", "quantity": "1", "price": "1"})
    assert "Unexpected error" not in body and "Problem:" in body
    _, body, _ = post(server + "/report", {"prices": EXAMPLE_PRICES})
    assert "Unexpected error" not in body


def test_robustness(server, tmp_path):
    srv = ui.make_server(0)
    try:
        assert srv.request_queue_size == 64
    finally:
        srv.server_close()
    host, port = server.removeprefix("http://").split(":")
    for bad in ("abc", "-5"):
        with socket.create_connection((host, int(port)), timeout=10) as c:
            c.sendall(f"POST /add HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Length: {bad}\r\n\r\n".encode())
            assert b" 400 " in c.recv(500).split(b"\r\n")[0]
    with socket.create_connection((host, int(port)), timeout=10) as c:
        c.sendall(f"POST /add HTTP/1.1\r\nHost: {host}:{port}\r\nContent-Length: abc\r\n\r\n".encode())
        assert b" 400 " in c.recv(500).split(b"\r\n")[0]
    assert get(server + "/")[0] == 200  # still serving

    settings = tmp_path / ".portfolio_tracker_ui.json"
    for bad in ('{"csv_path": 5, "dca": [1], "report": {"top": 3, "base": "USD"}}', "not json", "[1,2]",
                '{"csv_path": "", "dca": "x"}', b"\xff\xfe"):
        settings.write_bytes(bad if isinstance(bad, bytes) else bad.encode())
        assert ui.current_path() == ui.default_csv()
        for page in ("/", "/dca", "/report"):
            assert get(server + page)[0] == 200, (bad, page)
    settings.write_text('{"csv_path": 5, "dca": [1], "report": {"top": 3, "base": "USD"}}')
    page = get(server + "/report")[1]
    assert 'name="base" value="USD"' in page and 'name="top" value="5"' in page


def test_usability_texts(server):
    page = get(server + "/report")[1]
    assert "Biggest movers to show" not in page
    for word in ("XIRR", "TWR", "MWR", "Drawdown", "UP / DOWN"):
        assert word in page
    assert "leave empty unless" in page
    assert "Offline: cached prices only" in page and "no internet" in page
    dca = get(server + "/dca")[1]
    assert "only trading weekdays" in dca and "leave empty unless" in dca


def test_future_as_of_note_and_report_equals_cli(server, tmp_path, capsys):
    f = tmp_path / "my.csv"
    use_file(server, f)
    post(server + "/dca", {"ticker": "NQSE.DE", "amount": "40", "start": "2025-01-06", "end": "2026-06-30",
                           "mode": "weekly", "weekday": "mon", "prices": EXAMPLE_PRICES})
    form = {"base": "EUR", "as_of": "2026-06-30", "benchmarks": "VWCE.DE", "top": "3", "prices": EXAMPLE_PRICES}
    _, body, _ = post(server + "/report", form)
    assert "in the future" not in body
    shown = html.unescape(re.search(r"<pre>(.*?)</pre>", body, re.S).group(1))
    assert cli.main(["report", str(f), "--as-of", "2026-06-30", "--benchmark", "VWCE.DE", "--top", "3",
                     "--prices", EXAMPLE_PRICES]) == 0
    assert shown == capsys.readouterr().out.rstrip("\n")
    _, body, _ = post(server + "/report", {**form, "as_of": (pd.Timestamp.today() + pd.Timedelta(days=30)).strftime("%Y-%m-%d")})
    assert "in the future" in body and "PORTFOLIO REPORT" in body


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
