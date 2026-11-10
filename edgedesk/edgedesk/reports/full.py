"""The full report: every section, every figure, every source.

This is the one that has to survive an argument. The condensed report says the
valuation score is 82; this one shows the five signals behind that 82, the raw
input to each, the curve position it produced, the weight it carried, and the
evidence id the raw input came from, with the source and the date it was true.

Nothing is summarized away, including the parts that are missing: a signal that
abstained appears as an abstention rather than being dropped, because knowing
what the engine could not compute is part of knowing what it computed.
"""

from __future__ import annotations

from edgedesk.reports import longterm_block, research_block, swing_block

from edgedesk.reports.scorecard import SWING_NOTE
from edgedesk.reports.common import partial_label
from edgedesk.reports.common import (
    FAMILY_ORDER, HORIZON_ORDER, WINDOWS, etf_methodology, evidence_basis, fact_value,
    money, num, pct, pct0, price, rating_line, signal_value,
)


def render(run: dict) -> str:
    ev = run.get("evidence") or {}
    profile = ev.get("profile") or {}
    name = profile.get("name") or run["ticker"]
    out = [
        f"# {run['ticker']} full report: {name}",
        "",
        f"{profile.get('sector') or 'Sector unmapped'} · "
        f"{profile.get('sic_description') or 'industry unknown'} · "
        f"SIC {profile.get('sic') or 'unknown'}",
        "",
        f"As of **{run['as_of']}**, generated {run['generated_at']}.",
        "",
    ]
    method = etf_methodology(ev)
    if method:
        out += ["## Methodology annotation", "", f"**{method}**", ""]
    out += _summary(run)
    out += _verdict_detail(run)
    if run.get("long_term"):
        out += longterm_block.markdown(run, heading="##")
    out += _levels(run)
    out += _valuation(run, ev)
    out += _street(ev)
    out += _relative(ev)
    out += _factor_detail(run)
    out += _fund(ev)
    out += _fundamentals(ev)
    out += _events(ev)
    out += _written(run)
    out += research_block.markdown(run)
    out += _evidence_table(ev)
    out += _quality(ev)
    out += _provenance(run, ev)
    return "\n".join(out)


# ---------------------------------------------------------------------------

def _summary(run: dict) -> list[str]:
    ev = run.get("evidence") or {}
    anchors = ev.get("anchors") or {}
    latest = (ev.get("metrics") or [{}])[0]
    div = ev.get("dividend") or {}
    rows = [
        ("Last settled close", price(anchors.get("last_close"))),
        ("Market cap", money(((ev.get("valuation_now") or {}).get("market_cap"))
                             or latest.get("market_cap"))),
        ("Dividend yield", "no dividend" if div.get("yield") == 0
         else pct0(div.get("yield"))),
        ("P/E (TTM)", f"{num(latest.get('price_to_earnings_ratio'))}x"),
        ("Data quality", f"{ev.get('quality_score')}/100"),
    ]
    out = ["## At a glance", "", "| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    out.append("")
    for key in HORIZON_ORDER:
        v = (run.get("verdicts") or {}).get(key) or {}
        out.append(f"- **{v.get('label', key)}:** {rating_line(v)}")
    out.append("")
    return out


def _verdict_detail(run: dict) -> list[str]:
    out = ["## How each rating was reached", ""]
    for key in HORIZON_ORDER:
        v = (run.get("verdicts") or {}).get(key) or {}
        out.append(f"### {v.get('label', key)}")
        out.append("")
        if key == "swing" and run.get("swing"):
            out = out[:-2] + swing_block.markdown(run, heading="###")
            out += ["The old factor score for this horizon is shown below as working only. "
                    "It was measured and did not separate outcomes; the call above does not "
                    "use it.", ""]
        elif key == "swing":
            sig = run.get("signal") or {}
            opp = (run.get("opportunity") or {}).get("swing") or {}
            out += [f"**{sig.get('signal')}: {opp.get('state')}.** {sig.get('why')}", "",
                    f"Action: {opp.get('action')}", "",
                    SWING_NOTE, "",
                    "The score and its weights are shown below as working, because the "
                    "calibration needs them to measure whether a future version separates "
                    "where this one did not. They are not a rating.", ""]
        if v.get("quality_state") == "WITHHELD":
            out.append("**Rating withheld.**")
            out.append("")
            out += [f"- {r}" for r in v.get("reasons") or []]
            out.append("")
            out.append("The factors below were still computed and are shown for context. "
                       "They were not turned into a rating.")
            out.append("")
            continue
        out.append(f"**{rating_line(v)}** · action: {v.get('action')}")
        out.append("")
        cal = (run.get("calibration") or {}).get(key) or {}
        if cal.get("standing"):
            out.append(f"**Standing of this rating: {cal['standing']}.** {cal.get('summary')}")
            out.append("")
            out.append(f"{cal.get('advice')}")
            out.append("")
        out.append(f"Weighted evidence coverage {pct0(v.get('coverage'), 0)}. "
                   f"Risk score {num(v.get('risk_score'), 0)}/100, reported separately and "
                   "never summed into the rating.")
        out.append("")
        out.append("| Family | Weight | Score | Coverage | Contribution |")
        out.append("|---|---:|---:|---:|---:|")
        for c in v.get("contributions") or []:
            score = "abstained" if c.get("score") is None else num(c["score"], 1)
            contrib = "-" if c.get("contribution") is None else num(c["contribution"], 1)
            out.append(f"| {c['label']} | {pct0(c['weight'], 0)} | {score} | "
                       f"{pct0(c.get('coverage'), 0)} | {contrib} |")
        out.append("")
        if v.get("reasons"):
            out.append("Quality notes: " + " ".join(v["reasons"]))
            out.append("")
    return out


def _levels(run: dict) -> list[str]:
    out = ["## Levels", "",
           "Computed from bar history and the valuation panel. Each carries the rule that "
           "produced it, so the number can be argued with on its method.", ""]
    sw = (run.get("levels") or {}).get("swing") or {}
    lt = (run.get("levels") or {}).get("long_term") or {}

    out.append("### Swing")
    out.append("")
    if sw.get("available"):
        for key in ("entry_zone", "invalidation", "target_1", "target_2"):
            lv = sw[key]
            band = (f" (zone {price(lv['band_low'])} to {price(lv['band_high'])})"
                    if lv.get("band_low") else "")
            out.append(f"- **{lv['name']}: {price(lv['price'])}**{band}  \n  {lv['rule']}")
        out.append(f"- **Reward to risk:** {num(sw.get('reward_to_risk'))} to 1. "
                   f"{sw.get('note')}")
        out.append(f"- **ATR (14 day):** {price(sw.get('atr_14'))}")
    else:
        out.append(f"Not available: {sw.get('why')}")
    out.append("")

    out.append("### Long term")
    out.append("")
    if lt.get("available"):
        az, tz = lt["accumulation_zone"], lt.get("trim_zone") or {}
        out.append(f"- **{az['name']}: {price(az['price'])}** (clear bargain under "
                   f"{price(az['band_low'])})  \n  {az['rule']}")
        out.append(f"- **{tz.get('name')}: {price(tz.get('price'))}**  \n  {tz.get('rule')}")
        inval = lt.get("thesis_invalidation") or {}
        out.append(f"- **Thesis break marker: {price(inval.get('price_marker'))}**  \n  "
                   f"{inval.get('price_rule')}")
        out.append("")
        out.append("**Thesis conditions**, anchored to this company's current numbers rather "
                   "than to universal thresholds:")
        out.append("")
        for c in inval.get("conditions") or []:
            out.append(f"- {c['condition']}")
        review = lt.get("next_review") or {}
        out.append("")
        out.append(f"- **Next review:** {review.get('when') or 'unknown'}. {review.get('rule')}")
        out.append("")
        out.append(f"{lt.get('note')}")
    else:
        out.append(f"Not available: {lt.get('why')}")
    out.append("")
    return out


def _valuation(run: dict, ev: dict) -> list[str]:
    panel = run.get("valuation") or {}
    out = ["## Valuation", ""]
    if not panel:
        out += ["No valuation panel could be built.", ""]
        return out
    fv = panel.get("fair_value") or {}
    if fv:
        out.append(f"**Fair value {price(fv['value'])}**, {pct(fv.get('upside'))} from price. "
                   f"{fv['rule']}")
        out.append("")
        out.append("| Method | Value |")
        out.append("|---|---:|")
        for m in fv["methods"]:
            out.append(f"| {m['name']} | {price(m['value'])} |")
        if fv.get("excluded_own_history"):
            out.append("| Own-history multiple | excluded, the multiple re-rated |")
        out.append("")
    own = panel.get("own_history") or {}
    if own:
        out.append(f"**Own-history multiple:** median P/E {num(own['median_pe'], 1)} over "
                   f"{own['periods']} quarters (interquartile {num(own['pe_q1'], 1)} to "
                   f"{num(own['pe_q3'], 1)}), giving {price(own['fair_low'])} / "
                   f"{price(own['fair_mid'])} / {price(own['fair_high'])}. Price is "
                   f"{pct(own['price_vs_mid'])} against the mid.")
        out.append("")
    peg = panel.get("peg") or {}
    if peg:
        if peg.get("fair_value"):
            out.append(f"**PEG at 1:** EPS compounding {pct0(peg['eps_cagr'])} over "
                       f"{num(peg['years'], 1)} years implies a fair P/E of "
                       f"{num(peg['fair_pe'], 1)}"
                       + (" (capped at 30)" if peg.get("capped") else "")
                       + f", so {price(peg['fair_value'])}, "
                       f"{pct(peg['price_vs_fair'])} against price.")
        else:
            out.append(f"**PEG at 1:** {peg.get('note')}")
        out.append("")
    changes = panel.get("target_changes") or {}
    if changes.get("count"):
        out.append(f"**Analyst target actions** in the last 120 days: {changes['count']} "
                   f"({changes['raised']} raised, {changes['lowered']} lowered).")
        out.append("")
        for c in changes.get("latest") or []:
            rating = f" {c['rating']}," if c.get("rating") else ""
            out.append(f"- {c['date']} {c['firm']}: {c['action']},{rating} target "
                       f"{c['direction']} to {price(c['target'])}")
        out.append("")
    return out


def _street(ev: dict) -> list[str]:
    c = ev.get("consensus") or {}
    b = ev.get("breakdown") or {}
    cal = ev.get("calendar") or {}
    out = ["## The Street", ""]
    if not c:
        out += ["No analyst consensus was available for this run.", ""]
        return out
    out.append(f"Targets: low {price(c.get('target_low_price'))}, mean "
               f"{price(c.get('target_mean_price'))}, high "
               f"{price(c.get('target_high_price'))}. Recommendation mean "
               f"{num(c.get('recommendation_mean'))} of 5, where 1 is best.")
    out.append("")
    if b and b.get("detail"):
        d = b["detail"]
        out.append("| Strong buy | Buy | Hold | Sell | Strong sell | Total |")
        out.append("|---:|---:|---:|---:|---:|---:|")
        out.append(f"| {d.get('strongBuy')} | {d.get('buy')} | {d.get('hold')} | "
                   f"{d.get('sell')} | {d.get('strongSell')} | {b.get('total')} |")
        out.append("")
        history = b.get("history") or []
        if len(history) > 1:
            out.append("Recent months, oldest last:")
            out.append("")
            for row in history:
                out.append(f"- {row['period']}: strong buy {row['strongBuy']}, buy "
                           f"{row['buy']}, hold {row['hold']}, sell {row['sell']}, "
                           f"strong sell {row['strongSell']}")
            out.append("")
    if cal.get("next_earnings"):
        out.append(f"Next earnings {cal['next_earnings']}"
                   + (f", consensus {price(cal.get('eps_estimate'))} a share "
                      f"(range {price(cal.get('eps_estimate_low'))} to "
                      f"{price(cal.get('eps_estimate_high'))})"
                      if cal.get("eps_estimate") else "") + ".")
        out.append("")
    out.append("Consensus is current-only: no free source keeps a history of it, so it is "
               "never applied to a historical as-of date. The weekly snapshots this engine "
               "writes are what will make revisions measurable later.")
    out.append("")
    return out


def _relative(ev: dict) -> list[str]:
    rel = ev.get("relative") or {}
    out = ["## Relative strength", ""]
    if not rel.get("vs_spy"):
        out += ["No benchmark history was available.", ""]
        return out
    etf = rel.get("sector_etf")
    out.append("Excess return, so zero means it kept pace.")
    out.append("")
    out.append("| | " + " | ".join(WINDOWS) + " |")
    out.append("|---|" + "---:|" * len(WINDOWS))
    rows = [("vs SPY", rel.get("vs_spy"))]
    if rel.get("vs_sector"):
        rows.append((f"vs {etf}", rel.get("vs_sector")))
        rows.append((f"{etf} vs SPY", rel.get("sector_vs_spy")))
    for label, series in rows:
        out.append(f"| {label} | " +
                   " | ".join(pct((series or {}).get(w)) for w in WINDOWS) + " |")
    out.append("")
    out.append("A peer-by-peer comparison needs a peer list, which no free source provides. "
               "The sector ETF is the closest honest stand-in, and the mapping from SIC code "
               "to sector is recorded with the profile so a wrong benchmark is visible.")
    out.append("")
    return out


def _factor_detail(run: dict) -> list[str]:
    out = ["## Factors in full", "",
           "Every signal, its raw input, the 0-100 it scored, and the weight it carried "
           "inside its family. Signals that could not be computed are shown as abstentions "
           "rather than dropped.", ""]
    families = run.get("factors") or {}
    for key in FAMILY_ORDER:
        fam = families.get(key)
        if not fam:
            continue
        direction = ("higher means more risk" if fam.get("direction") == "higher_riskier"
                     else "higher is better")
        score = "abstained" if fam.get("score") is None else f"{num(fam['score'], 1)}/100"
        out.append(f"### {fam['label']}: {score}")
        out.append("")
        out.append(f"{direction.capitalize()}. Coverage {pct0(fam.get('coverage'), 0)} of "
                   "the family's weight.")
        out.append("")
        out.append("| Signal | Raw | Score | Weight | Evidence |")
        out.append("|---|---:|---:|---:|---|")
        for s in fam.get("signals") or []:
            sc = "abstained" if s.get("score") is None else num(s["score"], 1)
            out.append(f"| {s['label']} | {signal_value(s)} | {sc} | "
                       f"{pct0(s['weight'], 0)} | `{s.get('fact_id') or '-'}` |")
        out.append("")
        notes = [s for s in fam.get("signals") or [] if s.get("note")]
        for s in notes:
            out.append(f"- *{s['label']}:* {s['note']}")
        if notes:
            out.append("")
    return out


def _fundamentals(ev: dict) -> list[str]:
    metrics = ev.get("metrics") or []
    if not metrics:
        if (ev.get("profile") or {}).get("security_type") == "ETF":
            return ["## Company fundamentals", "",
                    "Not applicable: this security is an ETF. Fund structure and market "
                    "behavior are evaluated separately above.", ""]
        return ["## Fundamentals", "",
                "No US GAAP quarterly facts were available for this filer.", ""]
    out = ["## Fundamentals, point in time", "",
           "Trailing twelve months, keyed to the date each filing became public. A period "
           "only exists here once its 10-Q or 10-K was filed.", "",
           "| Period | Filed | Revenue growth | Earnings growth | Operating margin | "
           "ROE | P/E | Debt/equity |",
           "|---|---|---:|---:|---:|---:|---:|---:|"]
    for m in metrics[:8]:
        out.append(
            f"| {m.get('report_period')} | {m.get('filing_date')} | "
            f"{pct(m.get('revenue_growth'))} | {pct(m.get('earnings_growth'))} | "
            f"{pct0(m.get('operating_margin'))} | {pct0(m.get('return_on_equity'))} | "
            f"{num(m.get('price_to_earnings_ratio'))} | {num(m.get('debt_to_equity'))} |")
    out.append("")
    out.append("Each row's P/E uses the price on the day that filing went public, which is "
               "what makes the own-history multiple a genuine history rather than today's "
               "price divided by old earnings.")
    out.append("")
    return out


def _fund(ev: dict) -> list[str]:
    fund = ev.get("fund") or {}
    if not fund:
        return []
    top = fund.get("top_holdings") or []
    out = ["## ETF structure", "", f"{fund.get('description') or 'No strategy description available.'}", "",
           "| Measure | Value |", "|---|---:|",
           f"| Category | {fund.get('category') or 'n/a'} |",
           f"| Sponsor family | {fund.get('family') or 'n/a'} |",
           f"| Expense ratio | {pct0(fund.get('expense_ratio'))} |",
           f"| Category-average expense ratio | {pct0(fund.get('category_expense_ratio'))} |",
           f"| Net assets | {money(fund.get('total_assets'))} |",
           f"| NAV | {price(fund.get('nav_price'))} |",
           f"| Market premium/discount | {pct(fund.get('premium_discount'))} |",
           f"| Equity allocation | {pct0(fund.get('stock_position'))} |", ""]
    if top:
        out += ["**Largest reported holdings**", "",
                "| Ticker | Holding | Weight |", "|---|---|---:|"]
        out += [f"| {h.get('symbol')} | {h.get('name')} | {pct0(h.get('weight'))} |" for h in top]
        out.append("")
    return out


def _events(ev: dict) -> list[str]:
    out = ["## Events", ""]
    rel = ev.get("release") or {}
    if rel:
        out.append(f"**Last earnings release {rel['date']}** ({rel.get('days_since')} days "
                   f"ago): EPS {num(rel.get('eps'))} against {num(rel.get('eps_estimate'))} "
                   f"expected, a surprise of {pct(rel.get('eps_surprise'))}.")
        if rel.get("revenue_surprise") is not None:
            out.append("")
            out.append(f"Revenue {money(rel.get('revenue'))} against "
                       f"{money(rel.get('revenue_estimate'))}, "
                       f"{pct(rel.get('revenue_surprise'))}.")
        out.append("")
    else:
        out += ["No earnings release headline was found in the lookback window.", ""]
    news = ev.get("news") or []
    if news:
        out.append(f"**Recent headlines** ({len(news)} in the window, newest first):")
        out.append("")
        for n in news[:12]:
            out.append(f"- {(n.get('date') or '')[:10]} {n.get('title')}")
        out.append("")
    return out


def _written(run: dict) -> list[str]:
    """The analysis layer in full: both cases as they were made independently,
    every lens, and the synthesis that read them."""
    llm = run.get("llm") or {}
    if not llm:
        return []
    out = ["## Written analysis", ""]
    if not llm.get("ok"):
        out += [f"Not available: {llm.get('error', 'the layer did not complete')}.", ""]
        failed = [n for n, s in (llm.get("sections") or {}).items() if not s.get("ok")]
        if failed:
            out += ["Sections that failed validation: " + ", ".join(failed) + ".", ""]
        return out

    out += ["Produced from the same evidence package, with no tools and no access to "
            "anything beyond it. Every figure was checked against the run before "
            "publication, and a reply quoting a figure the run does not hold was "
            "rejected and asked again.", ""]

    synth = llm.get("synthesis") or {}
    if synth:
        partial = partial_label(llm.get("missing_cases"))
        out += ["### Synthesis", ""] + ([f"**{partial}**, so this synthesis is one-sided.", ""]
                                        if partial else []) + [synth["thesis"], "",
                "**Strongest counterargument:** " + synth["strongest_counterargument"], ""]
        removed = ((llm.get("sections") or {}).get("synthesis") or {}).get("uncited") or []
        if removed:
            out += [f"_Sentences quoting figures the evidence does not hold ({', '.join(removed)}) were "
                    "removed after a repair failed to source them._", ""]
        if synth.get("unresolved"):
            out += ["**Unresolved:** " + synth["unresolved"], ""]
        if synth.get("what_would_change_it"):
            out += ["**What would change it:**", ""]
            out += [f"- {x}" for x in synth["what_would_change_it"]]
            out.append("")

    dissent = llm.get("dissent")
    if dissent:
        out += ["### Dissent", "",
                f"The written analysis reads this {dissent.get('direction')} than the "
                f"formula did, on the "
                f"{str(dissent.get('horizon', 'both')).replace('_', ' ')} horizon.", "",
                f"{dissent.get('reason')}", "",
                "This is recorded beside the rating and does not change it. The rating "
                "came from a versioned formula; a model may argue with it in public and "
                "may not edit it.", ""]

    for key, label in (("bull", "The case for"), ("bear", "The case against")):
        case = llm.get(key)
        if not case:
            continue
        out += [f"### {label}", "", case["case"], ""]
        out += [f"- {point}" for point in case.get("points") or []]
        out += ["", f"**Disproved by:** {case.get('what_would_disprove_it')}", ""]
    if llm.get("bull") or llm.get("bear"):
        out += ["*The two cases were made independently. Neither saw the other.*", ""]

    lenses = llm.get("lenses") or {}
    if lenses:
        out += ["### Through four lenses", "",
                "The same evidence, read by investors who care about different things. "
                "These are interpretations, not votes: none of them feeds the rating.", ""]
        for key, view in lenses.items():
            out += [f"**{key.replace('_', ' ').title()}** (would own: {view['would_own']})",
                    "", view["read"], "",
                    f"- Strongest point: {view['strongest_point']}",
                    f"- Biggest worry: {view['biggest_worry']}", ""]
    return out


def _evidence_table(ev: dict) -> list[str]:
    facts = ev.get("facts") or {}
    out = ["## Evidence", "",
           "Every figure this run is built from, with the id a report or a model must cite "
           "it by, its source, and the date it is true as of.", "",
           "| Id | Figure | Value | Source | As of |",
           "|---|---|---:|---|---|"]
    for fid, f in sorted(facts.items()):
        out.append(f"| `{fid}` | {f['label']} | {fact_value(f)} | {f['source']} | "
                   f"{f['as_of']} |")
    out.append("")
    notes = [(fid, f) for fid, f in sorted(facts.items()) if f.get("note")]
    if notes:
        out.append("Notes:")
        out.append("")
        out += [f"- `{fid}`: {f['note']}" for fid, f in notes]
        out.append("")
    return out


def _quality(ev: dict) -> list[str]:
    caveats = ev.get("caveats") or []
    out = [f"## Data quality: {ev.get('quality_score')}/100", "",
           f"{evidence_basis(ev)} were available.", ""]
    if not caveats:
        out += ["No caveats were raised.", ""]
        return out
    out.append("| Severity | Code | Detail |")
    out.append("|---|---|---|")
    for c in caveats:
        out.append(f"| {c['severity']} | `{c['code']}` | {c['message']} |")
    out.append("")
    out.append("A `withhold` caveat means no rating may be published for this run. A "
               "`degrade` caveat means the rating stands but rests on less than it should.")
    out.append("")
    return out


def _provenance(run: dict, ev: dict) -> list[str]:
    profile = ev.get("profile") or {}
    return [
        "## Provenance",
        "",
        f"- Run hash `{run.get('content_hash')}`, which covers everything except wall clocks. "
        "The same ticker at the same as-of date must reproduce it exactly.",
        f"- Factors v{run['versions']['factors']}, rating v{run['versions']['rating']}, "
        f"run schema v{run['versions']['schema']}.",
        f"- Sector benchmark: {profile.get('sector_etf') or 'none'}, mapped from SIC "
        f"{profile.get('sic') or 'unknown'}.",
        f"- Historical run: {'yes' if ev.get('is_historical') else 'no'}. Historical runs "
        "exclude current analyst consensus by construction.",
        "- Sources: Alpaca REST for prices, corporate actions and headlines; SEC EDGAR "
        "companyfacts for fundamentals, keyed by filing date; Yahoo for analyst consensus "
        "and the earnings calendar; Benzinga headlines for earnings surprises.",
        "",
        "No language model contributed to any number in this report.",
        "",
        "Ratings come from an explicit, versioned formula, and each one's tested standing "
        "is stated in the section that reaches it. Research only: this places no orders "
        "and is not financial advice.",
    ]
