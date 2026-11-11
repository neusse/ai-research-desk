"""Run the selected research teams for a symbol, in parallel, each in its own virtualenv.

A team report saved earlier the same day is reused only when it was produced from the same
inputs (desk/provenance.py): same plan, price session, model settings, fund and code. Anything
else, or a report that records no inputs, runs the team again."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from desk import DeskError, maxplan, provenance
from desk.config import (AIHF_DIR, AIHF_PYTHON, AIHF_TIMEOUT_SECONDS, EDGE_DIR, EDGE_PYTHON,
                         EDGE_TIMEOUT_SECONDS, RUNNERS_DIR, TA_DIR, TA_PYTHON,
                         TA_REPORT_FILES, TA_TIMEOUT_SECONDS)
from desk.events import EventLog
from desk.procs import kill_tree, launch, register, unregister
from desk.ratings import normalize

_RATING_RE = re.compile(r"\*\*Rating\*\*:\s*\**\s*([A-Za-z]+)")


ENGINES = ("quant", "vets", "edge")            # the names the CLI and the dashboard use
CODE = {"quant": "A", "vets": "B", "edge": "C"}


_RUN_DIR = re.compile(r"^(?P<day>.+-\d{4}-\d{2}-\d{2})(?:-\d{6}-[0-9a-f]{6})?$")


def current_plan() -> str:
    return maxplan.plan() if maxplan.enabled() else "api"


def run_selected(engines: list[str], ticker: str, as_of: str, mandate: str, run_dir: Path,
                 fresh: bool, say=print, events: EventLog | None = None,
                 plan: str | None = None) -> dict[str, dict]:
    """Run the chosen engines in parallel. Returns {desk code: payload}, in A, B, C order.
    `as_of` is the settled session every team prices from."""
    run_dir.mkdir(parents=True, exist_ok=True)
    events = events or EventLog(None)
    plan = plan or current_plan()
    wanted = {e: provenance.expected(e, plan, as_of, mandate if e == "vets" else None) for e in ENGINES}
    ctx = {"run_dir": run_dir, "fresh": fresh, "say": say, "events": events, "plan": plan}
    jobs = {"quant": lambda: _tradingagents(ticker, as_of, wanted["quant"], ctx),
            "vets": lambda: _ai_hedge_fund(ticker, as_of, mandate, wanted["vets"], ctx),
            "edge": lambda: _edge_desk(ticker, as_of, wanted["edge"], ctx)}
    chosen = [e for e in ENGINES if e in engines]
    with ThreadPoolExecutor(max_workers=len(chosen) or 1) as pool:
        futures = {e: pool.submit(jobs[e]) for e in chosen}
        return {CODE[e]: f.result() for e, f in futures.items()}


def reusable(run_dir: Path, name: str, wanted: dict, label: str, say=print,
             allow_no_rating: bool = False) -> dict | None:
    """The newest report `name` saved today, in any run folder of this symbol and day, whose
    inputs equal `wanted`. It is copied into this run's folder, marked with where it came from."""
    m = _RUN_DIR.match(run_dir.name)
    day = m.group("day") if m else run_dir.name
    folders = []
    for f in run_dir.parent.glob(f"{day}*"):
        fm = _RUN_DIR.match(f.name)
        if f != run_dir and fm and fm.group("day") == day and (f / name).exists():
            folders.append(f)
    refused = None
    for folder in sorted(folders, key=lambda f: (f / name).stat().st_mtime, reverse=True):
        try:
            saved = json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        why = provenance.differs(saved.get("inputs"), wanted)
        if why:
            refused = refused or f"{folder.name}: {why}"
            continue
        try:
            payload = _load(folder / name, allow_no_rating)
        except DeskError:
            continue
        payload = {**payload, "reused": True, "reused_from": folder.name}
        (run_dir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        say(f"  {label}: reusing the report from {folder.name} (same inputs)")
        return payload
    if refused:
        say(f"  {label}: not reusing today's earlier report ({refused}); running it again")
    return None


def _stamp(out: Path, payload: dict, wanted: dict) -> dict:
    """Record what produced a fresh report, in the file and in the payload."""
    saved = json.loads(out.read_text(encoding="utf-8"))
    saved["inputs"] = wanted
    out.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    return {**payload, "inputs": wanted}


def _edge_desk(ticker: str, as_of: str, wanted: dict, ctx: dict) -> dict:
    run_dir, say, events = ctx["run_dir"], ctx["say"], ctx["events"]
    out = run_dir / "edge-desk.json"
    payload = None if ctx["fresh"] else reusable(run_dir, out.name, wanted, "Edge Desk", say, allow_no_rating=True)
    if payload:
        events.emit("engine_reused", engine="edge", rating=payload["rating"],
                    text=f"Reusing the report from {payload['reused_from']} (same inputs)")
        return payload
    if not EDGE_PYTHON.exists():
        raise DeskError(f"Edge Desk was not found at {EDGE_DIR} (set EDGE_DESK_DIR if it moved)")
    say("  Edge Desk: running (about 10 minutes with its written analysis and research layer)...")
    events.emit("engine_started", engine="edge", text="Edge Desk is building its evidence package")
    payload = _run(EDGE_PYTHON, EDGE_DIR, [RUNNERS_DIR / "edge_runner.py", ticker, as_of, out],
                   out, run_dir / "edge-desk.log", EDGE_TIMEOUT_SECONDS, "Edge Desk", say, events,
                   allow_no_rating=True, plan=ctx["plan"])
    payload = _stamp(out, payload, wanted)
    events.emit("engine_done", engine="edge", rating=payload["rating"],
                text=f"Edge Desk: {payload['rating'] or 'rating withheld'}")
    return payload


def _tradingagents(ticker: str, as_of: str, wanted: dict, ctx: dict) -> dict:
    # A run of TradingAgents' own CLI is no longer picked up: it records neither the plan nor the
    # models it ran on, so it could stand in for a run on different inputs (load_cli_reports stays
    # for reading one by hand).
    run_dir, say, events = ctx["run_dir"], ctx["say"], ctx["events"]
    out = run_dir / "tradingagents.json"
    reused = None if ctx["fresh"] else reusable(run_dir, out.name, wanted, "TradingAgents", say)
    if reused:
        events.emit("engine_reused", engine="tape", rating=reused["rating"],
                    text=f"Reusing the report from {reused['reused_from']} (same inputs)")
        return reused
    say("  TradingAgents: running (about 8 minutes)...")
    events.emit("engine_started", engine="tape", text="The quant desk is working the tape")
    payload = _run(TA_PYTHON, TA_DIR, [RUNNERS_DIR / "ta_runner.py", ticker, as_of, out],
                   out, run_dir / "tradingagents.log", TA_TIMEOUT_SECONDS, "TradingAgents", say, events,
                   plan=ctx["plan"])
    payload = _stamp(out, payload, wanted)
    events.emit("engine_done", engine="tape", rating=payload["rating"],
                text=f"Portfolio Manager: {payload['rating']}")
    return payload


def _ai_hedge_fund(ticker: str, as_of: str, mandate: str, wanted: dict, ctx: dict) -> dict:
    run_dir, say, events = ctx["run_dir"], ctx["say"], ctx["events"]
    out = run_dir / "ai-hedge-fund.json"
    payload = None if ctx["fresh"] else reusable(run_dir, out.name, wanted, "ai-hedge-fund", say,
                                                  allow_no_rating=True)
    if payload:
        events.emit("engine_reused", engine="value", rating=payload["rating"],
                    text=f"Reusing the report from {payload['reused_from']} (same inputs)")
        return payload
    say(f"  ai-hedge-fund: running fund '{mandate}' (about 1 minute)...")
    events.emit("engine_started", engine="value", text="The veterans are reading the filings")
    payload = _run(AIHF_PYTHON, AIHF_DIR, [RUNNERS_DIR / "aihf_runner.py", ticker, as_of, mandate, out],
                   out, run_dir / "ai-hedge-fund.log", AIHF_TIMEOUT_SECONDS, "ai-hedge-fund", say, events,
                   allow_no_rating=True, plan=ctx["plan"])
    payload = _stamp(out, payload, wanted)
    events.emit("engine_done", engine="value", rating=payload["rating"],
                text=f"Research Manager: {payload['rating'] or 'rating withheld'}")
    return payload


def load_cli_reports(ticker: str, as_of: str, reports_dir: Path) -> dict:
    reports = {}
    for name in TA_REPORT_FILES:
        path = reports_dir / f"{name}.md"
        reports[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    match = _RATING_RE.search(reports["final_trade_decision"])
    return {"engine": "tradingagents", "ticker": ticker, "date": as_of,
            "rating": match.group(1) if match else None, "reports": reports,
            "usage": None, "reused": True, "source": str(reports_dir)}


def _run(python: Path, cwd: Path, args: list, out: Path, log: Path, timeout: int,
         name: str, say, events: EventLog | None = None, allow_no_rating: bool = False,
         plan: str | None = None) -> dict:
    started = time.time()
    if out.exists():
        out.unlink()                 # a stale file must not pass for this run's output
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **(events.env if events else {})}
    if plan in ("claude", "chatgpt"):
        env["DESK_PLAN"] = plan      # stated, so no team's own settings can pick another plan
    with open(log, "w", encoding="utf-8") as log_file:
        # Its own process tree, so a timeout stops the model calls under the runner too;
        # subprocess.run's timeout kills only the runner and leaves them spending the plan.
        proc = launch([str(python), *map(str, args)], cwd=cwd, env=env,
                      stdout=log_file, stderr=subprocess.STDOUT, kill_on_close=True)
        # Its own group is outside the desk's, so the run folder records it while it is alive:
        # cancelling the run stops it through that record (desk/procs.py).
        register(log.parent, proc)
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            kill_tree(proc)
            raise DeskError(f"{name} timed out after {timeout // 60} minutes (log: {log})") from exc
        finally:
            # A runner that crashed (or even finished) can leave a model call running in its tree.
            # Stop that tree before the record forgets it, or no cancel could reach it later.
            kill_tree(proc, grace=2.0)
            unregister(log.parent, proc)
    if proc.returncode != 0 or not out.exists():
        tail = "\n".join(log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:])
        raise DeskError(f"{name} failed (exit {proc.returncode}). Last log lines:\n{tail}")
    say(f"  {name}: done in {time.time() - started:.0f}s")
    return _load(out, allow_no_rating)


def _load(path: Path, allow_no_rating: bool = False) -> dict:
    """Accept explicit withholds, but still reject a broken Veterans rating."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    rating = normalize(payload.get("rating"))
    if rating is None and not (allow_no_rating and
                              (payload.get("engine") != "ai-hedge-fund" or
                               _veterans_abstained(payload))):
        raise DeskError(f"{payload.get('engine')} produced no usable rating "
                        f"({payload.get('rating')!r}) in {path}")
    payload["rating"] = rating
    return payload


def _veterans_abstained(payload: dict) -> bool:
    """A no-rating Veterans report is valid only when every recorded persona had no view."""
    verdict = payload.get("verdict") or {}
    personas = payload.get("personas") or []
    return (payload.get("rating") is None and verdict.get("rating") is None and
            verdict.get("desk_rating") is None and verdict.get("desk_score") is None and
            bool(personas) and all(p.get("call") in ("abstained", "no view") for p in personas))
