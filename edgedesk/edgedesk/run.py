"""One analysis run, start to finish, and the JSON file that is its record.

The run file is the source of truth. Reports render from it, the next week's
run compares against it, and the LLM layer (Phase D) annotates it without ever
changing a number in it. There is no database: a run is a file, and the file is
complete enough to rebuild every view from.

Reproducibility is enforced, not hoped for. Everything the rating depends on is
hashed, and the hash excludes only the wall-clock stamps, so the same ticker at
the same as-of date twice must produce the same `content_hash`. If it does not,
something non-deterministic got into the path and the test suite says so.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timezone

from edgedesk.evidence import business, estimates
from edgedesk.evidence.package import Evidence, collect
from edgedesk.evidence.valuation import build_valuation, fair_value, rows_from_metrics
from edgedesk.factors.families import compute as compute_families
from edgedesk.paths import ensure, run_path
from edgedesk.providers.client import DataClient
from edgedesk.verdict import calibration, levels as levels_mod, signal as signal_mod
from edgedesk.verdict import swing as swing_mod
from edgedesk.verdict.rating import HORIZONS, WITHHELD, rate

logger = logging.getLogger(__name__)

RUN_SCHEMA_VERSION = 1

# How far back to look for price-target headlines for the valuation panel.
_TARGET_HEADLINE_DAYS = 120


def _swing_opportunity(signal: dict, levels: dict | None, anchors: dict,
                       owns: bool) -> dict:
    """Where a green setup sits relative to its entry zone.

    A red setup gets no entry price at all: printing one under a broken setup is
    an invitation dressed as information.
    """
    state = signal.get("signal")
    if state is None:
        return {"state": "Unavailable", "show_entry": False, "zone_position": None,
                "why": signal.get("why")}
    if state == signal_mod.RED:
        return {"state": "Stand aside", "show_entry": False, "zone_position": None,
                "why": signal.get("why"),
                "action": signal_mod.action_for(state, owns)}

    close = (anchors or {}).get("last_close")
    zone = ((levels or {}).get("entry_zone") or {})
    low, high = zone.get("band_low"), zone.get("band_high")
    action = signal_mod.action_for(state, owns)
    if close is None or low is None or high is None:
        return {"state": "Setup intact", "show_entry": True, "zone_position": None,
                "why": signal.get("why"), "action": action}
    where = levels_mod.zone_position(close, low, high)
    if low <= close <= high:
        return {"state": "In the entry zone", "show_entry": True, "zone_position": where,
                "why": f"{signal.get('why')} Price {close:,.2f} is inside the entry zone "
                       f"{low:,.2f} to {high:,.2f}.",
                "action": action}
    return {"state": "Above the entry zone", "show_entry": True, "zone_position": 1.0,
            "why": f"{signal.get('why')} Price {close:,.2f} is above the entry zone "
                   f"{low:,.2f} to {high:,.2f}, so the reward to risk on offer is worse "
                   "than the levels below suggest.",
            "action": action}


def analyze(ticker: str, as_of: date, client: DataClient | None = None,
            owns: bool = False, save: bool = True) -> dict:
    """Run the deterministic engine for one ticker. No LLM anywhere in here."""
    own_client = client is None
    client = client or DataClient()
    try:
        ev = collect(ticker, as_of, client)
        panel = _valuation_panel(ev)
        families = compute_families(ev, panel)
        verdicts = {
            key: rate(ev, families, key, owns=owns).to_dict() for key in HORIZONS
        }
        lv = levels_mod.compute(ev.anchors, panel, ev.latest_metrics, ev.release,
                                rerated=bool(panel and panel.get("rerated")),
                                calendar=ev.calendar)
        snapshot = estimates.snapshot(ev.ticker, as_of, ev.consensus, ev.latest_metrics,
                                      ev.breakdown, ev.calendar, ev.forward)
        snapshot_path = estimates.write(snapshot) if save else None

        ev_dict = ev.to_dict()
        swing_call = swing_mod.recommend(
            ev_dict, {k: f.to_dict() for k, f in families.items()}, panel, ev.bars,
            (ev.benchmarks or {}).get("SPY") or [], owns=owns)
        run = {
            "schema_version": RUN_SCHEMA_VERSION,
            "ticker": ev.ticker,
            "as_of": ev.as_of,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "owns": owns,
            "versions": {
                "factors": verdicts["long_term"]["factors_version"],
                "rating": verdicts["long_term"]["rating_version"],
                "swing": swing_mod.SWING_VERSION,
                "schema": RUN_SCHEMA_VERSION,
            },
            "evidence": ev_dict,
            "swing": swing_call,
            "long_term": (
                {"available": False, "call": None,
                 "why": ("The company owner-earnings scenario does not apply to an ETF. The "
                         "long-term rating above uses the separate equity-ETF methodology.")}
                if (ev.profile or {}).get("security_type") == "ETF" else
                {"available": False, "call": None,
                 "why": "No long-term case: the data for this name was withheld (see data quality)."}
                if verdicts["long_term"].get("quality_state") == WITHHELD else
                business.analyze(ev.metrics, ev.valuation_now, (ev.profile or {}).get("sic"),
                                 (ev.dividend or {}).get("yield"), ev.forward)),
            "valuation": panel,
            "factors": {k: f.to_dict() for k, f in families.items()},
            "verdicts": verdicts,
            "levels": lv,
            "signal": signal_mod.swing_signal(ev.anchors, lv.get("swing")),
            "opportunity": {
                "swing": _swing_opportunity(
                    signal_mod.swing_signal(ev.anchors, lv.get("swing")),
                    lv.get("swing"), ev.anchors, owns),
                "long_term": opportunity_state(verdicts["long_term"],
                                               lv.get("long_term"), ev.anchors,
                                               "long_term"),
            },
            "calibration": {k: calibration.standing(k, verdicts[k]["methodology"])
                            for k in HORIZONS},
            "estimate_snapshot": snapshot_path,
            "llm": None,
        }
        run["content_hash"] = content_hash(run)
        if save:
            write(run)
        return run
    finally:
        if own_client:
            client.close()


def _valuation_panel(ev: Evidence) -> dict | None:
    """The fair-value panel, when there is a price and fundamentals to build it from."""
    # The decision price, not the adjusted series: in a historical run the two
    # differ by every split since, and the panel divides this price by EPS.
    close = (ev.valuation_now or {}).get("price") or (ev.anchors or {}).get("last_close")
    if not close or not ev.metrics:
        return None
    cutoff = ev.as_of
    headlines = [(n.get("date") or "", n.get("title") or "") for n in ev.news
                 if (n.get("date") or "")[:10] <= cutoff]
    panel = build_valuation(close, rows_from_metrics(ev.metrics), ev.consensus,
                            headlines[: 200])
    panel["rerated"] = _flag_rerating(ev, panel)
    panel["fair_value"] = fair_value(panel, panel["rerated"])
    return panel


# A gap this wide between today's multiple and the stock's own median is not a
# discount, it is a re-rating: the market has changed its mind about what the
# business is worth, and the old multiple is not coming back just because it
# used to be normal.
_RERATING_GAP = 0.40


def _flag_rerating(ev: Evidence, panel: dict) -> bool:
    """Own-history fair value is the best free anchor available, and it has one
    well-known failure: a stock that de-rated from a bubble multiple looks cheap
    against its own past forever, and a stock that re-rated upward looks dear
    forever. When the gap is extreme, say so in the run rather than letting the
    valuation score quietly carry the assumption."""
    gap = ((panel.get("own_history") or {}).get("price_vs_mid"))
    if gap is None or abs(gap) < _RERATING_GAP:
        return False
    direction = "below" if gap < 0 else "above"
    ev.warn("multiple_rerated", "info",
            f"Price sits {abs(gap):.0%} {direction} the fair value implied by this stock's "
            f"own median P/E over {(panel['own_history'] or {}).get('periods')} quarters. A gap "
            "this wide usually means the multiple re-rated rather than that the stock is "
            "mispriced, so the own-history range is context here, not a target.")
    return True


def opportunity_state(verdict: dict, horizon_levels: dict | None, anchors: dict,
                      horizon: str = "swing") -> dict:
    """Where this sits between "nothing to do" and "act now".

    The rating says what it is worth; this says whether today is the day. They
    are different questions, and collapsing them is how a good business at a bad
    price gets bought.

    The two horizons read a zone differently, and they must. Under a swing entry
    zone means the setup broke. Under a long-term accumulation ceiling means the
    stock got cheaper than the price you were willing to pay, which is the
    opposite of a problem.
    """
    if verdict.get("quality_state") == WITHHELD or not verdict.get("rating"):
        return {"state": "Withheld", "why": "No rating was published for this horizon."}

    rating = verdict["rating"]
    close = (anchors or {}).get("last_close")

    if rating in ("Underweight", "Sell"):
        return _avoid(rating, anchors)
    if rating == "Hold":
        return {"state": "Watch", "show_entry": False, "zone_position": None,
                "why": "Rated Hold: no action, but worth tracking."}
    if horizon == "long_term":
        return _long_term_state(rating, close, horizon_levels)
    return _swing_state(rating, close, horizon_levels)   # kept for the record only


def _avoid(rating: str, anchors: dict) -> dict:
    """What would have to happen for this to be worth another look.

    The nearest moving average overhead, not a price target: reclaiming the
    20-day is a fact that could occur next week, while a target 18% away is not
    a trigger, it is a different stock.
    """
    close = (anchors or {}).get("last_close")
    overhead = sorted(v for v in ((anchors or {}).get("sma_20"), (anchors or {}).get("sma_50"),
                                  (anchors or {}).get("sma_200"))
                      if v and close and v > close)
    trigger = overhead[0] if overhead else None
    why = f"Rated {rating}: nothing to buy here."
    if trigger:
        which = {(anchors or {}).get("sma_20"): "20-day",
                 (anchors or {}).get("sma_50"): "50-day",
                 (anchors or {}).get("sma_200"): "200-day"}.get(trigger, "next")
        why += (f" A close back above the {which} average at {trigger:,.2f} would be the "
                "first sign that changed.")
    else:
        why += " Price is already above every moving average, so there is no reclaim level."
    return {"state": "Avoid", "show_entry": False, "zone_position": None,
            "trigger": trigger, "why": why}


def _swing_state(rating: str, close: float | None, horizon_levels: dict | None) -> dict:
    zone = (horizon_levels or {}).get("entry_zone") or {}
    low, high = zone.get("band_low"), zone.get("band_high")
    where = levels_mod.zone_position(close, low, high)
    if close is None or low is None or high is None:
        return {"state": "Actionable", "show_entry": True, "zone_position": None,
                "why": f"Rated {rating}; no entry zone could be placed."}
    if low <= close <= high:
        return {"state": "Actionable now", "show_entry": True, "zone_position": where,
                "why": f"Rated {rating} and price {close:,.2f} is in the entry zone "
                       f"({low:,.2f} to {high:,.2f})."}
    if close < low:
        return {"state": "Below the zone", "show_entry": True, "zone_position": 0.0,
                "why": (f"Rated {rating} but price {close:,.2f} is under the entry zone "
                        f"({low:,.2f} to {high:,.2f}), which usually means the setup broke: "
                        "check the invalidation before treating it as a discount.")}
    return {"state": "Wait for entry", "show_entry": True, "zone_position": 1.0,
            "why": f"Rated {rating} but price {close:,.2f} is above the entry zone "
                   f"{low:,.2f} to {high:,.2f}."}


def _long_term_state(rating: str, close: float | None, horizon_levels: dict | None) -> dict:
    """A ceiling, not a band: accumulate at or below it, and cheaper is better."""
    zone = (horizon_levels or {}).get("accumulation_zone") or {}
    ceiling, floor = zone.get("band_high"), zone.get("band_low")
    if close is None or ceiling is None:
        return {"state": "Actionable", "show_entry": True, "zone_position": None,
                "why": f"Rated {rating}; no accumulation level could be placed."}
    if close > ceiling:
        premium = close / ceiling - 1
        return {"state": "Wait for entry", "show_entry": True, "zone_position": 1.0,
                "why": (f"Rated {rating} but price {close:,.2f} is {premium:.0%} above the "
                        f"accumulation ceiling of {ceiling:,.2f}.")}
    depth = (1 - close / ceiling) if ceiling else 0.0
    note = ("at the ceiling, so it qualifies but there is no margin in the price"
            if depth < 0.02 else
            f"{depth:.0%} below the ceiling of {ceiling:,.2f}")
    if floor is not None and close < floor:
        note += f", and under the {floor:,.2f} support that anchors the zone"
    return {"state": "Actionable now", "show_entry": True,
            "zone_position": round(1 - depth, 4),
            "why": f"Rated {rating} and price {close:,.2f} is {note}."}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

_VOLATILE = {"generated_at", "content_hash", "estimate_snapshot"}


def content_hash(run: dict) -> str:
    """A hash over everything that should be reproducible.

    Wall-clock stamps are excluded; so is the evidence package's own
    `generated_at` and the consensus fetch date, because those move with the
    clock rather than with the data.
    """
    payload = {k: v for k, v in run.items() if k not in _VOLATILE}
    evidence = dict(payload.get("evidence") or {})
    evidence.pop("generated_at", None)
    payload["evidence"] = evidence
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def write(run: dict) -> str:
    path = ensure(run_path(run["ticker"], run["as_of"]))
    path.write_text(json.dumps(run, indent=2, sort_keys=True, default=str), encoding="utf-8")
    _update_index(run)
    return str(path)


def read(ticker: str, as_of: str) -> dict | None:
    path = run_path(ticker, as_of)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("unreadable run %s: %s", path, exc)
        return None


def previous(ticker: str, before: str) -> dict | None:
    """The most recent earlier run for this ticker, or None on run one.

    Run one is a normal state, not a missing feature: the caller says "first
    run, snapshot saved" rather than showing an empty comparison.
    """
    from edgedesk.paths import RUNS_DIR

    if not RUNS_DIR.exists():
        return None
    for day in sorted((d.name for d in RUNS_DIR.iterdir() if d.is_dir()), reverse=True):
        if day >= before:
            continue
        found = read(ticker, day)
        if found:
            return found
    return None


def _update_index(run: dict) -> None:
    """A small index so listing runs does not mean opening every file.

    Rebuildable from the run files at any time, which is the whole reason it is
    allowed to exist: it is a convenience, never a source of truth.
    """
    from edgedesk.paths import RUNS_DIR

    path = ensure(RUNS_DIR / "index.json")
    try:
        index = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"runs": []}
    except (OSError, json.JSONDecodeError):
        index = {"runs": []}

    entry = {
        "ticker": run["ticker"],
        "as_of": run["as_of"],
        "swing": (run.get("swing") or {}).get("call"),
        "long_term": (run["verdicts"]["long_term"] or {}).get("rating"),
        "long_term_case": (run.get("long_term") or {}).get("call"),
        "quality_state": (run["verdicts"]["long_term"] or {}).get("quality_state"),
        "content_hash": run.get("content_hash"),
    }
    index["runs"] = [r for r in index.get("runs", [])
                     if not (r.get("ticker") == entry["ticker"]
                             and r.get("as_of") == entry["as_of"])]
    index["runs"].append(entry)
    index["runs"].sort(key=lambda r: (r.get("as_of", ""), r.get("ticker", "")), reverse=True)
    try:
        path.write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        logger.warning("could not update run index: %s", exc)
