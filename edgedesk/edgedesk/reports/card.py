"""The scorecard as HTML, rendered from a run file.

Same content as the text scorecard and the same numbers, because both read the
one run file. This exists because a card is easier to scan than a column of
monospace, and because it is what gets looked at on a phone.

No JavaScript, no external requests, no fonts to fetch: a single self-contained
file that opens anywhere and prints correctly.
"""

from __future__ import annotations

from edgedesk.reports import longterm_block, swing_block

import html
import json
from pathlib import Path

RATING_CLASS = {
    "Buy": "buy", "Overweight": "ow", "Hold": "hold",
    "Underweight": "uw", "Sell": "sell",
}
FAMILIES = ("fund", "quality", "growth", "valuation", "momentum",
            "relative_strength", "technical", "earnings", "risk")
WINDOWS = ("1m", "3m", "6m", "12m")

from edgedesk.reports.scorecard import SWING_NOTE  # one sentence, every format
from edgedesk.reports.common import (etf_methodology, evidence_basis,
                                     foreign_filer_methodology, partial_label)


def _e(v) -> str:
    return html.escape(str(v), quote=True)


def _price(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _pct(v, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:+.{digits}f}%"


def _pct0(v) -> str:
    return "n/a" if v is None else f"{v * 100:.1f}%"


def _tone(score: float | None, riskier_is_worse: bool = False) -> str:
    """Colour band for a score. Risk is read the other way round on purpose."""
    if score is None:
        return "na"
    s = 100 - score if riskier_is_worse else score
    return "good" if s >= 70 else "mid" if s >= 45 else "bad"


def render(run: dict) -> str:
    ev = run.get("evidence") or {}
    profile = ev.get("profile") or {}
    anchors = ev.get("anchors") or {}
    name = profile.get("name") or run["ticker"]

    body = [
        _head(run, ev, profile, anchors, name),
        _methodology(ev),
        _horizon(run, "swing"),
        _horizon(run, "long_term"),
        longterm_block.html(run) if run.get("long_term") else "",
        _thesis(run),
        _fair_value(run),
        _street(ev),
        _relative(ev),
        _factors(run),
        _footer(run, ev),
    ]
    return _PAGE.format(title=_e(f"{run['ticker']} scorecard"),
                        css=_CSS, body="\n".join(b for b in body if b))


def _methodology(ev: dict) -> str:
    note = foreign_filer_methodology(ev) or etf_methodology(ev)
    return "" if not note else f'<div class="cav degrade method"><b>{_e(note)}</b></div>'


# ---------------------------------------------------------------------------

def _head(run, ev, profile, anchors, name) -> str:
    div = ev.get("dividend") or {}
    if div.get("yield") is None:
        dividend = ""
    elif not div["yield"]:
        dividend = '<span class="chip">no dividend</span>'
    else:
        dividend = f'<span class="chip">dividend {_pct0(div["yield"])}</span>'
    return f"""
  <div class="hd">
    <div>
      <div class="tk">{_e(run['ticker'])} <span class="nm">{_e(name)}</span></div>
      <div class="sec">{_e(profile.get('sector') or 'sector unmapped')}
        <span class="sub">{_e(profile.get('sic_description') or 'industry unknown')}</span></div>
    </div>
    <div class="px">{_price(anchors.get('last_close'))}
      <small>as of {_e(run['as_of'])}</small>{dividend}</div>
  </div>"""


def _swing(run) -> str:
    """A state and its levels. No pill, no score, nothing that reads as a call."""
    if run.get("swing"):
        return swing_block.html(run)
    sig = run.get("signal") or {}
    opp = (run.get("opportunity") or {}).get("swing") or {}
    lv = (run.get("levels") or {}).get("swing") or {}
    if not sig.get("signal"):
        return f"""
  <div class="hz">
    <div class="hzt">Swing (2 to 6 weeks)</div>
    <div class="st muted">{_e(sig.get("why", "The setup could not be described."))}</div>
  </div>"""
    tone = "green" if sig["signal"] == "GREEN" else "red"
    checks = "".join(
        f'<span class="chk {"ok" if c["pass"] else "no"}">{_e(c["name"])}</span>'
        for c in sig.get("checks") or [])
    levels = _swing_levels(lv) if opp.get("show_entry") else ""
    return f"""
  <div class="hz">
    <div class="hzt">Swing (2 to 6 weeks) <span class="muted">setup only, no rating</span></div>
    <div class="row">
      <span class="pill sig-{tone}">{_e(sig["signal"])}</span>
      <span class="sigstate">{_e(opp.get("state"))}</span>
    </div>
    <div class="st">{_e(sig.get("why"))}</div>
    <div class="st"><b>Action:</b> {_e(opp.get("action"))}</div>
    <div class="checks">{checks}</div>
    {levels}
    <div class="st stand">{_e(SWING_NOTE)}</div>
  </div>"""


def _horizon(run, key) -> str:
    if key == "swing":
        return _swing(run)
    v = (run.get("verdicts") or {}).get(key) or {}
    lv = (run.get("levels") or {}).get(key) or {}
    opp = (run.get("opportunity") or {}).get(key) or {}
    title = _e(v.get("label", key))

    if v.get("quality_state") == "WITHHELD":
        reasons = "".join(f"<li>{_e(r)}</li>" for r in v.get("reasons") or [])
        return f"""
  <div class="hz">
    <div class="hzt">{title}</div>
    <div class="row"><span class="pill withheld">RATING WITHHELD</span></div>
    <ul class="why">{reasons}</ul>
  </div>"""

    rating = v.get("rating") or "n/a"
    flag = ("" if v.get("quality_state") == "VALID"
            else '<span class="chip warn">degraded</span>')
    levels = ""
    if opp.get("show_entry", True):
        levels = _swing_levels(lv) if key == "swing" else _long_levels(lv)
    cal = (run.get("calibration") or {}).get(key) or {}
    standing = ""
    if cal.get("standing"):
        tone = "bad" if cal["standing"] in ("no separation found", "not measured") else "mid"
        standing = (f'<div class="st stand {tone}"><b>Tested:</b> '
                    f'{_e(cal["standing"])}. {_e(cal.get("advice"))}</div>')
    return f"""
  <div class="hz">
    <div class="hzt">{title}</div>
    <div class="row">
      <span class="pill {RATING_CLASS.get(rating, 'hold')}">{_e(rating.upper())}</span>
      <span class="sc">{v.get('score', 0):.1f} / 100 &middot; {_e(v.get('conviction'))}
        conviction</span>{flag}
    </div>
    <div class="st"><b>{_e(opp.get('state'))}.</b> {_e(opp.get('why'))}</div>
    {standing}
    {levels}
  </div>"""


def _swing_levels(lv) -> str:
    if not lv.get("available"):
        return f'<div class="st muted">Levels unavailable: {_e(lv.get("why", ""))}</div>'
    ez = lv["entry_zone"]
    rr = lv.get("reward_to_risk")
    return _levels([
        ("Entry zone", f"{_price(ez['band_low'])} to {_price(ez['band_high'])}"),
        ("Invalidation", _price(lv["invalidation"]["price"])),
        ("First target", _price(lv["target_1"]["price"])),
        ("Second target", _price(lv["target_2"]["price"])),
        ("Reward to risk", "n/a" if rr is None else f"{rr:.2f} to 1"),
    ])


def _long_levels(lv) -> str:
    if not lv.get("available"):
        return f'<div class="st muted">Levels unavailable: {_e(lv.get("why", ""))}</div>'
    az = lv["accumulation_zone"]
    review = lv.get("next_review") or {}
    when = review.get("when") or "unknown"
    if review.get("when"):
        when += " (confirmed)" if not review.get("approximate") else " (estimated)"
    return _levels([
        ("Accumulate at or below", _price(az["price"])),
        ("Clear bargain under", _price(az["band_low"])),
        ("Trim", _price((lv.get("trim_zone") or {}).get("price"))),
        ("Thesis break marker",
         _price((lv.get("thesis_invalidation") or {}).get("price_marker"))),
        ("Next earnings", when),
    ])


def _levels(pairs) -> str:
    cells = "".join(f"<div>{_e(k)}<strong>{_e(v)}</strong></div>" for k, v in pairs)
    return f'<div class="lv">{cells}</div>'


def _thesis(run) -> str:
    llm = run.get("llm") or {}
    synth = llm.get("synthesis")
    if not synth:
        return ""
    dissent = llm.get("dissent")
    block = ""
    if dissent:
        against = ", ".join(f"{k.replace('_', ' ')} {v}"
                            for k, v in (dissent.get("against_rating") or {}).items() if v)
        block = (f'<div class="st stand bad"><b>Dissent:</b> the written analysis reads '
                 f'this {_e(dissent.get("direction"))} than the formula did '
                 f'({_e(against)}). {_e(dissent.get("reason"))} The rating is '
                 f'unchanged.</div>')
    return f"""
  <div class="sec-block">
    <div class="bt">Thesis <span class="muted">written from the same evidence, every
      figure checked against the run</span></div>
    {f'<div class="st stand bad"><b>{_e(partial_label(llm.get("missing_cases")))}</b>, so this thesis is one-sided.</div>' if llm.get("missing_cases") else ""}
    <div class="st body">{_e(synth["thesis"])}</div>
    <div class="st body muted"><b>Against it:</b>
      {_e(synth["strongest_counterargument"])}</div>
    {block}
  </div>"""


def _fair_value(run) -> str:
    fv = ((run.get("valuation") or {}).get("fair_value")) or {}
    if not fv:
        return """
  <div class="sec-block"><div class="bt">Fair value</div>
    <div class="st muted">No valuation method had the inputs it needs.</div></div>"""
    methods = "".join(
        f'<div>{_e(m["name"])}<strong>{_price(m["value"])}</strong></div>'
        for m in fv["methods"])
    if fv.get("excluded_own_history"):
        methods += ('<div>Own-history multiple<strong class="muted">excluded, '
                    're-rated</strong></div>')
    spread = fv.get("spread")
    spread_note = ("" if spread is None else
                   f' &middot; methods disagree by {spread * 100:.0f}%')
    return f"""
  <div class="sec-block">
    <div class="bt">Fair value</div>
    <div class="row big">
      <span class="fv">{_price(fv['value'])}</span>
      <span class="sc">{_pct(fv.get('upside'))} from price &middot; median of
        {fv['method_count']} methods{spread_note}</span>
    </div>
    <div class="lv small">{methods}</div>
  </div>"""


def _street(ev) -> str:
    c = ev.get("consensus") or {}
    if not c:
        return """
  <div class="sec-block"><div class="bt">Street</div>
    <div class="st muted">No analyst consensus available.</div></div>"""
    b = ev.get("breakdown") or {}
    bars = ""
    if b and b.get("total"):
        total = b["total"]
        parts = [("bullish", b.get("bullish", 0), "good"),
                 ("neutral", b.get("neutral", 0), "mid"),
                 ("bearish", b.get("bearish", 0), "bad")]
        segs = "".join(
            f'<i class="{cls}" style="width:{(n / total) * 100:.1f}%"></i>'
            for _, n, cls in parts if n)
        counts = " &middot; ".join(f"{n} {label}" for label, n, _ in parts)
        d = b.get("detail") or {}
        bars = f"""
    <div class="split">{segs}</div>
    <div class="st muted">{total} analysts: {counts} &middot; mean
      {c.get('recommendation_mean')} of 5, 1 is best<br>
      strong buy {d.get('strongBuy')} / buy {d.get('buy')} / hold {d.get('hold')}
      / sell {d.get('sell')} / strong sell {d.get('strongSell')}</div>"""
    return f"""
  <div class="sec-block">
    <div class="bt">Street</div>
    <div class="lv small">
      <div>Target low<strong>{_price(c.get('target_low_price'))}</strong></div>
      <div>Target mean<strong>{_price(c.get('target_mean_price'))}</strong></div>
      <div>Target high<strong>{_price(c.get('target_high_price'))}</strong></div>
    </div>{bars}
  </div>"""


def _relative(ev) -> str:
    rel = ev.get("relative") or {}
    if not rel.get("vs_spy"):
        return """
  <div class="sec-block"><div class="bt">Relative strength</div>
    <div class="st muted">No benchmark history was available.</div></div>"""
    etf = rel.get("sector_etf")
    rows = [("vs SPY", rel.get("vs_spy"))]
    if rel.get("vs_sector"):
        rows.append((f"vs {etf}", rel.get("vs_sector")))
        rows.append((f"{etf} vs SPY", rel.get("sector_vs_spy")))
    head = "".join(f"<th>{w}</th>" for w in WINDOWS)
    body = ""
    for label, series in rows:
        cells = ""
        for w in WINDOWS:
            v = (series or {}).get(w)
            cls = "" if v is None else (" good" if v > 0 else " bad")
            cells += f'<td class="n{cls}">{_pct(v)}</td>'
        body += f"<tr><th>{_e(label)}</th>{cells}</tr>"
    return f"""
  <div class="sec-block">
    <div class="bt">Relative strength <span class="muted">excess return, zero means
      it kept pace</span></div>
    <table class="rs"><tr><th></th>{head}</tr>{body}</table>
    <div class="st muted">A peer-by-peer comparison needs a peer list, which no free
      source provides; the sector ETF is the closest honest stand-in.</div>
  </div>"""


def _factors(run) -> str:
    families = run.get("factors") or {}
    cells = ""
    for key in FAMILIES:
        fam = families.get(key)
        if not fam:
            continue
        score = fam.get("score")
        riskier = fam.get("direction") == "higher_riskier"
        label = fam.get("label", key)
        if riskier and "risk" not in label.lower():
            label += " (higher is worse)"
        if score is None:
            cells += (f'<div class="fc"><span>{_e(label)}</span>'
                      f'<b class="na">--</b><em>abstained</em></div>')
            continue
        thin = "" if (fam.get("coverage") or 0) >= 0.8 else "<em>partial</em>"
        cells += (f'<div class="fc"><span>{_e(label)}</span>'
                  f'<b class="{_tone(score, riskier)}">{score:.0f}</b>'
                  f'<i style="width:{score:.0f}%" class="{_tone(score, riskier)}"></i>{thin}</div>')
    return f"""
  <div class="sec-block">
    <div class="bt">Factors <span class="muted">0 to 100, higher is better except
      risk</span></div>
    <div class="fg">{cells}</div>
  </div>"""


def _footer(run, ev) -> str:
    caveats = "".join(
        f'<div class="cav {_e(c["severity"])}">{_e(c["message"])}</div>'
        for c in ev.get("caveats") or [])
    return f"""
  <div class="ft">
    Data quality {ev.get('quality_score')}/100 &middot; {_e(evidence_basis(ev))} &middot; run
    {_e(run.get('content_hash'))} &middot; factors v{_e(run['versions']['factors'])}
    {caveats}
    <div class="disc">Ratings come from an explicit formula; each one's tested
      standing is shown with it. Research only: this places no orders and is not
      advice.</div>
  </div>"""


def write(run: dict, path: str | Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(run), encoding="utf-8")
    return str(path)


def from_file(run_file: str | Path, out: str | Path) -> str:
    return write(json.loads(Path(run_file).read_text(encoding="utf-8")), out)


# ---------------------------------------------------------------------------

_CSS = """
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; padding:20px; background:#f6f5f2; color:#1a1a1a;
         font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  .card { max-width:760px; margin:0 auto; background:#fff; border:1px solid #d8d5cf;
          border-radius:10px; overflow:hidden; }
  .hd { padding:14px 18px; border-bottom:1px solid #e5e2dc; display:flex;
        justify-content:space-between; align-items:flex-start; gap:14px; flex-wrap:wrap; }
  .tk { font-size:21px; font-weight:700; letter-spacing:-.01em; }
  .nm { font-size:13px; color:#6b6b6b; font-weight:400; margin-left:6px; }
  .sec { font-size:12px; color:#3f6d8f; margin-top:3px; font-weight:600; }
  .sub { color:#6b6b6b; font-weight:400; }
  .sub:before { content:"\\00b7"; margin:0 5px; }
  .px { font-size:18px; font-weight:600; font-variant-numeric:tabular-nums; text-align:right; }
  .px small { display:block; font-size:11px; font-weight:400; color:#6b6b6b; }
  .chip { display:inline-block; margin-top:4px; font-size:10.5px; font-weight:600;
          color:#5a5a5a; background:#f0efec; border-radius:3px; padding:1px 6px; }
  .chip.warn { color:#7a5a12; background:#f6efdc; margin-left:6px; }
  .hz, .sec-block { padding:13px 18px; border-bottom:1px solid #eceae5; }
  .hzt, .bt { font-size:11px; letter-spacing:.09em; text-transform:uppercase;
              color:#6b6b6b; margin-bottom:7px; }
  .bt .muted { letter-spacing:0; text-transform:none; font-size:11px; }
  .row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:6px; }
  .pill { font-size:13.5px; font-weight:700; padding:3px 11px; border-radius:5px; color:#fff; }
  .buy { background:#17603f; } .ow { background:#3d7a4e; } .hold { background:#6b6b6b; }
  .uw { background:#8a2b2b; } .sell { background:#6e1f1f; } .withheld { background:#4a4a4a; }
  .sc { font-size:12.5px; color:#5a5a5a; font-variant-numeric:tabular-nums; }
  .st { font-size:12.5px; line-height:1.5; color:#5a5a5a; }
  .st b { color:#1a1a1a; font-weight:600; }
  .muted { color:#8a8a8a; }
  .body { line-height:1.6; margin-bottom:7px; }
  /* The word as well as the colour: a signal carried by colour alone is
     invisible in greyscale and to a colour-blind reader. */
  .sig-green { background:#17603f; } .sig-red { background:#8a2b2b; }
  .sigstate { font-size:14px; font-weight:700; letter-spacing:.01em; }
  .checks { display:flex; gap:6px; flex-wrap:wrap; margin:7px 0 2px; }
  .chk { font-size:10.5px; padding:1px 7px; border-radius:3px; }
  .chk.ok { background:#e8f0ea; color:#17603f; }
  .chk.no { background:#f6e9e9; color:#8a2b2b; text-decoration:line-through; }
  .stand { margin-top:5px; font-size:11.5px; padding-left:9px; border-left:2px solid #c9c6c0; }
  .stand.bad { border-color:#8a2b2b; } .stand.mid { border-color:#8a6a2b; }
  .why { margin:4px 0 0; padding-left:18px; font-size:12.5px; color:#5a5a5a; line-height:1.5; }
  .lv { display:flex; gap:16px; flex-wrap:wrap; margin-top:9px;
        font-variant-numeric:tabular-nums; }
  .lv div { font-size:11.5px; color:#6b6b6b; }
  .lv strong { display:block; font-size:13.5px; color:#1a1a1a; font-weight:600; margin-top:1px; }
  .lv.small strong { font-size:13px; }
  .big { margin-bottom:2px; }
  .fv { font-size:21px; font-weight:700; font-variant-numeric:tabular-nums; }
  .split { display:flex; height:8px; border-radius:4px; overflow:hidden; background:#eceae5;
           margin:9px 0 6px; }
  .split i { display:block; height:100%; }
  .split i.good { background:#17603f; } .split i.mid { background:#8a6a2b; }
  .split i.bad { background:#8a2b2b; }
  table.rs { border-collapse:collapse; font-size:12px; font-variant-numeric:tabular-nums;
             margin:2px 0 7px; }
  table.rs th { text-align:right; font-weight:600; color:#6b6b6b; padding:2px 10px 2px 0;
                font-size:11.5px; }
  table.rs tr th:first-child { text-align:left; color:#1a1a1a; padding-right:16px; }
  table.rs td { text-align:right; padding:2px 10px 2px 0; }
  td.n.good { color:#17603f; } td.n.bad { color:#8a2b2b; }
  .fg { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:4px 22px; }
  .fc { display:grid; grid-template-columns:1fr 26px 84px auto; align-items:center; gap:7px;
        font-size:12px; color:#5a5a5a; }
  .fc b { text-align:right; font-variant-numeric:tabular-nums; font-size:12.5px; }
  .fc b.good { color:#17603f; } .fc b.mid { color:#8a6a2b; } .fc b.bad { color:#8a2b2b; }
  .fc b.na { color:#a5a5a5; }
  .fc i { display:block; height:5px; border-radius:3px; background:#3f6d8f;
          box-shadow:inset 0 0 0 99px currentColor; }
  .fc i.good { color:#17603f; } .fc i.mid { color:#8a6a2b; } .fc i.bad { color:#8a2b2b; }
  .fc em { font-size:10px; font-style:normal; color:#8a8a8a; }
  .ft { padding:11px 18px; font-size:11.5px; color:#6b6b6b; line-height:1.55; }
  .cav { margin-top:6px; padding-left:9px; border-left:2px solid #b8934a; }
  .cav.withhold { border-color:#8a2b2b; }
  .disc { margin-top:8px; color:#9a9a9a; }
  @media (max-width:560px) { .fg { grid-template-columns:1fr; } }
"""

_PAGE = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
<div class="card">
{body}
</div>
</html>
"""
