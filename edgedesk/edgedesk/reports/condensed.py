"""The condensed report: one page, markdown.

The middle of the three formats. The scorecard says what to do, the full report
shows all the work, and this one is what gets read on a Monday: the verdict, the
handful of things actually driving it, what would make it wrong, and what to
watch next.

The "why" here is computed, not narrated. Families are ranked by how much they
moved the score (weight times distance from neutral), so the reasons given are
the reasons the number has, rather than the most quotable facts available. The
written thesis arrives with the LLM layer in Phase D and will sit alongside
these, never instead of them.
"""

from __future__ import annotations

from edgedesk.reports import longterm_block, research_block, swing_block

from edgedesk.reports.scorecard import SWING_NOTE
from edgedesk.reports.common import partial_label
from edgedesk.reports.common import (
    HORIZON_ORDER, WINDOWS, etf_methodology, evidence_basis, family_signals, money, num, pct, pct0, price,
    rating_line, signal_value, strongest_signals, top_contributors,
)


def render(run: dict) -> str:
    ev = run.get("evidence") or {}
    profile = ev.get("profile") or {}
    anchors = ev.get("anchors") or {}
    name = profile.get("name") or run["ticker"]

    out = [
        f"# {run['ticker']}: {name}",
        "",
        f"{profile.get('sector') or 'Sector unmapped'} · "
        f"{profile.get('sic_description') or 'industry unknown'}",
        "",
        f"**{price(anchors.get('last_close'))}** at the {anchors.get('as_of') or run['as_of']} close"
        f"{_dividend(ev)}{_mktcap(ev)}",
        "",
    ]
    method = etf_methodology(ev)
    if method:
        out += [f"**Methodology annotation: {method}**", ""]
    out += _verdicts(run)
    if run.get("long_term"):
        out += longterm_block.markdown(run)
    out += _thesis(run)
    out += research_block.markdown(run)
    out += _why(run)
    out += _valuation(run, ev)
    out += _risks(run, ev)
    out += _watch(run, ev)
    out += _footer(run, ev)
    return "\n".join(out)


# ---------------------------------------------------------------------------

def _dividend(ev: dict) -> str:
    div = ev.get("dividend") or {}
    if div.get("yield") is None:
        return ""
    return "  · no dividend" if not div["yield"] else \
        f"  · dividend {pct0(div['yield'])}"


def _mktcap(ev: dict) -> str:
    now = ev.get("valuation_now") or {}
    cap = now.get("market_cap") if now.get("available") else \
        ((ev.get("metrics") or [{}])[0]).get("market_cap")
    return f"  · market cap {money(cap)}" if cap else ""


def _verdicts(run: dict) -> list[str]:
    out = ["## Verdict", ""]
    for key in HORIZON_ORDER:
        v = (run.get("verdicts") or {}).get(key) or {}
        opp = (run.get("opportunity") or {}).get(key) or {}
        lv = (run.get("levels") or {}).get(key) or {}
        if key == "swing":
            out += _swing(run, lv, opp)
            continue
        out.append(f"### {v.get('label', key)}")
        out.append("")
        if v.get("quality_state") == "WITHHELD":
            out.append("**Rating withheld.** " + " ".join(v.get("reasons") or []))
            out.append("")
            continue
        out.append(f"**{rating_line(v)}**")
        out.append("")
        out.append(f"- **Action:** {v.get('action')}")
        out.append(f"- **State:** {opp.get('state')}. {opp.get('why')}")
        cal = (run.get("calibration") or {}).get(key) or {}
        if cal.get("standing"):
            out.append(f"- **How much to trust it:** {cal['standing']}. {cal.get('advice')}")
        if opp.get("show_entry", True) and lv.get("available"):
            out += _levels(key, lv)
        out.append("")
    return out


def _swing(run: dict, lv: dict, opp: dict) -> list[str]:
    if run.get("swing"):
        return swing_block.markdown(run)
    sig = run.get("signal") or {}
    out = ["### Swing (2 to 6 weeks)", ""]
    if not sig.get("signal"):
        return out + [sig.get("why", "The setup could not be described."), ""]
    out += [f"**{sig['signal']}: {opp.get('state')}**", "",
            sig.get("why", ""), "",
            f"- **Action:** {opp.get('action')}"]
    if opp.get("show_entry") and lv.get("available"):
        out += _levels("swing", lv)
    out += ["", f"*{SWING_NOTE} The levels above are still computed and still "
            "useful.*", ""]
    return out


def _levels(key: str, lv: dict) -> list[str]:
    if key == "swing":
        ez = lv["entry_zone"]
        rr = lv.get("reward_to_risk")
        return [
            f"- **Entry:** {price(ez['band_low'])} to {price(ez['band_high'])}",
            f"- **Invalidation:** {price(lv['invalidation']['price'])} "
            f"({lv['invalidation']['rule']})",
            f"- **Targets:** {price(lv['target_1']['price'])} then "
            f"{price(lv['target_2']['price'])}"
            + (f", reward to risk {num(rr)} to 1" if rr else ""),
        ]
    az = lv["accumulation_zone"]
    review = lv.get("next_review") or {}
    stamp = "confirmed" if review.get("when") and not review.get("approximate") else "estimated"
    return [
        f"- **Accumulate at or below:** {price(az['price'])}, a clear bargain under "
        f"{price(az['band_low'])}",
        f"- **Trim:** {price((lv.get('trim_zone') or {}).get('price'))}",
        f"- **Next earnings:** {review.get('when') or 'unknown'} ({stamp})",
    ]


def _thesis(run: dict) -> list[str]:
    """The written view, when there is one.

    Always after the verdict and always labelled as written rather than
    computed, so a reader knows which parts a model produced and which parts a
    formula did.
    """
    llm = run.get("llm") or {}
    synth = llm.get("synthesis")
    if not synth:
        return []
    partial = partial_label(llm.get("missing_cases"))
    out = ["## Thesis", "",
           "Written by the analysis layer from the same evidence. Every figure in it was "
           "checked against the run; no number here came from a model.", ""]
    if partial:
        out += [f"**{partial}**, so this thesis is one-sided.", ""]
    out += [
           synth["thesis"], "",
           "**The strongest argument against it:** " + synth["strongest_counterargument"],
           ""]
    dissent = llm.get("dissent")
    if dissent:
        against = ", ".join(f"{k.replace('_', ' ')} {v}"
                            for k, v in (dissent.get("against_rating") or {}).items() if v)
        out += [f"**Dissent.** The written analysis reads this {dissent.get('direction')} "
                f"than the formula did ({against}). {dissent.get('reason')}", "",
                "*The rating above is unchanged. A dissent is recorded, never applied.*",
                ""]
    if synth.get("unresolved"):
        out += ["**What the evidence cannot settle:** " + synth["unresolved"], ""]
    if synth.get("what_would_change_it"):
        out += ["**What would change it:**", ""]
        out += [f"- {x}" for x in synth["what_would_change_it"]]
        out.append("")
    return out


def _why(run: dict) -> list[str]:
    out = ["## Why", "",
           "Ranked by how much each family moved the score, which is its weight times "
           "its distance from neutral, not by which number looks most impressive.", ""]
    for key in HORIZON_ORDER:
        v = (run.get("verdicts") or {}).get(key) or {}
        if v.get("quality_state") == "WITHHELD":
            continue
        if key == "swing" and run.get("swing"):
            # The swing call explains itself in its own table above. The old factor
            # score for this horizon was withdrawn and must not explain it here.
            continue
        out.append(f"**{v.get('label', key)}**")
        out.append("")
        for row in top_contributors(run, key, 3):
            out.append(f"- **For:** {row['label']} at {num(row['score'], 0)}/100 "
                       f"(weight {pct0(row['weight'], 0)}). {_evidence_for(run, row['family'])}")
        for row in top_contributors(run, key, 2, worst=True):
            out.append(f"- **Against:** {row['label']} at {num(row['score'], 0)}/100 "
                       f"(weight {pct0(row['weight'], 0)}). "
                       f"{_evidence_for(run, row['family'], worst=True)}")
        out.append("")
    return out


def _evidence_for(run: dict, family: str, worst: bool = False) -> str:
    """The one or two actual figures behind a family's score."""
    signals = strongest_signals(run, family, 2, worst=worst)
    if not signals:
        return "No individual signal could be computed."
    return "; ".join(f"{s['label'].lower()} {signal_value(s)}" for s in signals) + "."


def _valuation(run: dict, ev: dict) -> list[str]:
    fv = ((run.get("valuation") or {}).get("fair_value")) or {}
    c = ev.get("consensus") or {}
    b = ev.get("breakdown") or {}
    out = ["## Valuation and the Street", ""]
    if fv:
        out.append(f"**Fair value {price(fv['value'])}**, {pct(fv.get('upside'))} from price, "
                   f"the median of {fv['method_count']} methods.")
        out.append("")
        for m in fv["methods"]:
            out.append(f"- {m['name']}: {price(m['value'])}")
        if fv.get("excluded_own_history"):
            out.append("- Own-history multiple: excluded, because the multiple re-rated and "
                       "the old one describes a market that has already changed its mind.")
        if fv.get("spread") is not None and fv["spread"] > 0.25:
            out.append(f"- The methods disagree by {pct0(fv['spread'], 0)}, so treat the "
                       "single figure as a midpoint of a real argument, not a target.")
        out.append("")
    else:
        out += ["No valuation method had the inputs it needs.", ""]
    if c:
        out.append(f"Street targets run {price(c.get('target_low_price'))} to "
                   f"{price(c.get('target_high_price'))}, mean "
                   f"{price(c.get('target_mean_price'))}.")
        if b and b.get("total"):
            out.append(f"Of {b['total']} analysts, {b.get('bullish')} are bullish, "
                       f"{b.get('neutral')} neutral and {b.get('bearish')} bearish.")
        out.append("")
    return out


def _risks(run: dict, ev: dict) -> list[str]:
    out = ["## Risks", ""]
    risk = (run.get("factors") or {}).get("risk") or {}
    if risk.get("score") is not None:
        out.append(f"Risk score **{num(risk['score'], 0)}/100**, where higher means more "
                   "risk. The measures behind it:")
        out.append("")
        for s in sorted(family_signals(run, "risk"), key=lambda s: -s["score"])[:3]:
            out.append(f"- {s['label']}: {signal_value(s)}")
        out.append("")
    for key in HORIZON_ORDER:
        lv = (run.get("levels") or {}).get(key) or {}
        if key == "long_term" and lv.get("available"):
            conditions = (lv.get("thesis_invalidation") or {}).get("conditions") or []
            if conditions:
                out.append("**What would break the long-term thesis** (business conditions, "
                           "not a chart level):")
                out.append("")
                out += [f"- {c['condition']}" for c in conditions]
                out.append("")
    caveats = [c for c in (ev.get("caveats") or []) if c["severity"] != "info"]
    if caveats:
        out.append("**Data problems affecting this run:**")
        out.append("")
        out += [f"- [{c['severity']}] {c['message']}" for c in caveats]
        out.append("")
    return out


def _watch(run: dict, ev: dict) -> list[str]:
    out = ["## What to watch", ""]
    llm = run.get("llm") or {}
    for case, label in ((llm.get("bull"), "Bull"), (llm.get("bear"), "Bear")):
        if case and case.get("what_would_disprove_it"):
            out.append(f"- **{label} case fails if:** {case['what_would_disprove_it']}")
    lt = (run.get("levels") or {}).get("long_term") or {}
    sw = (run.get("levels") or {}).get("swing") or {}
    review = (lt.get("next_review") or {})
    if review.get("when"):
        stamp = "confirmed" if not review.get("approximate") else "estimated"
        est = review.get("eps_estimate")
        out.append(f"- **Earnings {review['when']}** ({stamp})"
                   + (f", consensus {price(est)} a share." if est else "."))
    call = run.get("swing") or {}
    level = ((call.get("plan") or {}).get("invalidation") or call.get("reference_invalidation")
             if call else (sw.get("invalidation") or {}).get("price") if sw.get("available") else None)
    if level:
        out.append(f"- **{price(level)}** is where the swing structure fails. "
                   "It says nothing about the long-term case.")
    for key in HORIZON_ORDER:
        opp = (run.get("opportunity") or {}).get(key) or {}
        if opp.get("trigger"):
            out.append(f"- **{price(opp['trigger'])}** is the level that would put the "
                       f"{key.replace('_', ' ')} view back in play.")
    rel = ev.get("relative") or {}
    if rel.get("vs_sector") and rel.get("sector_etf"):
        etf = rel["sector_etf"]
        series = " / ".join(f"{w} {pct(rel['vs_sector'].get(w))}" for w in WINDOWS)
        out.append(f"- **Against {etf}:** {series}. Leading or lagging its own sector is "
                   "what separates a stock's story from its sector's.")
    out.append("")
    return out


def _footer(run: dict, ev: dict) -> list[str]:
    return [
        "---",
        "",
        f"Data quality {ev.get('quality_score')}/100 from {evidence_basis(ev)}. "
        f"Run `{run.get('content_hash')}`, factors v{run['versions']['factors']}, "
        f"rating v{run['versions']['rating']}.",
        "",
        "Ratings come from an explicit, versioned formula, and each one's tested standing "
        "is stated with it above. Research only: this places no orders and is not "
        "financial advice.",
    ]
