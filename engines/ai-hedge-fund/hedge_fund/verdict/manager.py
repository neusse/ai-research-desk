"""The research manager: one LLM that reads the whole desk and writes the verdict.

The analysts each see one slice (a persona's checklist, an earnings surprise,
the Street's targets) and the portfolio math only averages them. The manager
is the step that weighs them: which arguments hold up, which rest on missing
or stale data, and what that adds up to for someone deciding to buy, hold or
sell. It mirrors TradingAgents' research/portfolio manager, on top of this
fund's own analysts.

Contract, matching LLMAgent's:
- An LLM or parse failure does not fail the run: the ticker keeps its
  mechanical desk rating and the verdict carries `error`.
- Every call persists its exact prompt and response in the PromptCache, and an
  identical prompt (same day, same inputs) is never paid for twice.
- Run-today only. The prompt includes today's price, so it does not cache
  across days, and backtests never call it (no Opus bill per historical day,
  and no hindsight-laden verdicts on the past).
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from hedge_fund.data.protocol import DataClient
from hedge_fund.features.snapshot import InsufficientData, build_snapshot
from hedge_fund.llm import LLMClient, PromptCache, extract_json, make_llm, prompt_key
from hedge_fund.models import Signal
from hedge_fund.pipeline.models import CycleRecord, TickerVerdict
from hedge_fund.verdict.rating import RATINGS, desk_verdict, distinct_signals, has_view

logger = logging.getLogger(__name__)

MANAGER_MODEL_ENV = "HEDGE_FUND_MANAGER_MODEL"
DEFAULT_MANAGER_MODEL = "claude-opus-5"
_DISABLED = ("", "off", "none", "false", "0")
_MAX_PARALLEL = 4
# Recent news for the manager: the last month of headlines, plus the days
# around the latest earnings release even when that is older (a 20% drop after
# a beat-and-raise reads very differently from one after a miss). Roughly 50
# headlines at most, well under a cent of input.
_NEWS_DAYS = 30
_NEWS_LIMIT = 40
_RELEASE_BEFORE_DAYS = 1
_RELEASE_AFTER_DAYS = 3
_RELEASE_LIMIT = 12

_DISPLAY = {
    "buffett": "Warren Buffett", "munger": "Charlie Munger", "graham": "Benjamin Graham",
    "lynch": "Peter Lynch", "druckenmiller": "Stanley Druckenmiller",
    "pead": "Post-earnings drift (quant)", "analyst_consensus": "Street consensus (quant)",
}

SYSTEM_PROMPT = """You are the Research Manager of an equity research desk. A team of
analysts has each reviewed one stock from their own angle. Your job is to read all of
their work and deliver the desk's decision for a swing-to-long-term investor (weeks to
years) who will act on it with real money.

How to decide:
- Weigh arguments, do not count votes. A strong, evidence-based case outweighs several
  weak ones. Say plainly when an analyst's reasoning does not hold up, for example when it
  rests on data that is missing, stale, or misread.
- Cover what matters for this horizon: business quality, growth and its trend, margins,
  balance sheet, valuation against that quality and growth, earnings momentum, and what
  the sell side expects.
- The mechanical desk score is an unweighted average of the analysts' calls. Treat it as
  one input; you may disagree with it, and should say why when you do.
- Data caveats (stale fundamentals, approximated figures, missing data) must lower your
  confidence and be named in your verdict.
- Recent headlines cover what the filings cannot: why the price moved, guidance, rating
  changes, contracts, and events since the last filing. Headlines are unverified and
  carry no magnitude, so use them for context and catalysts, not as numbers. If the
  stock moved sharply and the headlines do not explain it, say so.
- Portfolio weights in the material are relative to the other names in the same run and
  capped by risk limits. Do not read them as conviction.

Ratings:
- Buy: high conviction; own it or add now.
- Overweight: positive; build a position or hold above normal size.
- Hold: keep what you own; no new money.
- Underweight: negative; trim.
- Sell: exit or avoid.

Hard rules:
- Use only the material provided. Do not invent numbers.
- Be concise: the thesis at most 120 words, 3-5 bullets per list, one sentence each.
- Do not use em dashes or en dashes; use commas, colons, or periods.
- Respond with JSON only, exactly this schema:
{"rating": "Buy" | "Overweight" | "Hold" | "Underweight" | "Sell",
 "confidence": <0-100>,
 "summary": "<the decision in 1-2 sentences>",
 "thesis": "<one paragraph: how you weighed the analysts and why this rating>",
 "bull_case": ["<strongest reasons it works>", ...],
 "bear_case": ["<strongest reasons it fails>", ...],
 "what_would_change": ["<specific, observable developments that would change the rating>", ...]}"""


def manager_model() -> str | None:
    """The configured manager model id, or None when the manager is switched off."""
    value = os.environ.get(MANAGER_MODEL_ENV)
    if value is None:
        return DEFAULT_MANAGER_MODEL
    return None if value.strip().lower() in _DISABLED else value.strip()


class ResearchManager:
    name = "research_manager"

    def __init__(self, llm: LLMClient | None = None, cache: PromptCache | None = None) -> None:
        self._llm = llm if llm is not None else make_llm(
            model=manager_model() or DEFAULT_MANAGER_MODEL, max_tokens=8000, timeout=240)
        self._cache = cache if cache is not None else PromptCache()

    @property
    def model(self) -> str:
        return self._llm.model

    def review(self, record: CycleRecord, ticker: str, base: TickerVerdict,
               fundamentals: str) -> TickerVerdict:
        user = build_user_prompt(record, ticker, base, fundamentals)
        key = prompt_key(self.name, self._llm.model, SYSTEM_PROMPT, user)

        cached = self._cache.get(key)
        if cached is not None and "parsed" in cached:
            return _merge(base, cached["parsed"], self._llm.model, cached=True)

        try:
            response = self._llm.complete(SYSTEM_PROMPT, user)
        except Exception as exc:
            logger.warning("research manager call failed for %s: %s", ticker, exc)
            return base.model_copy(update={"model": self._llm.model, "error": f"LLM call failed: {exc}"})

        record_entry = {"agent": self.name, "model": self._llm.model, "ticker": ticker,
                        "as_of": record.as_of, "system": SYSTEM_PROMPT, "user": user,
                        "response": response}
        try:
            parsed = parse_verdict(response)
        except Exception as exc:
            self._cache.put(key, {**record_entry, "parse_error": str(exc)})
            logger.warning("research manager parse failed for %s: %s", ticker, exc)
            return base.model_copy(update={"model": self._llm.model, "error": f"parse failed: {exc}"})
        self._cache.put(key, {**record_entry, "parsed": parsed})
        return _merge(base, parsed, self._llm.model, cached=False)


def add_verdicts(
    record: CycleRecord,
    data_client: DataClient,
    manager: ResearchManager | None = None,
) -> CycleRecord:
    """Return *record* with a TickerVerdict per traded ticker.

    Without a manager, verdicts carry only the mechanical desk rating (free).
    """
    tickers = [t for t in record.universe if t in record.marks]

    def one(ticker: str) -> TickerVerdict:
        base = desk_verdict(record, ticker)
        base = base.model_copy(update={
            "data_caveats": _caveats(data_client, ticker, record.as_of),
            "headlines": _headlines(data_client, ticker, record.as_of) if manager else [],
        })
        if base.desk_score is None:
            return base.model_copy(update={
                "data_caveats": [*base.data_caveats,
                                 "All analysts abstained or had no view. Veterans cannot publish "
                                 "a rating without an evidence based analyst call."],
            })
        if manager is None:
            return base
        return manager.review(record, ticker, base, _fundamentals(data_client, ticker, record.as_of))

    if manager is None or len(tickers) <= 1:
        verdicts = [one(t) for t in tickers]
    else:
        with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL, len(tickers))) as pool:
            verdicts = list(pool.map(one, tickers))
    return record.model_copy(update={"verdicts": {v.ticker: v for v in verdicts}})


def parse_verdict(response: str) -> dict:
    data = extract_json(response)
    rating = str(data.get("rating", "")).strip().title()
    if rating not in RATINGS:
        raise ValueError(f"invalid rating {data.get('rating')!r}")
    confidence = float(data.get("confidence", 0))
    if not 0 <= confidence <= 100:
        raise ValueError(f"confidence out of range: {confidence}")

    def strings(field: str) -> list[str]:
        value = data.get(field) or []
        return [str(x) for x in value] if isinstance(value, list) else [str(value)]

    return {
        "rating": rating,
        "confidence": confidence,
        "summary": str(data.get("summary", "")),
        "thesis": str(data.get("thesis", "")),
        "bull_case": strings("bull_case"),
        "bear_case": strings("bear_case"),
        "what_would_change": strings("what_would_change"),
    }


def build_user_prompt(record: CycleRecord, ticker: str, base: TickerVerdict,
                      fundamentals: str) -> str:
    lines = [f"STOCK: {ticker}   AS OF: {record.as_of}   LAST CLOSE: ${record.marks[ticker]:,.2f}", ""]

    votes = base.votes
    if base.desk_score is None:
        lines.append("MECHANICAL DESK SCORE: none (no analyst stated a view)")
    else:
        lines.append(
            f"MECHANICAL DESK SCORE: {base.desk_score:+.2f} -> {base.desk_rating} "
            f"({votes.get('bullish', 0)} bullish, {votes.get('bearish', 0)} bearish, "
            f"{votes.get('neutral', 0)} neutral, {votes.get('no_view', 0)} without a view)")

    lines += ["", "DATA CAVEATS:"]
    lines += [f"- {c}" for c in base.data_caveats] or ["- none detected"]

    lines += ["", f"RECENT NEWS (headlines only, newest first: the last {_NEWS_DAYS} days, "
              "plus the days around the latest earnings release):"]
    lines += [f"- {h}" for h in base.headlines] or ["- none found"]

    lines += ["", "FUNDAMENTALS THE ANALYSTS SAW (trailing twelve months, point-in-time as filed):",
              fundamentals, "", "ANALYST CALLS:"]
    for s in distinct_signals(record, ticker):
        lines += [_call_header(s), (s.reasoning or "(no written reasoning)").strip(), ""]

    lines.append("PORTFOLIO MATH (relative to the other names in this run; capped by risk limits):")
    for sr in record.strategies:
        if ticker in sr.convictions:
            lines.append(f"- {sr.name}: blended conviction {sr.convictions[ticker]:+.2f}, "
                         f"sleeve weight {sr.weights.get(ticker, 0.0):+.0%}")
    lines.append(f"- netted target {record.target_weights.get(ticker, 0.0):+.0%}, "
                 f"after risk limits {record.final_weights.get(ticker, 0.0):+.0%}, "
                 f"names in this run: {len(record.marks)}")
    return "\n".join(lines)


def _call_header(s: Signal) -> str:
    who = _DISPLAY.get(s.model_name, s.model_name)
    if s.metadata.get("abstained") is True:
        return f"[{who}] ABSTAINED"
    if not has_view(s):
        return f"[{who}] NO VIEW"
    call = s.metadata.get("signal") or ("bullish" if s.value > 0 else "bearish" if s.value < 0 else "neutral")
    confidence = s.metadata.get("confidence")
    detail = f", {confidence:.0f}% confidence" if isinstance(confidence, (int, float)) else ""
    return f"[{who}] {call.upper()}{detail} (conviction {s.value:+.2f})"


def _fundamentals(data_client: DataClient, ticker: str, as_of: str) -> str:
    try:
        text = build_snapshot(ticker, as_of, data_client).render()
    except InsufficientData as exc:
        text = f"(unavailable: {exc})"
    try:
        facts = data_client.get_company_facts(ticker)
    except Exception as exc:  # a profile is context, never a reason to fail the verdict
        logger.warning("company profile for %s failed: %s", ticker, exc)
        facts = None
    if facts is not None and facts.description:
        text = f"What the company does (current description): {facts.description}\n\n{text}"
    return text


def _headlines(data_client: DataClient, ticker: str, as_of: str) -> list[str]:
    """'YYYY-MM-DD  headline' lines, newest first, duplicates removed."""
    fetch = getattr(data_client, "get_news", None)
    if fetch is None:
        return []
    today = date.fromisoformat(as_of[:10])
    window_start = today - timedelta(days=_NEWS_DAYS)
    try:
        items = list(fetch(ticker, today.isoformat(), window_start.isoformat(), _NEWS_LIMIT))
        release = _last_release(data_client, ticker, today)
        if release is not None and release < window_start:
            end = min(today, release + timedelta(days=_RELEASE_AFTER_DAYS))
            items += fetch(ticker, end.isoformat(),
                           (release - timedelta(days=_RELEASE_BEFORE_DAYS)).isoformat(), _RELEASE_LIMIT)
    except Exception as exc:  # news is context; its absence must not sink the verdict
        logger.warning("news for %s failed: %s", ticker, exc)
        return []
    seen: set[str] = set()
    lines: list[str] = []
    for item in sorted(items, key=lambda n: n.date or "", reverse=True):
        title = " ".join((item.title or "").split())
        if title and title.lower() not in seen:
            seen.add(title.lower())
            lines.append(f"{(item.date or '')[:10]}  {title}")
    return lines


def _last_release(data_client: DataClient, ticker: str, today: date) -> date | None:
    fetch = getattr(data_client, "get_earnings_history", None)
    if fetch is None:
        return None
    try:
        dates = [r.filing_date for r in fetch(ticker, 4) if r.filing_date]
    except Exception as exc:
        logger.warning("earnings history for %s failed: %s", ticker, exc)
        return None
    past = [date.fromisoformat(d[:10]) for d in dates if d[:10] <= today.isoformat()]
    return max(past) if past else None


def _caveats(data_client: DataClient, ticker: str, as_of: str) -> list[str]:
    fetch = getattr(data_client, "get_data_caveats", None)
    if fetch is None:
        return []
    try:
        return list(fetch(ticker, as_of))
    except AttributeError:
        return []
    except Exception as exc:  # a caveat check must never sink the verdict it annotates
        logger.warning("data caveat check failed for %s: %s", ticker, exc)
        return []


def _merge(base: TickerVerdict, parsed: dict, model: str, cached: bool) -> TickerVerdict:
    return base.model_copy(update={**parsed, "model": model, "cached": cached, "error": None})
