"""Yahoo Finance (via yfinance): analyst consensus, insider transactions, and
the fallback source of earnings surprises.

Yahoo is scraped, unversioned, and rate limits hard, so it is kept off the
critical path: fundamentals come from SEC, prices/splits/surprises from
Alpaca. What remains retries on rate limits, then fails loud (FreeDataError)
rather than returning a quiet empty, and responses are memoized per ticker.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from edgedesk.providers._common import MISS, FreeDataError, TTLMemo, dash_symbol
from edgedesk.models import AnalystConsensus, InsiderTrade

logger = logging.getLogger(__name__)

_MEMO = TTLMemo(6 * 3600)
_RATE_LIMIT_DELAYS = (5, 20, 45)
_FORM4_DEADLINE_DAYS = 2  # Yahoo drops the filing date; Form 4 is due within 2 business days


@dataclass(frozen=True)
class EarningsEvent:
    announced: datetime
    estimate: float | None
    reported: float

    @property
    def surprise(self) -> str | None:
        """BEAT / MISS / MEET at cent precision, the way EPS is reported."""
        if self.estimate is None:
            return None
        reported, estimate = round(self.reported, 2), round(self.estimate, 2)
        if reported > estimate:
            return "BEAT"
        if reported < estimate:
            return "MISS"
        return "MEET"


def _default_ticker(symbol: str):
    import yfinance  # heavy import, paid only when Yahoo is actually used

    return yfinance.Ticker(symbol)


class YahooClient:
    def __init__(self, ticker_factory: Callable[[str], Any] | None = None) -> None:
        self._ticker = ticker_factory or _default_ticker

    def info(self, ticker: str) -> dict:
        """Yahoo's quote summary; {} when Yahoo does not know the symbol."""
        info = self._call(ticker, "info", lambda t: t.info)
        return info if isinstance(info, dict) and info.get("quoteType") else {}

    def fund_data(self, ticker: str) -> dict | None:
        """Normalize the current subset of Yahoo fund data used by the ETF model."""
        info = self.info(ticker)
        if str(info.get("quoteType") or "").upper() != "ETF":
            return None
        raw = self._call(ticker, "fund-data",
                         lambda t: _extract_fund_data(t.funds_data, info, ticker))
        return raw if isinstance(raw, dict) else None

    def earnings_events(self, ticker: str, limit: int) -> list[EarningsEvent]:
        """Reported quarters, newest first (scheduled future dates excluded)."""
        df = self._call(ticker, f"earnings:{limit}",
                        lambda t: t.get_earnings_dates(limit=limit))
        if df is None or len(df) == 0:
            return []
        events = []
        for ts, row in df.iterrows():
            reported = _num(row.get("Reported EPS"))
            if reported is None:
                continue
            events.append(EarningsEvent(
                announced=ts.to_pydatetime(),
                estimate=_num(row.get("EPS Estimate")),
                reported=reported,
            ))
        return sorted(events, key=lambda e: e.announced, reverse=True)

    def analyst_consensus(self, ticker: str) -> AnalystConsensus | None:
        info = self.info(ticker)
        if not info:
            return None
        count = info.get("numberOfAnalystOpinions")
        return AnalystConsensus(
            ticker=ticker,
            fetched_on=date.today().isoformat(),
            recommendation_mean=_num(info.get("recommendationMean")),
            recommendation_key=info.get("recommendationKey"),
            analyst_count=int(count) if count else None,
            target_mean_price=_num(info.get("targetMeanPrice")),
            target_high_price=_num(info.get("targetHighPrice")),
            target_low_price=_num(info.get("targetLowPrice")),
            current_price=_num(info.get("currentPrice") or info.get("regularMarketPrice")),
        )

    def analyst_breakdown(self, ticker: str) -> dict | None:
        """How the covering analysts actually split, not just their mean.

        A mean of 2.4 can be forty analysts clustered on Hold or a violent
        argument between twenty Buys and twenty Sells, and those are different
        facts about a stock. Yahoo publishes the current month plus the three
        before it, which is also the first genuinely free piece of estimate
        history available, so the snapshot keeps all four rows.
        """
        df = self._call(ticker, "recommendations", lambda t: t.recommendations)
        if df is None or len(df) == 0:
            return None
        rows = []
        for _, row in df.iterrows():
            counts = {k: int(row.get(k) or 0)
                      for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
            rows.append({"period": str(row.get("period") or ""), **counts})
        current = rows[0]
        bullish = current["strongBuy"] + current["buy"]
        bearish = current["sell"] + current["strongSell"]
        total = bullish + bearish + current["hold"]
        return {
            "bullish": bullish,
            "neutral": current["hold"],
            "bearish": bearish,
            "total": total,
            "detail": current,
            "history": rows,
        }

    def forward_estimates(self, ticker: str) -> dict | None:
        """Consensus EPS and revenue for this fiscal year and next, and how the EPS
        numbers have moved over the last 7, 30, 60 and 90 days.

        Two things to know about these figures. They are ADJUSTED earnings, which
        leave out stock compensation, so a forward P/E flatters any company that
        pays heavily in stock. And they are current-only: the trend columns are the
        only revision history the free stack offers, so each run snapshots them.
        """
        eps = self._call(ticker, "earnings_estimate", lambda t: t.earnings_estimate)
        rev = self._call(ticker, "revenue_estimate", lambda t: t.revenue_estimate)
        trend = self._call(ticker, "eps_trend", lambda t: t.eps_trend)
        counts = self._call(ticker, "eps_revisions", lambda t: t.eps_revisions)
        if eps is None or len(eps) == 0:
            return None

        def cell(df, period, column):
            try:
                return _num(df.loc[period, column])
            except Exception:                       # noqa: BLE001 - a missing row or column
                return None

        out: dict = {"fetched_on": date.today().isoformat(), "periods": {}}
        for period, label in (("0y", "this_year"), ("+1y", "next_year")):
            out["periods"][label] = {
                "eps": cell(eps, period, "avg"), "eps_low": cell(eps, period, "low"),
                "eps_high": cell(eps, period, "high"), "eps_growth": cell(eps, period, "growth"),
                "analysts": cell(eps, period, "numberOfAnalysts"),
                "revenue": cell(rev, period, "avg") if rev is not None else None,
                "revenue_growth": cell(rev, period, "growth") if rev is not None else None,
                "eps_7d_ago": cell(trend, period, "7daysAgo") if trend is not None else None,
                "eps_30d_ago": cell(trend, period, "30daysAgo") if trend is not None else None,
                "eps_90d_ago": cell(trend, period, "90daysAgo") if trend is not None else None,
                "up_30d": cell(counts, period, "upLast30days") if counts is not None else None,
                "down_30d": cell(counts, period, "downLast30days") if counts is not None else None,
            }
        return out

    def calendar(self, ticker: str) -> dict | None:
        """The confirmed next earnings date and the Street's estimates for it.

        This replaces guessing the date from the reporting rhythm. It is
        current-only, like every other Yahoo field: a historical run must not
        see a date that had not been announced yet.
        """
        cal = self._call(ticker, "calendar", lambda t: t.calendar)
        if not cal:
            return None
        dates = cal.get("Earnings Date") or []
        if not isinstance(dates, (list, tuple)):
            dates = [dates]
        when = _to_date(dates[0]) if dates else None
        return {
            "next_earnings": when.isoformat() if when else None,
            "confirmed": bool(when),
            "eps_estimate": _num(cal.get("Earnings Average")),
            "eps_estimate_low": _num(cal.get("Earnings Low")),
            "eps_estimate_high": _num(cal.get("Earnings High")),
            "revenue_estimate": _num(cal.get("Revenue Average")),
            "ex_dividend": (_to_date(cal.get("Ex-Dividend Date")).isoformat()
                            if _to_date(cal.get("Ex-Dividend Date")) else None),
        }

    def dividend_yield(self, ticker: str) -> float | None:
        """Trailing dividend yield as a fraction.

        Yahoo ships two fields that disagree by a factor of a hundred
        (`dividendYield` is a percentage, `trailingAnnualDividendYield` a
        fraction), so the fraction is used and the percentage is only a
        fallback, scaled.
        """
        info = self.info(ticker)
        if not info:
            return None
        fraction = _num(info.get("trailingAnnualDividendYield"))
        if fraction is not None:
            return round(fraction, 6)
        percent = _num(info.get("dividendYield"))
        return round(percent / 100.0, 6) if percent is not None else None

    def insider_trades(self, ticker: str, end_date: str, start_date: str | None,
                       limit: int) -> list[InsiderTrade]:
        """Recent insider transactions (Yahoo keeps roughly the last year).

        filing_date is estimated as transaction date + the Form 4 deadline,
        so a backtest never sees a trade before it could have been public.
        """
        df = self._call(ticker, "insiders", lambda t: t.insider_transactions)
        if df is None or len(df) == 0:
            return []
        out: list[InsiderTrade] = []
        for _, row in df.iterrows():
            traded = _to_date(row.get("Start Date"))
            if traded is None:
                continue
            filed = (traded + timedelta(days=_FORM4_DEADLINE_DAYS)).isoformat()
            if filed > end_date[:10] or (start_date and filed < start_date[:10]):
                continue
            position = str(row.get("Position") or "")
            out.append(InsiderTrade(
                ticker=ticker,
                name=str(row.get("Insider") or ""),
                filing_date=filed,
                is_board_director="director" in position.lower(),
                title=position or None,
                transaction_date=traded.isoformat(),
                transaction_type=str(row.get("Text") or row.get("Transaction") or "") or None,
                transaction_shares=_num(row.get("Shares")),
                transaction_value=_num(row.get("Value")),
            ))
        out.sort(key=lambda t: t.filing_date, reverse=True)
        return out[:limit]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call(self, ticker: str, what: str, fetch: Callable[[Any], Any]):
        symbol = dash_symbol(ticker)
        key = (symbol, what)
        hit = _MEMO.get(key)
        if hit is not MISS:
            return hit
        for delay in (*_RATE_LIMIT_DELAYS, None):
            try:
                value = fetch(self._ticker(symbol))
                break
            except Exception as exc:  # yfinance raises a zoo of types; all are infrastructure
                limited = any(s in str(exc).lower() for s in ("too many requests", "rate limit", "429"))
                if limited and delay is not None:
                    logger.info("Yahoo rate limited on %s %s, retrying in %ds", what, symbol, delay)
                    time.sleep(delay)
                    continue
                raise FreeDataError(f"Yahoo {what} for {symbol} failed: {exc}") from exc
        _MEMO.put(key, value)
        return value


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _to_date(v) -> date | None:
    if v is None:
        return None
    if hasattr(v, "date") and callable(v.date):
        try:
            return v.date()
        except (TypeError, ValueError):
            return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _clean_text(value) -> str | None:
    if value is None:
        return None
    return str(value).replace("\ufffd", "").strip() or None


def _extract_fund_data(funds, info: dict, ticker: str) -> dict:
    """Convert yfinance's mixed dict/DataFrame fund surface into stable primitives."""
    overview = getattr(funds, "fund_overview", None) or {}
    allocations = getattr(funds, "asset_classes", None) or {}
    operations = getattr(funds, "fund_operations", None)
    holdings = getattr(funds, "top_holdings", None)
    sectors = getattr(funds, "sector_weightings", None) or {}

    def operation(row: str, column: str | None = None):
        if operations is None or not hasattr(operations, "loc"):
            return None
        try:
            values = operations.loc[row]
        except (KeyError, TypeError):
            return None
        if column:
            return _num(values.get(column))
        preferred = dash_symbol(ticker)
        if preferred in getattr(values, "index", []):
            return _num(values.get(preferred))
        for key in getattr(values, "index", []):
            if str(key).lower() != "category average":
                return _num(values.get(key))
        return None

    top: list[dict] = []
    if holdings is not None and hasattr(holdings, "iterrows"):
        for symbol, row in holdings.iterrows():
            weight = _num(row.get("Holding Percent"))
            if weight is not None:
                top.append({"symbol": str(symbol), "name": _clean_text(row.get("Name") or symbol),
                            "weight": weight})

    nav = _num(info.get("navPrice"))
    market = _num(info.get("regularMarketPrice") or info.get("currentPrice"))
    premium = ((market - nav) / nav) if nav and market is not None else None
    return {
        "category": overview.get("categoryName") or info.get("category"),
        "family": overview.get("family") or info.get("fundFamily"),
        "legal_type": overview.get("legalType") or info.get("legalType"),
        "description": _clean_text(getattr(funds, "description", None)),
        "expense_ratio": operation("Annual Report Expense Ratio"),
        "category_expense_ratio": operation("Annual Report Expense Ratio", "Category Average"),
        "total_assets": _num(info.get("totalAssets")),
        "nav_price": nav, "market_price": market, "premium_discount": premium,
        "stock_position": _num(allocations.get("stockPosition")),
        "bond_position": _num(allocations.get("bondPosition")),
        "cash_position": _num(allocations.get("cashPosition")),
        "top_holdings": top,
        "sector_weightings": {str(k): v for k, v in sectors.items() if _num(v) is not None},
        "as_of": date.today().isoformat(),
    }
