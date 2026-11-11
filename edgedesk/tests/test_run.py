"""The run file: reproducible, self-contained, and the only source of truth."""

from __future__ import annotations

import json
from datetime import date

import pytest

from edgedesk import run as run_mod
from edgedesk.evidence import estimates
from edgedesk.paths import run_path
from edgedesk.reports import scorecard
from tests.conftest import FakeClient, make_bars, make_metrics

AS_OF = date(2026, 9, 15)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point every storage path at a temporary directory for the test."""
    monkeypatch.setattr("edgedesk.paths.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("edgedesk.paths.ESTIMATES_DIR", tmp_path / "estimates")
    return tmp_path


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

def test_same_inputs_produce_the_same_content_hash(home):
    a = run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)
    b = run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)
    assert a["content_hash"] == b["content_hash"]


def test_the_hash_ignores_wall_clock_but_notices_the_data(home):
    a = run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)
    changed = run_mod.analyze(
        "TEST", AS_OF, save=False,
        client=FakeClient(metrics=[make_metrics(operating_margin=0.02,
                                                revenue_growth=-0.2)]))
    assert a["content_hash"] != changed["content_hash"]


def test_ownership_does_not_change_the_analysis_hash(home):
    """Owning a stock changes what to do about it, never what it is worth."""
    fresh = run_mod.analyze("TEST", AS_OF, client=FakeClient(), owns=False, save=False)
    held = run_mod.analyze("TEST", AS_OF, client=FakeClient(), owns=True, save=False)
    assert fresh["verdicts"]["long_term"]["score"] == held["verdicts"]["long_term"]["score"]
    assert (fresh["verdicts"]["long_term"]["action"]
            != held["verdicts"]["long_term"]["action"])


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def test_the_run_writes_one_json_per_ticker_per_date(home):
    run_mod.analyze("TEST", AS_OF, client=FakeClient())
    path = run_path("TEST", AS_OF.isoformat())
    assert path.exists()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["ticker"] == "TEST"
    assert set(saved["verdicts"]) == {"swing", "long_term"}
    assert saved["evidence"]["facts"], "the run kept no citable facts"


def test_the_index_is_updated_and_stays_rebuildable(home):
    run_mod.analyze("TEST", AS_OF, client=FakeClient())
    index = json.loads((home / "runs" / "index.json").read_text(encoding="utf-8"))
    assert index["runs"][0]["ticker"] == "TEST"
    assert index["runs"][0]["as_of"] == AS_OF.isoformat()


def test_no_llm_field_is_populated_by_the_deterministic_run(home):
    """Phase A must be complete without a model in the path."""
    run = run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)
    assert run["llm"] is None


def test_previous_returns_none_on_the_first_run(home):
    run_mod.analyze("TEST", AS_OF, client=FakeClient())
    assert run_mod.previous("TEST", AS_OF.isoformat()) is None


def test_previous_finds_the_earlier_run(home):
    run_mod.analyze("TEST", date(2026, 9, 8), client=FakeClient())
    run_mod.analyze("TEST", AS_OF, client=FakeClient())
    earlier = run_mod.previous("TEST", AS_OF.isoformat())
    assert earlier and earlier["as_of"] == "2026-09-08"


# ---------------------------------------------------------------------------
# Estimate capture: the one thing written before anything reads it
# ---------------------------------------------------------------------------

def test_estimate_snapshot_is_captured_from_the_very_first_run(home):
    run_mod.analyze("TEST", AS_OF, client=FakeClient())
    saved = estimates.history("TEST")
    assert len(saved) == 1
    assert saved[0]["target_mean"] == 140.0
    assert saved[0]["source"] == "yahoo"


def test_history_is_empty_not_broken_before_anything_is_captured(home):
    assert estimates.history("NOTHING") == []


def test_no_snapshot_is_written_when_there_is_no_consensus(home):
    run_mod.analyze("TEST", AS_OF, client=FakeClient(consensus=False))
    assert estimates.history("TEST") == []


# ---------------------------------------------------------------------------
# Opportunity state
# ---------------------------------------------------------------------------

def test_a_withheld_horizon_is_never_actionable(home):
    run = run_mod.analyze("TEST", AS_OF, client=FakeClient(metrics=[]), save=False)
    assert run["opportunity"]["long_term"]["state"] == "Withheld"


def test_a_broken_swing_setup_offers_no_entry_price(home):
    """Printing an entry under a broken setup is an invitation dressed as
    information."""
    falling = FakeClient(bars=make_bars(400, drift=-0.004))
    run = run_mod.analyze("TEST", AS_OF, client=falling, save=False)
    assert run["signal"]["signal"] == "RED"
    swing = run["opportunity"]["swing"]
    assert swing["state"] == "Stand aside"
    assert swing["show_entry"] is False


def test_an_intact_swing_setup_says_where_price_sits(home):
    run = run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)
    assert run["signal"]["signal"] == "GREEN"
    assert run["opportunity"]["swing"]["state"] in {
        "In the entry zone", "Above the entry zone", "Setup intact"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_the_scorecard_renders_from_the_run_alone(home):
    run = run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)
    text = scorecard.render(run)
    assert run["ticker"] in text
    assert "SWING" in text and "LONG TERM" in text
    assert "DATA QUALITY" in text
    assert run["content_hash"] in text


def test_the_scorecard_says_withheld_loudly(home):
    run = run_mod.analyze("TEST", AS_OF, client=FakeClient(metrics=[]), save=False)
    text = scorecard.render(run)
    assert "RATING WITHHELD" in text
    assert "US GAAP" in text


def test_the_scorecard_never_prints_a_rating_it_does_not_have(home):
    run = run_mod.analyze("TEST", AS_OF, client=FakeClient(metrics=[]), save=False)
    text = scorecard.render(run)
    for banned in ("BUY", "OVERWEIGHT", "UNDERWEIGHT"):
        assert banned not in text


def test_a_run_without_alpaca_keys_says_its_prices_are_from_yahoo(tmp_path, monkeypatch):
    """The data chain (2026-10-05): a run on Yahoo prices carries the caveat, it is not refused."""
    from edgedesk.evidence import package
    from tests.conftest import FakeClient
    client = FakeClient()
    client.alpaca = type("A", (), {"source": "yahoo"})()
    ev = package.Evidence(ticker="TEST", as_of="2026-09-15", generated_at="2026-09-15T21:00:00Z",
                          is_historical=False)
    package._collect_market(ev, __import__("datetime").date(2026, 9, 15), client)
    assert any(c.code == "prices_from_yahoo" and c.severity == "info" for c in ev.caveats)


def test_foreign_filer_caveat_names_the_filing_and_parser_limit(home):
    from tests.conftest import FakeClient
    profile = {"cik": 1, "name": "Arm Holdings plc", "sic": "3674",
               "sic_description": "Semiconductors", "exchanges": ["Nasdaq"],
               "latest_report": None,
               "latest_foreign_report": {"form": "20-F", "filed": "2026-06-24",
                                           "period": "2026-03-31"}}
    run = run_mod.analyze("ARM", AS_OF, client=FakeClient(metrics=[], profile=profile), save=False)
    caveat = next(c for c in run["evidence"]["caveats"] if c["code"] == "no_gaap_fundamentals")
    assert "20-F filed 2026-06-24" in caveat["message"]
    assert "No complete TTM row" in caveat["message"]
    assert "not treated as zero" in caveat["message"]


def test_normalized_foreign_filer_is_degraded_not_withheld(home):
    from tests.conftest import FakeClient
    profile = {"cik": 1, "name": "Arm Holdings plc", "sic": "3674",
               "sic_description": "Semiconductors", "exchanges": ["Nasdaq"],
               "latest_report": None,
               "latest_foreign_report": {"form": "20-F", "filed": "2026-06-24",
                                           "period": "2026-03-31"}}
    run = run_mod.analyze("ARM", AS_OF, client=FakeClient(profile=profile, ads_ratio=1.0), save=False)
    caveat = next(c for c in run["evidence"]["caveats"] if c["code"] == "foreign_filer_normalized")
    assert caveat["severity"] == "degrade"
    assert run["evidence"]["quality_score"] > 0
    assert run["verdicts"]["long_term"]["quality_state"] != "WITHHELD"


def test_foreign_filer_with_unresolved_ads_ratio_is_withheld(home):
    from tests.conftest import FakeClient
    profile = {"cik": 1, "name": "ADS Test", "sic": "3674",
               "latest_foreign_report": {"form": "20-F", "filed": "2026-06-24",
                                         "period": "2026-03-31"}}
    for ratio in (0.2, None):  # five ordinary shares per ADS, or no public float
        run = run_mod.analyze("TEST", AS_OF,
                              client=FakeClient(profile=profile, ads_ratio=ratio), save=False)
        caveat = next(c for c in run["evidence"]["caveats"]
                      if c["code"] == "ads_ratio_unresolved")
        assert caveat["severity"] == "withhold"
        assert run["verdicts"]["long_term"]["quality_state"] == "WITHHELD"
