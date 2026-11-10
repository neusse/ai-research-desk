"""Three formats, one run file, no disagreement.

The acceptance test for the report layer is not that each format looks right on
its own. It is that they cannot contradict each other, because the moment the
scorecard says Buy and the full report says Hold, none of them is trustworthy.
"""

from __future__ import annotations

import re
from datetime import date

import pytest

from edgedesk import reports, run as run_mod
from tests.conftest import FakeClient, make_bars, make_metrics

AS_OF = date(2026, 9, 15)
TEXTUAL = ("scorecard", "condensed", "full", "card")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("edgedesk.paths.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("edgedesk.paths.ESTIMATES_DIR", tmp_path / "estimates")
    return tmp_path


@pytest.fixture
def run(home):
    return run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)


@pytest.fixture
def withheld(home):
    return run_mod.analyze("TEST", AS_OF, client=FakeClient(metrics=[]), save=False)


@pytest.fixture
def foreign_filer(home):
    profile = {"cik": 1, "name": "Arm Holdings plc", "sic": "3674",
               "sic_description": "Semiconductors", "exchanges": ["Nasdaq"],
               "latest_report": None,
               "latest_foreign_report": {"form": "20-F", "filed": "2026-06-24",
                                           "period": "2026-03-31"}}
    return run_mod.analyze("ARM", AS_OF, client=FakeClient(profile=profile), save=False)


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", TEXTUAL)
def test_every_format_renders(run, fmt):
    text = reports.render(run, fmt)
    assert text.strip()
    assert run["ticker"] in text


@pytest.mark.parametrize("fmt", TEXTUAL)
def test_every_report_annotates_foreign_filer_normalization(foreign_filer, fmt):
    text = reports.render(foreign_filer, fmt)
    assert "Foreign-filer normalization used" in text
    assert "20-F" in text and "financial 6-K" in text


@pytest.mark.parametrize("fmt", TEXTUAL)
def test_every_format_shows_the_long_term_rating(run, fmt):
    rating = (run["verdicts"]["long_term"]["rating"] or "").lower()
    assert rating in reports.render(run, fmt).lower()


@pytest.mark.parametrize("fmt", ("scorecard", "condensed", "card"))
def test_the_compact_formats_publish_the_swing_call_not_the_old_factor_rating(run, fmt):
    """The old swing factor score did not separate forward returns, so its rating
    ladder stays unpublished. What is published is the Buy or Sell call, which is
    built differently and says how."""
    text = reports.render(run, fmt)
    swing_section = text.split("Long term")[0].split("LONG TERM")[0]
    old = run["verdicts"]["swing"]["rating"]
    if old not in ("Buy", "Sell"):                 # the two words the new call also uses
        assert old not in swing_section
    assert run["swing"]["call"].upper() in swing_section.upper()


@pytest.mark.parametrize("fmt", TEXTUAL)
def test_every_format_prints_the_swing_call_with_its_evidence_standing(run, fmt):
    text = reports.render(run, fmt)
    assert "What is known about this call" in text
    assert "setup" in text and "sentiment" in text and "earnings" in text


def test_a_buy_always_carries_a_complete_plan_and_a_sell_never_invents_one():
    from edgedesk.verdict import swing
    plan = swing._plan({"close": 100.0, "atr": 2.0, "last_pivot_low": 95.5, "sma200": 80.0,
                        "sma100": 90.0, "low_20": 94.0, "ema21": 103.0, "ema50": 106.0,
                        "last_pivot_high": 112.0, "high_20": 112.0, "high_63": 118.0,
                        "high_252": 125.0})
    assert plan["invalidation"] == pytest.approx(95.3)            # just under the swing low
    assert plan["shares"] == int(1000 // plan["risk_per_share"])
    assert plan["target_1"] >= 100 + plan["risk_per_share"]        # at least 1R away
    assert plan["target_2"] > plan["target_1"]
    none = swing._plan({"close": 100.0, "atr": 2.0})
    assert none["invalidation"] == pytest.approx(96.0)            # 2 ATR when no structure fits


def test_earnings_inside_the_blackout_blocks_a_buy_whatever_the_score():
    from edgedesk.verdict import swing
    from tests.conftest import FakeClient
    from edgedesk import run as run_mod
    import datetime
    r = run_mod.analyze("TEST", datetime.date(2026, 9, 15), client=FakeClient(), save=False)
    ev = dict(r["evidence"], calendar={"next_earnings": "2026-09-17"})
    from edgedesk.evidence.package import collect
    live = collect("TEST", datetime.date(2026, 9, 15), FakeClient())
    call = swing.recommend(ev, r["factors"], r["valuation"], live.bars,
                           live.benchmarks.get("SPY") or [])
    assert call["call"] == "Sell" and any("Earnings on" in b for b in call["blockers"])
    assert call["plan"] is None


@pytest.mark.parametrize("fmt", TEXTUAL)
def test_every_format_shows_the_last_close(run, fmt):
    close = run["evidence"]["anchors"]["last_close"]
    assert f"{close:,.2f}" in reports.render(run, fmt)


def test_the_formats_quote_the_same_long_term_score(run):
    score = run["verdicts"]["long_term"]["score"]
    for fmt in ("scorecard", "condensed", "card"):
        assert f"{score:.1f}" in reports.render(run, fmt), f"{fmt} lost the score"


def test_the_formats_quote_the_same_fair_value(run):
    fv = (run["valuation"] or {}).get("fair_value")
    assert fv, "the fixture should produce a fair value"
    rendered = f"${fv['value']:,.2f}"
    for fmt in ("scorecard", "condensed", "full", "card"):
        assert rendered in reports.render(run, fmt), f"{fmt} lost the fair value"


def test_no_format_invents_a_number_the_run_does_not_have(run):
    """Every dollar figure in the condensed report must appear in the run file.

    This is the guard against a renderer quietly computing something. If a
    report needs a figure the run does not contain, the run should record it.
    """
    import json

    blob = json.dumps(run, default=str)
    printed = {m for m in re.findall(r"\$([\d,]+\.\d{2})", reports.render(run, "condensed"))}
    for value in printed:
        plain = value.replace(",", "")
        assert plain in blob or plain.rstrip("0").rstrip(".") in blob, \
            f"{value} appears in the report but not in the run"


# ---------------------------------------------------------------------------
# Withholding must survive every format
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", TEXTUAL)
def test_withheld_is_visible_in_every_format(withheld, fmt):
    text = reports.render(withheld, fmt).lower()
    assert "withheld" in text


@pytest.mark.parametrize("fmt", TEXTUAL)
def test_no_format_prints_a_rating_when_it_was_withheld(withheld, fmt):
    """No verdict may be published for a withheld run.

    Matched on how a verdict is actually written, not on the bare words: the
    Street section legitimately contains "Buy" and "Sell" as analyst labels, and
    a test that cannot tell those apart from a rating would push the report into
    hiding real information to stay green.
    """
    text = reports.render(withheld, fmt)
    bands = "Buy|Overweight|Hold|Underweight|Sell"
    leaks = [
        rf"\b({bands}) \(\d",            # condensed and full: "Buy (76.4/100)"
        rf"\b({bands.upper()})\b",       # scorecard and card pills: "BUY"
    ]
    if fmt != "full":
        # The full report deliberately still shows the factor scores for a
        # withheld run, labelled as context. The compact formats must not show
        # any score at all, because there a number reads as a verdict.
        leaks.append(r"\b\d{1,3}\.\d ?/ ?100")
    for pattern in leaks:
        assert not re.search(pattern, text), f"{fmt} leaked a verdict: {pattern}"


@pytest.mark.parametrize("fmt", TEXTUAL)
def test_every_format_carries_the_research_only_disclaimer(run, fmt):
    assert "research only" in reports.render(run, fmt).lower()


@pytest.mark.parametrize("fmt", TEXTUAL)
def test_every_format_records_the_run_hash(run, fmt):
    assert run["content_hash"] in reports.render(run, fmt)


# ---------------------------------------------------------------------------
# The full report is the one that has to survive an argument
# ---------------------------------------------------------------------------

def test_full_report_cites_every_factor_signal(run):
    text = reports.full.render(run)
    for key, fam in run["factors"].items():
        for signal in fam["signals"]:
            assert signal["label"] in text, f"{key}.{signal['key']} is missing"


def test_full_report_lists_every_evidence_id(run):
    text = reports.full.render(run)
    for fact_id in run["evidence"]["facts"]:
        assert f"`{fact_id}`" in text


def test_full_report_shows_abstentions_rather_than_hiding_them(home):
    run = run_mod.analyze("TEST", AS_OF, save=False,
                          client=FakeClient(metrics=[make_metrics(return_on_equity=None,
                                                                  interest_coverage=None)]))
    assert "abstained" in reports.full.render(run)


def test_full_report_states_no_model_touched_the_numbers(run):
    assert "No language model contributed" in reports.full.render(run)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def test_condensed_and_full_do_not_overwrite_each_other(run, tmp_path):
    a = reports.write(run, "condensed", tmp_path)
    b = reports.write(run, "full", tmp_path)
    assert a != b
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_card_is_self_contained(run, tmp_path):
    html = reports.render(run, "card")
    assert "<script" not in html.lower()
    assert "http://" not in html and "https://" not in html


# ---------------------------------------------------------------------------
# Swing 2.1: a Buy can come from five setups, each carrying its own record
# ---------------------------------------------------------------------------

_UP = {"close": 100.0, "sma200": 80.0, "slope_200": 0.02, "atr": 2.0}


@pytest.mark.parametrize("extra, expected", [
    (dict(roc_5=-0.09), "deep_dip"),
    (dict(roc_5=-0.06), "dip"),
    (dict(roc_5=0.01, dip_recent=True, reclaimed_prior_high=True, up_day=True, close_loc=0.8),
     "dip_confirmed"),
    (dict(roc_5=0.01, mom_12_1=0.40, roc_63=0.10, updown_vol_20=1.5, accum_days_20=6,
          distrib_days_20=2, ext_ema21_atr=1.0), "momentum_accumulation"),
    (dict(roc_5=0.02, bb_width_pct_prev=0.2, updown_vol_20_prev=1.2, prior_high_20=99.0,
          rvol=1.8, close_loc=0.9), "range_breakout"),
    (dict(roc_5=-0.02, drop_from_high20_atr=3.4), "slide"),
])
def test_each_setup_path_is_recognised(extra, expected):
    from edgedesk.verdict import swing
    assert expected in swing.live_setups({**_UP, **extra})


def test_no_setup_is_live_in_a_downtrend_or_on_a_quiet_day():
    from edgedesk.verdict import swing
    assert swing.live_setups({**_UP, "roc_5": 0.01}) == []
    down = {"close": 70.0, "sma200": 80.0, "slope_200": -0.02, "roc_5": -0.09}
    assert swing.live_setups(down) == []


def test_every_setup_prints_its_record_against_the_random_day_baseline():
    from edgedesk.verdict import swing
    got = swing._setup({**_UP, "roc_5": 0.01, "dip_recent": True, "reclaimed_prior_high": True,
                        "up_day": True, "close_loc": 0.8})
    text = " ".join(got["notes"])
    assert "BEHIND a random day" in text and "any random day made" in text
    assert sum(swing.WEIGHTS.values()) == 100 and swing.WEIGHTS["momentum"] == 20


def test_a_confirmed_dip_is_invalidated_under_the_dip_low():
    from edgedesk.verdict import swing
    plan = swing._plan({"close": 100.0, "atr": 2.0, "low_7": 96.0, "sma200": 70.0},
                       ["dip_confirmed"])
    assert plan["invalidation"] == pytest.approx(95.8)
    assert "dip the buyers took back" in plan["invalidation_rule"]


def test_the_condensed_report_never_explains_swing_with_the_withdrawn_factor_score(run):
    text = reports.render(run, "condensed")
    why = text.split("## Why")[1].split("## Valuation")[0]
    assert "Swing" not in why and "Long term" in why
    level = (run["swing"].get("plan") or {}).get("invalidation") or run["swing"]["reference_invalidation"]
    assert f"{level:,.2f}** is where the swing structure fails" in text
