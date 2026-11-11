"""Run ai-hedge-fund (analysts + research manager) on one ticker and write JSON.

Executed by the desk with ai-hedge-fund's own interpreter:
    <ai-hedge-fund>\\.venv\\Scripts\\python.exe aihf_runner.py TICKER YYYY-MM-DD MANDATE OUT.json
Also computes the price anchors, since this engine owns the Alpaca data client.
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

DESK_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(DESK_DIR))

from desk import usage_meter  # noqa: E402
from desk.events import EventLog  # noqa: E402
from desk.levels import compute_anchors  # noqa: E402
from desk.valuation import build_valuation  # noqa: E402

usage_meter.install()

from hedge_fund.tui.keys import apply_credentials  # noqa: E402

apply_credentials()

from hedge_fund.brokers import SimBroker  # noqa: E402
from hedge_fund.data import CachedDataClient, make_data_client  # noqa: E402
from hedge_fund.fund import Fund, load_spec  # noqa: E402
from hedge_fund.pipeline import run_cycle  # noqa: E402
from hedge_fund.verdict import ResearchManager, add_verdicts  # noqa: E402
from hedge_fund.verdict.manager import _fundamentals  # noqa: E402
from hedge_fund.verdict.rating import distinct_signals, has_view  # noqa: E402

from desk import maxplan  # noqa: E402


class MaxLLM:
    """ai-hedge-fund's LLMClient protocol (complete(system, user) -> str) on the Max plan."""

    def __init__(self, model: str, effort: str | None = "medium") -> None:
        # The model that will answer: ai-hedge-fund keys its prompt cache on this name, so on the
        # ChatGPT plan a Claude id here would hand back Claude's cached answers as ChatGPT's.
        if maxplan.plan() == "chatgpt":
            from desk import chatgpt
            model = chatgpt.model_for(model)
        self.model = model
        self.effort = effort

    def complete(self, system: str, user: str) -> str:
        return maxplan.ask(user, system, self.model, self.effort).text

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        """The same call with the reply held to `schema` by the CLI (--json-schema on Claude, a
        strict schema on ChatGPT), so an answer cannot stop mid-JSON. Returned as JSON text, so
        the caller's own parsing and checks run unchanged."""
        return json.dumps(maxplan.ask(user, system, self.model, self.effort, schema=schema).data)


def _max_make_llm(model=None, timeout=60.0, max_tokens=4096, on_token=None):
    import os
    return MaxLLM(model or os.environ.get("HEDGE_FUND_LLM_MODEL") or "claude-sonnet-5")


if maxplan.enabled():
    # The personas and the research manager both build their client through make_llm(), which
    # every module imported by name; each copy is replaced. The model ids still come from
    # HEDGE_FUND_LLM_MODEL / HEDGE_FUND_MANAGER_MODEL. DESK_LLM=api restores the metered client.
    import hedge_fund.llm as _l
    import hedge_fund.llm.client as _lc
    import hedge_fund.signals.llm_agent as _la
    import hedge_fund.verdict.manager as _vm
    for _mod in (_l, _lc, _la, _vm):
        _mod.make_llm = _max_make_llm

VETERANS = {"buffett": ("Buffett", "Moats, returns on capital, a fair price"),
            "munger": ("Munger", "Invert: what makes this fail?"),
            "graham": ("Graham", "Margin of safety, or no deal"),
            "lynch": ("Lynch", "Fast growers at a sensible multiple"),
            "druckenmiller": ("Druckenmiller", "Inflections and momentum in the numbers"),
            "pead": ("Drift-O-Tron", "Post-earnings drift quant"),
            "analyst_consensus": ("Street Whisperer", "What the Street is paying for it")}


from desk.gist import gist as _gist  # noqa: E402  (shared with the dashboard)


_ANCHOR_HISTORY_DAYS = 400   # enough daily bars for a 200-day average and a 52-week range
_TARGET_NEWS_DAYS = 120      # a quarter and a bit of analyst price-target actions


def main() -> None:
    ticker, as_of, mandate, out = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
    spec = load_spec(Path.home() / ".hedge-fund" / "mandates" / f"{mandate}.yaml")
    events = EventLog.from_env()
    started = time.time()
    for name, role in VETERANS.values():
        events.emit("agent_queued", engine="value", who=name, role=role)
    with make_data_client() as raw:
        data = CachedDataClient(raw)
        record = run_cycle(Fund(spec), as_of, SimBroker(cash=spec.capital), data, [ticker])
        if ticker not in record.marks:
            raise SystemExit(f"ai-hedge-fund could not price {ticker} as of {as_of}")
        for s_ in distinct_signals(record, ticker):
            name, role = VETERANS.get(s_.model_name, (s_.model_name.replace("_", " ").title(), ""))
            call = ("abstained" if s_.metadata.get("abstained") else
                    "no view" if not has_view(s_) else s_.metadata.get("signal") or
                    ("bullish" if s_.value > 0 else "bearish" if s_.value < 0 else "neutral"))
            conf = s_.metadata.get("confidence")
            events.emit("agent_done", engine="value", who=name, role=role,
                        text=_gist(s_.reasoning or ""), full=s_.reasoning or "", step=s_.model_name,
                        call=call.upper() + (f" {conf:.0f}%" if conf else ""),
                        tone="bull" if call == "bullish" else "bear" if call == "bearish" else "neutral")
        events.emit("agent_queued", engine="value", who="Professor Ledger", role="Research manager")
        record = add_verdicts(record, data, ResearchManager())
        _v = record.verdicts[ticker]
        _manager_text = (_v.summary or "\n\n".join(_v.data_caveats) or
                         "No analyst call was available.")
        events.emit("agent_done", engine="value", who="Professor Ledger", role="Research manager",
                    step="research_manager", text=_gist(_manager_text),
                    full="\n\n".join(x for x in (_v.summary, _v.thesis) if x) or _manager_text,
                    call=f"{(_v.rating or _v.desk_rating or 'WITHHELD').upper()}"
                         + (f" {_v.confidence:.0f}%" if _v.confidence else ""),
                    tone="bull" if (_v.rating or "") in ("Buy", "Overweight") else
                         "bear" if (_v.rating or "") in ("Sell", "Underweight") else "neutral")
        fundamentals = _fundamentals(data, ticker, as_of)
        start = (date.fromisoformat(as_of) - timedelta(days=_ANCHOR_HISTORY_DAYS)).isoformat()
        bars = [{"date": p.time[:10], "high": p.high, "low": p.low, "close": p.close}
                for p in data.get_prices(ticker, start, as_of) if p.time[:10] <= as_of]
        valuation = _valuation(data, ticker, as_of, record.marks[ticker])

    verdict = record.verdicts[ticker]
    personas = []
    for s in distinct_signals(record, ticker):
        call = ("abstained" if s.metadata.get("abstained") else
                "no view" if not has_view(s) else s.metadata.get("signal") or
                ("bullish" if s.value > 0 else "bearish" if s.value < 0 else "neutral"))
        personas.append({"model": s.model_name, "call": call, "value": s.value,
                         "confidence": s.metadata.get("confidence"), "reasoning": s.reasoning or ""})

    # Upstream catches every model failure per persona and abstains, so a spent allowance or an
    # expired login (AKAM 2026-10-04) left a Buy built on the formula models alone, saved and
    # reused the same day. Stop without saving instead, as Edge Desk's runner does.
    notice = maxplan.first_limit_notice([*(p["reasoning"] for p in personas),
                                         getattr(verdict, "error", None) or ""])
    if notice:
        raise SystemExit(f"ai-hedge-fund stopped, nothing saved: {notice[:300]}")

    payload = {
        "engine": "ai-hedge-fund",
        "ticker": ticker,
        "date": as_of,
        "mandate": mandate,
        "rating": verdict.rating or verdict.desk_rating,
        "verdict": verdict.model_dump(),
        "personas": personas,
        "fundamentals": fundamentals,
        "last_close": record.marks[ticker],
        "anchors": compute_anchors(sorted(bars, key=lambda b: b["date"])),
        "valuation": valuation,
        "seconds": round(time.time() - started),
        "usage": maxplan.summary() if maxplan.enabled() else usage_meter.summary(),
        "reused": False,
    }
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _valuation(data, ticker: str, as_of: str, price: float) -> dict:
    metrics = data.get_financial_metrics(ticker, as_of, period="ttm", limit=20)
    rows = [{"period": m.report_period, "pe": m.price_to_earnings_ratio, "eps": m.earnings_per_share}
            for m in metrics]
    try:
        consensus = data.get_analyst_consensus(ticker)
        consensus = consensus.model_dump() if consensus else None
    except Exception as exc:  # Yahoo is enrichment; the panel still has the other methods
        print(f"analyst consensus unavailable: {exc}")
        consensus = None
    try:
        start = (date.fromisoformat(as_of) - timedelta(days=_TARGET_NEWS_DAYS)).isoformat()
        headlines = [(n.date or "", n.title) for n in data.get_news(ticker, as_of, start, 400)]
    except Exception as exc:
        print(f"news for target changes unavailable: {exc}")
        headlines = []
    return build_valuation(price, rows, consensus, headlines)


if __name__ == "__main__":
    main()
