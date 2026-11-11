"""The swing call: Buy or Sell, the plan if it is a Buy, and how it was reached.

The user decides whether to take the trade. This module's job is to put a call in
front of him with every reason attached and its evidence standing stated, so the
judgement is his and informed.

What the call is built from, and why these weights. The research in
docs/SWING-EVIDENCE-2026-09.md tested 46 chart readings and four pattern setups on
229 names over 2019 to 2026. Classic trend and momentum readings did not predict
the next 1 to 13 weeks; buying weakness inside long-term strength was the one
direction that held in both samples; a strong earnings reaction was followed by
underperformance; tight one-candle stops were taken out by noise; fixed R targets
lowered expectancy. The call follows that evidence where it exists, and where it
does not (sentiment, fundamentals at this horizon) the component is included at a
modest weight and labelled untested, because a reader wants it and the honest
label costs nothing.

  setup (30)         which of five recognised setups is live, in its long-term context
  momentum (20)      12-1 and 6-month return, strength against the sector, accumulation
  earnings (10)      last reaction INVERTED, the surprise, earnings growth
  revisions (10)     how next year's EPS estimate has moved in 30 and 90 days, and how
                     many analysts raised against how many cut
  fundamentals (10)  the quality, growth and valuation families
  sentiment (15)     Street upside, target changes, headline tone
  opportunity (5)    volatility: more range, more to capture, more risk

Version 2.1 (2026-09-20) follows the author's direction: momentum carries more weight, and
a Buy can come from any of five setups, not only a pullback: a deep dip, a dip the
buyers have visibly taken back, a leader under accumulation, a breakout from a coiled
range, or a slide of 3 ATR from the high. Each was run as a full trade plan on 229
names first, and its record is printed beside it, because three of the five did not
beat buying on a random day and the reader is the one who decides.

Score = 50 + half the weighted sum of component readings in [-1, +1]. Buy at 60 or
above with no blocker AND at least one live setup; otherwise Sell. Most names are a
Sell on most days, which is what a selective swing call should look like. For a long-only book Sell means do not buy,
and exit or tighten if held.

The plan comes from the chart, in the order a swing trader works: invalidation
first (a real structural level 1.5 to 3 ATR away, or 2 ATR if there is none),
then shares from the dollar risk, then targets from structure.
"""

from __future__ import annotations

import math

import pandas as pd

from edgedesk.evidence import sentiment as sentiment_mod
from edgedesk.research import technical

SWING_VERSION = "2.1.0"
BUY, SELL = "Buy", "Sell"
BUY_AT = 60.0
RISK_DOLLARS = 1000.0
TIME_LIMIT_SESSIONS = 40
BLACKOUT_SESSIONS = 5
WEIGHTS = {"setup": 30, "momentum": 20, "earnings": 10, "revisions": 10,
           "fundamentals": 10, "sentiment": 15, "opportunity": 5}

# Each setup as a full plan on 229 names (2 ATR or structural stop, no target, 40
# sessions): mean return on the position, 2019-2023 then 2024-2026. Buying on any
# random day made +2.1% then +1.8%, which is the bar.
BASELINE = "any random day made +2.1% then +1.8%"
SETUPS = {
    "deep_dip": {
        "label": "Deep dip in an uptrend", "points": 0.60,
        "what": "down 8% or more in five sessions, above a rising 200-day",
        "record": "+2.8% then +2.4% a trade: ahead of a random day in both samples"},
    "dip_confirmed": {
        "label": "Dip the buyers took back", "points": 0.45,
        "what": ("a 5%+ five-session drop in an uptrend, then a session that closed above "
                 "the prior high, up on the day, in the top 40% of its range"),
        "record": ("+1.6% then +1.5% a trade: slightly BEHIND a random day. Waiting for "
                   "confirmation gave up part of the bounce")},
    "dip": {
        "label": "Dip in an uptrend", "points": 0.35,
        "what": "down 5% or more in five sessions, above a rising 200-day",
        "record": "+2.3% then +1.5% a trade: level with a random day"},
    "momentum_accumulation": {
        "label": "Leader under accumulation", "points": 0.40,
        "what": ("12-1 momentum 25%+, up 5%+ in three months, up-day volume 1.3x down-day "
                 "volume with 4+ accumulation days, within 2 ATR of the 21-day"),
        "record": "+1.0% then +1.6% a trade: behind a random day in both samples"},
    "range_breakout": {
        "label": "Breakout from a coiled range", "points": 0.40,
        "what": ("Bollinger width in the bottom 30% of six months, net buying in the base, "
                 "then a close above the 20-session high on 1.5x volume"),
        "record": "+0.7% then +1.8% a trade: behind a random day, then level with it"},
    "slide": {
        "label": "3 ATR under the 20-session high", "points": 0.25,
        "what": "a slower pullback of 3 ATR or more from the recent high, in an uptrend",
        "record": "+1.4% then +1.2% a trade: behind a random day in both samples"},
}

TESTED = "tested: held in development and held out"
WEAK = "tested: weak or mixed"
UNTESTED = "untested: no free history, forward test only"

STANDING = (
    "What is known about this call. Its core, buying a fast drop inside a long-term "
    "uptrend with a 2 ATR stop and no fixed target, made +0.26R per trade in 2019 to 2023 "
    "and +0.29R in 2024 to 2026 on 229 large caps (about 1,700 trades), ahead of buying on "
    "a random day per dollar deployed, with intervals that touch zero. The blended call was "
    "then run on 24 large caps over 2024 to 2026 without its sentiment inputs, which have "
    "no history: Buys and Sells did the same over the next 10, 20 and 30 sessions (+0.7% "
    "against +0.7% over SPY at 20 sessions). So the score has NOT been shown to pick "
    "winners; what it gives is a disciplined entry, a real invalidation and a position "
    "size. Expect 40 to 55 percent of Buys to win, with winners larger than losers.")


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _ramp(x: float | None, lo: float, hi: float) -> float | None:
    """-1 at lo, +1 at hi, linear between."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return _clip(2.0 * (x - lo) / (hi - lo) - 1.0)


def _num(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------

def live_setups(t: dict) -> list[str]:
    """Which recognised setups are true on the last session."""
    out = []
    close, sma200, slope200 = t.get("close"), t.get("sma200"), t.get("slope_200")
    lt_up = bool(close and sma200 and slope200 is not None and close > sma200 and slope200 > 0)
    roc5 = t.get("roc_5")
    if lt_up and roc5 is not None and roc5 <= -0.08:
        out.append("deep_dip")
    if lt_up and t.get("dip_recent") and t.get("reclaimed_prior_high") and t.get("up_day") \
            and (t.get("close_loc") or 0) >= 0.6:
        out.append("dip_confirmed")
    if lt_up and roc5 is not None and -0.08 < roc5 <= -0.05:
        out.append("dip")
    if (lt_up and (t.get("mom_12_1") or -1) >= 0.25 and (t.get("roc_63") or -1) >= 0.05
            and (t.get("updown_vol_20") or 0) >= 1.3 and (t.get("accum_days_20") or 0) >= 4
            and (t.get("accum_days_20") or 0) - (t.get("distrib_days_20") or 0) >= 2
            and (t.get("ext_ema21_atr") or 0) <= 2.0):
        out.append("momentum_accumulation")
    if (lt_up and t.get("bb_width_pct_prev") is not None and t["bb_width_pct_prev"] <= 0.30
            and (t.get("updown_vol_20_prev") or 0) >= 1.1 and t.get("prior_high_20")
            and close > t["prior_high_20"] and (t.get("rvol") or 0) >= 1.5
            and (t.get("close_loc") or 0) >= 0.6):
        out.append("range_breakout")
    drop_atr = t.get("drop_from_high20_atr")
    if lt_up and drop_atr is not None and drop_atr >= 3.0 \
            and not {"deep_dip", "dip", "dip_confirmed"} & set(out):
        out.append("slide")
    return out


def _setup(t: dict) -> dict:
    notes, s = [], 0.0
    close, sma200, slope200 = t.get("close"), t.get("sma200"), t.get("slope_200")
    if close and sma200 and slope200 is not None:
        if close > sma200 and slope200 > 0:
            s += 0.20; notes.append("long-term uptrend: above a rising 200-day")
        elif close < sma200 and slope200 < 0:
            s -= 0.35; notes.append("long-term downtrend: below a falling 200-day")
        else:
            notes.append("long-term trend mixed")
    live = live_setups(t)
    if live:
        best = max(live, key=lambda k: SETUPS[k]["points"])
        s += SETUPS[best]["points"]
        for k in live:
            notes.append(f"{SETUPS[k]['label']} ({SETUPS[k]['what']}). Record: "
                         f"{SETUPS[k]['record']}; {BASELINE}")
    ext = t.get("ext_ema21_atr")
    if ext is not None and ext >= 3.0:
        s -= 0.35; notes.append(f"stretched: {ext:.1f} ATR above the 21-day, so this would be chasing")
    rsi = t.get("rsi")
    if rsi is not None and rsi >= 75:
        s -= 0.15; notes.append(f"RSI {rsi:.0f}, overbought")
    ema50 = t.get("ema50")
    if close and ema50 and sma200 and sma200 < close < ema50:
        s += 0.15; notes.append("under the 50-day, above the 200-day")
    if not live:
        notes.append("no setup is live: no dip, no confirmed reclaim, no accumulation in a "
                     "leader, no breakout from a coiled range")
    return {"reading": _clip(s), "notes": notes, "live": live,
            "evidence": "each setup carries its own tested record, printed beside it"}


def _earnings(ev: dict, t: dict) -> dict:
    notes, s = [], 0.0
    rel = ev.get("release") or {}
    reaction, days = t.get("earn_reaction"), rel.get("days_since")
    if reaction is not None and days is not None and days <= 90:
        fresh = max(0.0, 1.0 - days / 90.0)
        if reaction >= 0.08: part = -0.5
        elif reaction >= 0.04: part = -0.25
        elif reaction <= -0.08: part = 0.4
        elif reaction <= -0.04: part = 0.2
        else: part = 0.0
        if part:
            s += part * fresh
            notes.append(f"market reaction to the last report {reaction:+.1%} "
                         f"({days} days ago); strong reactions have reversed, so this "
                         f"counts {'against' if part < 0 else 'for'}")
    eps_s = rel.get("eps_surprise")
    if eps_s is not None:
        if eps_s >= 0.05: s += 0.2; notes.append(f"EPS beat by {eps_s:.0%}")
        elif eps_s <= -0.05: s -= 0.3; notes.append(f"EPS missed by {abs(eps_s):.0%}")
    growth = ((ev.get("metrics") or [{}])[0] or {}).get("earnings_growth")
    if growth is not None:
        if growth > 0.10: s += 0.2; notes.append(f"earnings growing {growth:+.0%} year on year")
        elif growth < 0: s -= 0.2; notes.append(f"earnings shrinking {growth:+.0%} year on year")
    if not notes:
        notes.append("no recent report on file")
    return {"reading": _clip(s), "notes": notes,
            "evidence": "reaction " + TESTED + "; surprise and growth untested at this horizon"}


def _revisions(ev: dict) -> dict:
    """Estimate revisions: among the best-documented signals at a horizon of weeks.
    The level of a forward P/E is not used here; the direction of the estimate is."""
    fwd = ev.get("forward") or {}
    parts, notes = [], []
    r30, r90 = fwd.get("revision_30d"), fwd.get("revision_90d")
    if r30 is not None:
        parts.append((0.5, _ramp(r30, -0.03, 0.03)))
        notes.append(f"next-year EPS estimate {r30:+.1%} in 30 days")
    if r90 is not None:
        parts.append((0.3, _ramp(r90, -0.06, 0.06)))
        notes.append(f"{r90:+.1%} in 90 days")
    up, down = fwd.get("raised_30d"), fwd.get("cut_30d")
    if up is not None and down is not None and up + down > 0:
        parts.append((0.2, _clip((up - down) / (up + down))))
        notes.append(f"{up} analysts raised and {down} cut in 30 days")
    if not parts:
        return {"reading": None, "notes": ["no forward estimates available"], "evidence": UNTESTED}
    reading = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    if fwd.get("forward_pe"):
        notes.append(f"forward P/E {fwd['forward_pe']:.0f} (adjusted EPS, shown for context, not scored)")
    return {"reading": _clip(reading), "notes": notes,
            "evidence": ("well documented in research, but " + UNTESTED + "; the engine saves "
                         "each run's estimates so a record builds")}


def _fundamentals(families: dict) -> dict:
    parts, notes = [], []
    for key, w in (("quality", 0.5), ("growth", 0.3), ("valuation", 0.2)):
        score = (families.get(key) or {}).get("score")
        if score is not None:
            parts.append((w, (score - 50.0) / 50.0))
            notes.append(f"{key} {score:.0f}")
    if not parts:
        return {"reading": None, "notes": ["no fundamentals available"], "evidence": UNTESTED}
    reading = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return {"reading": _clip(reading), "notes": [", ".join(notes) + " of 100"],
            "evidence": "untested at the swing horizon; a guard against buying a falling knife"}


def _sentiment(ev: dict, panel: dict | None) -> dict:
    read = sentiment_mod.read(ev, panel)
    notes, parts = [], []
    up = read["upside_to_mean_target"]
    if up is not None:
        parts.append(0.3 * _ramp(up, 0.0, 0.20)); notes.append(f"Street mean target {up:+.0%} away")
    raised, lowered = read["targets_raised_30d"], read["targets_lowered_30d"]
    if raised or lowered:
        parts.append(0.3 * _clip((raised - lowered) / 3.0))
        notes.append(f"targets raised {raised}, lowered {lowered} in 30 days")
    tone = read["news"]["tone"]
    if tone is not None and read["news"]["positive"] + read["news"]["negative"] > 0:
        parts.append(0.4 * _clip(tone * 2))
        notes.append(f"headlines: {read['news']['positive']} positive, "
                     f"{read['news']['negative']} negative in 14 days")
    if not parts:
        return {"reading": None, "notes": ["no sentiment data"], "evidence": UNTESTED,
                "detail": read}
    return {"reading": _clip(sum(parts)), "notes": notes, "evidence": UNTESTED, "detail": read}


def _momentum(ev: dict, t: dict) -> dict:
    rets = ((ev.get("anchors") or {}).get("returns")) or {}
    notes, parts = [], []
    r12, r1, r6 = rets.get("12m"), rets.get("1m"), rets.get("6m")
    if r12 is not None and r1 is not None:
        mom = (1 + r12) / (1 + r1) - 1
        parts.append((0.40, _ramp(mom, -0.10, 0.40)))
        notes.append(f"12-month return skipping the last month {mom:+.0%}")
    if r6 is not None:
        parts.append((0.20, _ramp(r6, -0.10, 0.25)))
        notes.append(f"6-month return {r6:+.0%}")
    vs_sector = (((ev.get("relative") or {}).get("vs_sector")) or {}).get("3m")
    if vs_sector is not None:
        parts.append((0.25, _ramp(vs_sector, -0.10, 0.10)))
        notes.append(f"{vs_sector:+.0%} against its sector over 3 months")
    updown = t.get("updown_vol_20")
    if updown is not None:
        parts.append((0.15, _ramp(updown, 0.7, 1.5)))
        notes.append(f"up-day volume {updown:.1f}x down-day volume over 20 sessions"
                     + (" (accumulation)" if updown >= 1.3 else
                        " (distribution)" if updown <= 0.8 else ""))
    if not parts:
        return {"reading": None, "notes": ["not enough history"], "evidence": WEAK}
    reading = sum(w * v for w, v in parts) / sum(w for w, _ in parts)
    return {"reading": _clip(reading), "notes": notes,
            "evidence": ("tested: weak. Ranked across 229 names, momentum and accumulation "
                         "readings did not separate the next 1 to 13 weeks; only the top "
                         "tenth on 12-1 momentum showed a spread. Weighted at 20 on the author's "
                         "direction, not on the evidence")}


def _opportunity(t: dict) -> dict:
    atr_pct = t.get("atr_pct")
    if atr_pct is None:
        return {"reading": None, "notes": ["no ATR"], "evidence": TESTED}
    return {"reading": _ramp(atr_pct, 0.012, 0.035),
            "notes": [f"daily range {atr_pct:.1%} of price; wider ranges paid more, and risk more"],
            "evidence": TESTED + " (it is market risk, not selection skill)"}


# ---------------------------------------------------------------------------
# The chart reading and the plan
# ---------------------------------------------------------------------------

def _technical_snapshot(bars: list[dict], spy_bars: list[dict], release: dict | None) -> dict:
    if len(bars) < 210:
        return {}
    df = technical.frame(bars)
    f = technical.features(df)
    last = f.iloc[-1]
    out = {k: _num(last.get(k)) for k in (
        "atr", "atr_pct", "ema21", "ema50", "sma100", "sma200", "slope_200", "roc_5", "rsi",
        "ext_ema21_atr", "last_pivot_low", "last_pivot_high", "close_loc", "hammer",
        "bull_engulf", "adx", "rvol", "mom_12_1", "roc_63", "updown_vol_20", "accum_days_20",
        "distrib_days_20")}
    prev = f.iloc[-2]
    out["bb_width_pct_prev"] = _num(prev.get("bb_width_pct"))
    out["updown_vol_20_prev"] = _num(prev.get("updown_vol_20"))
    out["prior_high_20"] = float(df["high"].iloc[-21:-1].max())
    out["low_7"] = float(df["low"].iloc[-7:].min())
    out["dip_recent"] = bool((f["roc_5"].iloc[-6:-1] <= -0.05).any())
    out["reclaimed_prior_high"] = bool(df["close"].iloc[-1] > df["high"].iloc[-2])
    out["up_day"] = bool(df["close"].iloc[-1] > df["open"].iloc[-1])
    out["close"] = float(df["close"].iloc[-1])
    high20 = float(df["high"].iloc[-20:].max())
    out["high_20"], out["low_20"] = high20, float(df["low"].iloc[-20:].min())
    out["high_63"] = float(df["high"].iloc[-63:].max())
    out["high_252"] = float(df["high"].iloc[-252:].max())
    if out["atr"]:
        out["drop_from_high20_atr"] = (high20 - out["close"]) / out["atr"]
    out["stabilizing"] = bool(
        (out.get("close_loc") or 0) >= 0.7 or (out.get("hammer") or 0) > 0
        or (out.get("bull_engulf") or 0) > 0
        or (len(df) > 1 and df["close"].iloc[-1] > df["high"].iloc[-2]))
    # The market's reaction to the last report: two sessions, so a release before
    # the open and one after the close are both covered.
    if release and release.get("date") and spy_bars:
        spy = technical.frame(spy_bars)["close"]
        day = pd.Timestamp(release["date"])
        idx = df.index.searchsorted(day)
        if 1 <= idx < len(df) - 1:
            a, b = df.index[idx - 1], df.index[min(idx + 1, len(df) - 1)]
            if a in spy.index and b in spy.index:
                out["earn_reaction"] = float((df["close"][b] / df["close"][a])
                                             - (spy[b] / spy[a]))
    return out


def _plan(t: dict, live: list[str] | None = None) -> dict | None:
    close, atr = t.get("close"), t.get("atr")
    if not close or not atr:
        return None
    candidates = [("the last confirmed swing low", t.get("last_pivot_low")),
                  ("the 200-day average", t.get("sma200")),
                  ("the 100-day average", t.get("sma100")),
                  ("the 20-session low", t.get("low_20"))]
    if live and "dip_confirmed" in live:
        candidates.insert(0, ("the low of the dip the buyers took back", t.get("low_7")))
    usable = [(name, lvl - 0.1 * atr) for name, lvl in candidates
              if lvl and 1.5 * atr <= close - (lvl - 0.1 * atr) <= 3.0 * atr]
    if usable:
        name, stop = max(usable, key=lambda c: c[1])
        rule = (f"Just under {name}, {(close - stop) / atr:.1f} ATR away. A close below it "
                "means the structure that justified the trade is gone.")
    else:
        stop = close - 2.0 * atr
        rule = ("No structural level sits 1.5 to 3 ATR below price, so 2 ATR below the "
                "close. One-candle stops were tested and are taken out by ordinary noise.")
    risk = close - stop
    shares = int(RISK_DOLLARS // risk)

    above = sorted({round(v, 2) for v in (t.get("ema21"), t.get("ema50"), t.get("last_pivot_high"),
                                         t.get("high_20"), t.get("high_63"), t.get("high_252"))
                    if v and v > close})
    t1 = next((v for v in above if v >= close + 1.0 * risk), None)
    t2 = next((v for v in above if t1 and v >= max(close + 2.0 * risk, t1 * 1.01)), None)
    t1_rule = "nearest structure at least 1R away" if t1 else "1.5R: no structure in range"
    t2_rule = "next structure at least 2R away" if t2 else "3R: no further structure in range"
    t1 = t1 or close + 1.5 * risk
    t2 = t2 or max(close + 3.0 * risk, t1 + risk)
    return {
        "entry": round(close, 2),
        "entry_rule": ("The next open, near the last close. Do not chase more than half an ATR "
                       f"above it ({close + 0.5 * atr:,.2f}); the plan's risk no longer holds there."),
        "invalidation": round(stop, 2), "invalidation_rule": rule,
        "risk_per_share": round(risk, 2), "risk_dollars": RISK_DOLLARS,
        "shares": shares, "position_dollars": round(shares * close, 0),
        "target_1": round(t1, 2), "target_1_rule": t1_rule, "target_1_r": round((t1 - close) / risk, 2),
        "target_2": round(t2, 2), "target_2_rule": t2_rule, "target_2_r": round((t2 - close) / risk, 2),
        "time_limit_sessions": TIME_LIMIT_SESSIONS,
        "management": (
            "Tested best: no fixed target, exit at the invalidation or after "
            f"{TIME_LIMIT_SESSIONS} sessions, whichever comes first. Taking half at the first "
            "target and moving the stop to entry raises the win rate (about 55% against 40%) "
            "and lowers the average result; both are valid, the choice is comfort against "
            "expectancy. Be flat before the next earnings report."),
        "atr": round(atr, 2),
    }


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def recommend(ev: dict, families: dict, panel: dict | None, bars: list[dict],
              spy_bars: list[dict], owns: bool = False) -> dict:
    """*ev*, *families* and *panel* are the serialized forms stored in the run."""
    if (ev.get("profile") or {}).get("security_type") == "ETF":
        return {"version": SWING_VERSION, "call": None, "score": None,
                "why": "The stock swing setup has not been tested on ETFs; no ETF swing call is published.",
                "components": [], "blockers": [], "plan": None,
                "standing": "Not measured for ETFs."}
    t = _technical_snapshot(bars, spy_bars, ev.get("release"))
    if not t:
        return {"version": SWING_VERSION, "call": None, "score": None,
                "why": "Not enough price history for a swing call.", "components": [],
                "blockers": [], "plan": None, "standing": STANDING}

    if any(c.get("severity") == "withhold" for c in ev.get("caveats") or []):
        return {"version": SWING_VERSION, "call": None, "score": None,
                "why": ("No swing call: the data for this name is incomplete (see data "
                        "quality), and a call built on numbers the engine refuses to rate "
                        "would not be a call."),
                "components": [], "blockers": [], "plan": None, "standing": STANDING}

    parts = {"setup": _setup(t), "earnings": _earnings(ev, t), "revisions": _revisions(ev),
             "fundamentals": _fundamentals(families), "sentiment": _sentiment(ev, panel),
             "momentum": _momentum(ev, t), "opportunity": _opportunity(t)}
    live = {k: p for k, p in parts.items() if p["reading"] is not None}
    total_w = sum(WEIGHTS[k] for k in live) or 1
    weighted = sum(WEIGHTS[k] * p["reading"] for k, p in live.items()) * (100.0 / total_w)
    score = round(_clip(50.0 + weighted / 2.0, 0.0, 100.0), 1)

    blockers = []
    nxt = ((ev.get("calendar") or {}).get("next_earnings"))
    if nxt:
        days = int((pd.Timestamp(nxt) - pd.Timestamp(ev["as_of"])).days)
        if 0 <= days <= BLACKOUT_SESSIONS + 2:
            blockers.append(f"Earnings on {nxt}, {days} days away: a report can gap through any "
                            "invalidation, so no new swing entry before it.")
    live = parts["setup"].get("live") or []
    if not live:
        blockers.append("No setup: no dip, no confirmed reclaim, no accumulation in a leader "
                        "and no breakout from a coiled range. A score is not an entry.")
    plan = _plan(t, live)
    if plan is None or plan["shares"] < 1:
        blockers.append("No workable invalidation could be placed.")

    call = BUY if score >= BUY_AT and not blockers else SELL
    components = [{"key": k, "weight": WEIGHTS[k], "reading": (None if p["reading"] is None
                                                               else round(p["reading"], 3)),
                   "points": (None if p["reading"] is None
                              else round(WEIGHTS[k] * p["reading"] * (100.0 / total_w) / 2.0, 1)),
                   "notes": p["notes"], "evidence": p["evidence"]}
                  for k, p in parts.items()]
    best = max((c for c in components if c["points"] is not None),
               key=lambda c: c["points"], default=None)
    worst = min((c for c in components if c["points"] is not None),
                key=lambda c: c["points"], default=None)
    if call == BUY:
        why = (f"Buy: score {score:.0f} of 100 (Buy at {BUY_AT:.0f}). Setup: "
               + "; ".join(SETUPS[k]["label"] for k in live) + ". "
               + f"Most in favour: {best['key']}.")
        action = ("Add only inside the plan below" if owns
                  else "Candidate: the plan below is the whole trade")
    else:
        reason = (blockers[0] if blockers else
                  f"score {score:.0f} of 100 is under the Buy line of {BUY_AT:.0f}. "
                  f"Most against: {worst['key']} ({'; '.join(worst['notes'][:2])}).")
        why = "Sell: " + reason
        action = ("Exit, or hold only against the invalidation below" if owns
                  else "Do not buy; nothing here is worth the risk today")
        waiting = (score >= BUY_AT and len(blockers) == 1 and blockers[0].startswith("No setup")
                   and t.get("high_20") and t.get("atr"))
        if waiting:
            level = min(t["high_20"] - 3.0 * t["atr"], t["close"] * 0.95)
            why = (f"Sell for now: everything but the entry is in place (score {score:.0f}), "
                   "and no setup is live.")
            action = (("Hold; do not add here. " if owns else "Do not buy today. ")
                      + f"It becomes a candidate on a pullback to about {level:,.2f}, on a "
                        f"high-volume close above {t.get('prior_high_20') or 0:,.2f} out of a "
                        "tight range, or when accumulation shows up in the volume.")
    return {"version": SWING_VERSION, "call": call, "score": score, "buy_at": BUY_AT,
            "why": why, "action": action, "components": components, "blockers": blockers,
            "plan": plan if call == BUY or owns else None,
            "reference_invalidation": plan["invalidation"] if plan else None,
            "setups": [{"key": k, **SETUPS[k]} for k in live],
            "technical": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in t.items()},
            "sentiment": parts["sentiment"].get("detail"),
            "standing": STANDING}
