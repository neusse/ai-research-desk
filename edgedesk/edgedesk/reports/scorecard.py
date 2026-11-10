"""The scorecard: one screen, everything needed to act or to decide not to.

Renders from a run file and nothing else, so it cannot disagree with the
condensed or full report. Plain text, because the first consumer is a terminal
and the second is a markdown file, and neither should need a colour to be read
correctly.

A withheld rating is rendered loudly rather than quietly: the failure mode this
engine exists to avoid is a confident-looking number resting on data that was
not there.
"""

from __future__ import annotations

from edgedesk.reports import longterm_block, swing_block
from edgedesk.reports.common import foreign_filer_methodology

ORDER = ("swing", "long_term")

# The same sentence in every format, because a caveat that is worded differently
# in three places reads as three different caveats.
SWING_NOTE = ("No score is published for this horizon: it was measured against forward returns across 6,168 observations and did not separate them, so what is shown describes the setup rather than forecasting it.")

# Compact enough to sit two families to a line: the score is the information,
# the gauge only makes the shape scannable.
_GAUGE = 10


def _pct(v, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:+.{digits}f}%"


def _pct0(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _num(v, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:,.{digits}f}"


def _price(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _rule(width: int = 78, char: str = "-") -> str:
    return char * width


def render(run: dict) -> str:
    ev = run.get("evidence") or {}
    profile = ev.get("profile") or {}
    anchors = ev.get("anchors") or {}
    out: list[str] = []

    name = profile.get("name") or run["ticker"]
    out.append(_rule(78, "="))
    out.append(f"{run['ticker']}  {name}")
    out.append(f"{profile.get('sector') or 'sector unmapped'}"
               f"  |  {profile.get('sic_description') or 'industry unknown'}")
    out.append(f"as of {run['as_of']}  |  {anchors.get('as_of') or 'last'} close "
               f"{_price(anchors.get('last_close'))}"
               f"{_dividend_bit(ev)}")
    out.append(_rule(78, "="))
    method = foreign_filer_methodology(ev)
    if method:
        out.append(f"METHOD: {method}")
        out.append(_rule())

    for key in ORDER:
        out.extend(_horizon_block(run, key))
        if key == "long_term" and run.get("long_term"):
            out.append(_rule())
            out.extend(longterm_block.plain(run))
        out.append("")

    out.extend(_valuation_block(run))
    out.append("")
    out.extend(_street_block(run))
    out.append("")
    out.extend(_relative_block(run))
    out.append("")
    out.extend(_scores_block(run))
    out.append("")
    out.extend(_quality_block(run))
    out.append(_rule())
    out.append(f"run {run.get('content_hash')}  |  factors v{run['versions']['factors']}  "
               f"|  rating v{run['versions']['rating']}")
    out.append("Each rating's standing is printed with it above. Research only: no "
               "advice, no order is placed by this tool.")
    return "\n".join(out)


def _dividend_bit(ev: dict) -> str:
    div = ev.get("dividend") or {}
    if div.get("yield") is None:
        return ""
    if not div["yield"]:
        return "  |  no dividend"
    return f"  |  dividend {_pct0(div['yield'])}"


def _horizon_block(run: dict, key: str) -> list[str]:
    v = (run.get("verdicts") or {}).get(key) or {}
    lv = (run.get("levels") or {}).get(key) or {}
    opp = (run.get("opportunity") or {}).get(key) or {}
    if key == "swing":
        return _swing_block(run, lv, opp)
    out = [_rule(), v.get("label", key).upper()]

    if v.get("quality_state") == "WITHHELD":
        out.append("  RATING WITHHELD")
        for reason in v.get("reasons") or []:
            out.append(f"    - {reason}")
        return out

    flag = "" if v.get("quality_state") == "VALID" else "  [DEGRADED]"
    out.append(f"  {v.get('rating', 'n/a').upper()}   score {_num(v.get('score'), 1)}/100   "
               f"{v.get('conviction')} conviction{flag}")
    out.append(f"  Action: {v.get('action')}")
    out.append(f"  State:  {opp.get('state')} - {opp.get('why')}")
    cal = (run.get("calibration") or {}).get(key) or {}
    if cal.get("standing"):
        out.append(f"  Tested: {cal['standing']}. {cal.get('advice', '')}")

    if not opp.get("show_entry", True):
        # No entry prices under a rating that says do not enter.
        return out
    if key == "swing" and lv.get("available"):
        ez = lv["entry_zone"]
        out.append(f"  Entry   {_price(ez['band_low'])} to {_price(ez['band_high'])}"
                   f"   Invalidation {_price(lv['invalidation']['price'])}")
        out.append(f"  Target  {_price(lv['target_1']['price'])} then "
                   f"{_price(lv['target_2']['price'])}   R:R {_num(lv.get('reward_to_risk'), 2)}")
    elif key == "long_term" and lv.get("available"):
        az = lv["accumulation_zone"]
        out.append(f"  Accumulate at or below {_price(az['price'])}"
                   f"   (clear bargain under {_price(az['band_low'])})"
                   f"   Trim {_price((lv.get('trim_zone') or {}).get('price'))}")
        out.append("  Thesis break marker "
                   f"{_price((lv.get('thesis_invalidation') or {}).get('price_marker'))}"
                   f"   {_review_bit(lv)}")
    else:
        out.append(f"  Levels unavailable: {lv.get('why', 'not computed')}")
    return out


def _swing_block(run: dict, lv: dict, opp: dict) -> list[str]:
    """No rating and no score: a state, a reason, and the levels.

    The swing score was measured and did not separate forward returns, so what
    is published is a description of the setup that a reader can check against a
    chart, rather than a verdict they cannot.
    """
    if run.get("swing"):
        return [_rule()] + swing_block.plain(run)
    sig = run.get("signal") or {}
    out = [_rule(), "SWING (2 TO 6 WEEKS)   setup only, no rating"]
    if not sig.get("signal"):
        out.append(f"  {sig.get('why', 'The setup could not be described.')}")
        return out
    out.append(f"  {sig['signal']}   {opp.get('state')}")
    out.append(f"  {sig.get('why')}")
    out.append(f"  Action: {opp.get('action')}")
    for check in sig.get("checks") or []:
        mark = "yes" if check["pass"] else "NO "
        out.append(f"    [{mark}] {check['name']}: {check['detail']}")
    if opp.get("show_entry") and lv.get("available"):
        ez = lv["entry_zone"]
        out.append(f"  Entry   {_price(ez['band_low'])} to {_price(ez['band_high'])}"
                   f"   Invalidation {_price(lv['invalidation']['price'])}")
        out.append(f"  Target  {_price(lv['target_1']['price'])} then "
                   f"{_price(lv['target_2']['price'])}   R:R {_num(lv.get('reward_to_risk'), 2)}")
    out.append("  " + SWING_NOTE)
    return out


def _review_bit(lv: dict) -> str:
    review = lv.get("next_review") or {}
    if not review.get("when"):
        return "Next earnings unknown"
    mark = "estimated" if review.get("approximate") else "confirmed"
    est = review.get("eps_estimate")
    tail = f", consensus {_price(est)}" if est else ""
    return f"Next earnings {review['when']} ({mark}{tail})"


def _valuation_block(run: dict) -> list[str]:
    fv = ((run.get("valuation") or {}).get("fair_value")) or {}
    vr = ((run.get("levels") or {}).get("long_term") or {}).get("valuation_range") or {}
    out = [_rule(), "FAIR VALUE"]
    if not fv:
        out.append("  Not computable: no valuation method had the inputs it needs.")
        return out
    out.append(f"  {_price(fv['value'])}   {_pct(fv.get('upside'))} from price"
               f"   (median of {fv['method_count']} methods, "
               f"{_price(fv['low'])} to {_price(fv['high'])})")
    for m in fv["methods"]:
        out.append(f"    {m['name']:<24} {_price(m['value'])}")
    if fv.get("excluded_own_history"):
        out.append("    Own-history multiple     excluded: the multiple re-rated")
    elif vr.get("own_history_mid"):
        out.append(f"    Own-history range        {_price(vr.get('own_history_low'))} to "
                   f"{_price(vr.get('own_history_high'))}")
    return out


def _street_block(run: dict) -> list[str]:
    ev = run.get("evidence") or {}
    c = ev.get("consensus") or {}
    b = ev.get("breakdown") or {}
    out = [_rule(), "STREET"]
    if not c:
        out.append("  No analyst consensus available.")
        return out
    out.append(f"  Targets  low {_price(c.get('target_low_price'))}   "
               f"mean {_price(c.get('target_mean_price'))}   "
               f"high {_price(c.get('target_high_price'))}")
    if b:
        d = b.get("detail") or {}
        out.append(f"  Ratings  {b.get('total')} analysts: {b.get('bullish')} bullish, "
                   f"{b.get('neutral')} neutral, {b.get('bearish')} bearish"
                   f"   (mean {_num(c.get('recommendation_mean'), 2)} of 5, 1 is best)")
        out.append(f"           strong buy {d.get('strongBuy')} / buy {d.get('buy')} / "
                   f"hold {d.get('hold')} / sell {d.get('sell')} / "
                   f"strong sell {d.get('strongSell')}")
    else:
        out.append(f"  Ratings  {c.get('analyst_count')} analysts, mean "
                   f"{_num(c.get('recommendation_mean'), 2)} of 5 (1 is best); "
                   "no split available")
    return out


def _relative_block(run: dict) -> list[str]:
    rel = (run.get("evidence") or {}).get("relative") or {}
    etf = rel.get("sector_etf")
    out = [_rule(), "RELATIVE STRENGTH (excess return, so zero means it kept pace)"]
    if not rel.get("vs_spy"):
        out.append("  Not measured: no benchmark history was available.")
        return out
    windows = ("1m", "3m", "6m", "12m")
    out.append("                " + "".join(f"{w:>10}" for w in windows))
    out.append("  vs SPY        " + "".join(f"{_pct(rel['vs_spy'].get(w), 1):>10}"
                                            for w in windows))
    if rel.get("vs_sector"):
        out.append(f"  vs {etf:<11}" + "".join(f"{_pct(rel['vs_sector'].get(w), 1):>10}"
                                               for w in windows))
        out.append(f"  {etf} vs SPY   " + "".join(
            f"{_pct(rel['sector_vs_spy'].get(w), 1):>10}" for w in windows))
    else:
        out.append("  No sector benchmark was available for this name.")
    out.append("  A peer-by-peer comparison needs a peer list, which no free source provides;"
               "\n  the sector ETF is the closest honest stand-in.")
    return out


def _scores_block(run: dict) -> list[str]:
    out = [_rule(), "FACTORS (0-100, higher is better; risk higher means riskier)"]
    families = run.get("factors") or {}
    keys = ("quality", "growth", "valuation", "momentum", "relative_strength",
            "technical", "earnings", "risk")
    cells = []
    for key in keys:
        fam = families.get(key)
        if not fam:
            continue
        score, label = fam.get("score"), fam.get("label", key)
        if score is None:
            cells.append(f"{label:<18}  --  abstained")
            continue
        filled = int(round(score / 100 * _GAUGE))
        gauge = "#" * filled + "." * (_GAUGE - filled)
        cover = fam.get("coverage") or 0
        mark = "*" if cover < 0.8 else " "
        cells.append(f"{label:<18}{score:4.0f} {gauge}{mark}")
    for i in range(0, len(cells), 2):
        out.append("  " + "   ".join(cells[i:i + 2]))
    if any("*" in c for c in cells):
        out.append("  * computed from less than 80% of that family's inputs")
    return out


def _quality_block(run: dict) -> list[str]:
    ev = run.get("evidence") or {}
    caveats = ev.get("caveats") or []
    out = [_rule(), f"DATA QUALITY {ev.get('quality_score')}/100   "
                    f"{ev.get('bar_count')} daily bars   "
                    f"{len(ev.get('metrics') or [])} filed quarters"]
    if not caveats:
        out.append("  No data caveats.")
        return out
    for c in caveats:
        out.append(f"  [{c['severity']}] {c['message']}")
    return out
