"""The canonical evidence package: everything one run is allowed to know.

This is the layer the two GitHub projects do not have. There, each engine
fetched its own numbers, so a disagreement could be a difference of judgement
or simply a difference of data, and nobody could tell which. Here every factor,
every level and every sentence the LLM later writes reads from one package,
built once for `(ticker, as_of)`, where each figure carries an id, a source and
the date it is true as of.

Three rules hold the whole thing up:

* **Point in time.** Fundamentals are keyed to SEC filing date, prices stop at
  the last settled close, and nothing current is applied to a historical as_of.
  Consensus is fetched today and is therefore stamped current-only: it is
  excluded outright when the run is historical.
* **Missing is missing.** A figure that cannot be computed is None and its
  factor abstains. It never becomes a zero, and it never becomes a neutral
  score, because a neutral score is an opinion nobody formed.
* **Caveats are graded.** Every data problem carries a severity, and the
  verdict layer turns severities into VALID / DEGRADED / WITHHELD.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

from edgedesk.evidence import forward as forward_mod
from edgedesk.evidence.decision import decision_row, valuation_now
from edgedesk.evidence import sectors
from edgedesk.evidence.anchors import align_closes, compute_anchors, relative_strength
from edgedesk.providers.client import DataClient
from edgedesk.session import last_settled_day

logger = logging.getLogger(__name__)

# Enough history for a 200-day average plus a full year of returns, with slack
# for holidays and halts.
_BAR_LOOKBACK_DAYS = 800
# A quarter's worth of headlines is what a release and the recent narrative need.
_NEWS_LOOKBACK_DAYS = 120
# Below this many bars the technical and momentum families cannot be computed
# at all, which is a withholding condition rather than a caveat.
_MIN_BARS = 60
# Consensus has no history, so it may only describe a run at today's date.
_CONSENSUS_MAX_AGE_DAYS = 7

INFO, DEGRADE, WITHHOLD = "info", "degrade", "withhold"


@dataclass(frozen=True)
class Fact:
    """One cited figure. `id` is what a report or an LLM sentence points at."""

    id: str
    label: str
    value: float | int | str | None
    unit: str            # usd | ratio | pct | price | x | count | date | text
    source: str          # alpaca | edgar | yahoo | benzinga | computed
    as_of: str           # the date this value is true as of
    note: str | None = None


@dataclass(frozen=True)
class Caveat:
    """A data problem, graded. `withhold` means no rating may be published."""

    code: str
    severity: str
    message: str


@dataclass
class Evidence:
    ticker: str
    as_of: str
    generated_at: str
    is_historical: bool
    profile: dict = field(default_factory=dict)
    facts: dict[str, Fact] = field(default_factory=dict)
    bars: list[dict] = field(default_factory=list)
    benchmarks: dict[str, list[dict]] = field(default_factory=dict)
    anchors: dict = field(default_factory=dict)
    relative: dict = field(default_factory=dict)
    metrics: list[dict] = field(default_factory=list)
    valuation_now: dict | None = None
    forward: dict | None = None
    consensus: dict | None = None
    breakdown: dict | None = None
    calendar: dict | None = None
    dividend: dict | None = None
    fund: dict | None = None
    release: dict | None = None
    news: list[dict] = field(default_factory=list)
    caveats: list[Caveat] = field(default_factory=list)

    # ------------------------------------------------------------------

    def add(self, fact: Fact) -> None:
        self.facts[fact.id] = fact

    def value(self, fact_id: str):
        fact = self.facts.get(fact_id)
        return fact.value if fact else None

    def warn(self, code: str, severity: str, message: str) -> None:
        self.caveats.append(Caveat(code, severity, message))

    @property
    def blocking(self) -> list[Caveat]:
        return [c for c in self.caveats if c.severity == WITHHOLD]

    @property
    def degrading(self) -> list[Caveat]:
        return [c for c in self.caveats if c.severity == DEGRADE]

    @property
    def latest_metrics(self) -> dict | None:
        return self.metrics[0] if self.metrics else None

    @property
    def decision_metrics(self) -> dict | None:
        """The newest row with its valuation restated at the decision price.
        Anything that scores or prints a current multiple reads this one."""
        if not self.metrics:
            return None
        return decision_row(self.metrics[0], self.valuation_now)

    def quality_score(self) -> int:
        """0-100, higher is better data. Blocking problems floor it, because a
        withheld rating should never look like a merely imperfect one."""
        if self.blocking:
            return 0
        return max(0, 100 - 12 * len(self.degrading) - 3 * len(
            [c for c in self.caveats if c.severity == INFO]))

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "as_of": self.as_of,
            "generated_at": self.generated_at,
            "is_historical": self.is_historical,
            "profile": self.profile,
            "anchors": self.anchors,
            "relative": self.relative,
            "metrics": self.metrics,
            "valuation_now": self.valuation_now,
            "forward": self.forward,
            "consensus": self.consensus,
            "breakdown": self.breakdown,
            "calendar": self.calendar,
            "dividend": self.dividend,
            "fund": self.fund,
            "release": self.release,
            "news": self.news,
            "facts": {k: asdict(v) for k, v in sorted(self.facts.items())},
            "caveats": [asdict(c) for c in self.caveats],
            "quality_score": self.quality_score(),
            "bar_count": len(self.bars),
        }


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def collect(ticker: str, as_of: date, client: DataClient) -> Evidence:
    """Build the package for one ticker at one date. Never raises on missing
    data: a gap becomes a caveat, and the caller decides what that gap costs."""
    ticker = ticker.upper()
    ev = Evidence(
        ticker=ticker,
        as_of=as_of.isoformat(),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        is_historical=as_of < last_settled_day(),
    )
    _collect_profile(ev, client)
    _collect_market(ev, as_of, client)
    _collect_fund(ev, client)
    _collect_fundamentals(ev, as_of, client)
    _collect_estimates(ev, client)
    _collect_events(ev, as_of, client)
    return ev


def _collect_profile(ev: Evidence, client: DataClient) -> None:
    profile = _point_in_time_profile(client.profile(ev.ticker) or {}, ev.as_of)
    sector, etf = sectors.classify(profile.get("sic"), ev.ticker)
    overridden = ev.ticker in sectors.OVERRIDES
    ev.profile = {**profile, "sector": sector, "sector_etf": etf}
    if profile.get("name"):
        ev.add(Fact("pro.name", "Company", profile["name"], "text", "edgar", ev.as_of))
    if profile.get("sic_description"):
        ev.add(Fact("pro.industry", "Industry", profile["sic_description"], "text",
                    "edgar", ev.as_of, note=f"SIC {profile.get('sic')}"))
    if sector:
        note = (f"benchmark {etf}; mapped by explicit override, because SIC "
                f"{profile.get('sic')} would place it elsewhere" if overridden
                else f"mapped from SIC {profile.get('sic')}, benchmark {etf}")
        ev.add(Fact("pro.sector", "Sector", sector, "text", "computed", ev.as_of, note=note))
    elif profile.get("security_type") != "ETF":
        ev.warn("no_sector", INFO,
                "No sector could be mapped from the SIC code; relative strength "
                "is measured against the market only.")


def _point_in_time_profile(profile: dict, as_of: str) -> dict:
    """The SEC submissions index is fetched as it stands today, not as of a date.

    Name, SIC and exchange barely move, so serving today's copy is fine. The
    newest filing on the index does move, and storing one filed after the as-of
    date would put a fact from the future inside a historical run, where the
    freshness check and the event check both read it. It is dropped instead.
    """
    result = dict(profile)
    report = result.get("latest_report") or {}
    if report and (report.get("filed") or "") > as_of:
        result["latest_report"] = None
        result["latest_report_note"] = (
            f"The newest filing on the SEC index was filed {report.get('filed')}, "
            f"after this run's as-of date, so it was excluded.")
    foreign = result.get("latest_foreign_report") or {}
    if foreign and (foreign.get("filed") or "") > as_of:
        result["latest_foreign_report"] = None
    return result


def _collect_market(ev: Evidence, as_of: date, client: DataClient) -> None:
    start = as_of - timedelta(days=_BAR_LOOKBACK_DAYS)
    try:
        ev.bars = client.daily_closes(ev.ticker, start, as_of)
    except Exception as exc:                       # noqa: BLE001 - provider failure is a caveat
        ev.warn("no_prices", WITHHOLD, f"Price history could not be fetched: {exc}")
        return
    source = getattr(getattr(client, "alpaca", None), "source", None)
    if source == "yahoo":
        from edgedesk.providers.alpaca import credential_help
        ev.warn("prices_from_yahoo", INFO,
                "No complete Alpaca credential pair was found, so prices come from Yahoo and "
                "Benzinga news is unavailable. Checked: " + credential_help() + ".")
    elif source == "alpaca-iex":
        ev.warn("prices_from_iex", INFO,
                "Prices come from Alpaca's IEX feed (this account has no SIP data): volume is a "
                "fraction of the full tape, so volume-based signals read low.")
    if len(ev.bars) < _MIN_BARS:
        ev.warn("short_history", WITHHOLD,
                f"Only {len(ev.bars)} daily bars available (need {_MIN_BARS}); the "
                "momentum and technical factors cannot be computed.")
        if not ev.bars:
            return

    for fix in (getattr(client, "split_repairs", None) or {}).get(ev.ticker, []):
        ev.warn("split_repaired", INFO,
                f"The price feed left the {fix['ex_date']} {fix['ratio']:g}-for-1 split "
                f"unadjusted; {fix['bars_restated']} earlier bars were restated here.")

    ev.anchors = compute_anchors(ev.bars)
    a = ev.anchors
    price_facts = [
        ("mkt.last_close", "Last close", a.get("last_close"), "price"),
        ("mkt.sma_20", "20-day average", a.get("sma_20"), "price"),
        ("mkt.sma_50", "50-day average", a.get("sma_50"), "price"),
        ("mkt.sma_200", "200-day average", a.get("sma_200"), "price"),
        ("mkt.high_52w", "52-week high", a.get("high_52w"), "price"),
        ("mkt.low_52w", "52-week low", a.get("low_52w"), "price"),
        ("mkt.atr_14", "14-day ATR", a.get("atr_14"), "price"),
        ("mkt.atr_pct", "ATR as % of price", a.get("atr_pct"), "pct"),
        ("mkt.from_52w_high", "Distance from 52-week high", a.get("pct_from_52w_high"), "pct"),
        ("mkt.volatility", "Annualized volatility", a.get("volatility_annual"), "pct"),
        ("mkt.max_drawdown_1y", "Worst 1-year drawdown", a.get("max_drawdown_1y"), "pct"),
        ("mkt.volume_trend", "20d vs 60d volume", a.get("volume_trend"), "x"),
    ]
    for fid, label, value, unit in price_facts:
        if value is not None:
            ev.add(Fact(fid, label, value, unit, "alpaca", a.get("as_of", ev.as_of)))
    for window, value in (a.get("returns") or {}).items():
        if value is not None:
            ev.add(Fact(f"mkt.ret_{window}", f"{window} return", value, "pct", "alpaca",
                        a.get("as_of", ev.as_of)))

    _collect_relative(ev, as_of, client)


def _collect_relative(ev: Evidence, as_of: date, client: DataClient) -> None:
    """Stock vs market, stock vs sector, and sector vs market.

    Kept as its own family rather than folded into momentum: over a 2-6 week
    hold it matters a great deal whether a stock is rising with its sector or
    genuinely leading it, and one blended momentum number cannot say which.
    """
    start = as_of - timedelta(days=_BAR_LOOKBACK_DAYS)
    etf = ev.profile.get("sector_etf")
    wanted = [sectors.SPY] + ([etf] if etf else [])
    for symbol in wanted:
        try:
            ev.benchmarks[symbol] = client.daily_closes(symbol, start, as_of)
        except Exception as exc:                   # noqa: BLE001
            logger.info("benchmark %s unavailable: %s", symbol, exc)

    spy = ev.benchmarks.get(sectors.SPY) or []
    if not spy:
        ev.warn("no_benchmark", DEGRADE,
                "SPY history was unavailable, so relative strength could not be measured.")
        return

    stock_closes, spy_closes = align_closes(ev.bars, spy)
    ev.relative["vs_spy"] = relative_strength(stock_closes, spy_closes)
    for window, value in ev.relative["vs_spy"].items():
        if value is not None:
            ev.add(Fact(f"rel.spy_{window}", f"{window} excess return vs SPY", value,
                        "pct", "computed", ev.as_of))

    sector_bars = ev.benchmarks.get(etf) if etf else None
    if sector_bars:
        s_closes, sec_closes = align_closes(ev.bars, sector_bars)
        ev.relative["vs_sector"] = relative_strength(s_closes, sec_closes)
        sec_vs_spy_a, sec_vs_spy_b = align_closes(sector_bars, spy)
        ev.relative["sector_vs_spy"] = relative_strength(sec_vs_spy_a, sec_vs_spy_b)
        ev.relative["sector_etf"] = etf
        for window, value in ev.relative["vs_sector"].items():
            if value is not None:
                ev.add(Fact(f"rel.sector_{window}", f"{window} excess return vs {etf}",
                            value, "pct", "computed", ev.as_of))
        for window, value in ev.relative["sector_vs_spy"].items():
            if value is not None:
                ev.add(Fact(f"rel.sector_spy_{window}", f"{window} {etf} vs SPY", value,
                            "pct", "computed", ev.as_of))
    elif etf:
        ev.warn("no_sector_prices", DEGRADE,
                f"{etf} history was unavailable, so sector-relative strength is missing.")


def _collect_fundamentals(ev: Evidence, as_of: date, client: DataClient) -> None:
    if (ev.profile or {}).get("security_type") == "ETF":
        return
    try:
        rows = client.metrics(ev.ticker, as_of, limit=12)
    except Exception as exc:                       # noqa: BLE001
        ev.warn("no_fundamentals", WITHHOLD, f"EDGAR fundamentals failed: {exc}")
        return
    if not rows:
        foreign = (ev.profile or {}).get("latest_foreign_report") or {}
        if foreign:
            detail = (f"SEC identifies this as a foreign filer; its latest annual report is a "
                      f"{foreign.get('form')} filed {foreign.get('filed')}. No complete TTM row "
                      "could be normalized from its standard US GAAP 20-F/6-K facts. The filing "
                      "may use unsupported tags or an IFRS-only taxonomy; missing values were not "
                      "treated as zero.")
        else:
            detail = ("SEC EDGAR returned no usable US GAAP quarterly facts. This can mean a "
                      "foreign 20-F/40-F filer, a newly listed company, or incomplete XBRL. "
                      "Edge Desk withheld fundamentals rather than treating missing facts as zero.")
        ev.warn("no_gaap_fundamentals", WITHHOLD, detail)
        return

    ev.metrics = [r.model_dump() for r in rows]
    foreign = (ev.profile or {}).get("latest_foreign_report") or {}
    if foreign:
        ev.warn("foreign_filer_normalized", DEGRADE,
                f"Fundamentals were normalized from standard US GAAP facts in "
                f"{foreign.get('form')} and financial 6-K filings. Interim 6-K disclosure is less "
                "uniform than a 10-Q, so a supported rating has reduced data confidence.")
        try:
            ratio = client.ads_price_ratio(ev.ticker, as_of)
        except Exception:  # noqa: BLE001
            ratio = None
        if ratio is None or not 0.5 <= ratio <= 2.0:
            ev.warn("ads_ratio_unresolved", WITHHOLD,
                    "ADS-to-ordinary share ratio could not be confirmed from a filed public "
                    "float, the same filing's ordinary-share count, and the float-date close; "
                    "per-share and valuation figures may be scaled wrong.")
    latest = ev.metrics[0]
    _check_freshness(ev, latest)
    _check_shares(ev, latest)
    _collect_valuation_now(ev, as_of, client, latest)
    filing_row, latest = latest, ev.decision_metrics

    _collect_dividend(ev, latest)
    stamp = latest.get("filing_date") or latest.get("report_period") or ev.as_of
    note = f"TTM through {latest.get('report_period')}, filed {latest.get('filing_date')}"
    if (ev.valuation_now or {}).get("available"):
        note += f"; price-based ratios restated at the {ev.as_of} close"
        if filing_row.get("price_to_earnings_ratio") is not None:
            ev.add(Fact("fnd.pe_at_filing", "P/E on the filing date",
                        filing_row["price_to_earnings_ratio"], "x", "edgar",
                        filing_row.get("filing_date") or ev.as_of,
                        note="History only. The current multiple is fnd.pe."))
    for fid, label, key, unit in (
        ("fnd.market_cap", "Market cap", "market_cap", "usd"),
        ("fnd.pe", "P/E (TTM)", "price_to_earnings_ratio", "x"),
        ("fnd.peg", "PEG", "peg_ratio", "x"),
        ("fnd.ps", "P/S", "price_to_sales_ratio", "x"),
        ("fnd.pb", "P/B", "price_to_book_ratio", "x"),
        ("fnd.ev_ebitda", "EV/EBITDA", "enterprise_value_to_ebitda_ratio", "x"),
        ("fnd.fcf_yield", "Free cash flow yield", "free_cash_flow_yield", "pct"),
        ("fnd.gross_margin", "Gross margin", "gross_margin", "pct"),
        ("fnd.operating_margin", "Operating margin", "operating_margin", "pct"),
        ("fnd.net_margin", "Net margin", "net_margin", "pct"),
        ("fnd.roe", "Return on equity", "return_on_equity", "pct"),
        ("fnd.roa", "Return on assets", "return_on_assets", "pct"),
        ("fnd.current_ratio", "Current ratio", "current_ratio", "x"),
        ("fnd.debt_to_equity", "Debt to equity", "debt_to_equity", "x"),
        ("fnd.interest_coverage", "Interest coverage", "interest_coverage", "x"),
        ("fnd.revenue_growth", "Revenue growth (YoY)", "revenue_growth", "pct"),
        ("fnd.earnings_growth", "Earnings growth (YoY)", "earnings_growth", "pct"),
        ("fnd.fcf_growth", "Free cash flow growth (YoY)", "free_cash_flow_growth", "pct"),
        ("fnd.eps", "EPS (TTM)", "earnings_per_share", "usd"),
    ):
        if latest.get(key) is not None:
            ev.add(Fact(fid, label, latest[key], unit, "edgar", stamp, note=note))


def _collect_fund(ev: Evidence, client: DataClient) -> None:
    """Collect current fund structure for ETFs; company accounts do not apply."""
    if (ev.profile or {}).get("security_type") != "ETF":
        return
    if ev.is_historical:
        ev.warn("fund_data_excluded", INFO,
                "Current ETF holdings and fee data were excluded from this historical run "
                "because the free source has no point-in-time history.")
        return
    try:
        fund = client.fund_data(ev.ticker)
    except Exception as exc:                       # noqa: BLE001
        ev.warn("no_fund_data", WITHHOLD, f"ETF structure data could not be fetched: {exc}")
        return
    if not fund:
        ev.warn("no_fund_data", WITHHOLD,
                "The security is an ETF, but no fund structure data were available; a company "
                "fundamentals rating would be the wrong substitute.")
        return
    ev.fund = fund
    stock = fund.get("stock_position")
    if stock is None or stock < 0.50:
        ev.warn("unsupported_etf_type", WITHHOLD,
                "This ETF is not primarily an equity fund. Edge Desk's ETF model currently "
                "supports equity ETFs only; bond, commodity, cash and multi-asset funds need "
                "different benchmarks and risk factors.")
        return
    ev.warn("etf_methodology", DEGRADE,
            "ETF methodology used: the rating is based on fund structure, fees, concentration, "
            "market trend and performance relative to SPY. Company GAAP fundamentals do not "
            "apply. This model does not assess distribution sustainability or treat a high "
            "distribution rate as investment return.")
    top = fund.get("top_holdings") or []
    top_weight = sum((h.get("weight") or 0.0) for h in top)
    stamp = fund.get("as_of") or ev.as_of
    for fid, label, value, unit in (
        ("fund.expense_ratio", "Expense ratio", fund.get("expense_ratio"), "pct"),
        ("fund.category_expense_ratio", "Category-average expense ratio",
         fund.get("category_expense_ratio"), "pct"),
        ("fund.total_assets", "Fund net assets", fund.get("total_assets"), "usd"),
        ("fund.nav_price", "Net asset value", fund.get("nav_price"), "price"),
        ("fund.premium_discount", "Market price premium/discount to NAV",
         fund.get("premium_discount"), "pct"),
        ("fund.stock_position", "Equity allocation", stock, "pct"),
        ("fund.top_holdings_weight", "Reported top-holdings concentration", top_weight, "pct"),
    ):
        if value is not None:
            ev.add(Fact(fid, label, value, unit, "yahoo", stamp))


def _collect_valuation_now(ev: Evidence, as_of: date, client: DataClient,
                           latest: dict) -> None:
    """Restate the newest row's valuation at the decision price.

    The raw close is asked for explicitly. The adjusted series is only a
    fallback for a client that cannot supply one, and it is only safe when the
    bars end at the as-of date, which is when adjusted and raw agree.
    """
    price, basis = None, "unadjusted close at the as-of date"
    fetch = getattr(client, "raw_close", None)
    if fetch is not None:
        try:
            price = fetch(ev.ticker, as_of)
        except Exception as exc:                   # noqa: BLE001
            logger.info("raw close for %s unavailable: %s", ev.ticker, exc)
    if price is None:
        price = (ev.anchors or {}).get("last_close")
        basis = "last close of the adjusted series (no unadjusted close available)"
    ev.valuation_now = valuation_now(latest, price, basis, ev.as_of)
    if not (ev.valuation_now or {}).get("available"):
        ev.warn("valuation_at_filing_price", DEGRADE,
                "Current valuation could not be restated at today's price "
                f"({(ev.valuation_now or {}).get('why')}), so the multiples shown are "
                f"as of the filing date {latest.get('filing_date')}.")


def _collect_dividend(ev: Evidence, latest: dict) -> None:
    """Trailing dividend yield, derived from filings rather than fetched.

    payout ratio is dividends over net income and P/E is market cap over net
    income, so payout / P/E is dividends over market cap, which is the yield.
    Deriving it keeps the figure point-in-time and consistent with every other
    number on the card; Yahoo is only a fallback, and only for a current run.
    """
    payout, pe = latest.get("payout_ratio"), latest.get("price_to_earnings_ratio")
    if payout is not None and pe:
        ev.dividend = {"yield": round(payout / pe, 6), "payout_ratio": round(payout, 4),
                       "source": "edgar", "as_of": latest.get("report_period")}
        ev.add(Fact("fnd.dividend_yield", "Dividend yield", ev.dividend["yield"], "pct",
                    "computed", ev.dividend["as_of"] or ev.as_of,
                    note="payout ratio divided by P/E, both from the same filing"))
    elif payout is None and pe:
        # No dividends line in the filing means no dividend, which is a fact.
        ev.dividend = {"yield": 0.0, "payout_ratio": 0.0, "source": "edgar",
                       "as_of": latest.get("report_period"),
                       "note": "no dividends reported in the filings on file"}


def _check_freshness(ev: Evidence, latest: dict) -> None:
    """The companyfacts feed can lag the filing index by weeks. Rating a company
    on last quarter's numbers when this quarter is already public is exactly the
    silent error this engine exists to stop, so it withholds."""
    report = (ev.profile or {}).get("latest_report") or {}
    filed_period, have_period = report.get("period"), latest.get("report_period")
    if not filed_period or not have_period:
        return
    if filed_period > have_period and report.get("filed", "") <= ev.as_of:
        ev.warn("companyfacts_stale", WITHHOLD,
                f"A {report.get('form')} for the period ending {filed_period} was filed on "
                f"{report.get('filed')}, but EDGAR's companyfacts feed still stops at "
                f"{have_period}. Rating on superseded fundamentals is not honest; "
                "the feed usually catches up within a few weeks.")


def _check_shares(ev: Evidence, latest: dict) -> None:
    """Without a reliable share count, every per-share and valuation figure is
    invalid: P/E, PEG, market cap and EPS all rest on it."""
    if latest.get("market_cap") is None:
        ev.warn("shares_unresolved", WITHHOLD,
                "Shares outstanding could not be determined (multi-class filers report "
                "per class, and the net income / diluted EPS and public-float fallbacks "
                "both failed), so market cap and every per-share valuation are invalid.")


def _collect_estimates(ev: Evidence, client: DataClient) -> None:
    """Consensus is current-only. It describes today, so a historical run must
    not see it at all: applying today's targets to a date two months back is the
    cleanest way to manufacture a result that could never have existed."""
    if (ev.profile or {}).get("security_type") == "ETF":
        return
    if ev.is_historical:
        ev.warn("consensus_excluded", INFO,
                "This run is historical, so current analyst consensus was excluded "
                "rather than applied backward.")
        return
    consensus = client.consensus(ev.ticker)
    if consensus is None:
        ev.warn("no_consensus", DEGRADE,
                "No analyst consensus was available, so Street targets and the "
                "recommendation mean are missing.")
        return
    ev.consensus = consensus.model_dump()
    age = (date.today() - date.fromisoformat(ev.consensus["fetched_on"])).days
    if age > _CONSENSUS_MAX_AGE_DAYS:
        ev.warn("consensus_stale", DEGRADE,
                f"Analyst consensus was fetched {age} days ago.")

    # How the analysts split, not just where their mean landed.
    ev.breakdown = client.analyst_breakdown(ev.ticker)
    if ev.breakdown:
        b = ev.breakdown
        ev.add(Fact("est.bullish", "Analysts bullish", b["bullish"], "count", "yahoo",
                    ev.consensus["fetched_on"], note="strong buy plus buy"))
        ev.add(Fact("est.neutral", "Analysts neutral", b["neutral"], "count", "yahoo",
                    ev.consensus["fetched_on"]))
        ev.add(Fact("est.bearish", "Analysts bearish", b["bearish"], "count", "yahoo",
                    ev.consensus["fetched_on"], note="sell plus strong sell"))

    # Forward earnings and how they have been revised. Current-only, like the rest.
    fetch = getattr(client, "forward_estimates", None)
    if fetch is not None:
        try:
            ev.forward = forward_mod.read(fetch(ev.ticker), ev.valuation_now, ev.anchors)
        except Exception as exc:                   # noqa: BLE001
            logger.info("forward estimates failed for %s: %s", ev.ticker, exc)
    if ev.forward:
        for fid, label, key, unit in (
                ("est.forward_pe", "Forward P/E (next fiscal year, adjusted EPS)", "forward_pe", "x"),
                ("est.eps_next_year", "Consensus EPS, next fiscal year", "eps_next_year", "usd"),
                ("est.revenue_growth_next_year", "Consensus revenue growth, next fiscal year",
                 "revenue_growth_next_year", "pct"),
                ("est.eps_revision_30d", "Next-year EPS estimate, 30-day change",
                 "revision_30d", "pct"),
                ("est.eps_revision_90d", "Next-year EPS estimate, 90-day change",
                 "revision_90d", "pct")):
            if ev.forward.get(key) is not None:
                ev.add(Fact(fid, label, ev.forward[key], unit, "yahoo", ev.forward["fetched_on"]))

    # The confirmed next earnings date, which beats estimating it from the
    # reporting rhythm. Current-only, so historical runs never see it.
    ev.calendar = client.calendar(ev.ticker)
    if ev.calendar and ev.calendar.get("next_earnings"):
        ev.add(Fact("evt.next_earnings", "Next earnings", ev.calendar["next_earnings"],
                    "date", "yahoo", ev.consensus["fetched_on"],
                    note="as announced; Yahoo marks unconfirmed dates as estimates"))
    if ev.calendar and ev.calendar.get("eps_estimate") is not None:
        ev.add(Fact("est.next_eps", "Consensus EPS, next report",
                    ev.calendar["eps_estimate"], "usd", "yahoo",
                    ev.consensus["fetched_on"]))
    for fid, label, key, unit in (
        ("est.target_mean", "Street target (mean)", "target_mean_price", "price"),
        ("est.target_high", "Street target (high)", "target_high_price", "price"),
        ("est.target_low", "Street target (low)", "target_low_price", "price"),
        ("est.rec_mean", "Recommendation mean (1 buy to 5 sell)", "recommendation_mean", "x"),
        ("est.analysts", "Analysts covering", "analyst_count", "count"),
    ):
        if ev.consensus.get(key) is not None:
            ev.add(Fact(fid, label, ev.consensus[key], unit, "yahoo",
                        ev.consensus["fetched_on"], note="current-only, no history"))


def _collect_events(ev: Evidence, as_of: date, client: DataClient) -> None:
    start = as_of - timedelta(days=_NEWS_LOOKBACK_DAYS)
    try:
        ev.news = [n.model_dump() for n in client.news(ev.ticker, as_of, start, limit=40)]
    except Exception as exc:                       # noqa: BLE001
        ev.warn("no_news", INFO, f"News headlines were unavailable: {exc}")
    if (ev.profile or {}).get("security_type") == "ETF":
        return
    try:
        release = client.latest_release(ev.ticker, start, as_of)
    except Exception as exc:                       # noqa: BLE001
        logger.info("earnings release lookup failed for %s: %s", ev.ticker, exc)
        release = None
    if release is None:
        alpaca = getattr(client, "alpaca", None)
        has_news_source = alpaca.has_keys() if alpaca and hasattr(alpaca, "has_keys") else None
        if has_news_source is False:
            ev.warn("no_release_source", INFO,
                    "The earnings-release search was skipped because Alpaca/Benzinga credentials "
                    "were unavailable. This is not evidence that no release occurred; the "
                    "post-earnings drift factor abstains.")
        else:
            ev.warn("no_release", INFO,
                    "Alpaca/Benzinga was searched, but no earnings release headline was found in "
                    f"the last {_NEWS_LOOKBACK_DAYS} days, so the post-earnings drift factor abstains.")
        return

    eps_surprise = ((release.eps - release.eps_estimate) / abs(release.eps_estimate)
                    if release.eps_estimate else None)
    rev_surprise = ((release.revenue - release.revenue_estimate) / abs(release.revenue_estimate)
                    if release.revenue and release.revenue_estimate else None)
    ev.release = {
        "date": release.day.isoformat(),
        "eps": release.eps,
        "eps_estimate": release.eps_estimate,
        "eps_surprise": round(eps_surprise, 6) if eps_surprise is not None else None,
        "revenue": release.revenue,
        "revenue_estimate": release.revenue_estimate,
        "revenue_surprise": round(rev_surprise, 6) if rev_surprise is not None else None,
        "days_since": (as_of - release.day).days,
    }
    ev.add(Fact("evt.release_date", "Last earnings release", ev.release["date"], "date",
                "benzinga", ev.release["date"]))
    if eps_surprise is not None:
        ev.add(Fact("evt.eps_surprise", "EPS surprise", ev.release["eps_surprise"], "pct",
                    "benzinga", ev.release["date"],
                    note=f"reported {release.eps} vs {release.eps_estimate} consensus"))
    if rev_surprise is not None:
        ev.add(Fact("evt.revenue_surprise", "Revenue surprise", ev.release["revenue_surprise"],
                    "pct", "benzinga", ev.release["date"]))
