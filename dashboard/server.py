"""The Desk dashboard: a local page that starts desk runs, shows them live, and keeps every
finished run as a snapshot.

    Run Dashboard.bat          or    desk\\.venv\\Scripts\\python.exe dashboard\\server.py
    http://localhost:8790

API (local only, binds 127.0.0.1):
    GET  /api/runs                 finished runs (from memos/) and runs in progress
    GET  /api/run?t=ECG&d=DATE[&r=RUN]   the day's latest run, or one run of that day (runview.build)
    GET  /api/profile?t=ECG        the symbol card (Yahoo, cached a day)
    GET  /api/logo?t=ECG           the company logo, or 404 (the page draws a letter tile)
    POST /api/run                  {ticker, own, engines, debate} -> starts `python -m desk TICKER ...`
    POST /api/cancel               {id} -> kills the run's whole process tree
    GET  /api/pdf?t=&d=&kind=      brief|full -> the run as a PDF (headless Chrome prints ?print=)
    GET  /api/status?id=&from=     new events since `from`, whether the run is alive, the log tail
    GET  /api/token                the token this page sends back on every POST

Every request must carry a local Host header (a rebound hostname is refused), every ticker and
date is validated before it becomes a path (validate.py), and a POST must also carry no foreign
Origin and the X-Desk-Token header. Another site's page can send a request here but cannot read
the token, so it cannot start or cancel a run.

A run is the desk's own CLI in its own process, exactly as from the terminal: same memo, same
files, same Obsidian note. The server only starts it and reads what it writes. Every run keeps
its own folder (memos/runs/TICKER-DATE-RUN). A team report saved earlier the same day is reused
only when it came from the same inputs (plan, price session, models, fund, code), so "Run again"
after a failure only redoes the team that failed, and switching plan reruns every team.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import symbol_card as symbol
import runview
import validate
from desk.procs import kill_tree, launch, owned   # runview put the desk package on the path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DESK_DIR = ROOT / "desk"
DESK_PYTHON = DESK_DIR / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
MEMOS = Path(os.environ.get("DESK_MEMOS_DIR") or ROOT / "memos")
PORT = 8790

RUNS: dict[str, dict] = {}
LOCK = threading.Lock()
TOKEN = secrets.token_urlsafe(32)          # new each start; the page fetches it from /api/token


def replay_run(ticker: str, own: bool) -> dict:
    """Test mode (DESK_REPLAY=MSFT-2026-09-15[:fail-at]): replays that run's recorded events into
    a scratch file instead of starting the desk. No model calls, no broker calls."""
    spec = os.environ["DESK_REPLAY"]
    source, _, fail = spec.partition(":")
    src = MEMOS / "runs" / source / "events.jsonl"
    ticker, as_of = source.rsplit("-", 3)[0], "-".join(source.rsplit("-", 3)[1:])
    run_dir = HERE / "cache" / "replay"
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(HERE / "replay_run.py"), str(src), str(run_dir / "events.jsonl"),
           "--speed", os.environ.get("DESK_REPLAY_SPEED", "12"), *(["--fail-at", fail] if fail else [])]
    log = (run_dir / "desk.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    rid = f"{ticker}-{as_of}-replay-{uuid.uuid4().hex[:8]}"
    with LOCK:
        RUNS[rid] = {"proc": proc, "ticker": ticker, "date": as_of, "own": own, "run_dir": run_dir,
                     "started": time.time()}
    print(f"[desk] replaying {src}")
    return {"id": rid, "ticker": ticker, "date": as_of}


ENGINES = ("quant", "vets", "edge")


PLANS = ("claude", "chatgpt")


def _settings() -> dict[str, str]:
    """The desk's settings as a run would see them: the environment first, then ~/.hedge-desk/.env,
    then ~/.hedge-fund/.env, blank values skipped. Read on every call, so an edit counts at once."""
    from dotenv import dotenv_values
    out = {k: v for k, v in os.environ.items() if v}
    for path in (Path.home() / ".hedge-desk" / ".env", Path.home() / ".hedge-fund" / ".env"):
        for k, v in dotenv_values(path).items():
            if v and k not in out:
                out[k] = v
    return out


def plan_info() -> dict:
    """Which plan a new run uses by default, and the models each plan would call."""
    s = _settings()
    default = "chatgpt" if s.get("DESK_PLAN", "claude").strip().lower() in ("chatgpt", "codex", "openai") else "claude"
    return {"default": default, "models": {
        "claude": {"strong": s.get("DESK_MODEL", "claude-opus-5-5"), "fast": "claude-sonnet-5"},
        "chatgpt": {"strong": s.get("DESK_CHATGPT_STRONG", "gpt-6-astra"), "fast": s.get("DESK_CHATGPT_FAST", "gpt-6-luna")}}}


def start_run(ticker: str, own: bool, engines: list[str] | None = None, debate: bool = True,
              plan: str | None = None) -> dict:
    engines = [e for e in ENGINES if e in (engines or ["quant", "vets"])] or ["quant", "vets"]
    plan = plan if plan in PLANS else plan_info()["default"]
    debate = debate and len(engines) > 1
    if os.environ.get("DESK_REPLAY"):
        return replay_run(ticker, own)
    as_of = time.strftime("%Y-%m-%d")
    # The check and the launch are one step under the lock. Apart, two quick clicks both pass the
    # check, both start the desk, and only one of the two processes is tracked or cancellable.
    with LOCK:
        for rid, r in RUNS.items():                       # one run per symbol at a time
            if r["ticker"] == ticker and r["proc"].poll() is None:
                return {"id": rid, "ticker": ticker, "date": r["date"], "already": True}
        run_id = f"{time.strftime('%H%M%S')}-{secrets.token_hex(3)}"
        run_dir = MEMOS / "runs" / f"{ticker}-{as_of}-{run_id}"     # its own folder: nothing earlier is reset
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.jsonl").write_text("", encoding="utf-8")
        cmd = [str(DESK_PYTHON), "-m", "desk", ticker, "--own", *([ticker] if own else []),
               "--horizon", "all", "--date", as_of, "--engines", ",".join(engines),
               *([] if debate else ["--no-debate"]), "--run-id", run_id]
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "DESK_PLAN": plan}
        log = (run_dir / "desk.log").open("w", encoding="utf-8")
        proc = launch(cmd, cwd=DESK_DIR, env=env, stdout=log, stderr=subprocess.STDOUT)
        rid = f"{ticker}-{as_of}-{run_id}"
        RUNS[rid] = {"proc": proc, "ticker": ticker, "date": as_of, "own": own, "run_dir": run_dir,
                     "started": time.time(), "engines": engines, "debate": debate, "plan": plan}
    threading.Thread(target=symbol.get, args=(ticker,), daemon=True).start()
    print(f"[desk] {' '.join(cmd)}")
    return {"id": rid, "ticker": ticker, "date": as_of}


def cancel_run(rid: str) -> dict:
    """Stop a run and everything it started. A run is a tree of processes (the desk, each team's
    runner in its own venv, and the claude calls under those), so the whole tree is killed;
    ending only the top process would leave the teams working and spending the Max allowance.
    Reports a team already saved stay on disk and are reused by the next run that day."""
    with LOCK:
        run = RUNS.get(rid)
    if not run:
        return {"error": "This run is not known to the server."}
    # The desk's tree and every team runner the run folder records as alive. Each runner heads its
    # own group (so one team can time out alone), which takes it out of the desk's tree on Mac and
    # Linux; the record reaches it even when the desk itself already died.
    kill_tree(run["proc"], groups=owned(run["run_dir"]))
    run["cancelled"] = True
    print(f"[desk] cancelled {run['ticker']} ({rid})")
    return {"ok": True, "ticker": run["ticker"]}


CHROME = [Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
          Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
          Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
          Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
          Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
          *[Path(p) for p in (shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chromium",
                                                         "chromium-browser", "microsoft-edge")) if p]]


def make_pdf(ticker: str, as_of: str, kind: str, run: str | None = None) -> bytes:
    """Print the page's own print view (?print=brief|full) to PDF with headless Chrome or Edge,
    so the PDF is the dashboard's typography and charts, not a second renderer to keep in step."""
    import tempfile
    browser = next((c for c in CHROME if c.exists()), None)
    if not browser:
        raise RuntimeError("Chrome or Edge was not found, so the PDF cannot be made. Use the browser's Print instead.")
    kind = "full" if kind == "full" else "brief"
    with tempfile.TemporaryDirectory(prefix="desk-pdf-") as tmp:
        out = Path(tmp) / "report.pdf"
        url = f"http://127.0.0.1:{PORT_IN_USE}/?t={ticker}&d={as_of}&print={kind}" + (f"&r={run}" if run else "")
        subprocess.run([str(browser), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                        f"--user-data-dir={Path(tmp) / 'profile'}", "--virtual-time-budget=30000",
                        "--run-all-compositor-stages-before-draw", f"--print-to-pdf={out}", url],
                       capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not out.exists():
            raise RuntimeError("The browser did not produce a PDF.")
        return out.read_bytes()


PORT_IN_USE = PORT


def local_hosts() -> set[str]:
    return {f"localhost:{PORT_IN_USE}", f"127.0.0.1:{PORT_IN_USE}"}


def read_events(path: Path, start: int) -> list[dict]:
    """Both engine runners append to the same file with their own counters, so the line number
    is the only sequence a reader can trust."""
    if not path.exists():
        return []
    out, n = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:                      # half-written last line: next poll
            continue
        n += 1
        if n > start:
            out.append({**row, "seq": n})
    return out


def status(rid: str, since: int) -> dict:
    with LOCK:
        run = RUNS.get(rid)
    if not run:
        # RUNS is intentionally in-memory, while completed research is durable.  A browser may
        # still be on ?live= after the server restarts (or after a brief tracking failure).  If
        # the run wrote its final bundle, let the ordinary successful-completion path take over
        # instead of telling the user that the finished decision was lost.
        run_dir = MEMOS / "runs" / rid
        memo = run_dir / "memo.json"
        if memo.exists():
            bundle = json.loads(memo.read_text(encoding="utf-8"))
            events = read_events(run_dir / "events.jsonl", since)
            plan = bundle.get("plan") or {}
            return {"events": events, "running": False, "exit": 0, "elapsed": 0,
                    "ticker": bundle.get("ticker"), "date": bundle.get("date"),
                    "own": bool(bundle.get("owns")), "engines": bundle.get("engines") or ["quant", "vets"],
                    "debate": bundle.get("mode") == "debate", "plan": plan.get("name", "claude"),
                    "cancelled": False, "recovered": True}
        return {"error": "This run is not known to the server. It may have been started before a restart."}
    code = run["proc"].poll()
    out = {"events": read_events(run["run_dir"] / "events.jsonl", since), "running": code is None,
           "exit": code, "elapsed": round(time.time() - run["started"], 1),
           "ticker": run["ticker"], "date": run["date"], "own": run["own"],
           "engines": run.get("engines") or ["quant", "vets"], "debate": run.get("debate", True),
           "plan": run.get("plan", "claude"), "cancelled": bool(run.get("cancelled"))}
    if code not in (None, 0):
        log = run["run_dir"] / "desk.log"
        out["log"] = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]) if log.exists() else ""
    return out


def live_runs() -> list[dict]:
    with LOCK:
        return [{"id": rid, "ticker": r["ticker"], "date": r["date"], "own": r["own"], "plan": r.get("plan", "claude"),
                 "elapsed": round(time.time() - r["started"]), "exit": r["proc"].poll()}
                for rid, r in RUNS.items()
                if not r.get("cancelled") and (r["proc"].poll() is None or r["proc"].poll() != 0)]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(HERE / "web"), **kw)

    def log_message(self, fmt, *args):
        if "/api/" not in (self.path or ""):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _host_ok(self) -> bool:
        """DNS rebinding: a hostile name re-pointed at 127.0.0.1 is same-origin to the browser,
        so it could read this server. Its Host header still carries that name."""
        return self.headers.get("Host") in local_hosts()

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "Open the dashboard at localhost."}, 403)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/api/token":
                return self._json({"token": TOKEN})
            if url.path == "/api/runs":
                return self._json({"runs": runview.index(), "live": live_runs(), "plan": plan_info()})
            if url.path == "/api/run":
                return self._json(runview.build(validate.ticker(q.get("t")), validate.day(q.get("d")),
                                                validate.run_id(q["r"]) if q.get("r") else None))
            if url.path == "/api/profile":
                return self._json(symbol.get(validate.ticker(q.get("t")), refresh=q.get("refresh") == "1"))
            if url.path == "/api/logo":
                t = validate.ticker(q.get("t"))
                png = symbol.logo(t, symbol.get(t).get("website"))
                if not png:
                    return self._json({"error": "no logo"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.end_headers()
                self.wfile.write(png)
                return
            if url.path == "/api/pdf":
                # Validated first: both go into a URL for headless Chrome and into a header.
                t, d = validate.ticker(q.get("t")), validate.day(q.get("d"))
                r = validate.run_id(q["r"]) if q.get("r") else None
                kind = "full" if q.get("kind") == "full" else "brief"
                pdf = make_pdf(t, d, kind, r)
                name = f"{t}-{d}{'-' + r if r else ''}-{'full' if kind == 'full' else 'summary'}.pdf"
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(pdf)))
                self.end_headers()
                self.wfile.write(pdf)
                return
            if url.path == "/api/status":
                return self._json(status(q.get("id", ""), int(q.get("from", "0"))))
        except validate.BadInput as exc:
            return self._json({"error": str(exc)}, 400)
        except FileNotFoundError as exc:
            return self._json({"error": str(exc)}, 404)
        except Exception as exc:                           # show it on the page, keep serving
            return self._json({"error": f"{exc.__class__.__name__}: {exc}"}, 500)
        if url.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/run", "/api/cancel"):
            return self._json({"error": "not found"}, 404)
        # A POST starts or kills processes, so it must come from this page: a local Host, no
        # foreign Origin (a form on another site sends one), and the token only this page can read.
        origin = self.headers.get("Origin")
        if (not self._host_ok() or (origin is not None and origin not in {f"http://{h}" for h in local_hosts()})
                or not secrets.compare_digest((self.headers.get("X-Desk-Token") or "").encode(), TOKEN.encode())):
            return self._json({"error": "This request did not come from the dashboard page. Reload it."}, 403)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad request"}, 400)
        if not isinstance(body, dict):
            return self._json({"error": "bad request"}, 400)
        if path == "/api/cancel":
            return self._json(cancel_run(str(body.get("id") or "")))
        try:
            ticker = validate.ticker(body.get("ticker"))
        except validate.BadInput as exc:
            return self._json({"error": str(exc)}, 400)
        if not DESK_PYTHON.exists():
            return self._json({"error": f"The desk's Python was not found at {DESK_PYTHON}."}, 500)
        return self._json(start_run(ticker, bool(body.get("own")), body.get("engines"),
                                    body.get("debate", True), body.get("plan")))


def main() -> None:
    ap = argparse.ArgumentParser(prog="dashboard", description="The Desk dashboard")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    global PORT_IN_USE
    PORT_IN_USE = args.port
    url = f"http://localhost:{args.port}/"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"The Desk on {url}\nRuns go through {DESK_PYTHON}\nCtrl+C or close this window to stop.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
