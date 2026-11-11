"""SEC EDGAR fundamentals: free, point-in-time trailing-twelve-month metrics.

EDGAR's XBRL companyfacts API carries every number a US filer has reported
since ~2009, each stamped with the date it was filed. That stamp is what makes
it a stand-in for Financial Datasets' point-in-time metrics: as of any date we
use only facts filed by then, and a period only exists once its 10-Q/10-K is
public.

TTM values are built from what 10-Qs actually report, year-to-date figures:

    TTM(E) = YTD(E) + prior fiscal year - prior-year YTD of the same length

and at a fiscal year end the 10-K figure is the TTM itself. One identity covers
income-statement and cash-flow lines alike (cash flow is only reported YTD) and
tolerates 52/53-week fiscal calendars through day-count windows.

Known approximations, all documented where they happen: total debt is summed
from the common debt tags; multi-class filers (Visa, Alphabet) report share
counts only per class, so shares fall back to net income / diluted EPS and then
to the annual public float / that day's price. Foreign filers using standard
US-GAAP tags are normalized from annual 20-F/40-F and financial 6-K facts; a
6-K creates a period only when it contains a core statement fact, so ordinary
news and shareholder-communication 6-Ks do not become financial quarters.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import requests

from edgedesk.providers._common import MISS, FreeDataError, Throttle, TTLMemo, dash_symbol
from edgedesk.models import FinancialMetrics
from edgedesk.paths import CACHE_DIR

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
USER_AGENT_ENV = "SEC_USER_AGENT"

DEFAULT_CACHE_DIR = CACHE_DIR / "edgar"
_FACTS_TTL_SECONDS = 12 * 3600  # new filings land a few times a quarter
_THROTTLE = Throttle(0.15)  # SEC fair access: at most 10 requests/second
_COMPANIES = TTLMemo(_FACTS_TTL_SECONDS)
_PROFILES = TTLMemo(_FACTS_TTL_SECONDS)
_CIKS = TTLMemo(7 * 24 * 3600)
_CIK_FILE_TTL_SECONDS = 30 * 24 * 3600  # a ticker's CIK almost never changes
_RETRY_DELAYS = (1, 3, 8)  # SEC's search endpoint throws the odd 500
_CIK_LOCK = threading.Lock()

# A period exists once one of these is filed. Proxy and registration
# statements repeat old figures months later; counting them would re-date
# facts that were public long before.
_DOMESTIC_REPORT_FORMS = {"10-Q", "10-K", "10-QT", "10-KT"}
_FOREIGN_REPORT_FORMS = {"20-F", "40-F"}
_FOREIGN_INTERIM_FORMS = {"6-K"}
_REPORT_FORMS = _DOMESTIC_REPORT_FORMS | _FOREIGN_REPORT_FORMS | _FOREIGN_INTERIM_FORMS
_VALUE_FORMS = _REPORT_FORMS | {f"{form}/A" for form in _REPORT_FORMS}

_ANNUAL_DAYS = (350, 380)
_YTD_DAYS = (80, 290)
_TOLERANCE_DAYS = 15

# Concept fallbacks, broadest total first. Filers change tags over time (ASC
# 606 moved most revenue to RevenueFromContractWithCustomer... in 2018), so a
# period takes its value from the first concept that reports it.
DURATION_CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RevenuesNetOfInterestExpense",
    ],
    "cost_of_revenue": [
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold", "CostOfServices",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": [
        "NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "depreciation": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"],
    "dividends": ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"],
    "stock_compensation": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock", "PaymentsForRepurchaseOfEquity"],
    "income_tax": ["IncomeTaxExpenseBenefit"],
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ],
}

INSTANT_CONCEPTS: dict[str, list[str]] = {
    "assets": ["Assets"],
    "current_assets": ["AssetsCurrent"],
    "current_liabilities": ["LiabilitiesCurrent"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "Cash",
    ],
    "inventory": ["InventoryNet"],
    "receivables": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    # LongTermDebt includes current maturities; the split tags do not.
    "debt_total_long_term": ["LongTermDebt"],
    "debt_noncurrent": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "debt_current": [
        "LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
    ],
    "short_term_borrowings": ["ShortTermBorrowings", "CommercialPaper"],
    # Convertible notes are often tagged on their own, with no LongTermDebt total (AKAM: $7.56B in
    # ConvertibleLongTermNotesPayable + ConvertibleNotesPayableCurrent, read as no debt at all).
    # Marketable securities are cash for net debt (AKAM: $3.1B beside $1.48B of cash).
    "investments_current": ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                            "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "investments_noncurrent": ["MarketableSecuritiesNoncurrent",
                               "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"],
    "convertible_debt_total": ["ConvertibleNotesPayable", "ConvertibleDebt"],
    "convertible_debt_noncurrent": ["ConvertibleLongTermNotesPayable", "ConvertibleDebtNoncurrent"],
    "convertible_debt_current": ["ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent"],
    "shares_outstanding": ["CommonStockSharesOutstanding"],
}

WEIGHTED_SHARE_CONCEPTS = [
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
]
EPS_CONCEPTS = ["EarningsPerShareDiluted", "EarningsPerShareBasic"]
# A public float older than this is too stale to stand in for a share count.
_FLOAT_MAX_AGE_DAYS = 460

# ---------------------------------------------------------------------------
# Parsed filings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fact:
    start: date | None  # None for balance-sheet (instant) facts
    end: date
    val: float
    filed: date
    form: str
    accn: str


@dataclass(frozen=True)
class ReportPeriod:
    """A fiscal quarter or year end, and the 10-Q/10-K that made it public."""

    end: date
    filed: date
    accn: str


@dataclass
class Company:
    cik: int
    facts: dict[str, list[Fact]] = field(default_factory=dict)
    cover_shares: list[Fact] = field(default_factory=list)
    public_float: list[Fact] = field(default_factory=list)
    periods: list[ReportPeriod] = field(default_factory=list)  # newest first


def parse_companyfacts(payload: dict) -> Company:
    """Turn a companyfacts JSON document into Facts and report periods."""
    company = Company(cik=int(payload.get("cik") or 0))
    gaap = payload.get("facts", {}).get("us-gaap", {})
    wanted = {c for cs in (*DURATION_CONCEPTS.values(), *INSTANT_CONCEPTS.values()) for c in cs}
    wanted.update(WEIGHTED_SHARE_CONCEPTS)
    wanted.update(EPS_CONCEPTS)

    # accn -> (filed, latest period end reported by that filing)
    report_ends: dict[str, tuple[date, date]] = {}
    for concept, body in gaap.items():
        if concept not in wanted:
            continue
        units = body.get("units", {})
        rows = units.get("USD") or units.get("shares") or units.get("USD/shares") or []
        facts = [f for f in (_to_fact(r) for r in rows) if f is not None]
        company.facts[concept] = facts
        if concept in ("Assets", "NetIncomeLoss", "StockholdersEquity"):
            for f in facts:
                if f.form in _REPORT_FORMS:
                    filed, end = report_ends.get(f.accn, (f.filed, f.end))
                    report_ends[f.accn] = (min(filed, f.filed), max(end, f.end))

    dei = payload.get("facts", {}).get("dei", {})
    shares = dei.get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    company.cover_shares = [f for f in (_to_fact(r) for r in shares) if f is not None]
    public_float = dei.get("EntityPublicFloat", {}).get("units", {}).get("USD", [])
    company.public_float = [f for f in (_to_fact(r) for r in public_float) if f is not None]

    # One period per fiscal end, dated by the first filing that reported it.
    first: dict[date, ReportPeriod] = {}
    for accn, (filed, end) in report_ends.items():
        current = first.get(end)
        if current is None or filed < current.filed:
            first[end] = ReportPeriod(end=end, filed=filed, accn=accn)
    company.periods = sorted(first.values(), key=lambda p: p.end, reverse=True)
    return company


def _to_fact(row: dict) -> Fact | None:
    try:
        return Fact(
            start=date.fromisoformat(row["start"]) if row.get("start") else None,
            end=date.fromisoformat(row["end"]),
            val=float(row["val"]),
            filed=date.fromisoformat(row["filed"]),
            form=row.get("form") or "",
            accn=row.get("accn") or "",
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Point-in-time values
# ---------------------------------------------------------------------------

Values = dict[tuple[date | None, date], float]


def pit_values(company: Company, concepts: list[str], as_of: date) -> Values:
    """(start, end) -> value, using only facts filed by *as_of*.

    Within a concept, a later filing's figure for the same period wins
    (restatements are real information once public). Across concepts, the
    first concept that reports a period wins.
    """
    merged: Values = {}
    for concept in concepts:
        latest: dict[tuple[date | None, date], Fact] = {}
        for f in company.facts.get(concept, []):
            if f.filed > as_of or f.form not in _VALUE_FORMS:
                continue
            key = (f.start, f.end)
            if key not in latest or f.filed >= latest[key].filed:
                latest[key] = f
        for key, f in latest.items():
            merged.setdefault(key, f.val)
    return merged


def ttm(values: Values, end: date) -> float | None:
    """Trailing-twelve-month value ending at *end* (see module docstring)."""
    durations = [(s, e, v) for (s, e), v in values.items() if s is not None]

    for s, e, v in durations:
        if e == end and _ANNUAL_DAYS[0] <= (e - s).days <= _ANNUAL_DAYS[1]:
            return v

    ytd = [(s, v) for s, e, v in durations
           if e == end and _YTD_DAYS[0] <= (e - s).days <= _YTD_DAYS[1]]
    if not ytd:
        return None
    ytd_start, ytd_val = min(ytd, key=lambda sv: sv[0])  # longest = year to date
    length = (end - ytd_start).days

    prior_years = [(s, v) for s, e, v in durations
                   if _ANNUAL_DAYS[0] <= (e - s).days <= _ANNUAL_DAYS[1]
                   and 1 <= (ytd_start - e).days <= 7]
    if not prior_years:
        return None
    fy_start, fy_val = prior_years[0]

    prior_ytd = [v for s, e, v in durations
                 if abs((s - fy_start).days) <= 7
                 and abs((e - s).days - length) <= _TOLERANCE_DAYS]
    if not prior_ytd:
        return None
    return ytd_val + fy_val - prior_ytd[0]


def latest_duration(values: Values, end: date) -> float | None:
    """The shortest-period value ending at *end* (share counts are averages,
    not sums, so the quarter's own figure is the right one)."""
    ending = [(e - s, v) for (s, e), v in values.items() if s is not None and e == end]
    return min(ending, key=lambda dv: dv[0])[1] if ending else None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def build_metrics(
    ticker: str,
    company: Company,
    as_of: date,
    limit: int,
    price_on: Callable[[date], float | None],
    split_factor: Callable[[date, date], float],
) -> list[FinancialMetrics]:
    """TTM FinancialMetrics rows public by *as_of*, newest first.

    *price_on(d)* is the unadjusted close on or just before d; market cap is
    shares as filed times the price the day the filing went public.
    *split_factor(after, through)* is the product of share splits in that
    window; it moves per-share figures onto the as-of share basis so a split
    does not read as a 90% earnings collapse.
    """
    periods = [p for p in company.periods if p.filed <= as_of]
    if not periods:
        return []

    values = {name: pit_values(company, concepts, as_of)
              for name, concepts in {**DURATION_CONCEPTS, **INSTANT_CONCEPTS}.items()}
    weighted = pit_values(company, WEIGHTED_SHARE_CONCEPTS, as_of)
    diluted = pit_values(company, WEIGHTED_SHARE_CONCEPTS[:1], as_of)
    eps = pit_values(company, EPS_CONCEPTS, as_of)

    by_end = {p.end: p for p in periods}
    raw: dict[date, dict] = {}

    def raw_for(end: date) -> dict:
        if end not in raw:
            r = _period_raw(values, weighted, eps, company, by_end[end], as_of)
            # Dilution is measured on diluted shares: convertibles and warrants show up there and
            # not in the outstanding count (AKAM: diluted +5.8% in the year to June 2026 on
            # convertible dilution, while the outstanding count fell).
            r["diluted_shares"] = latest_duration(diluted, end)
            if r["shares"] is None:
                r["shares"] = _shares_from_float(company, by_end[end], as_of, price_on, split_factor)
            raw[end] = r
        return raw[end]

    rows: list[FinancialMetrics] = []
    for p in periods[:limit]:
        prior_end = _year_earlier(by_end, p.end)
        prev = raw_for(prior_end) if prior_end is not None else None
        rows.append(_to_metrics(ticker, p, raw_for(p.end), prev, price_on(p.filed),
                                split_factor(p.filed, as_of)))
    return rows


def derive_debt(r: dict) -> None:
    """Total debt, whether it is unresolved, and cash plus marketable securities, from one
    period's raw tag values (in place)."""
    # Approximation: total debt from the common tags. LongTermDebt already
    # includes current maturities, so it replaces the noncurrent+current pair.
    long_term = r["debt_total_long_term"]
    if long_term is None and (r["debt_noncurrent"] is not None or r["debt_current"] is not None):
        long_term = (r["debt_noncurrent"] or 0.0) + (r["debt_current"] or 0.0)
    if long_term is None:
        long_term = r["convertible_debt_total"]
    if long_term is None and (r["convertible_debt_noncurrent"] is not None
                              or r["convertible_debt_current"] is not None):
        long_term = (r["convertible_debt_noncurrent"] or 0.0) + (r["convertible_debt_current"] or 0.0)
    if long_term is None and r["short_term_borrowings"] is None:
        r["total_debt"] = None
    else:
        r["total_debt"] = (long_term or 0.0) + (r["short_term_borrowings"] or 0.0)
    # No debt tag at all usually means no debt. But a company paying interest on debt has debt,
    # under a tag this reader does not know, and reading it as none invents a net cash position.
    r["debt_unresolved"] = r["total_debt"] is None and (r["interest_expense"] or 0.0) > 0
    r["cash_and_investments"] = (None if r["cash"] is None else
                                 r["cash"] + (r["investments_current"] or 0.0) + (r["investments_noncurrent"] or 0.0))


def _period_raw(values: dict[str, Values], weighted: Values, eps: Values, company: Company,
                period: ReportPeriod, as_of: date) -> dict[str, float | None]:
    end = period.end
    r: dict[str, float | None] = {name: ttm(values[name], end) for name in DURATION_CONCEPTS}
    r.update({name: values[name].get((None, end)) for name in INSTANT_CONCEPTS})

    if r["gross_profit"] is None and r["revenue"] is not None and r["cost_of_revenue"] is not None:
        r["gross_profit"] = r["revenue"] - r["cost_of_revenue"]

    derive_debt(r)

    ocf = r["operating_cash_flow"]
    r["free_cash_flow"] = None if ocf is None or r["capex"] is None else ocf - r["capex"]   # R2-06
    op = r["operating_income"]
    r["ebitda"] = None if op is None or r["depreciation"] is None else op + r["depreciation"]

    # Shares: the cover-page count from this very filing when it is a single
    # number; multi-class filers report per class (dimensional, absent here),
    # so fall back to the balance-sheet count, then weighted diluted shares,
    # then net income / diluted EPS for the same period (class-A-equivalent
    # shares, the right basis for a market cap at the class A price).
    cover = [f.val for f in company.cover_shares
             if f.accn == period.accn and f.filed <= as_of]
    if len(cover) == 1:
        r["shares"] = cover[0]
    else:
        r["shares"] = (r["shares_outstanding"] or latest_duration(weighted, end)
                       or _shares_from_eps(values["net_income"], eps, end))
    return r


def _shares_from_eps(net_income: Values, eps: Values, end: date) -> float | None:
    pairs = [((e - s).days, net_income[(s, e)] / v) for (s, e), v in eps.items()
             if s is not None and e == end and v and (s, e) in net_income]
    shares = min(pairs)[1] if pairs else None
    return shares if shares and shares > 0 else None


def _shares_from_float(company: Company, period: ReportPeriod, as_of: date,
                       price_on: Callable[[date], float | None],
                       split_factor: Callable[[date, date], float]) -> float | None:
    """Last resort for filers with no company-wide share count anywhere (Visa):
    the public float in dollars over the price that day. It excludes affiliate
    holdings, so it slightly understates shares; better than no valuation."""
    floats = [f for f in company.public_float
              if f.filed <= as_of and f.end <= period.filed
              and (period.end - f.end).days <= _FLOAT_MAX_AGE_DAYS]
    if not floats:
        return None
    latest = max(floats, key=lambda f: f.end)
    price = price_on(latest.end)
    if not price:
        return None
    return latest.val / price * split_factor(latest.end, period.filed)


def _year_earlier(ends, end: date) -> date | None:
    target = end - timedelta(days=365)
    candidates = [e for e in ends if abs((e - target).days) <= _TOLERANCE_DAYS]
    return min(candidates, key=lambda e: abs((e - target).days)) if candidates else None


def _to_metrics(ticker: str, period: ReportPeriod, r: dict, prev: dict | None,
                price: float | None, split: float) -> FinancialMetrics:
    """Anything not derivable honestly from filings (ROIC, operating cycle,
    per-share growth across share-basis changes) stays null, not guessed."""
    shares = r["shares"]
    market_cap = shares * price if shares and price else None
    per_share = shares * split if shares else None
    debt, cash = r["total_debt"], r["cash"]
    liquid = r.get("cash_and_investments") if r.get("cash_and_investments") is not None else cash
    ev = market_cap + (debt or 0.0) - (liquid or 0.0) if market_cap is not None else None
    ni, rev, equity = r["net_income"], r["revenue"], r["equity"]
    ca, cl, inv = r["current_assets"], r["current_liabilities"], r["inventory"]

    # A negative P/E reads as "cheap" to a value screen; null is more honest.
    pe = _ratio(market_cap, ni) if ni is not None and ni > 0 else None
    earnings_growth = _growth(ni, prev["net_income"]) if prev else None
    receivables_turnover = _ratio(rev, r["receivables"])

    def g(key: str) -> float | None:
        return _growth(r[key], prev[key]) if prev else None

    return FinancialMetrics(
        ticker=ticker,
        report_period=period.end.isoformat(),
        period="ttm",
        currency="USD",
        filing_date=period.filed.isoformat(),
        price_at_filing=price,
        split_factor_to_as_of=split,
        market_cap=_round(market_cap, 0),
        enterprise_value=_round(ev, 0),
        price_to_earnings_ratio=pe,
        price_to_book_ratio=_ratio(market_cap, equity) if equity and equity > 0 else None,
        price_to_sales_ratio=_ratio(market_cap, rev),
        enterprise_value_to_ebitda_ratio=_ratio(ev, r["ebitda"]) if r["ebitda"] and r["ebitda"] > 0 else None,
        enterprise_value_to_revenue_ratio=_ratio(ev, rev),
        free_cash_flow_yield=_ratio(r["free_cash_flow"], market_cap),
        peg_ratio=_ratio(pe, earnings_growth * 100) if pe and earnings_growth and earnings_growth > 0 else None,
        gross_margin=_ratio(r["gross_profit"], rev),
        operating_margin=_ratio(r["operating_income"], rev),
        net_margin=_ratio(ni, rev),
        return_on_equity=_ratio(ni, equity) if equity and equity > 0 else None,
        return_on_assets=_ratio(ni, r["assets"]),
        asset_turnover=_ratio(rev, r["assets"]),
        inventory_turnover=_ratio(r["cost_of_revenue"], inv),
        receivables_turnover=receivables_turnover,
        days_sales_outstanding=_ratio(365.0, receivables_turnover),
        working_capital_turnover=_ratio(rev, ca - cl) if ca is not None and cl is not None else None,
        current_ratio=_ratio(ca, cl),
        quick_ratio=_ratio(ca - (inv or 0.0), cl) if ca is not None else None,
        cash_ratio=_ratio(cash, cl),
        operating_cash_flow_ratio=_ratio(r["operating_cash_flow"], cl),
        debt_to_equity=_ratio(debt, equity) if equity and equity > 0 else None,
        debt_to_assets=_ratio(debt, r["assets"]),
        interest_coverage=_ratio(r["operating_income"], r["interest_expense"]),
        revenue_growth=g("revenue"),
        earnings_growth=earnings_growth,
        book_value_growth=g("equity"),
        free_cash_flow_growth=g("free_cash_flow"),
        operating_income_growth=g("operating_income"),
        ebitda_growth=g("ebitda"),
        payout_ratio=_ratio(r["dividends"], ni) if ni and ni > 0 else None,
        earnings_per_share=_ratio(ni, per_share),
        book_value_per_share=_ratio(equity, per_share),
        free_cash_flow_per_share=_ratio(r["free_cash_flow"], per_share),
        revenue=rev, gross_profit=r["gross_profit"], operating_income=r["operating_income"],
        net_income=ni, pretax_income=r.get("pretax_income"), income_tax=r.get("income_tax"),
        operating_cash_flow=r["operating_cash_flow"], capex=r["capex"],
        free_cash_flow=r["free_cash_flow"], stock_compensation=r.get("stock_compensation"),
        buybacks=r.get("buybacks"), dividends_paid=r["dividends"], total_debt=debt, cash=cash,
        equity=equity, assets=r["assets"], ebitda=r["ebitda"], shares=per_share,
        diluted_shares=r["diluted_shares"] * split if r.get("diluted_shares") else None,
        debt_unresolved=bool(r.get("debt_unresolved")),
        cash_and_investments=r.get("cash_and_investments"),
    )


def _latest_report(submissions: dict, forms: set[str] = _REPORT_FORMS) -> dict | None:
    """The newest requested report form on the SEC filing index."""
    recent = (submissions.get("filings") or {}).get("recent") or {}
    for form, filed, period in zip(recent.get("form", []), recent.get("filingDate", []),
                                   recent.get("reportDate", [])):
        if form in forms:
            return {"form": form, "filed": filed, "period": period}
    return None


def _ratio(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return round(a / b, 4)


def _growth(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur - prev) / abs(prev), 4)


def _round(v: float | None, digits: int) -> float | None:
    return None if v is None else round(v, digits)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class EdgarClient:
    """Fetches and caches companyfacts; one instance per thread is fine, the
    parsed companies and the rate limiter are shared process-wide."""

    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        timeout: float = 60.0,
    ) -> None:
        self._user_agent = user_agent or os.environ.get(USER_AGENT_ENV, "")
        self._dir = Path(cache_dir)
        self._timeout = timeout
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def company(self, ticker: str) -> Company | None:
        """Parsed filings for *ticker*, or None if it is not an SEC filer."""
        cik = self.cik(ticker)
        if cik is None:
            return None
        hit = _COMPANIES.get(cik)
        if hit is not MISS:
            return hit
        payload = self._facts_payload(cik)
        company = parse_companyfacts(payload) if payload is not None else None
        _COMPANIES.put(cik, company)
        return company

    def profile(self, ticker: str) -> dict | None:
        """Name, SIC code/description, exchange and location from the SEC
        submissions index. Slow-moving, so it is cached for a week."""
        cik = self.cik(ticker)
        if cik is None:
            return None
        hit = _PROFILES.get(cik)
        if hit is not MISS:
            return hit
        body = self._cached_json(SUBMISSIONS_URL.format(cik=cik), f"submissions-{cik:010d}.json",
                                 _FACTS_TTL_SECONDS)
        profile = None
        if body is not None:
            business = (body.get("addresses") or {}).get("business") or {}
            profile = {
                "cik": cik,
                "name": body.get("name"),
                "sic": body.get("sic"),
                "sic_description": body.get("sicDescription"),
                "exchanges": body.get("exchanges") or [],
                "location": ", ".join(x for x in (business.get("city"),
                                                  business.get("stateOrCountry")) if x) or None,
                "latest_report": _latest_report(body),
                "latest_foreign_report": _latest_report(body, _FOREIGN_REPORT_FORMS),
            }
        _PROFILES.put(cik, profile)
        return profile

    def cik(self, ticker: str) -> int | None:
        """SEC company id for a ticker. Looked up once and kept on disk: the
        lookup endpoint is the least reliable call in the stack, and a run
        should not depend on it for names it has already resolved."""
        symbol = dash_symbol(ticker)
        hit = _CIKS.get(symbol)
        if hit is not MISS:
            return hit
        saved = self._saved_ciks().get(symbol)
        if saved and time.time() - saved[1] < _CIK_FILE_TTL_SECONDS:
            _CIKS.put(symbol, saved[0])
            return saved[0]
        body = self._get_json(SEARCH_URL, {"keysTyped": symbol})
        cik = None
        for h in (body or {}).get("hits", {}).get("hits", []):
            tickers = [t.strip().upper() for t in h.get("_source", {}).get("tickers", "").split(",")]
            if symbol in tickers:
                cik = int(h["_id"])
                break
        _CIKS.put(symbol, cik)
        if cik is not None:  # a miss may be transient; only remember hits
            self._save_cik(symbol, cik)
        return cik

    def _cik_path(self) -> Path:
        return self._dir / "ciks.json"

    def _saved_ciks(self) -> dict:
        try:
            return json.loads(self._cik_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cik(self, symbol: str, cik: int) -> None:
        with _CIK_LOCK:
            saved = self._saved_ciks()
            saved[symbol] = [cik, time.time()]
            self._dir.mkdir(parents=True, exist_ok=True)
            self._cik_path().write_text(json.dumps(saved, sort_keys=True), encoding="utf-8")

    def _facts_payload(self, cik: int) -> dict | None:
        return self._cached_json(FACTS_URL.format(cik=cik), f"CIK{cik:010d}.json",
                                 _FACTS_TTL_SECONDS)

    def _cached_json(self, url: str, filename: str, ttl_seconds: float) -> dict | None:
        """GET with a disk copy: companyfacts is megabytes per company and
        only changes when the company files."""
        path = self._dir / filename
        if path.exists() and time.time() - path.stat().st_mtime < ttl_seconds:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass  # corrupt entry -> refetch
        payload = self._get_json(url, None)
        if payload is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def _get_json(self, url: str, params: dict | None) -> dict | None:
        if not self._user_agent:
            raise FreeDataError(
                f"{USER_AGENT_ENV} is not set. SEC requires an identifying "
                "User-Agent, e.g. SEC_USER_AGENT=\"Your Name you@example.com\""
            )
        for delay in (*_RETRY_DELAYS, None):
            _THROTTLE.wait()
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout,
                                         headers={"User-Agent": self._user_agent})
            except requests.RequestException as exc:
                if delay is not None:
                    time.sleep(delay)
                    continue
                raise FreeDataError(f"SEC request failed: {exc}") from exc
            if resp.status_code == 404:
                return None
            if (resp.status_code == 429 or resp.status_code >= 500) and delay is not None:
                time.sleep(delay)
                continue
            if resp.status_code >= 400:
                raise FreeDataError(f"SEC {url} returned {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise FreeDataError(f"SEC {url} kept failing after {len(_RETRY_DELAYS)} retries")
