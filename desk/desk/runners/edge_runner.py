"""Run Edge Desk on one ticker and write its report as JSON.

Executed by the desk with Edge Desk's own interpreter:
    <EdgeDesk>\\.venv\\Scripts\\python.exe edge_runner.py TICKER YYYY-MM-DD OUT.json

Edge Desk is used as it is: the deterministic run (evidence, factors, rating, levels), then its
written analysis (four lenses, independent bull and bear, synthesis), which already runs on the
Max plan through its own headless caller. The run is also saved in ~/.edge-desk/runs as usual.

What the desk needs from it:
    rating          the long-term rating, or None when Edge withheld it (then it does not vote)
    swing           the swing call (Buy or Sell, with its plan). Edge does not VOTE on swing: its call was measured
                    so it never votes on the swing horizon; the setup is evidence for the others
    report_md       the full report, which is what the other managers argue against
    steps           one entry per stage, for the dashboard's agent list
"""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DESK_DIR))

from desk.events import EventLog  # noqa: E402
from desk.gist import gist  # noqa: E402

from edgedesk import reports, run as run_mod  # noqa: E402
from edgedesk.config import ENV_PATH, load as load_env  # noqa: E402
from edgedesk.llm import prompts  # noqa: E402
from edgedesk.llm.analysis import attach  # noqa: E402
from edgedesk.providers.client import DataClient, settled_as_of  # noqa: E402
from edgedesk.reports.common import etf_methodology, foreign_filer_methodology  # noqa: E402

LENS_NAMES = {"quality_compounder": "Quality lens", "deep_value": "Deep value lens",
              "growth_at_reasonable_price": "Growth lens", "trend_and_flow": "Trend lens"}
RESEARCH = [("second_look", "Swing second look"), ("headline_read", "Headline read"),
            ("filing_research", "Filing research")]
WRITTEN = [*[(f"lens:{k}", LENS_NAMES.get(k, k.replace("_", " ").title())) for k in prompts.LENSES],
           ("bull", "Bull case"), ("bear", "Bear case"), ("synthesis", "Synthesis"), *RESEARCH]
STAGES = [("evidence", "Evidence package", "Evidence"), ("rating", "Factors and rating", "Rating"),
          *[(k, n, "Written analysis") for k, n in WRITTEN]]


def _plan() -> str:
    """The plan Edge Desk ran on, as its own call layer decides it."""
    from edgedesk.llm import headless
    return headless.plan() if hasattr(headless, "plan") else "claude"


def _model() -> str | None:
    from edgedesk.llm import headless
    if _plan() == "chatgpt":
        from edgedesk.llm import chatgpt
        return chatgpt.model_for(headless.current_model())
    return headless.current_model() if hasattr(headless, "current_model") else getattr(headless, "MODEL", None)


def section_events(events):
    """Edge reports each written section's real outcome as it finishes (audit R2-17). The desk
    used to mark a section "Written" when the next one started, whatever had happened."""
    names = dict(WRITTEN)

    def on_section(step: str, ok: bool, error: str | None, text: str = "") -> None:
        if step not in names:
            return
        # The opening of what the section wrote, as the other teams show live. Older Edge Desk
        # versions report the outcome only, hence the fallback.
        text = ((gist(text) if text else "Written. The text opens in the finished report.") if ok else
                "Not written: " + (error or "no reason given").strip().rstrip(".")[:200] + ".")
        events.emit("agent_done", engine="edge", who=names[step], step=step, text=text,
                    failed=not ok)

    return on_section


def edge_settings_win(env=os.environ, path: Path = ENV_PATH) -> list[str]:
    """Drop every inherited key that Edge Desk's own .env defines, so its settings win.

    The desk starts this runner with its own environment, already holding ~/.hedge-fund/.env,
    and Edge's loader never overrides a key that is set. So the desk's SEC_USER_AGENT (with no
    contact email) silently replaced Edge's, and SEC refused the filing requests on AVGO, V and
    COST (audit B8). Returns the key names dropped; values are never read here."""
    from dotenv import dotenv_values
    if not Path(path).exists():
        return []
    dropped = [k for k in dotenv_values(path) if k in env]
    for key in dropped:
        env.pop(key, None)
    return dropped


def main() -> None:
    ticker, as_of, out = sys.argv[1].upper(), sys.argv[2], Path(sys.argv[3])
    events = EventLog.from_env()
    started = time.time()
    # The plan the desk chose for this run. Edge's own settings win for everything else, but a
    # DESK_PLAN or EDGE_DESK_PLAN in its .env must not quietly run a ChatGPT run on Claude.
    chosen = os.environ.get("DESK_PLAN")
    edge_settings_win()
    load_env()
    if chosen:
        os.environ["DESK_PLAN"] = os.environ["EDGE_DESK_PLAN"] = chosen
    for step, name, stage in STAGES:
        events.emit("agent_queued", engine="edge", who=name, role=stage)

    client = DataClient()
    try:
        run = run_mod.analyze(ticker, settled_as_of(as_of), client=client, owns=False, save=True)
    finally:
        client.close()
    ev, lt = run.get("evidence") or {}, (run.get("verdicts") or {}).get("long_term") or {}
    # Edge Desk 2.x publishes two calls of its own: a swing Buy or Sell with a plan, and a
    # long-term business case (expected annual return against a hurdle). The factor screen
    # that used to be the vote is kept as supporting evidence.
    call, case = run.get("swing") or {}, run.get("long_term") or {}
    exp = (case.get("scenario_return") or {}).get("expected_annual_return")
    sig = {"signal": call.get("call"), "why": call.get("why"), "score": call.get("score"),
           "plan": call.get("plan"), "setups": [s.get("label") for s in call.get("setups") or []],
           "standing": call.get("standing")}
    quality = ev.get("quality_score")
    evidence_line = (f"{ev.get('bar_count') or 0} daily bars, "
                     f"{len(ev.get('metrics') or [])} filed quarters"
                     + (f", data quality {quality:.0f}/100." if isinstance(quality, (int, float)) else "."))
    rating_line = (f"Long-term business case {case.get('call') or 'none'}"
                   + (f", expected {exp * 100:+.1f}% a year against a 10% hurdle" if exp is not None else "")
                   + f" (factor screen {lt.get('rating') or 'withheld'}"
                   + (f" {lt['score']:.0f}/100" if isinstance(lt.get("score"), (int, float)) else "")
                   + f"). Swing call {sig.get('signal') or 'none'}: " + (sig.get("why") or ""))
    events.emit("agent_done", engine="edge", who="Evidence package", step="evidence", text=evidence_line)
    events.emit("agent_done", engine="edge", who="Factors and rating", step="rating", text=rating_line)

    attach(run, on_section=section_events(events))
    run_mod.write(run)

    llm = run.get("llm") or {}
    if not llm.get("ok") and not llm.get("usage_limited"):
        raise SystemExit("Edge Desk written analysis was unavailable: "
                         + str(llm.get("error") or "no successful synthesis"))
    if llm.get("usage_limited"):
        # Exit without writing the report: a half-written analysis must not be reused later today.
        raise SystemExit(f"The {'ChatGPT' if _plan() == 'chatgpt' else 'Claude'} plan stopped Edge Desk's written analysis "
                         "(usage limit or login): "
                         f"{llm['usage_limited']}")
    sections = llm.get("sections") or {}

    def written(step: str) -> dict:
        if step in dict(RESEARCH):
            part = llm.get(step) or {}
            body = (part.get("reason") or part.get("summary")
                    or (f"{part.get('material')} material headlines: {part.get('positive')} positive, "
                        f"{part.get('negative')} negative." if step == "headline_read" and part.get("ok") else ""))
            if step == "second_look" and part.get("ok"):
                body = f"{str(part.get('stance')).upper()}. {body}"
            return {"saved": bool(part.get("ok") and body), "text": body,
                    "gist": gist(body) if body else "", "error": None if body else part.get("error")}
        sec = sections.get(step) or {}
        data = sec.get("data") or {}
        body = data.get("read") or data.get("case") or data.get("thesis") or ""
        return {"saved": bool(sec.get("ok", bool(data)) and body), "text": body,
                "gist": gist(body) if body else "", "error": None if body else sec.get("error")}

    steps = [{"id": "evidence", "name": "Evidence package", "stage": "Evidence", "gist": evidence_line, "saved": True},
             {"id": "rating", "name": "Factors and rating", "stage": "Rating", "gist": rating_line, "saved": True}]
    steps += [{"id": k, "name": n, "stage": "Written analysis", **written(k)} for k, n in WRITTEN]

    payload = {
        "engine": "edge-desk", "ticker": ticker, "date": as_of, "as_of": run.get("as_of"),
        # The vote is the business case when there is one, else the factor screen.
        "rating": ((case.get("call") or lt.get("rating"))
                   if lt.get("quality_state") != "WITHHELD" else None),
        "rating_basis": "business case" if case.get("call") else "factor screen",
        "expected_annual_return": exp, "business_case": case,
        "screen_rating": lt.get("rating"), "swing_call": call,
        "score": lt.get("score"), "conviction": lt.get("conviction"),
        "quality_state": lt.get("quality_state"), "action": lt.get("action"),
        # The codes the desk's eligibility step reads (desk/eligibility.py).
        "caveats": [{k: c.get(k) for k in ("code", "severity", "message")} for c in ev.get("caveats") or []],
        "methodology": foreign_filer_methodology(ev) or etf_methodology(ev),
        "calibration": run.get("calibration"), "swing": sig,
        "report_md": reports.render(run, "full"), "condensed_md": reports.render(run, "condensed"),
        "steps": steps, "last_close": (ev.get("anchors") or {}).get("last_close"),
        "anchors": ev.get("anchors"), "valuation": run.get("valuation"), "levels": run.get("levels"),
        "name": (ev.get("profile") or {}).get("name"),
        "seconds": round(time.time() - started),
        "usage": {"billing": "chatgpt" if _plan() == "chatgpt" else "max", "usd": 0.0,
                  "usage_limited": llm.get("usage_limited")},
        # The model the call layer actually used, so a report never claims another one.
        "model": _model(),
        "reused": False,
    }
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
