"""The dashboard's boundaries (audit B02, B03, B04): paths, the POST gate, and one launch per symbol.

    cd dashboard && PYTHONPATH=../desk ../desk/.venv/Scripts/python.exe -m pytest -q
"""

import http.client
import json
import threading
from pathlib import Path
import sys
import time
from http.server import ThreadingHTTPServer

import pytest

import server
import symbol_card
import validate


# ---- validate: the one rule every ticker and date passes through ------------------------------

@pytest.mark.parametrize("raw, want", [("ECG", "ECG"), ("brk.b", "BRK.B"), (" nvda ", "NVDA"), ("A", "A")])
def test_ticker_accepts_plain_symbols(raw, want):
    assert validate.ticker(raw) == want


@pytest.mark.parametrize("raw", ["", None, "..\\..\\memos\\X", "../x", "A/B", "TOOLONG", "1ABC",
                                 ".A", "ÉCG", "NVDA\x00"])
def test_ticker_refuses_anything_that_could_be_a_path(raw):
    with pytest.raises(validate.BadInput):
        validate.ticker(raw)


def test_day_accepts_real_dates_only():
    assert validate.day("2026-09-18") == "2026-09-18"
    for raw in ["", "2026-13-40", "../../x", "2026-09-18/../x", "２０２６-09-18", '2026-09-18"\r\nX: 1']:
        with pytest.raises(validate.BadInput):
            validate.day(raw)


def test_inside_refuses_an_escape(tmp_path):
    assert validate.inside(tmp_path, "a.json") == (tmp_path / "a.json").resolve()
    for name in ["../a.json", "..\\a.json", "sub/a.json"]:
        with pytest.raises(validate.BadInput):
            validate.inside(tmp_path, name)


def test_profile_cache_cannot_be_walked_out_of(tmp_path, monkeypatch):
    """The audit's B03, which is a write as well as a read: on a miss get() saves the fetch."""
    cache = tmp_path / "cache" / "profiles"
    victim = tmp_path / "memos" / "X.json"
    victim.parent.mkdir(parents=True)
    victim.write_text('{"keep": true}', encoding="utf-8")
    monkeypatch.setattr(symbol_card, "CACHE", cache)
    monkeypatch.setattr(symbol_card, "fetch", lambda t: pytest.fail("fetched for a bad ticker"))
    for bad in ["..\\..\\memos\\X", "../../memos/X"]:
        with pytest.raises(validate.BadInput):
            symbol_card.get(bad)
        with pytest.raises(validate.BadInput):
            symbol_card.logo(bad, None)
    assert json.loads(victim.read_text(encoding="utf-8")) == {"keep": True}


# ---- the HTTP boundary ----------------------------------------------------------------------------

@pytest.fixture
def port(monkeypatch):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    p = httpd.server_address[1]
    monkeypatch.setattr(server, "PORT_IN_USE", p)
    monkeypatch.setattr(server, "make_pdf", lambda *a: pytest.fail("PDF made for a bad request"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield p
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, **headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, data, {"Content-Type": "application/json", **headers})
    r = conn.getresponse()
    out = r.status, json.loads(r.read() or b"{}")
    conn.close()
    return out


@pytest.mark.parametrize("path", [
    "/api/profile?t=..%5C..%5Cmemos%5CX",
    "/api/logo?t=..%2F..%2Fmemos%2FX",
    "/api/run?t=NVDA&d=..%2F..%2Fx",
    "/api/run?t=..%2Fx&d=2026-09-18",
    "/api/pdf?t=NVDA&d=2026-09-18%22%0D%0AX-Evil%3A%201",      # header injection into the filename
])
def test_bad_query_values_are_refused_before_any_path(port, path):
    status, body = call(port, "GET", path)
    assert status == 400 and body["error"]


def test_a_rebound_hostname_is_refused(port):
    status, _ = call(port, "GET", "/api/runs", Host=f"evil.example:{port}")
    assert status == 403
    status, _ = call(port, "GET", "/api/token", Host=f"evil.example:{port}")
    assert status == 403


def test_the_page_can_read_its_token(port):
    status, body = call(port, "GET", "/api/token")
    assert status == 200 and body["token"] == server.TOKEN


@pytest.fixture
def launched(monkeypatch):
    got = []
    monkeypatch.setattr(server, "start_run", lambda *a: got.append(a) or {"id": "x", "ticker": a[0]})
    # CI installs without the desk's own .venv; any existing interpreter will do for the check.
    monkeypatch.setattr(server, "DESK_PYTHON", Path(sys.executable))
    return got


def test_post_without_the_token_is_refused(port, launched):
    status, _ = call(port, "POST", "/api/run", {"ticker": "NVDA"}, Origin=f"http://127.0.0.1:{port}")
    assert status == 403 and not launched
    status, _ = call(port, "POST", "/api/cancel", {"id": "x"})
    assert status == 403


def test_a_non_ascii_token_is_refused_cleanly(port, launched):
    """compare_digest raises on a non-ASCII str; comparing bytes keeps this a plain 403."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.putrequest("POST", "/api/run")
    conn.putheader("X-Desk-Token", b"caf\xe9")
    conn.putheader("Content-Length", "2")
    conn.endheaders(b"{}")
    assert conn.getresponse().status == 403 and not launched
    conn.close()


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://localhost:1"])
def test_post_from_a_foreign_origin_is_refused_even_with_the_token(port, launched, origin):
    status, _ = call(port, "POST", "/api/run", {"ticker": "NVDA"}, Origin=origin, **{"X-Desk-Token": server.TOKEN})
    assert status == 403 and not launched


def test_post_from_the_page_starts_a_run(port, launched):
    for origin in [f"http://localhost:{port}", f"http://127.0.0.1:{port}"]:
        status, body = call(port, "POST", "/api/run", {"ticker": "nvda"}, Origin=origin,
                            **{"X-Desk-Token": server.TOKEN})
        assert status == 200 and body["ticker"] == "NVDA"
    status, _ = call(port, "POST", "/api/run", {"ticker": "NVDA"}, **{"X-Desk-Token": server.TOKEN})
    assert status == 200                       # a script with no Origin header, but with the token
    assert len(launched) == 3


def test_post_body_must_be_an_object_and_the_ticker_plain(port, launched):
    ok = {"Origin": f"http://127.0.0.1:{port}", "X-Desk-Token": server.TOKEN}
    assert call(port, "POST", "/api/run", ["NVDA"], **ok)[0] == 400
    assert call(port, "POST", "/api/run", {"ticker": "../x"}, **ok)[0] == 400
    assert not launched


# ---- one launch per symbol, even on a double click -----------------------------------------------

class SlowProc:
    """Stands in for the desk process; slow to start, so a race has room to happen."""
    started = 0
    lock = threading.Lock()

    def __init__(self, *a, **kw):
        time.sleep(0.05)
        with SlowProc.lock:
            SlowProc.started += 1
        self.pid = 4242

    def poll(self):
        return None


def test_simultaneous_launches_start_one_process(tmp_path, monkeypatch):
    monkeypatch.delenv("DESK_REPLAY", raising=False)
    monkeypatch.setattr(server, "MEMOS", tmp_path)
    monkeypatch.setattr(server, "RUNS", {})
    monkeypatch.setattr(server.subprocess, "Popen", SlowProc)
    monkeypatch.setattr(server.symbol, "get", lambda *a, **k: {})
    SlowProc.started = 0
    gate, results = threading.Barrier(8), []

    def click():
        gate.wait()
        results.append(server.start_run("NVDA", False))

    threads = [threading.Thread(target=click) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert SlowProc.started == 1
    assert len(server.RUNS) == 1
    assert len({r["id"] for r in results}) == 1
    assert sum(1 for r in results if r.get("already")) == 7


def test_a_run_carries_the_plan_picked_in_the_form(tmp_path, monkeypatch):
    """The AI plan (2026-10-05): the form's Claude or ChatGPT choice reaches the desk as DESK_PLAN,
    an unknown value falls back to the settings, and the run remembers its plan."""
    seen = []

    class Proc:
        def poll(self):
            return None

    monkeypatch.setattr(server, "MEMOS", tmp_path)
    monkeypatch.setattr(server.subprocess, "Popen", lambda cmd, **kw: seen.append(kw["env"]["DESK_PLAN"]) or Proc())
    monkeypatch.setattr(server.symbol, "get", lambda *a, **k: None)
    monkeypatch.setattr(server, "plan_info", lambda: {"default": "claude", "models": {}})
    monkeypatch.delenv("DESK_REPLAY", raising=False)
    server.RUNS.clear()
    rid = server.start_run("MSFT", False, ["quant", "vets"], True, "chatgpt")["id"]
    assert seen == ["chatgpt"] and server.status(rid, 0)["plan"] == "chatgpt"
    server.RUNS.clear()
    server.start_run("NVDA", False, ["quant", "vets"], True, "gpt-9000")
    assert seen[-1] == "claude"
    server.RUNS.clear()


def test_status_recovers_a_finished_run_after_live_tracking_is_lost(tmp_path, monkeypatch):
    """A completed bundle is authoritative even if the dashboard forgot its process object."""
    rid = "RUN-2026-11-10-101833-6d87d3"
    run_dir = tmp_path / "runs" / rid
    run_dir.mkdir(parents=True)
    (run_dir / "memo.json").write_text(json.dumps({
        "ticker": "RUN", "date": "2026-11-10", "run_id": "101833-6d87d3",
        "owns": False, "engines": ["quant", "vets"], "mode": "debate",
        "plan": {"name": "chatgpt"},
    }), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "run_started", "ticker": "RUN", "date": "2026-11-10"}) + "\n" +
        json.dumps({"type": "run_finished", "text": "Memo saved"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "MEMOS", tmp_path)
    monkeypatch.setattr(server, "RUNS", {})

    recovered = server.status(rid, 1)

    assert recovered["running"] is False
    assert recovered["exit"] == 0
    assert recovered["ticker"] == "RUN"
    assert recovered["date"] == "2026-11-10"
    assert recovered["events"] == [{"type": "run_finished", "text": "Memo saved", "seq": 2}]
