"""One client over the free stack: Alpaca bars and news, EDGAR fundamentals,
Yahoo consensus.

Deliberately thinner than ai-hedge-fund's `FreeDataClient`: that one implements
a vendor's whole interface behind a disk cache. This one answers only what the
evidence layer asks, and every answer carries the date it is true as of.

Two rules the whole engine inherits from here:

* **Settled close.** A daily bar for today exists during the session but moves
  every minute. Until 16:15 ET, "latest close" means yesterday's, so two runs
  in one day see the same prices.
* **Point in time.** Fundamentals come from EDGAR keyed by filing date, so an
  `as_of` two months back sees only what was public then.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from edgedesk.providers.splits import repair_unadjusted_splits
from edgedesk.models import AnalystConsensus, CompanyNews, FinancialMetrics, Price
from edgedesk.providers._common import MISS, FreeDataError, TTLMemo, dash_symbol
from edgedesk.providers.alpaca import AlpacaClient
from edgedesk.providers.benzinga import Release, first_release
from edgedesk.providers.edgar import EdgarClient, build_metrics
from edgedesk.providers.yahoo import YahooClient
from edgedesk.session import last_settled_day

logger = logging.getLogger(__name__)

# Reaching back for a close when dating a market cap: covers weekends and
# holiday clusters around a filing date.
_PRICE_LOOKBACK_DAYS = 10
# The public-float share-count fallback prices a float reported up to a year
# and a bit earlier, so raw closes must reach back at least that far.
_FLOAT_LOOKBACK_DAYS = 500
_SPLIT_HISTORY_START = date(2000, 1, 1)
# A headline naming dozens of symbols is a market roundup, not company news.
_MAX_NEWS_SYMBOLS = 10

_SPLITS = TTLMemo(12 * 3600)


class DataClient:
    """Free market and fundamental data, all of it point-in-time addressable."""

    def __init__(self) -> None:
        self.alpaca = AlpacaClient()
        self.edgar = EdgarClient()
        self.yahoo = YahooClient()
        # Splits the feed left unadjusted and this client restated, by ticker.
        self.split_repairs: dict[str, list[dict]] = {}

    def close(self) -> None:
        self.alpaca.close()
        self.edgar.close()

    # ------------------------------------------------------------------
    # Market
    # ------------------------------------------------------------------

    def bars(self, ticker: str, start: date, end: date, adjustment: str = "all") -> list[Price]:
        """Split- and dividend-adjusted daily bars through *end* inclusive."""
        bars = self.alpaca.bars(ticker, start.isoformat(), end.isoformat(),
                                adjustment=adjustment)
        if adjustment == "raw" or not bars:
            return bars
        try:
            known = self._splits(ticker)
        except Exception as exc:                   # noqa: BLE001
            logger.info("splits for %s unavailable, bars left as fetched: %s", ticker, exc)
            return bars
        bars, repaired = repair_unadjusted_splits(bars, known, ticker)
        if repaired:
            self.split_repairs[ticker.upper()] = repaired
        return bars

    def daily_closes(self, ticker: str, start: date, end: date) -> list[dict]:
        """`[{"date", "open", "high", "low", "close", "volume"}]`, oldest first.

        The shape `levels.compute_anchors` and the factor engine both consume.
        """
        return [
            {"date": b.time[:10], "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume}
            for b in self.bars(ticker, start, end)
        ]

    # ------------------------------------------------------------------
    # Fundamentals
    # ------------------------------------------------------------------

    def metrics(self, ticker: str, as_of: date, limit: int = 12) -> list[FinancialMetrics]:
        """TTM metric rows public by *as_of*, newest first. Empty for a filer
        EDGAR has no US GAAP quarterly facts for (IFRS 20-F/40-F filers), which
        the caller must treat as missing data, never as zero."""
        company = self.edgar.company(ticker)
        if company is None:
            return []
        visible = [p for p in company.periods if p.filed <= as_of]
        if not visible:
            return []

        closes = self.alpaca.raw_closes(
            ticker,
            min(p.filed for p in visible) - timedelta(days=_FLOAT_LOOKBACK_DAYS),
            max(p.filed for p in visible),
        )
        splits = self._splits(ticker)

        def price_on(day: date) -> float | None:
            for back in range(_PRICE_LOOKBACK_DAYS + 1):
                close = closes.get(day - timedelta(days=back))
                if close is not None:
                    return close
            return None

        def split_factor(after: date, through: date) -> float:
            factor = 1.0
            for ex_date, ratio in splits:
                if after < ex_date <= through:
                    factor *= ratio
            return factor

        return build_metrics(ticker, company, as_of, limit, price_on, split_factor)

    def raw_close(self, ticker: str, as_of: date) -> float | None:
        """The unadjusted close on or just before *as_of*: the price a decision
        made that day is actually made at."""
        closes = self.alpaca.raw_closes(
            ticker, as_of - timedelta(days=_PRICE_LOOKBACK_DAYS), as_of)
        days = [d for d in closes if d <= as_of]
        return closes[max(days)] if days else None

    def profile(self, ticker: str) -> dict | None:
        """Name, SIC code and description, exchange, and the newest 10-Q/10-K on
        the filing index (which the freshness check compares against)."""
        profile = self.edgar.profile(ticker) or {}
        try:
            info = self.yahoo.info(ticker)
        except FreeDataError as exc:
            logger.info("security type unavailable for %s: %s", ticker, exc)
            info = {}
        if info.get("quoteType"):
            profile["security_type"] = str(info["quoteType"]).upper()
        if not profile.get("name") and (info.get("longName") or info.get("shortName")):
            profile["name"] = info.get("longName") or info.get("shortName")
        return profile

    def fund_data(self, ticker: str) -> dict | None:
        """Current ETF structure and holdings. Historical runs exclude this snapshot."""
        try:
            return self.yahoo.fund_data(ticker)
        except FreeDataError as exc:
            logger.info("fund data unavailable for %s: %s", ticker, exc)
            return None

    # ------------------------------------------------------------------
    # Estimates and events
    # ------------------------------------------------------------------

    def consensus(self, ticker: str) -> AnalystConsensus | None:
        """Sell-side consensus as of right now. Current-only: it has no history,
        so it may never be applied to a historical as_of."""
        try:
            return self.yahoo.analyst_consensus(ticker)
        except FreeDataError as exc:
            logger.info("consensus unavailable for %s: %s", ticker, exc)
            return None

    def forward_estimates(self, ticker: str) -> dict | None:
        """Forward EPS and revenue and their revision trend. Current-only."""
        try:
            return self.yahoo.forward_estimates(ticker)
        except FreeDataError as exc:
            logger.info("forward estimates unavailable for %s: %s", ticker, exc)
            return None

    def analyst_breakdown(self, ticker: str) -> dict | None:
        """The bullish / neutral / bearish split behind the consensus mean."""
        try:
            return self.yahoo.analyst_breakdown(ticker)
        except FreeDataError as exc:
            logger.info("analyst breakdown unavailable for %s: %s", ticker, exc)
            return None

    def calendar(self, ticker: str) -> dict | None:
        """Confirmed next earnings date and the estimates for that quarter."""
        try:
            return self.yahoo.calendar(ticker)
        except FreeDataError as exc:
            logger.info("calendar unavailable for %s: %s", ticker, exc)
            return None

    def dividend_yield(self, ticker: str) -> float | None:
        try:
            return self.yahoo.dividend_yield(ticker)
        except FreeDataError as exc:
            logger.info("dividend yield unavailable for %s: %s", ticker, exc)
            return None

    def news(self, ticker: str, end: date, start: date | None = None,
             limit: int = 40) -> list[CompanyNews]:
        """Benzinga headlines about *ticker*, newest first, roundups excluded."""
        out: list[CompanyNews] = []
        for item in self.alpaca.iter_news(ticker, end.isoformat(),
                                          start.isoformat() if start else None):
            if len(item.get("symbols") or []) > _MAX_NEWS_SYMBOLS:
                continue
            out.append(CompanyNews(
                ticker=ticker,
                title=item.get("headline") or "",
                source=item.get("source") or item.get("author") or "Benzinga",
                date=item.get("created_at"),
                url=item.get("url"),
            ))
            if len(out) >= limit:
                break
        return out

    def latest_release(self, ticker: str, since: date, until: date) -> Release | None:
        """The most recent Benzinga earnings release headline in the window,
        parsed into reported vs consensus EPS and revenue."""
        items = list(self.alpaca.iter_news(ticker, until.isoformat(), since.isoformat()))
        return first_release(items, ticker)

    # ------------------------------------------------------------------

    def _splits(self, ticker: str) -> list[tuple[date, float]]:
        symbol = dash_symbol(ticker)
        hit = _SPLITS.get(symbol)
        if hit is MISS:
            hit = self.alpaca.splits(ticker, _SPLIT_HISTORY_START, date.today())
            _SPLITS.put(symbol, hit)
        return hit


def settled_as_of(requested: str | None = None) -> date:
    """The as-of date a run should use: the one asked for, or the last settled
    session, and never later than that. Never today's unsettled bar.

    An explicit date used to be returned unchanged, so the desk, which passed the
    run's own date, had Edge Desk analyze today's live bar mid-session while the
    other teams used the last settled close (audit 2, 2026-09-25)."""
    settled = last_settled_day()
    if requested:
        return min(date.fromisoformat(requested[:10]), settled)
    return settled
