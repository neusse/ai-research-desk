"""The eight factor families.

Each family reads the evidence package, scores the signals it can compute, and
abstains on the ones it cannot. Every signal records the fact id it came from,
so a number in any report can be traced back to a source and a timestamp.

Two conventions hold everywhere:

* **Higher is better**, in every family except `risk`, where higher always means
  more risk. The two GitHub projects both had a "risk score" that pointed one
  way in one place and the other way somewhere else; naming the direction once
  and never breaking it is worth more than any extra factor.
* **Coverage is reported, not hidden.** A family says how much of itself it was
  able to compute. A score built from a fraction of its inputs is still a score,
  but the verdict layer is told how thin it is.

FACTORS_VERSION changes whenever a curve or a weight changes, so a stored run
always says which formula produced it. A rating from v1 and a rating from v2 are
not the same measurement and the run files must never pretend otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from edgedesk.evidence.package import Evidence
from edgedesk.factors.scale import band, blend, inverse, percentile

FACTORS_VERSION = "1.1.0"   # Company factors: current valuation is priced at the decision date
ETF_FACTORS_VERSION = "etf-1.0.0"


@dataclass(frozen=True)
class Signal:
    key: str
    label: str
    raw: float | int | str | None
    score: float | None
    weight: float
    fact_id: str | None = None
    note: str | None = None


@dataclass
class Family:
    key: str
    label: str
    score: float | None
    coverage: float
    direction: str                       # "higher_better" | "higher_riskier"
    signals: list[Signal] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.score is not None

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "score": self.score,
            "coverage": self.coverage, "direction": self.direction,
            "signals": [
                {"key": s.key, "label": s.label, "raw": s.raw, "score": s.score,
                 "weight": s.weight, "fact_id": s.fact_id, "note": s.note}
                for s in self.signals
            ],
        }


def _family(key: str, label: str, signals: list[Signal],
            direction: str = "higher_better") -> Family:
    score, coverage = blend([(s.score, s.weight) for s in signals])
    return Family(key=key, label=label, score=score, coverage=coverage,
                  direction=direction, signals=signals)


def _m(ev: Evidence, key: str):
    latest = ev.decision_metrics or {}
    return latest.get(key)


def fund(ev: Evidence) -> Family:
    """Structure and implementation quality for an equity ETF.

    Distribution yield is deliberately absent: cash distributed is not the same
    as return earned, particularly for option-income funds and return of capital.
    """
    f = ev.fund or {}
    expense = f.get("expense_ratio")
    category_expense = f.get("category_expense_ratio")
    relative_fee = expense / category_expense if expense is not None and category_expense else None
    concentration = sum((h.get("weight") or 0.0) for h in (f.get("top_holdings") or [])) or None
    premium = f.get("premium_discount")
    abs_premium = abs(premium) if premium is not None else None
    signals = [
        Signal("expense_ratio", "Annual expense ratio", expense,
               inverse(expense, [(0.0, 0), (0.002, 10), (0.005, 35), (0.010, 70), (0.020, 100)]),
               0.30, "fund.expense_ratio"),
        Signal("relative_fee", "Expense ratio against category average", relative_fee,
               inverse(relative_fee, [(0.5, 0), (1.0, 50), (1.5, 100)]),
               0.15, "fund.category_expense_ratio"),
        Signal("fund_assets", "Fund net assets", f.get("total_assets"),
               band(f.get("total_assets"), [(50e6, 10), (250e6, 40), (1e9, 70), (10e9, 100)]),
               0.25, "fund.total_assets",
               note="Scale is a liquidity and closure-risk proxy, not a return forecast"),
        Signal("top_holdings_concentration", "Reported top-holdings concentration", concentration,
               inverse(concentration, [(0.10, 0), (0.25, 30), (0.50, 70), (0.80, 100)]),
               0.20, "fund.top_holdings_weight"),
        Signal("nav_gap", "Absolute premium or discount to NAV", abs_premium,
               inverse(abs_premium, [(0.0, 0), (0.001, 15), (0.005, 60), (0.020, 100)]),
               0.10, "fund.premium_discount"),
    ]
    return _family("fund", "Fund structure", signals)


# ---------------------------------------------------------------------------
# Quality: does this business earn well and can it pay its bills?
# ---------------------------------------------------------------------------

def quality(ev: Evidence) -> Family:
    signals = [
        Signal("gross_margin", "Gross margin", _m(ev, "gross_margin"),
               band(_m(ev, "gross_margin"), [(0.05, 0), (0.25, 40), (0.45, 70), (0.70, 100)]),
               0.15, "fnd.gross_margin"),
        Signal("operating_margin", "Operating margin", _m(ev, "operating_margin"),
               band(_m(ev, "operating_margin"), [(-0.05, 0), (0.05, 35), (0.15, 65), (0.30, 100)]),
               0.20, "fnd.operating_margin"),
        Signal("net_margin", "Net margin", _m(ev, "net_margin"),
               band(_m(ev, "net_margin"), [(-0.05, 0), (0.03, 35), (0.12, 70), (0.25, 100)]),
               0.15, "fnd.net_margin"),
        Signal("roe", "Return on equity", _m(ev, "return_on_equity"),
               band(_m(ev, "return_on_equity"), [(0.0, 0), (0.08, 35), (0.18, 70), (0.35, 100)]),
               0.20, "fnd.roe",
               note="Null when equity is negative, which buybacks can cause at healthy firms"),
        Signal("roa", "Return on assets", _m(ev, "return_on_assets"),
               band(_m(ev, "return_on_assets"), [(0.0, 0), (0.04, 40), (0.10, 75), (0.20, 100)]),
               0.10, "fnd.roa"),
        Signal("interest_coverage", "Interest coverage", _m(ev, "interest_coverage"),
               band(_m(ev, "interest_coverage"), [(1.0, 0), (3.0, 40), (8.0, 75), (20.0, 100)]),
               0.10, "fnd.interest_coverage"),
        Signal("current_ratio", "Current ratio", _m(ev, "current_ratio"),
               band(_m(ev, "current_ratio"), [(0.7, 0), (1.0, 40), (1.8, 80), (3.0, 100)]),
               0.10, "fnd.current_ratio"),
    ]
    return _family("quality", "Quality", signals)


# ---------------------------------------------------------------------------
# Growth: is the business getting bigger, and is cash following earnings?
# ---------------------------------------------------------------------------

def growth(ev: Evidence) -> Family:
    signals = [
        Signal("revenue_growth", "Revenue growth (YoY)", _m(ev, "revenue_growth"),
               band(_m(ev, "revenue_growth"), [(-0.10, 0), (0.0, 30), (0.10, 60), (0.30, 100)]),
               0.40, "fnd.revenue_growth"),
        Signal("earnings_growth", "Earnings growth (YoY)", _m(ev, "earnings_growth"),
               band(_m(ev, "earnings_growth"), [(-0.25, 0), (0.0, 30), (0.15, 65), (0.50, 100)]),
               0.35, "fnd.earnings_growth"),
        Signal("fcf_growth", "Free cash flow growth (YoY)", _m(ev, "free_cash_flow_growth"),
               band(_m(ev, "free_cash_flow_growth"), [(-0.30, 0), (0.0, 35), (0.20, 70), (0.60, 100)]),
               0.25, "fnd.fcf_growth",
               note="Cash confirming earnings; volatile quarter to quarter by nature"),
    ]
    return _family("growth", "Growth", signals)


# ---------------------------------------------------------------------------
# Valuation: cheap or dear against its own history and against the Street
# ---------------------------------------------------------------------------

def valuation(ev: Evidence, panel: dict | None = None) -> Family:
    """Own-history first. Comparing a multiple to an absolute threshold says
    more about the sector than the stock; comparing it to the range this stock
    has actually traded at is a question about this stock."""
    pe_history = [m.get("price_to_earnings_ratio") for m in ev.metrics
                  if m.get("price_to_earnings_ratio") and m["price_to_earnings_ratio"] > 0]
    pe_now = _m(ev, "price_to_earnings_ratio")
    pe_pct = percentile(pe_now, pe_history)
    # A high percentile means expensive against its own record, so it inverts.
    pe_score = None if pe_pct is None else round(100.0 - pe_pct, 2)

    street_upside = None
    if panel and (panel.get("street") or {}).get("upside_to_mean") is not None:
        street_upside = panel["street"]["upside_to_mean"]

    # When the run has flagged the multiple as re-rated, this signal abstains.
    # The same figure cannot be untrustworthy enough to drop from fair value and
    # trustworthy enough to carry a fifth of the valuation score, and a report
    # that says both is arguing with itself.
    rerated = bool(panel and panel.get("rerated"))
    own_history_gap = None
    if (not rerated and panel
            and (panel.get("own_history") or {}).get("price_vs_mid") is not None):
        own_history_gap = -panel["own_history"]["price_vs_mid"]

    signals = [
        Signal("pe_vs_own", "P/E against its own history", pe_now, pe_score, 0.30, "fnd.pe",
               note=(f"{len(pe_history)} quarters of positive-P/E history"
                     if pe_history else "no positive-P/E history")),
        Signal("own_history_gap", "Discount to own-history fair value", own_history_gap,
               band(own_history_gap, [(-0.35, 0), (-0.10, 35), (0.10, 70), (0.35, 100)]),
               0.20, None,
               note=("Abstained: the multiple re-rated, so the old one is not a fair value"
                     if rerated else "Median own P/E times TTM EPS, versus price")),
        Signal("peg", "PEG", _m(ev, "peg_ratio"),
               inverse(_m(ev, "peg_ratio"), [(0.5, 0), (1.0, 30), (2.0, 70), (3.5, 100)]),
               0.15, "fnd.peg", note="Null unless earnings are both positive and growing"),
        Signal("fcf_yield", "Free cash flow yield", _m(ev, "free_cash_flow_yield"),
               band(_m(ev, "free_cash_flow_yield"), [(0.0, 0), (0.03, 40), (0.06, 75), (0.10, 100)]),
               0.20, "fnd.fcf_yield"),
        Signal("street_upside", "Upside to the Street mean target", street_upside,
               band(street_upside, [(-0.15, 0), (0.0, 35), (0.15, 70), (0.40, 100)]),
               0.15, "est.target_mean",
               note="Current-only: excluded from historical runs by construction"),
    ]
    return _family("valuation", "Valuation", signals)


# ---------------------------------------------------------------------------
# Momentum: is price working, on its own terms
# ---------------------------------------------------------------------------

def momentum(ev: Evidence) -> Family:
    rets = (ev.anchors or {}).get("returns") or {}
    a = ev.anchors or {}
    close, sma50, sma200 = a.get("last_close"), a.get("sma_50"), a.get("sma_200")
    vs_50 = (close / sma50 - 1) if close and sma50 else None
    vs_200 = (close / sma200 - 1) if close and sma200 else None

    signals = [
        Signal("ret_1m", "1-month return", rets.get("1m"),
               band(rets.get("1m"), [(-0.15, 0), (-0.02, 35), (0.05, 70), (0.20, 100)]),
               0.20, "mkt.ret_1m"),
        Signal("ret_3m", "3-month return", rets.get("3m"),
               band(rets.get("3m"), [(-0.25, 0), (-0.03, 35), (0.10, 70), (0.35, 100)]),
               0.25, "mkt.ret_3m"),
        Signal("ret_6m", "6-month return", rets.get("6m"),
               band(rets.get("6m"), [(-0.35, 0), (-0.05, 35), (0.15, 70), (0.50, 100)]),
               0.20, "mkt.ret_6m"),
        Signal("ret_12m", "12-month return", rets.get("12m"),
               band(rets.get("12m"), [(-0.40, 0), (0.0, 40), (0.20, 70), (0.60, 100)]),
               0.15, "mkt.ret_12m"),
        Signal("vs_sma_50", "Price against the 50-day average", vs_50,
               band(vs_50, [(-0.15, 0), (-0.03, 35), (0.03, 70), (0.15, 100)]),
               0.10, "mkt.sma_50"),
        Signal("vs_sma_200", "Price against the 200-day average", vs_200,
               band(vs_200, [(-0.25, 0), (-0.05, 35), (0.08, 70), (0.30, 100)]),
               0.10, "mkt.sma_200"),
    ]
    return _family("momentum", "Momentum", signals)


# ---------------------------------------------------------------------------
# Relative strength: its own family, on purpose
# ---------------------------------------------------------------------------

def relative_strength(ev: Evidence) -> Family:
    """Rising with the sector and leading the sector are different facts, and
    over a 2-6 week hold the difference is most of the edge. A single blended
    momentum number cannot tell them apart, so this stays separate."""
    spy = (ev.relative or {}).get("vs_spy") or {}
    sector = (ev.relative or {}).get("vs_sector") or {}
    sector_spy = (ev.relative or {}).get("sector_vs_spy") or {}
    etf = (ev.relative or {}).get("sector_etf")

    signals = [
        Signal("spy_1m", "1-month excess vs SPY", spy.get("1m"),
               band(spy.get("1m"), [(-0.10, 0), (-0.01, 40), (0.03, 70), (0.12, 100)]),
               0.20, "rel.spy_1m"),
        Signal("spy_3m", "3-month excess vs SPY", spy.get("3m"),
               band(spy.get("3m"), [(-0.18, 0), (-0.02, 40), (0.05, 70), (0.20, 100)]),
               0.25, "rel.spy_3m"),
        Signal("spy_6m", "6-month excess vs SPY", spy.get("6m"),
               band(spy.get("6m"), [(-0.25, 0), (-0.03, 40), (0.08, 70), (0.30, 100)]),
               0.15, "rel.spy_6m"),
        Signal("sector_1m", f"1-month excess vs {etf or 'sector'}", sector.get("1m"),
               band(sector.get("1m"), [(-0.10, 0), (-0.01, 40), (0.03, 70), (0.12, 100)]),
               0.15, "rel.sector_1m"),
        Signal("sector_3m", f"3-month excess vs {etf or 'sector'}", sector.get("3m"),
               band(sector.get("3m"), [(-0.18, 0), (-0.02, 40), (0.05, 70), (0.20, 100)]),
               0.15, "rel.sector_3m"),
        Signal("sector_tailwind", f"{etf or 'Sector'} against SPY, 3 months",
               sector_spy.get("3m"),
               band(sector_spy.get("3m"), [(-0.12, 0), (-0.02, 40), (0.03, 70), (0.12, 100)]),
               0.10, "rel.sector_spy_3m",
               note="Context, not credit: a stock is not good because its sector is"),
    ]
    return _family("relative_strength", "Relative strength", signals)


# ---------------------------------------------------------------------------
# Earnings: the last print and the drift after it
# ---------------------------------------------------------------------------

def earnings(ev: Evidence) -> Family:
    """Post-earnings drift decays. A 10% beat eight weeks ago is history; the
    same beat last week is a live signal, so the weight fades with age rather
    than switching off at an arbitrary cutoff."""
    rel = ev.release or {}
    days = rel.get("days_since")
    freshness = None
    if days is not None:
        freshness = max(0.0, min(1.0, 1.0 - days / 90.0))

    eps_score = band(rel.get("eps_surprise"),
                     [(-0.15, 0), (-0.02, 35), (0.03, 65), (0.20, 100)])
    rev_score = band(rel.get("revenue_surprise"),
                     [(-0.06, 0), (-0.01, 35), (0.01, 65), (0.06, 100)])
    if freshness is not None and freshness < 1.0:
        # Pull a stale surprise toward neutral rather than deleting it.
        eps_score = None if eps_score is None else round(50 + (eps_score - 50) * freshness, 2)
        rev_score = None if rev_score is None else round(50 + (rev_score - 50) * freshness, 2)

    signals = [
        Signal("eps_surprise", "EPS surprise", rel.get("eps_surprise"), eps_score, 0.60,
               "evt.eps_surprise",
               note=(f"{days} days ago, weight faded to {freshness:.0%}"
                     if freshness is not None else None)),
        Signal("revenue_surprise", "Revenue surprise", rel.get("revenue_surprise"), rev_score,
               0.40, "evt.revenue_surprise"),
    ]
    return _family("earnings", "Earnings", signals)


# ---------------------------------------------------------------------------
# Technical: where price sits in its own structure
# ---------------------------------------------------------------------------

def technical(ev: Evidence) -> Family:
    a = ev.anchors or {}
    close = a.get("last_close")
    sma20, sma50, sma200 = a.get("sma_20"), a.get("sma_50"), a.get("sma_200")
    high_52w, low_52w = a.get("high_52w"), a.get("low_52w")

    stack = None
    if close and sma20 and sma50 and sma200:
        # Four ordered conditions, each worth a quarter of the score.
        ordered = [close > sma20, sma20 > sma50, sma50 > sma200, close > sma200]
        stack = round(100.0 * sum(ordered) / len(ordered), 2)

    range_pos = None
    if close and high_52w and low_52w and high_52w > low_52w:
        range_pos = (close - low_52w) / (high_52w - low_52w)

    signals = [
        Signal("trend_stack", "Moving-average structure", stack, stack, 0.35, "mkt.sma_50",
               note="Close over 20 over 50 over 200, one quarter of the score each"),
        Signal("range_position", "Position in the 52-week range", range_pos,
               band(range_pos, [(0.0, 10), (0.35, 40), (0.70, 80), (0.95, 100)]),
               0.25, "mkt.from_52w_high",
               note="Strength near the high, not weakness: this is not a mean-reversion engine"),
        Signal("from_high", "Distance from the 52-week high", a.get("pct_from_52w_high"),
               band(a.get("pct_from_52w_high"), [(-0.40, 0), (-0.15, 45), (-0.05, 80), (0.0, 100)]),
               0.20, "mkt.from_52w_high"),
        Signal("volume_trend", "20-day against 60-day volume", a.get("volume_trend"),
               band(a.get("volume_trend"), [(0.6, 30), (0.9, 50), (1.2, 75), (2.0, 100)]),
               0.20, "mkt.volume_trend",
               note="Participation, which confirms a move without judging its direction"),
    ]
    return _family("technical", "Technical", signals)


# ---------------------------------------------------------------------------
# Risk: higher is worse, always
# ---------------------------------------------------------------------------

def risk(ev: Evidence) -> Family:
    a = ev.anchors or {}
    signals = [
        Signal("volatility", "Annualized volatility", a.get("volatility_annual"),
               band(a.get("volatility_annual"), [(0.15, 0), (0.30, 40), (0.50, 75), (0.90, 100)]),
               0.25, "mkt.volatility"),
        Signal("drawdown", "Worst 1-year drawdown", a.get("max_drawdown_1y"),
               band(a.get("max_drawdown_1y"),
                    [(-0.60, 100), (-0.35, 75), (-0.18, 40), (-0.05, 0)]),
               0.20, "mkt.max_drawdown_1y",
               note="Curve runs deepest-first because drawdowns are negative"),
        Signal("atr_pct", "ATR as a share of price", a.get("atr_pct"),
               band(a.get("atr_pct"), [(0.01, 0), (0.02, 40), (0.035, 75), (0.07, 100)]),
               0.15, "mkt.atr_pct"),
        Signal("leverage", "Debt to equity", _m(ev, "debt_to_equity"),
               band(_m(ev, "debt_to_equity"), [(0.2, 0), (0.8, 40), (1.8, 75), (3.5, 100)]),
               0.20, "fnd.debt_to_equity"),
        Signal("coverage_risk", "Thin interest coverage", _m(ev, "interest_coverage"),
               inverse(_m(ev, "interest_coverage"), [(1.0, 0), (3.0, 40), (8.0, 75), (20.0, 100)]),
               0.20, "fnd.interest_coverage"),
    ]
    return _family("risk", "Risk", signals, direction="higher_riskier")


# ---------------------------------------------------------------------------

BUILDERS = {
    "quality": quality,
    "growth": growth,
    "momentum": momentum,
    "relative_strength": relative_strength,
    "earnings": earnings,
    "technical": technical,
    "risk": risk,
}


def compute(ev: Evidence, panel: dict | None = None) -> dict[str, Family]:
    """Every family for one evidence package, keyed by family name."""
    out = {key: builder(ev) for key, builder in BUILDERS.items()}
    if (ev.profile or {}).get("security_type") == "ETF":
        out["fund"] = fund(ev)
    out["valuation"] = valuation(ev, panel)
    return out
