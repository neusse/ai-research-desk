"""Verdict tests: the mechanical desk rating and the research manager, with a fake LLM."""

import json
from pathlib import Path

from hedge_fund.fund.spec import load_spec
from hedge_fund.llm import PromptCache
from hedge_fund.models import Signal
from hedge_fund.pipeline.models import CycleRecord, StrategyRecord
from hedge_fund.verdict import ResearchManager, add_verdicts, desk_rating, desk_verdict
from hedge_fund.verdict.manager import build_user_prompt

EXAMPLE = Path(__file__).resolve().parent.parent / "fund" / "example.yaml"


def _llm(name, value, signal, confidence, reasoning="because"):
    return Signal(model_name=name, ticker="V", date="2026-09-15", value=value, reasoning=reasoning,
                  metadata={"signal": signal, "confidence": confidence, "abstained": False})


def _record(signals_by_strategy):
    strategies = [StrategyRecord(name=name, slice=1 / len(signals_by_strategy), signals=signals,
                                 convictions={"V": 0.3}, weights={"V": 1.0})
                  for name, signals in signals_by_strategy.items()]
    return CycleRecord(fund="test", as_of="2026-09-15", spec=load_spec(EXAMPLE), universe=["V"],
                       marks={"V": 374.94}, skipped=[], strategies=strategies,
                       target_weights={"V": 0.33}, clamps=[], final_weights={"V": 0.25},
                       equity_before=100000, cash_before=100000, orders=[], fills=[],
                       positions={}, cash=100000, nav=100000)


def _desk():
    buffett = _llm("buffett", 0.85, "bullish", 85, "Wonderful business at a fair price.")
    graham = _llm("graham", 0.0, "neutral", 35, "I am handed no price.")
    lynch = _llm("lynch", -0.40, "bearish", 40)
    pead = Signal(model_name="pead", ticker="V", date="2026-09-15", value=0.0)  # no event: no view
    druck = Signal(model_name="druckenmiller", ticker="V", date="2026-09-15", value=0.0,
                   reasoning="abstained: LLM call failed", metadata={"abstained": True})
    return _record({
        "deep-value": [graham, buffett],
        "fundamental-ls": [buffett, graham, lynch, druck],  # personas repeat across pods
        "earnings-drift": [pead],
    })


def test_bands():
    assert [desk_rating(x) for x in (0.9, 0.5, 0.2, 0.0, -0.2, -0.5)] == [
        "Buy", "Buy", "Overweight", "Hold", "Underweight", "Sell"]


def test_desk_counts_each_analyst_once_and_skips_non_views():
    v = desk_verdict(_desk(), "V")
    # buffett +0.85, graham 0.0 (a stated neutral), lynch -0.40; pead and druck excluded
    assert v.desk_score == round((0.85 + 0.0 - 0.40) / 3, 4)
    assert v.desk_rating == "Hold"
    assert v.votes == {"bullish": 1, "bearish": 1, "neutral": 1, "no_view": 2}


class FakeLLM:
    model = "claude-opus-5"

    def __init__(self, response=None, error=None):
        self.calls, self._response, self._error = 0, response, error

    def complete(self, system, user):
        self.calls += 1
        self.last_user = user
        if self._error:
            raise self._error
        return self._response


VERDICT = json.dumps({
    "rating": "overweight", "confidence": 60, "summary": "Quality compounder, fair price.",
    "thesis": "Buffett's case holds; Graham's neutral rests on missing price data.",
    "bull_case": ["Network effects"], "bear_case": ["Regulatory pressure"],
    "what_would_change": ["Cross-border volume slowdown"],
})


class CaveatClient:
    def get_data_caveats(self, ticker, as_of):
        return ["Fundamentals are a quarter stale."]

    def get_news(self, ticker, end_date, start_date=None, limit=1000):
        from hedge_fund.data.models import CompanyNews
        self.news_window = (start_date, end_date, limit)
        return [CompanyNews(ticker=ticker, title="Visa guides FY revenue above consensus",
                            source="Benzinga", date="2026-09-10T12:00:00Z"),
                CompanyNews(ticker=ticker, title="Visa  guides FY revenue above consensus",
                            source="Benzinga", date="2026-09-10T12:05:00Z")]

    def get_financial_metrics(self, *a, **kw):
        return []

    def get_company_facts(self, ticker):
        from hedge_fund.data.models import CompanyFacts
        return CompanyFacts(ticker=ticker, name="Visa Inc.", description="Runs a global payments network.")


def test_manager_verdict_is_parsed_merged_and_cached(tmp_path):
    llm = FakeLLM(VERDICT)
    manager = ResearchManager(llm=llm, cache=PromptCache(tmp_path))
    record = add_verdicts(_desk(), CaveatClient(), manager)
    v = record.verdicts["V"]
    assert (v.rating, v.confidence, v.desk_rating) == ("Overweight", 60, "Hold")
    assert v.bear_case == ["Regulatory pressure"] and v.error is None and not v.cached
    assert "Fundamentals are a quarter stale." in llm.last_user
    assert "Graham" in llm.last_user and "I am handed no price." in llm.last_user
    assert "Runs a global payments network." in llm.last_user
    assert "- 2026-09-10  Visa guides FY revenue above consensus" in llm.last_user
    assert v.headlines == ["2026-09-10  Visa guides FY revenue above consensus"]  # deduped

    again = add_verdicts(_desk(), CaveatClient(), ResearchManager(llm=llm, cache=PromptCache(tmp_path)))
    assert again.verdicts["V"].cached and llm.calls == 1  # same prompt, same day: free


def test_manager_failure_keeps_the_desk_rating(tmp_path):
    for llm in (FakeLLM(error=RuntimeError("overloaded")), FakeLLM('{"rating": "Moon"}')):
        v = add_verdicts(_desk(), CaveatClient(), ResearchManager(llm=llm, cache=PromptCache(tmp_path))).verdicts["V"]
        assert v.rating is None and v.desk_rating == "Hold" and v.error


def test_without_a_manager_there_is_no_llm_call():
    v = add_verdicts(_desk(), CaveatClient()).verdicts["V"]
    assert v.desk_rating == "Hold" and v.rating is None and v.data_caveats
    assert v.headlines == []  # news is only fetched for the manager


def test_every_analyst_abstaining_withholds_manager_rating(tmp_path):
    signals = [Signal(model_name=name, ticker="V", date="2026-09-15", value=0.0,
                      reasoning="insufficient data", metadata={"abstained": True})
               for name in ("graham", "buffett", "munger")]
    llm = FakeLLM(VERDICT)
    result = add_verdicts(_record({"value": signals}), CaveatClient(),
                          ResearchManager(llm=llm, cache=PromptCache(tmp_path)))
    verdict = result.verdicts["V"]
    assert verdict.rating is None and verdict.desk_rating is None
    assert verdict.votes["no_view"] == 3 and llm.calls == 0
    assert any("All analysts abstained" in c for c in verdict.data_caveats)


def test_news_reaches_back_to_the_latest_earnings_release(tmp_path):
    from hedge_fund.data.models import CompanyNews, EarningsRecord

    class ReleaseClient(CaveatClient):
        def get_earnings_history(self, ticker, limit=12):
            return [EarningsRecord(ticker=ticker, report_period="2026-06-30", source_type="8-K",
                                   filing_date="2026-08-04")]

        def get_news(self, ticker, end_date, start_date=None, limit=1000):
            news = {"2026-09-01": "Analyst cuts target", "2026-08-04": "Q2 EPS beats, raises guidance",
                    "2026-07-01": "Unrelated old item"}
            return [CompanyNews(ticker=ticker, title=t, source="Benzinga", date=f"{d}T12:00:00Z")
                    for d, t in news.items() if start_date <= d <= end_date]

    llm = FakeLLM(VERDICT)
    v = add_verdicts(_desk(), ReleaseClient(), ResearchManager(llm=llm, cache=PromptCache(tmp_path))).verdicts["V"]
    assert v.headlines == ["2026-09-01  Analyst cuts target", "2026-08-04  Q2 EPS beats, raises guidance"]


def test_news_failure_does_not_block_the_verdict(tmp_path):
    class NoNews(CaveatClient):
        def get_news(self, *a, **kw):
            raise RuntimeError("feed down")

    llm = FakeLLM(VERDICT)
    v = add_verdicts(_desk(), NoNews(), ResearchManager(llm=llm, cache=PromptCache(tmp_path))).verdicts["V"]
    assert v.rating == "Overweight" and "- none found" in llm.last_user


def test_prompt_warns_that_weights_are_relative():
    record = _desk()
    prompt = build_user_prompt(record, "V", desk_verdict(record, "V"), "(unavailable)")
    assert "names in this run: 1" in prompt and "NO VIEW" in prompt and "ABSTAINED" in prompt


def test_receipts_round_trip_and_old_receipts_still_load():
    record = add_verdicts(_desk(), CaveatClient())
    assert CycleRecord.model_validate_json(record.model_dump_json()).verdicts["V"].desk_rating == "Hold"
    old = json.loads(record.model_dump_json())
    old.pop("verdicts")
    assert CycleRecord.model_validate(old).verdicts == {}
