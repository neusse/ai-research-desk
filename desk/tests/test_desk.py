"""Desk tests: agreement, horizons, debate enforcement, blind analysis, valuation, rendering. No network."""

import json
from pathlib import Path

import pytest

from desk.debate import DebateResult, Turn, reports_block, run_debate
from desk.engines import load_cli_reports
from desk.horizons import action_for
from desk.levels import compute_anchors
from desk.memo import outcome
from desk.pipeline import analyze, native_views
from desk.ratings import agree, more_conservative
from desk.render import markdown, vault_note
from desk.valuation import build_valuation, parse_target_changes


def quiet(*_):
    pass


def test_same_side_agreement():
    assert agree("Buy", "Overweight") and agree("Sell", "Underweight") and agree("Hold", "Hold")
    assert not agree("Overweight", "Hold") and not agree("Underweight", "Hold")
    assert more_conservative("Buy", "Overweight") == "Overweight"
    assert more_conservative("Sell", "Underweight") == "Underweight"


def _engines(ta="Hold", aihf="Overweight"):
    ta_payload = {"engine": "tradingagents", "rating": ta, "reports": {
        "final_trade_decision": f"**Rating**: {ta}\n\n**Executive Summary**: Hold at current weight.\n\n"
                                "**Investment Thesis**: x\n\n**Time Horizon**: 3-6 months"}}
    aihf_payload = {
        "engine": "ai-hedge-fund", "rating": aihf, "last_close": 111.24,
        "verdict": {"rating": aihf, "confidence": 66, "summary": "Beat and raised.", "thesis": "t",
                    "bull_case": ["b"], "bear_case": ["r"], "what_would_change": ["w"],
                    "data_caveats": [], "headlines": ["2026-08-04  Q2 EPS beats"],
                    "desk_score": 0.38, "desk_rating": "Overweight"},
        "personas": [{"model": "buffett", "call": "bullish", "confidence": 68, "reasoning": "moat"}],
        "fundamentals": "Company: ECG", "anchors": {"last_close": 111.24, "sma_50": 127.66},
        "valuation": build_valuation(111.24, _rows(), _consensus(), []),
    }
    return ta_payload, aihf_payload


def _rows():
    """20 TTM rows newest first: EPS doubling over 3 years, P/E between 15 and 35."""
    return [{"period": f"p{i}", "eps": round(5.0 / (1.26 ** (i / 4)), 3), "pe": 15 + i} for i in range(20)]


def _consensus():
    return {"analyst_count": 6, "recommendation_key": "buy", "recommendation_mean": 1.5,
            "target_low_price": 150.0, "target_mean_price": 176.0, "target_high_price": 200.0}


def _turn(decision, rating, argument="because", changed="", gives_up=()):
    return {"decision": decision, "rating": rating, "holes_in_other_report": ["hole"],
            "new_evidence": [], "argument": argument, "what_changed_my_mind": changed,
            "gives_up": list(gives_up)}


def _memo():
    level = {"price": "104-108", "reason": "near the 20-day low"}
    return {"headline": "Quality at a fair price", "summary": "s", "action_note": "a", "valuation_view": "v",
            "key_reasons": ["r"], "key_risks": ["k"],
            "levels": {"entry_zone": level, "stop": level, "first_target": level, "trim": level},
            "what_to_watch": ["w"], "disagreement_crux": "", "super_manager_view": ""}


class ScriptedLLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def json(self, blocks, schema, max_tokens=16000, system=None, model=None, effort=None):
        self.prompts.append(blocks)
        return self.replies.pop(0)


def _reports():
    ta, aihf = _engines()
    return reports_block("ECG", "2026-09-15", {"A": ta, "B": aihf})


def test_debate_ends_when_a_manager_concedes_and_adopts_the_other_rating():
    llm = ScriptedLLM([_turn("defend", "Hold"), _turn("concede", "Hold", changed="the cash-flow gap")])
    result = run_debate(llm, _reports(), "long_term", {"A": "Hold", "B": "Overweight"}, 3, say=quiet)
    assert result.outcome == "agreed" and result.conceded_by == "B" and result.agreed_rating == "Hold"
    assert len(result.turns) == 2
    # the reports block is identical for every turn, and marked for caching
    assert llm.prompts[0][0] == llm.prompts[1][0] and "cache_control" in llm.prompts[0][0]
    assert "Long term" in llm.prompts[0][1]["text"]


def test_a_bad_turn_is_asked_again_and_never_repaired():
    llm = ScriptedLLM([_turn("defend", "Hold"),
                       _turn("defend", "Hold"),                          # B drifts to Hold while defending
                       _turn("defend", "Buy"),                           #   asked again: holds Buy
                       _turn("concede", "Overweight", changed="x"),      # A concedes to a rating nobody holds
                       _turn("concede", "Buy", changed="the backlog")])  #   asked again: adopts B's exactly
    result = run_debate(llm, _reports(), "swing", {"A": "Hold", "B": "Buy"}, 3, say=quiet)
    assert "compromise" in llm.prompts[2][1]["text"] and "REJECTED" in llm.prompts[2][1]["text"]
    assert result.turns[1].rating == "Buy" and "asked again" in result.turns[1].note
    assert "not Overweight" in llm.prompts[4][1]["text"]
    assert result.turns[2].decision == "concede" and result.turns[2].rating == "Buy"
    assert result.outcome == "agreed" and result.agreed_rating == "Buy" and len(result.turns) == 3


def test_a_turn_wrong_twice_is_recorded_as_holding_and_manufactures_no_agreement():
    """The audit's A04. The old code accepted a concession with no reason, and rewrote one to a
    rating nobody held into another manager's rating; both manufactured an agreement."""
    llm = ScriptedLLM([_turn("defend", "Overweight"),
                       _turn("concede", "Overweight", changed=""),     # right rating, no reason given
                       _turn("concede", "Buy", changed="x"),           #   asked again: nobody holds Buy
                       _turn("defend", "Overweight"), _turn("defend", "Underweight")])
    result = run_debate(llm, _reports(), "long_term", {"A": "Overweight", "B": "Underweight"}, 2, say=quiet)
    held = result.turns[1]
    assert held.decision == "defend" and held.rating == "Underweight" and "rejected twice" in held.note
    assert "what changed your mind" in llm.prompts[2][1]["text"]
    assert result.conceded == [] and result.outcome == "no_agreement" and len(result.turns) == 4


def test_no_agreement_after_max_rounds():
    llm = ScriptedLLM([_turn("defend", "Hold"), _turn("defend", "Overweight")] * 2)
    result = run_debate(llm, _reports(), "swing", {"A": "Hold", "B": "Overweight"}, 2, say=quiet)
    assert result.outcome == "no_agreement" and len(result.turns) == 4
    assert "last turn" in llm.prompts[-1][1]["text"]


def test_both_horizons_debate_only_where_the_voters_disagree():
    ta, aihf = _engines(ta="Hold", aihf="Overweight")
    llm = ScriptedLLM([
        {"rating": "Overweight", "rationale": "fundamentals"}, {"rating": "Overweight", "rationale": "ok"},
        _memo(),                                                                # long term: same side
        {"rating": "Hold", "rationale": "broken chart"}, {"rating": "Overweight", "rationale": "catalyst"},
        _turn("defend", "Hold"), _turn("concede", "Hold", changed="below all averages"),
        {"corrections": []},                                                    # the fact check after a debate
        _memo(),                                                                # swing: debated
    ])
    results = analyze(llm, "ECG", "2026-09-15", {"A": ta, "B": aihf}, ["long_term", "swing"],
                      owns=True, max_rounds=3, say=quiet)
    assert set(results) == {"long_term", "swing"} and len(llm.prompts) == 9     # no timeframe call
    lt, sw = results["long_term"], results["swing"]
    assert lt["outcome"]["conviction"] == "Agreed before debate" and lt["debate"] is None
    assert sw["outcome"]["rating"] == "Hold" and sw["outcome"]["conviction"] == "Agreed after debate"
    assert sw["span"] == "2 days to 8 weeks" and lt["span"] == "1 year or more"
    assert sw["action"] == "Hold; no new money"
    assert "2 days to 8 weeks" in llm.prompts[3][1]["text"] and "2 to 8 week end" in llm.prompts[3][1]["text"]
    memo_prompts = {2, 8}
    analysis = [p[1]["text"] for i, p in enumerate(llm.prompts) if i not in memo_prompts]
    assert all("INVESTOR POSITION" not in t and "owns" not in t for t in analysis)
    assert "already owns this stock" in llm.prompts[2][1]["text"]
    assert "never call the outcome high" in llm.prompts[2][1]["text"].lower()


def test_as_they_ran_is_retired_and_each_team_s_own_call_is_kept_as_provenance():
    ta, aihf = _engines(ta="Hold", aihf="Overweight")
    with pytest.raises(ValueError, match="as_they_ran"):
        analyze(ScriptedLLM([]), "ECG", "2026-09-15", {"A": ta, "B": aihf}, ["as_they_ran"],
                owns=False, max_rounds=3, say=quiet)
    assert native_views({"A": ta, "B": aihf, "C": _edge()}) == {
        "A": {"rating": "Hold", "timeframe": "3-6 months"},          # stated in its own decision
        "B": {"rating": "Overweight", "timeframe": None},           # never states one; no model is asked
        "C": {"rating": "Underweight", "timeframe": "1 year or more"}}


def test_stated_horizon():
    from desk.views import stated_horizon
    assert stated_horizon({"final_trade_decision": "**Time Horizon**: 3-6 months."}) == "3-6 months"
    assert stated_horizon({"final_trade_decision": "no horizon here"}) is None


def test_actions_follow_rating_and_position():
    assert action_for("Overweight", owns=False) == "Start a starter position"
    assert action_for("Underweight", owns=True) == "Trim the position"
    assert action_for(None, owns=False).startswith("No consensus: stay out")


def test_outcome_labels():
    assert outcome({"A": "Buy", "B": "Overweight"}, None)["rating"] == "Overweight"
    assert outcome({"A": "Buy", "B": "Overweight"}, None)["conviction"] == "Agreed before debate"
    agreed = DebateResult([Turn(1, "A", "defend", "Hold", "a"), Turn(1, "B", "concede", "Hold", "b")],
                          "agreed", {"A": "Hold", "B": "Hold"}, conceded_by="B", agreed_rating="Hold")
    assert outcome({"A": "Hold", "B": "Overweight"}, agreed)["conviction"] == "Agreed after debate"
    assert "ai-hedge-fund manager conceded to TradingAgents" in outcome({"A": "Hold", "B": "Overweight"}, agreed)["how"]
    split = DebateResult([], "no_agreement", {"A": "Hold", "B": "Overweight"})
    assert outcome({"A": "Hold", "B": "Overweight"}, split)["rating"] is None
    assert outcome({"A": "Hold", "B": "Overweight"}, split)["conviction"] == "Split"


def test_opus_5_5_is_priced_at_its_own_rate_not_opus_5s():
    from desk.usage_meter import cost, price_for
    assert price_for("claude-opus-5-5") == (4.00, 20.00) and price_for("claude-opus-5") == (5.00, 25.00)
    row = {"input": 0, "cache_write": 0, "cache_read": 1_000_000, "output": 0}
    assert cost([{**row, "model": "claude-opus-5-5"}]) == 0.20        # cache reads 0.05x
    assert cost([{**row, "model": "claude-opus-5"}]) == 0.50          # cache reads 0.1x


def test_valuation_methods():
    panel = build_valuation(111.24, _rows(), _consensus(), [])
    h, p, s = panel["own_history"], panel["peg"], panel["street"]
    assert h["median_pe"] == 24.5 and h["fair_mid"] == round(5.0 * 24.5, 2)
    assert 0.25 < p["eps_cagr"] < 0.27 and p["fair_pe"] == round(p["eps_cagr"] * 100, 1)
    assert s["upside_to_mean"] == round(176 / 111.24 - 1, 4)
    shrinking = [{"period": "p", "eps": 5.0 - i * 0.1, "pe": 20} for i in range(20)][::-1][::-1]
    assert build_valuation(100, [{"period": "a", "eps": 3.0, "pe": 20}] + shrinking[1:], None, [])["peg"]["fair_value"] is None


def test_target_changes_from_benzinga_headlines():
    t = parse_target_changes([
        ("2026-08-21", "DA Davidson Initiates Coverage On Everus Construction Group with Buy Rating, "
                       "Announces Price Target of $168"),
        ("2026-08-13", "Cantor Fitzgerald Maintains Neutral on Everus Construction Group, Lowers Price Target to $152"),
        ("2026-08-04", "Everus Construction Group Q2 EPS $1.64 Beats $1.16 Estimate"),
    ])
    assert (t["count"], t["lowered"]) == (2, 1)
    assert t["latest"][0] == {"date": "2026-08-21", "firm": "DA Davidson", "action": "Initiates Coverage On",
                              "rating": "Buy", "target": 168.0, "direction": "new"}


def test_markdown_and_vault_note_render_without_dashes():
    ta, aihf = _engines()
    horizons = {"long_term": {"restated": {"A": {"rating": "Hold", "rationale": "x"},
                                           "B": {"rating": "Hold", "rationale": "y"}},
                              "debate": None, "outcome": outcome({"A": "Hold", "B": "Hold"}, None),
                              "action": action_for("Hold", False), "memo": _memo()}}
    bundle = {"ticker": "ECG", "date": "2026-09-15", "owns": False, "horizons": horizons,
              "native": native_views({"A": ta, "B": aihf}),
              "tradingagents": ta, "ai_hedge_fund": aihf,
              "costs": {"tradingagents": None, "ai_hedge_fund": 0.1, "desk": 0.07, "total": 0.17},
              "files": {"json": "ECG-2026-09-15.json"}}
    md = markdown(bundle)
    assert "| Verdict | Rating | Agreement | Action for you |" in md and "| Agreed before debate |" in md
    assert "own call, on its own clock (provenance, not compared): quant desk Hold (3-6 months), " \
           "veterans Overweight (clock not stated)" in md
    assert "conviction: Agreed before debate" in vault_note(bundle, md)
    assert "# ECG desk memo, 2026-09-15" in md and "| Entry zone | 104-108 (unchecked) |" in md
    assert "## Valuation" in md and "mean 176.0" in md and "Hold at current weight." in md
    assert "Stay out; keep on the watchlist with an entry level" in md
    note = vault_note(bundle, md)
    assert note.startswith("---\ntype: thesis") and "rating_long_term: Hold" in note and "owned: false" in note
    assert chr(0x2014) not in md and chr(0x2013) not in md  # no em or en dashes


def test_cli_reports_are_reused_with_their_rating(tmp_path: Path):
    (tmp_path / "final_trade_decision.md").write_text("**Rating**: Hold\n\nbody", encoding="utf-8")
    payload = load_cli_reports("ECG", "2026-09-15", tmp_path)
    assert payload["rating"] == "Hold" and payload["reused"] and payload["reports"]["market_report"] == ""


def test_anchors():
    bars = [{"date": f"d{i}", "high": 10 + i + 1, "low": 10 + i - 1, "close": 10 + i} for i in range(60)]
    a = compute_anchors(bars)
    assert a["last_close"] == 69 and a["sma_50"] == 44.5 and a["sma_200"] is None
    assert a["high_20d"] == 70 and a["atr_14"] == 2.0


def test_every_desk_call_shares_one_system_prompt_so_the_reports_cache_is_reused():
    import inspect

    from desk import debate, llm, memo
    for fn in (debate.run_debate, debate.rate_for_horizon, memo.write_memo):
        assert "system" not in inspect.getsource(fn)
    assert inspect.signature(llm.DeskLLM.json).parameters["system"].default == llm.DESK_SYSTEM


# ---------------------------------------------------------------- three desks
def _edge(rating="Underweight"):
    return {"engine": "edge-desk", "rating": rating, "score": 37.4, "conviction": "High",
            "swing": {"signal": "GREEN", "why": "above the 20-day and the 50-day"},
            "report_md": "# Edge report", "last_close": 111.24, "anchors": {}, "valuation": {}}


def test_two_against_one_with_nobody_moving_is_gridlock_and_publishes_no_rating():
    llm = ScriptedLLM([_turn("defend", "Overweight"), _turn("defend", "Overweight"), _turn("defend", "Underweight")] * 2)
    start = {"A": "Overweight", "B": "Overweight", "C": "Underweight"}
    result = run_debate(llm, _reports(), "long_term", start, 2, say=quiet)
    assert result.outcome == "gridlock" and len(result.turns) == 6
    out = outcome(start, result)
    assert out["rating"] is None and out["conviction"] == "Gridlock" and "two against one" in out["how"]
    assert action_for(out["rating"], True).startswith("No consensus")


def test_three_different_sides_is_a_split_and_the_holdout_can_be_won_over():
    llm = ScriptedLLM([_turn("defend", "Buy"), _turn("defend", "Hold"), _turn("defend", "Sell")])
    split = run_debate(llm, _reports(), "long_term", {"A": "Buy", "B": "Hold", "C": "Sell"}, 1, say=quiet)
    assert split.outcome == "no_agreement"

    llm = ScriptedLLM([_turn("defend", "Overweight"), _turn("defend", "Buy"),
                       _turn("concede", "Hold", changed="the cash flow math"),      # a rating nobody holds
                       _turn("concede", "Overweight", changed="the cash flow math")])  # asked again: A's exactly
    won = run_debate(llm, _reports(), "long_term", {"A": "Overweight", "B": "Buy", "C": "Underweight"}, 2, say=quiet)
    assert won.outcome == "agreed" and won.conceded == ["C"] and len(won.turns) == 3
    assert won.ratings["C"] == "Overweight" and "asked again" in won.turns[-1].note
    assert won.agreed_rating == "Overweight"                                        # the most conservative
    assert "Edge Desk manager conceded" in outcome({"A": "Overweight", "B": "Buy", "C": "Underweight"}, won)["how"]


def test_edge_votes_long_term_with_its_own_rating_and_sits_out_swing():
    ta, aihf = _engines(ta="Overweight", aihf="Overweight")
    llm = ScriptedLLM([
        {"rating": "Overweight", "rationale": "a"}, {"rating": "Overweight", "rationale": "b"},   # swing: A, B only
        _memo(),
        {"rating": "Overweight", "rationale": "a"}, {"rating": "Overweight", "rationale": "b"},   # long term: A, B asked
        _turn("defend", "Overweight"), _turn("defend", "Overweight"), _turn("defend", "Underweight"),
        _turn("defend", "Overweight"), _turn("defend", "Overweight"), _turn("defend", "Underweight"),
        {"corrections": []},
        _memo(),
    ])
    results = analyze(llm, "ECG", "2026-09-15", {"A": ta, "B": aihf, "C": _edge()}, ["swing", "long_term"],
                      owns=False, max_rounds=3, say=quiet)
    assert results["swing"]["voters"] == ["A", "B"] and "C" not in results["swing"]["restated"]
    assert "does not vote on the swing horizon" in results["swing"]["outcome"]["note"]
    lt = results["long_term"]
    assert lt["voters"] == ["A", "B", "C"] and lt["restated"]["C"]["own"] is True
    assert lt["outcome"]["status"] == "gridlock" and len(lt["debate"]["turns"]) == 6    # capped at 2 rounds
    assert "business case" in llm.prompts[7][1]["text"]                                       # Edge's manager is told what it argues for


def test_reports_mode_memo_lists_each_engine_without_a_combined_rating():
    ta, aihf = _engines()
    bundle = {"ticker": "ECG", "date": "2026-09-15", "owns": False, "horizons": {}, "mode": "reports",
              "tradingagents": ta, "ai_hedge_fund": None, "edge_desk": _edge(),
              "costs": {"tradingagents": 0, "edge_desk": 0, "desk": 0, "total": 0, "billing": "max"},
              "files": {"json": "x.json"}}
    md = markdown(bundle)
    assert "# ECG desk reports" in md and "without a debate" in md and "| Edge Desk | Underweight (37.4/100)" in md
    assert "ai-hedge-fund" not in md.split("## Desk reports")[1].split("---")[0]


def test_desk_memo_preserves_edge_methodology_annotations():
    edge = {**_edge(), "methodology": ("Foreign-filer normalization used. Standard US GAAP facts from the "
                                        "issuer's annual 20-F and financial 6-K filings were converted into "
                                        "point-in-time TTM periods.")}
    bundle = {"ticker": "ARM", "date": "2026-11-10", "owns": False, "horizons": {}, "mode": "reports",
              "tradingagents": None, "ai_hedge_fund": None, "edge_desk": edge,
              "costs": {"edge_desk": 0, "desk": 0, "total": 0, "billing": "max"},
              "files": {"json": "x.json"}}
    md = markdown(bundle)
    assert "## Methodology annotation" in md
    assert "annual 20-F and financial 6-K" in md


def test_withdrawn_needs_the_owner_to_have_conceded_in_that_round():
    """The audit's A05. The old check accepted "withdrawn" if the owner merely spoke in the cited round
    (every manager speaks every round) or conceded in any round at all."""
    from desk.corrections import verify
    texts = {"A": "Cash conversion broke in Q2. Goodwill nearly doubled in the year."}
    turns = [{"desk": "A", "round": 1, "decision": "defend"}, {"desk": "B", "round": 1, "decision": "defend"},
             {"desk": "A", "round": 2, "decision": "concede", "gives_up": ["Goodwill nearly doubled in the year."]}]
    item = {"desk": "A", "quote": "Goodwill nearly doubled in the year.", "status": "withdrawn",
            "reason": "r", "raised_by": "B"}
    assert verify([{**item, "round": 2}], texts, turns)[0]["status"] == "withdrawn"
    assert verify([{**item, "round": 1}], texts, turns)[0]["status"] == "challenged"   # it spoke, and defended
    assert verify([{**item, "round": 3}], texts, turns)[0]["status"] == "challenged"   # it never spoke then


def test_corrections_keep_only_exact_quotes_and_never_strike_what_the_owner_defended():
    from desk.corrections import verify
    texts = {"A": "Cash conversion broke in Q2. Goodwill nearly doubled.", "B": "Free cash flow per share is 4.90."}
    turns = [{"desk": "A", "round": 1, "decision": "defend"}, {"desk": "B", "round": 1, "decision": "defend"},
             {"desk": "A", "round": 2, "decision": "concede", "gives_up": ["Cash conversion broke in Q2"]}]
    items = [
        {"desk": "A", "quote": "Cash conversion broke in Q2.", "status": "withdrawn", "reason": "H1 covers 138%", "raised_by": "B", "round": 2},
        {"desk": "A", "quote": "Cash conversion collapsed", "status": "withdrawn", "reason": "paraphrase", "raised_by": "B", "round": 2},
        {"desk": "B", "quote": "Free cash flow per share is 4.90.", "status": "withdrawn", "reason": "trailing figure", "raised_by": "A", "round": 3},
    ]
    kept = verify(items, texts, turns)
    assert [k["quote"] for k in kept] == ["Cash conversion broke in Q2.", "Free cash flow per share is 4.90."]
    assert kept[0]["status"] == "withdrawn"                 # its owner conceded
    assert kept[1]["status"] == "challenged"                # B never accepted it, so it is flagged, not struck


def test_a_full_run_saves_each_team_s_own_call_and_no_retired_horizon(tmp_path, monkeypatch):
    """run_one end to end with fake teams and a scripted model: the path a real run takes, so a
    mistake here is found now rather than on the next run that spends the Max allowance."""
    import argparse
    import json

    import desk.__main__ as cli
    import desk.engines
    import desk.llm

    ta, aihf = _engines(ta="Overweight", aihf="Overweight")

    class FakeLLM(ScriptedLLM):
        max = True

        def __init__(self, model=None):
            super().__init__([{"rating": "Overweight", "rationale": "a"}, {"rating": "Overweight", "rationale": "b"},
                              _memo()] * 2)
            self.calls = []

        def cost(self):
            return 0.0

        def notional(self):
            return 0.0

    monkeypatch.setattr(desk.engines, "run_selected", lambda *a, **k: {"A": ta, "B": aihf})
    monkeypatch.setattr(desk.llm, "DeskLLM", FakeLLM)
    monkeypatch.setattr(cli, "MEMOS_DIR", tmp_path)
    args = argparse.Namespace(date="2026-09-24", fresh=False, mandate="test-2", rounds=3, horizon="all",
                              no_debate=False, engines=["quant", "vets"], no_vault=True)
    cli.run_one("ECG", False, args)

    bundle = json.loads((tmp_path / "ECG-2026-09-24.json").read_text(encoding="utf-8"))
    assert list(bundle["horizons"]) == ["swing", "long_term"]
    assert bundle["native"] == {"A": {"rating": "Overweight", "timeframe": "3-6 months"},
                                "B": {"rating": "Overweight", "timeframe": None}}
    assert bundle["horizons"]["swing"]["span"] == "2 days to 8 weeks"
    run_dir = tmp_path / "runs" / f"ECG-2026-09-24-{bundle['run_id']}"      # every run keeps its own folder
    assert json.loads((run_dir / "memo.json").read_text(encoding="utf-8")) == bundle
    log = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    clocks = next(e for e in map(json.loads, log) if e["type"] == "timeframes")
    assert clocks["tape"] == "3-6 months" and "value" not in clocks        # emit drops None; the live view copes
    md = (tmp_path / "ECG-2026-09-24.md").read_text(encoding="utf-8")
    assert "Agreed before debate" in md and "As they ran" not in md and "(2 days to 8 weeks)" in md


# ---- Stage B1 (decision 2): one settled price date for every team -------------------------------

def test_the_price_session_is_the_last_settled_close():
    from datetime import datetime

    from desk.session import ET, price_session
    at = lambda h, m: datetime(2026, 9, 24, h, m, tzinfo=ET)
    assert price_session("2026-09-24", at(12, 15)) == "2026-09-23"      # mid-session: yesterday's close
    assert price_session("2026-09-24", at(16, 14)) == "2026-09-23"      # the close auction is still printing
    assert price_session("2026-09-24", at(16, 20)) == "2026-09-24"      # settled
    assert price_session("2026-09-15", at(12, 15)) == "2026-09-15"      # a past run keeps its date


def test_the_price_check_catches_mixed_and_late_dates():
    from desk.session import price_check
    b = lambda d: {"anchors": {"as_of": d}}
    assert price_check("2026-09-23", {"B": b("2026-09-23"), "C": b("2026-09-23")})["consistent"]
    assert price_check("2026-09-21", {"B": b("2026-09-19"), "C": b("2026-09-19")})["consistent"]   # a weekend
    mixed = price_check("2026-09-23", {"B": b("2026-09-22"), "C": b("2026-09-23")})
    assert not mixed["consistent"] and "different sessions" in mixed["problem"]
    late = price_check("2026-09-23", {"B": b("2026-09-23"), "C": b("2026-09-24")})
    assert not late["consistent"] and "edge priced after the session" in late["problem"]


def test_a_run_hands_every_team_the_session_not_the_run_date(tmp_path, monkeypatch):
    """The NVDA and AVGO runs of 2026-09-24 went out before the settle: Veterans priced from Sep 23,
    Edge Desk from Sep 24's live bar. Now every team gets the same, settled date."""
    import argparse
    import json

    import desk.__main__ as cli
    import desk.engines
    import desk.llm
    import desk.session

    ta, aihf = _engines(ta="Overweight", aihf="Overweight")
    aihf = {**aihf, "anchors": {**aihf["anchors"], "as_of": "2026-09-23"}}
    edge = {**_edge("Buy"), "anchors": {"as_of": "2026-09-24"}}          # the old mid-session behaviour
    seen = {}

    def run_selected(engines, ticker, as_of, *a, **k):
        seen["as_of"] = as_of
        return {"A": ta, "B": aihf, "C": edge}

    class FakeLLM(ScriptedLLM):
        max = True

        def __init__(self, model=None):
            super().__init__([])
            self.calls = []

        def cost(self):
            return 0.0

        def notional(self):
            return 0.0

    monkeypatch.setattr(desk.session, "price_session", lambda run_date, now=None: "2026-09-23")
    monkeypatch.setattr(desk.engines, "run_selected", run_selected)
    monkeypatch.setattr(desk.llm, "DeskLLM", FakeLLM)
    monkeypatch.setattr(cli, "MEMOS_DIR", tmp_path)
    args = argparse.Namespace(date="2026-09-24", fresh=False, mandate="test-2", rounds=3, horizon="all",
                              no_debate=True, engines=["quant", "vets", "edge"], no_vault=True)
    cli.run_one("NVDA", False, args)
    bundle = json.loads((tmp_path / "NVDA-2026-09-24.json").read_text(encoding="utf-8"))
    assert seen["as_of"] == "2026-09-23" and bundle["price_session"] == "2026-09-23"
    assert bundle["price_check"]["consistent"] is False                   # the reused Edge report is caught


def test_the_rendered_memo_has_no_dashes_but_the_stored_report_keeps_them():
    """Stage B7. TradingAgents wrote "1.25–1.5x" into NVDA 2026-09-24's memo. The stored report
    stays verbatim for the fact check; the memo the reader sees is cleaned."""
    from desk.render import plain_dashes
    assert plain_dashes("about 1.25–1.5x benchmark weight") == "about 1.25-1.5x benchmark weight"
    assert plain_dashes("$360–$368") == "$360-$368"
    assert plain_dashes("strong — but priced") == "strong, but priced"
    ta, aihf = _engines()
    ta = {**ta, "reports": {"final_trade_decision": "**Rating**: Hold\n\n**Executive Summary**: Hold 1.25–1.5x "
                                                   "weight — no adds.\n\n**Time Horizon**: 3-6 months"}}
    horizons = {"long_term": {"restated": {"A": {"rating": "Hold", "rationale": "x"}, "B": {"rating": "Hold", "rationale": "y"}},
                              "debate": None, "outcome": outcome({"A": "Hold", "B": "Hold"}, None),
                              "action": action_for("Hold", False), "memo": _memo()}}
    bundle = {"ticker": "ECG", "date": "2026-09-15", "owns": False, "horizons": horizons, "price_session": "2026-09-15",
              "tradingagents": ta, "ai_hedge_fund": aihf,
              "costs": {"tradingagents": None, "ai_hedge_fund": 0.1, "desk": 0.07, "total": 0.17},
              "files": {"json": "ECG-2026-09-15.json"}}
    md = markdown(bundle)
    assert "–" not in md and "—" not in md and "1.25-1.5x" in md
    assert "(2026-09-15, the one close every team used)" in md
    assert "–" in ta["reports"]["final_trade_decision"]      # the stored text is untouched


_EDGE_PY = Path(__file__).resolve().parents[3] / "EdgeDesk" / ".venv" / "Scripts" / "python.exe"
_EDGE_PROBE = r'''
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from desk.runners import edge_runner as er

class Log:
    def __init__(self): self.seen = []
    def emit(self, kind, **kw): self.seen.append({"type": kind, **kw})

log = Log()
cb = er.section_events(log)
cb("bull", True, None)
cb("bear", False, "failed validation twice")
cb("bear", True, None, text="Margins are falling and the multiple is rich for that.")
cb("not_a_step", True, None)

env_file = Path(tempfile.mkdtemp()) / ".env"
env_file.write_text("SEC_USER_AGENT=edge value\nEDGE_ONLY=x\n", encoding="utf-8")
env = {"SEC_USER_AGENT": "desk value", "DESK_EVENTS": "keep"}
dropped = er.edge_settings_win(env, env_file)
print(json.dumps({"events": log.seen, "dropped": dropped, "env": env}))
'''


@pytest.mark.skipif(not _EDGE_PY.exists(), reason="Edge Desk's venv is not installed")
def test_the_edge_runner_reports_real_outcomes_and_lets_edge_settings_win():
    """Stage B4 (R2-17): a failed bear case showed as "Written". Stage B8: the desk's
    SEC_USER_AGENT, inherited by the runner, silently replaced Edge's own."""
    import subprocess
    desk_root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run([str(_EDGE_PY), "-c", _EDGE_PROBE, desk_root], capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    bull, bear, written = got["events"]
    assert len(got["events"]) == 3
    assert written["text"].startswith("Margins are falling")       # the section's own words, live
    assert bull["failed"] is False and bull["text"].startswith("Written")
    assert bear["failed"] is True and bear["text"] == "Not written: failed validation twice."
    assert got["dropped"] == ["SEC_USER_AGENT"] and got["env"] == {"DESK_EVENTS": "keep"}


# ---------------------------------------------------------------------------
# Stage C1 (decision 1, audit R2-01): a stale filing withholds long term for the whole desk
# ---------------------------------------------------------------------------

_STALE_V = ("Fundamentals are a quarter stale: SEC's financial-data feed ends at the period ending "
            "2026-03-31 (filed 2026-04-29), but a newer 10-Q for 2026-06-30 was filed 2026-07-29 and "
            "is not in the feed yet.")
_EDGE_STALE = {"code": "companyfacts_stale", "severity": "withhold",
               "message": "A 10-Q for the period ending 2026-06-30 was filed on 2026-07-29, but EDGAR's "
                          "companyfacts feed still stops at 2026-03-31."}


def _v_reports():
    """V 2026-09-24: the Veterans voted Overweight on the stale feed Edge refused to rate on."""
    ta, aihf = _engines(ta="Hold", aihf="Overweight")
    aihf["verdict"]["data_caveats"] = [_STALE_V]
    edge = {**_edge(None), "quality_state": "WITHHELD", "caveats": [_EDGE_STALE]}
    return {"A": ta, "B": aihf, "C": edge}


def test_the_manifest_reads_each_teams_own_stale_finding():
    from desk import eligibility
    q = eligibility.manifest(_v_reports())
    assert q["withheld"]["long_term"]["teams"] == ["B", "C"] and q["withheld"]["swing"] is None
    assert {f["team"] for f in q["findings"]} == {"B", "C"}
    ta, aihf = _engines()
    assert eligibility.manifest({"A": ta, "B": aihf})["withheld"] == {"long_term": None, "swing": None}
    # One team's finding is enough, and an Edge payload saved before the codes were passed through
    # is read from its report's data-quality table.
    old_edge = {**_edge(None), "report_md": "| Severity | Code | Note |\n|---|---|---|\n"
                "| withhold | `companyfacts_stale` | feed stops at 2026-03-31 |\n"}
    assert eligibility.manifest({"A": ta, "C": old_edge})["withheld"]["long_term"]["codes"] == ["companyfacts_stale"]
    # A non-withholding caveat changes nothing.
    aihf["verdict"]["data_caveats"] = ["Market cap derived from public float."]
    assert eligibility.manifest({"A": ta, "B": aihf})["withheld"]["long_term"] is None


def test_a_stale_filing_withholds_long_term_desk_wide_and_swing_still_runs():
    from desk import eligibility
    reports = _v_reports()
    llm = ScriptedLLM([{"rating": "Hold", "rationale": "x"}, {"rating": "Hold", "rationale": "y"}, _memo()])
    results = analyze(llm, "V", "2026-09-24", reports, ["long_term", "swing"], owns=False,
                      max_rounds=3, say=quiet, quality=eligibility.manifest(reports))
    lt, sw = results["long_term"], results["swing"]
    assert lt["outcome"]["status"] == "withheld" and lt["outcome"]["rating"] is None
    assert lt["voters"] == [] and lt["debate"] is None and "quarter stale" in lt["outcome"]["how"]
    assert lt["action"].startswith("Withheld")
    assert len(llm.prompts) == 3 and all("Long term" not in p[1]["text"] for p in llm.prompts)
    assert sw["outcome"]["rating"] == "Hold"
    bundle = {"ticker": "V", "date": "2026-09-24", "owns": False, "horizons": results,
              "tradingagents": reports["A"], "ai_hedge_fund": reports["B"], "edge_desk": reports["C"],
              "costs": {"total": 0, "desk": 0}, "files": {"json": "V-2026-09-24.json"}}
    md = markdown(bundle)
    assert "| Long term (1+ years) | **WITHHELD** | Withheld |" in md
    assert "## Long term (1+ years): WITHHELD" in md and "combined long-term rating" in md
    vault_note(bundle, md)


# ---------------------------------------------------------------------------
# Stage C2 (audit A02, R2-07): the horizon restatement is blind, and No view votes nowhere
# ---------------------------------------------------------------------------

def _sentinel_reports():
    ta, aihf = _engines(ta="Hold", aihf="Overweight")
    ta["reports"]["market_report"] = "SENTINEL-A-ONLY tape read"
    aihf["verdict"]["summary"] = "SENTINEL-B-ONLY beat and raised"
    return {"A": ta, "B": aihf}


def test_each_manager_restates_from_its_own_report_only():
    reports = _sentinel_reports()
    llm = ScriptedLLM([{"rating": "Overweight", "rationale": "a"}, {"rating": "Overweight", "rationale": "b"}, _memo()])
    analyze(llm, "ECG", "2026-09-15", reports, ["long_term"], owns=False, max_rounds=3, say=quiet)
    a_seen, b_seen = (p[0]["text"] for p in llm.prompts[:2])
    assert "DESK A" in llm.prompts[0][1]["text"] and "DESK B" in llm.prompts[1][1]["text"]
    assert "SENTINEL-A-ONLY" in a_seen and "SENTINEL-B-ONLY" not in a_seen
    assert "SENTINEL-B-ONLY" in b_seen and "SENTINEL-A-ONLY" not in b_seen
    assert "SHARED COMPUTED DATA" in a_seen            # the computed valuation is not an opinion
    memo_blocks = llm.prompts[2][0]["text"]
    assert "SENTINEL-A-ONLY" in memo_blocks and "SENTINEL-B-ONLY" in memo_blocks


@pytest.mark.parametrize("answers, status, rating", [
    (("No view", "No view"), "no_view", None),
    (("No view", "Overweight"), "single", "Overweight"),
    (("Overweight", "Buy"), "agreed", "Overweight"),
])
def test_no_view_votes_nowhere(answers, status, rating):
    reports = _sentinel_reports()
    replies = [{"rating": r, "rationale": "x"} for r in answers] + [_memo()]
    llm = ScriptedLLM(replies)
    h = analyze(llm, "ECG", "2026-09-15", reports, ["swing"], owns=False, max_rounds=3, say=quiet)["swing"]
    assert h["outcome"]["status"] == status and h["outcome"]["rating"] == rating
    assert h["voters"] == [d for d, r in zip("AB", answers) if r != "No view"]
    assert all(h["restated"][d]["rating"] is None and h["restated"][d]["no_view"]
               for d, r in zip("AB", answers) if r == "No view")



def test_a_concession_on_one_point_does_not_strike_an_unrelated_claim():
    """Stage C3 (R2-08). Codex's probe: A conceded on valuation in round 2, and the fact check marked
    an unrelated revenue sentence from A's report as withdrawn in that round. Now only a statement
    the owner itself listed as given up can be struck; the rest is challenged."""
    from desk.corrections import verify
    texts = {"A": "The stock trades at 45 times earnings. Revenue grew 30% on data-center demand."}
    turns = [{"desk": "A", "round": 2, "decision": "concede",
              "gives_up": ["The stock trades at 45 times earnings"]}]
    base = {"desk": "A", "status": "withdrawn", "reason": "r", "raised_by": "B", "round": 2}
    kept = verify([{**base, "quote": "Revenue grew 30% on data-center demand."},
                   {**base, "quote": "The stock trades at 45 times earnings."}], texts, turns)
    assert [k["status"] for k in kept] == ["challenged", "withdrawn"]
    # A concession that disowns nothing strikes nothing.
    assert verify([{**base, "quote": "The stock trades at 45 times earnings."}], texts,
                  [{**turns[0], "gives_up": []}])[0]["status"] == "challenged"


def test_a_conceding_turn_keeps_what_it_gave_up_and_a_defense_gives_up_nothing():
    from desk.debate import _turn as make_turn, transcript
    t = make_turn(_turn("concede", "Hold", changed="the margin", gives_up=[" Margins expand to 40%. "]), "B", 2)
    assert t.gives_up == ["Margins expand to 40%."] and "Margins expand to 40%." in transcript([t])
    assert make_turn(_turn("defend", "Buy", gives_up=["x"]), "A", 1).gives_up == []



# ---------------------------------------------------------------------------
# Debate figures are looked up in the reports (from the AKAM 2026-10-04 review)
# ---------------------------------------------------------------------------

def test_figures_match_across_ways_of_writing_them():
    from desk import figures as figs
    k = figs.known("Net debt $6,084,000,000. Margin 0.105. Diluted shares rose 5.8%. Price $108.92.")
    assert figs.unverified("net debt of $6.08B and a 10.5% margin at $108.92, up 5.8%", k) == []
    assert figs.unverified("free cash flow of $630M and a 4% yield", k) == ["$630M", "4%"]
    assert figs.unverified("Round 2, the 50-day, 3 to 6 months, fiscal 2026", k) == []


def test_a_turn_with_an_unsupported_figure_is_asked_again_then_flagged():
    from desk.debate import run_debate
    reports = _reports()
    llm = ScriptedLLM([_turn("defend", "Hold", argument="Diluted shares rose 9.9%."),
                       _turn("defend", "Hold", argument="Diluted shares rose 9.9% again."),
                       _turn("concede", "Hold", changed="the dilution")])
    result = run_debate(llm, reports, "long_term", {"A": "Hold", "B": "Overweight"}, 1, say=quiet)
    first = result.turns[0]
    assert "APPEAR IN NO REPORT: 9.9%" in llm.prompts[1][1]["text"]
    assert "figures found in no report: 9.9%" in first.note
    assert "Moderator note: figures found in no report: 9.9%" in __import__("desk.debate", fromlist=["x"]).transcript(result.turns)


def test_a_turn_that_drops_the_unsupported_figure_stands_with_a_light_note():
    from desk.debate import run_debate
    llm = ScriptedLLM([_turn("defend", "Hold", argument="Diluted shares rose 9.9%."),
                       _turn("defend", "Hold", argument="Dilution is a risk."),
                       _turn("concede", "Hold", changed="x")])
    t = run_debate(llm, _reports(), "long_term", {"A": "Hold", "B": "Overweight"}, 1, say=quiet).turns[0]
    assert t.argument == "Dilution is a risk." and "asked again" in t.note
