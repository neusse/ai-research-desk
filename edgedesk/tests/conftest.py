"""Fixtures. Nothing here touches the network.

The tests that matter most (point in time, reproducibility, withholding) are
about rules, not about live data, so the whole suite runs offline against a
fake client that records what it was asked for.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from edgedesk.evidence.package import Evidence, collect
from edgedesk.models import AnalystConsensus, CompanyNews, FinancialMetrics, Price


def make_bars(n: int, start_price: float = 100.0, drift: float = 0.001,
              end: date = date(2026, 9, 15), wobble: float = 0.35) -> list[dict]:
    """A deterministic bar series, oldest first, ending on *end*."""
    bars = []
    price = start_price
    day = end - timedelta(days=n - 1)
    for i in range(n):
        price = price * (1 + drift) + (wobble if i % 2 else -wobble)
        bars.append({
            "date": (day + timedelta(days=i)).isoformat(),
            "open": round(price * 0.995, 4),
            "high": round(price * 1.01, 4),
            "low": round(price * 0.99, 4),
            "close": round(price, 4),
            "volume": 1_000_000 + i * 1_000,
        })
    return bars


def make_metrics(**overrides) -> FinancialMetrics:
    base = dict(
        ticker="TEST", report_period="2026-06-30", period="ttm", filing_date="2026-07-29",
        market_cap=1.0e11, price_to_earnings_ratio=25.0, peg_ratio=1.4,
        price_to_sales_ratio=6.0, price_to_book_ratio=8.0,
        free_cash_flow_yield=0.04, gross_margin=0.60, operating_margin=0.25,
        net_margin=0.20, return_on_equity=0.28, return_on_assets=0.14,
        current_ratio=2.1, debt_to_equity=0.5, interest_coverage=15.0,
        revenue_growth=0.18, earnings_growth=0.22, free_cash_flow_growth=0.15,
        earnings_per_share=4.0,
    )
    base.update(overrides)
    return FinancialMetrics(**base)


class FakeClient:
    """Stands in for `DataClient`, and records every date boundary it is given
    so the point-in-time test can prove nothing later than `as_of` was asked for."""

    def __init__(self, bars=None, metrics=None, consensus=True, news=True,
                 release=True, profile=None, fund=None):
        self._bars = bars if bars is not None else make_bars(400)
        self._metrics = metrics if metrics is not None else [make_metrics()]
        self._consensus = consensus
        self._news = news
        self._release = release
        self._fund = fund
        self._profile = profile if profile is not None else {
            "cik": 1, "name": "Test Corp", "sic": "3674",
            "sic_description": "Semiconductors", "exchanges": ["Nasdaq"],
            "latest_report": {"form": "10-Q", "filed": "2026-07-29", "period": "2026-06-30"},
        }
        self.requested_ends: list[date] = []
        self.metrics_as_of: list[date] = []

    def daily_closes(self, ticker, start, end):
        self.requested_ends.append(end)
        lo, hi = start.isoformat(), end.isoformat()
        rows = self._bars if ticker == "TEST" else _shift(self._bars, 0.5)
        return [b for b in rows if lo <= b["date"] <= hi]

    def metrics(self, ticker, as_of, limit=12):
        self.metrics_as_of.append(as_of)
        rows = [m for m in self._metrics
                if date.fromisoformat(m.filing_date) <= as_of]
        # Unless a test says otherwise, the filing was priced at the decision
        # price, so a fixture's multiples are its current multiples.
        close = self.raw_close(ticker, as_of)
        return [m if m.price_at_filing is not None or close is None
                else m.model_copy(update={"price_at_filing": close,
                                          "split_factor_to_as_of": 1.0})
                for m in rows]

    def raw_close(self, ticker, as_of):
        rows = [b for b in self._bars if b["date"] <= as_of.isoformat()]
        if not rows:
            return None
        return rows[-1]["close"] * (1.0 if ticker == "TEST" else 0.5)

    def profile(self, ticker):
        return dict(self._profile)

    def fund_data(self, ticker):
        return dict(self._fund) if self._fund is not None else None

    def consensus(self, ticker):
        if not self._consensus:
            return None
        return AnalystConsensus(
            ticker=ticker, fetched_on=date.today().isoformat(),
            recommendation_mean=2.0, recommendation_key="buy", analyst_count=40,
            target_mean_price=140.0, target_high_price=180.0, target_low_price=100.0,
            current_price=120.0)

    def analyst_breakdown(self, ticker):
        if not self._consensus:
            return None
        return {"bullish": 30, "neutral": 8, "bearish": 2, "total": 40,
                "detail": {"period": "0m", "strongBuy": 10, "buy": 20, "hold": 8,
                           "sell": 2, "strongSell": 0},
                "history": [{"period": "0m", "strongBuy": 10, "buy": 20, "hold": 8,
                             "sell": 2, "strongSell": 0}]}

    def calendar(self, ticker):
        if not self._consensus:
            return None
        return {"next_earnings": "2026-11-20", "confirmed": True, "eps_estimate": 1.25,
                "eps_estimate_low": 1.15, "eps_estimate_high": 1.35,
                "revenue_estimate": 5.9e9, "ex_dividend": None}

    def dividend_yield(self, ticker):
        return 0.0075 if self._consensus else None

    def news(self, ticker, end, start=None, limit=40):
        self.requested_ends.append(end)
        if not self._news:
            return []
        return [CompanyNews(ticker=ticker, title="Test Corp Announces Something",
                            source="Benzinga", date=f"{end.isoformat()}T12:00:00Z")]

    def latest_release(self, ticker, since, until):
        self.requested_ends.append(until)
        if not self._release:
            return None
        from edgedesk.providers.benzinga import Release
        from datetime import datetime, timezone
        return Release(published=datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc),
                       eps=1.10, eps_estimate=1.00, revenue=5.5e9,
                       revenue_estimate=5.2e9, kind_rank=0)

    def close(self):
        pass


def _shift(bars, factor):
    return [{**b, "open": b["open"] * factor, "high": b["high"] * factor,
             "low": b["low"] * factor, "close": b["close"] * factor} for b in bars]


@pytest.fixture
def as_of() -> date:
    return date(2026, 9, 15)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    """The fixtures are built around 2026-09-15 as "today". Without this, every day after it
    the suite saw that date as historical and dropped current-only data such as consensus,
    so five tests started failing on the calendar alone."""
    today = date(2026, 9, 15)
    monkeypatch.setattr("edgedesk.evidence.package.last_settled_day", lambda *a, **k: today)
    monkeypatch.setattr("edgedesk.providers.client.last_settled_day", lambda *a, **k: today)


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def evidence(client, as_of) -> Evidence:
    return collect("TEST", as_of, client)


@pytest.fixture(autouse=True)
def _no_filing_fetches(monkeypatch):
    """The research layer reads filings from the SEC. Tests never do."""
    monkeypatch.setattr("edgedesk.llm.analysis._load_filings",
                        lambda run: ([], "filings are not fetched in tests"))


@pytest.fixture(autouse=True)
def _claude_plan_by_default(monkeypatch):
    """The user's own plan setting must not decide which path a test takes."""
    monkeypatch.delenv("EDGE_DESK_PLAN", raising=False)
    monkeypatch.delenv("DESK_PLAN", raising=False)
