"""Alpaca market data over REST: bars, splits, and the Benzinga news wire.

REST only, on purpose. Alpaca allows one market-data websocket per account,
and a live scanner may already own it; nothing here may ever open one.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import dotenv_values

from edgedesk.providers._common import FreeDataError, alpaca_symbol
from edgedesk.models import CompanyNews, Price

logger = logging.getLogger(__name__)

BASE_URL = "https://data.alpaca.markets"
KEY_ENV = "ALPACA_API_KEY"
SECRET_ENV = "ALPACA_SECRET_KEY"
FEED_ENV = "ALPACA_DATA_FEED"  # "auto" (SIP, then IEX), "sip" (paid, full tape) or "iex" (free tier)
# Point at another project's .env to share its keys instead of copying them.
ENV_FILE_ENV = "ALPACA_ENV_FILE"

# Accepted spellings, in order: mode-specific local setup, this app's, Alpaca SDK's, a trading bot's.
_KEY_NAMES = (("alpaca_paper_api_key", "alpaca_paper_secret"),
              ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"),
              ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
              ("ALPACA_TRADING_KEY", "ALPACA_TRADING_SECRET"))


def credential_help() -> str:
    return "; ".join(f"{key} + {secret}" for key, secret in _KEY_NAMES)


def resolve_credentials() -> tuple[str, str]:
    """(key id, secret) from the environment, else from ALPACA_ENV_FILE; empty
    strings when neither has a complete pair."""
    sources = [os.environ]
    env_file = os.environ.get(ENV_FILE_ENV)
    if env_file and Path(env_file).exists():
        sources.append(dotenv_values(env_file))
    for source in sources:
        for key_name, secret_name in _KEY_NAMES:
            key, secret = source.get(key_name), source.get(secret_name)
            if key and secret:
                return key, secret
    return "", ""

_TIMEFRAMES = {"minute": "Min", "hour": "Hour", "day": "Day", "week": "Week", "month": "Month"}
_RETRY_DELAYS = (2, 5, 15)
_NEWS_PAGE = 50  # Alpaca's maximum page size for /news


class AlpacaClient:
    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        feed: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if api_key and secret_key:
            self._key, self._secret = api_key, secret_key
        else:
            self._key, self._secret = resolve_credentials()
        # The data chain (2026-10-05): Alpaca SIP, then Alpaca IEX when the account is not
        # entitled to SIP, then Yahoo when there are no Alpaca keys at all. `source` says which.
        self._feed = (feed or os.environ.get(FEED_ENV, "auto")).lower()
        self._auto = self._feed == "auto"
        if self._auto:
            self._feed = "sip"
        self.source = f"alpaca-{self._feed}" if self._key and self._secret else "yahoo"
        self._timeout = timeout
        self._session = requests.Session()

    def close(self) -> None:
        self._session.close()

    def bars(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: str = "day",
        interval_multiplier: int = 1,
        adjustment: str = "all",
    ) -> list[Price]:
        """OHLCV bars, *end_date* inclusive. adjustment="all" (splits and
        dividends) suits returns; "raw" is the price actually printed that day,
        which is what a market cap from as-filed share counts needs."""
        unit = _TIMEFRAMES.get(interval)
        if unit is None:
            raise ValueError(f"unsupported interval {interval!r}; use one of {sorted(_TIMEFRAMES)}")
        if not (self._key and self._secret):
            return yahoo_bars(ticker, start_date, end_date, interval, adjustment)
        params = {
            "timeframe": f"{interval_multiplier}{unit}",
            "start": start_date[:10],
            "end": f"{end_date[:10]}T23:59:59Z",
            "adjustment": adjustment,
            "feed": self._feed,
            "limit": 10000,
        }
        rows: list[dict] = []
        try:
            for page in self._pages(f"/v2/stocks/{alpaca_symbol(ticker)}/bars", params, "bars"):
                rows.extend(page or [])
        except FreeDataError as exc:
            if not (self._auto and self._feed == "sip" and _not_entitled(str(exc))):
                raise
            logger.info("Alpaca SIP is not on this account; using the IEX feed")
            self._feed, self.source, params["feed"], rows = "iex", "alpaca-iex", "iex", []
            for page in self._pages(f"/v2/stocks/{alpaca_symbol(ticker)}/bars", params, "bars"):
                rows.extend(page or [])
        return [
            Price(open=r["o"], close=r["c"], high=r["h"], low=r["l"],
                  volume=int(r["v"]), time=r["t"])
            for r in rows
        ]

    def raw_closes(self, ticker: str, start: date, end: date) -> dict[date, float]:
        """Unadjusted daily closes keyed by trading date."""
        bars = self.bars(ticker, start.isoformat(), end.isoformat(), adjustment="raw")
        return {date.fromisoformat(b.time[:10]): b.close for b in bars}

    def has_keys(self) -> bool:
        return bool(self._key and self._secret)

    def news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        """Benzinga headlines mentioning *ticker*, newest first."""
        out: list[CompanyNews] = []
        for a in self.iter_news(ticker, end_date, start_date):
            out.append(CompanyNews(
                ticker=ticker,
                title=a.get("headline") or "",
                source=a.get("source") or a.get("author") or "Benzinga",
                date=a.get("created_at"),
                url=a.get("url"),
            ))
            if len(out) >= limit:
                break
        return out

    def iter_news(self, ticker: str, end_date: str, start_date: str | None = None):
        """Raw news items, newest first, fetched a page at a time so a caller
        that finds what it needs early stops paying for pages. Benzinga news is Alpaca's: with no
        keys there is none, and the caller's data caveats say so."""
        if not self.has_keys():
            return
        params = {
            "symbols": alpaca_symbol(ticker),
            "end": f"{end_date[:10]}T23:59:59Z",
            "limit": _NEWS_PAGE,
            "sort": "desc",
        }
        if start_date:
            params["start"] = start_date[:10]
        for page in self._pages("/v1beta1/news", params, "news"):
            yield from page or []

    def splits(self, ticker: str, start: date, end: date) -> list[tuple[date, float]]:
        """(ex-date, new shares per old share) for forward and reverse splits."""
        if not self.has_keys():
            return yahoo_splits(ticker, start, end)
        params = {
            "symbols": alpaca_symbol(ticker),
            "types": "forward_split,reverse_split",
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        out: list[tuple[date, float]] = []
        for page in self._pages("/v1/corporate-actions", params, "corporate_actions"):
            actions = page or {}
            for a in (*actions.get("forward_splits", []), *actions.get("reverse_splits", [])):
                if a.get("old_rate"):
                    out.append((date.fromisoformat(a["ex_date"]), a["new_rate"] / a["old_rate"]))
        return sorted(out)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _pages(self, path: str, params: dict, key: str):
        token = None
        while True:
            body = self._get(path, {**params, **({"page_token": token} if token else {})})
            if body is None:
                return
            yield body.get(key)
            token = body.get("next_page_token")
            if not token:
                return

    def _get(self, path: str, params: dict) -> dict | None:
        """Fail-loud GET. None only for "this symbol has no data" (404/422)."""
        if not self._key or not self._secret:
            raise FreeDataError(
                f"No complete Alpaca credential pair found. Checked {credential_help()}. "
                f"Alternatively set {ENV_FILE_ENV} to an .env containing one of those pairs."
            )
        headers = {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret}
        for delay in (*_RETRY_DELAYS, None):
            try:
                resp = self._session.get(BASE_URL + path, params=params,
                                         headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                raise FreeDataError(f"Alpaca {path} failed: {exc}") from exc
            if resp.status_code == 429 and delay is not None:
                logger.info("Alpaca rate limited, retrying in %ds", delay)
                time.sleep(delay)
                continue
            # An unknown symbol is a data fact; any other 422 is a bad request
            # and must not masquerade as "no bars".
            if resp.status_code == 404 or (resp.status_code == 422 and "symbol" in resp.text.lower()):
                return None
            if resp.status_code >= 400:
                raise FreeDataError(f"Alpaca {path} returned {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        raise FreeDataError(f"Alpaca {path} still rate limited after {len(_RETRY_DELAYS)} retries")



def _not_entitled(message: str) -> bool:
    """Alpaca's 403 for an account without the SIP subscription."""
    m = message.lower()
    return "403" in m and ("subscription" in m or "sip" in m or "not permit" in m)


def yahoo_bars(ticker: str, start_date: str, end_date: str, interval: str = "day",
               adjustment: str = "all") -> list[Price]:
    """Daily bars from Yahoo, the last link of the data chain (no Alpaca keys). adjustment="all"
    uses Yahoo's split- and dividend-adjusted prices; anything else its split-adjusted close,
    the nearest Yahoo has to a raw print."""
    if interval != "day":
        raise FreeDataError("Without Alpaca keys only daily bars are available (Yahoo)")
    import yfinance as yf
    from datetime import timedelta
    end = (date.fromisoformat(end_date[:10]) + timedelta(days=1)).isoformat()
    frame = yf.Ticker(ticker.replace(".", "-")).history(start=start_date[:10], end=end, interval="1d",
                                                         auto_adjust=adjustment == "all")
    out = []
    for stamp, row in frame.iterrows():
        out.append(Price(open=float(row["Open"]), close=float(row["Close"]), high=float(row["High"]),
                         low=float(row["Low"]), volume=int(row["Volume"]),
                         time=f"{stamp.date().isoformat()}T04:00:00Z"))
    return out


def yahoo_splits(ticker: str, start: date, end: date) -> list[tuple[date, float]]:
    import yfinance as yf
    s = yf.Ticker(ticker.replace(".", "-")).splits
    return sorted((d.date(), float(r)) for d, r in s.items() if start <= d.date() <= end and r)
