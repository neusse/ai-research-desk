"""The rule the whole engine rests on: a run may only see what was public.

If this suite passes and the calibration still produces a flattering result, the
result is worth something. If this suite is weak, the calibration is worthless
no matter how good it looks, so these tests get the same machinery the
calibration harness uses rather than a separate, laxer path.
"""

from __future__ import annotations

from datetime import date, timedelta

from edgedesk.evidence.package import collect
from edgedesk.providers.edgar import (Company, Fact, ReportPeriod, _latest_report,
                                      build_metrics, implied_ads_price_ratio,
                                      parse_companyfacts, pit_values)
from tests.conftest import FakeClient, make_bars, make_metrics


def test_no_bar_later_than_as_of_is_requested(as_of):
    client = FakeClient()
    collect("TEST", as_of, client)
    assert client.requested_ends, "the run asked for no price data at all"
    assert max(client.requested_ends) <= as_of


def test_bars_stop_at_as_of():
    past = date(2026, 6, 30)
    client = FakeClient(bars=make_bars(400, end=date(2026, 9, 15)))
    ev = collect("TEST", past, client)
    assert ev.bars, "no bars survived the cutoff"
    assert max(b["date"] for b in ev.bars) <= past.isoformat()


def test_fundamentals_filed_after_as_of_are_invisible():
    """A filing dated after the as-of date must not exist for that run, even
    though the file on disk contains it."""
    past = date(2026, 7, 1)
    later = make_metrics(filing_date="2026-07-29", report_period="2026-06-30")
    earlier = make_metrics(filing_date="2026-04-25", report_period="2026-03-31")
    ev = collect("TEST", past, FakeClient(metrics=[later, earlier]))
    periods = {m["report_period"] for m in ev.metrics}
    assert periods == {"2026-03-31"}


def test_pit_values_ignores_facts_filed_later():
    """The EDGAR layer's own guarantee, tested where it is implemented."""
    company = Company(cik=1, facts={"Revenues": [
        Fact(start=date(2026, 1, 1), end=date(2026, 3, 31), val=100.0,
             filed=date(2026, 4, 25), form="10-Q", accn="a"),
        Fact(start=date(2026, 4, 1), end=date(2026, 6, 30), val=200.0,
             filed=date(2026, 7, 29), form="10-Q", accn="b"),
    ]}, periods=[ReportPeriod(end=date(2026, 3, 31), filed=date(2026, 4, 25), accn="a")])

    visible = pit_values(company, ["Revenues"], date(2026, 7, 1))
    assert (date(2026, 4, 1), date(2026, 6, 30)) not in visible
    assert visible[(date(2026, 1, 1), date(2026, 3, 31))] == 100.0

    later = pit_values(company, ["Revenues"], date(2026, 8, 1))
    assert (date(2026, 4, 1), date(2026, 6, 30)) in later


def test_us_gaap_foreign_filer_builds_ttm_from_20f_and_financial_6k():
    """ARM-style filers publish annual 20-F and interim 6-K XBRL, not 10-K/10-Q."""
    def fact(val, start, end, filed, form, accn):
        return {"val": val, "start": start, "end": end, "filed": filed,
                "form": form, "accn": accn}

    def concept(rows):
        return {"units": {"USD": rows}}

    annual_revenue = fact(1_000, "2025-04-01", "2026-03-31", "2026-05-26", "20-F", "annual")
    annual_income = fact(100, "2025-04-01", "2026-03-31", "2026-05-26", "20-F", "annual")
    prior_ytd_revenue = fact(450, "2025-04-01", "2025-09-30", "2025-11-05", "6-K", "prior")
    prior_ytd_income = fact(40, "2025-04-01", "2025-09-30", "2025-11-05", "6-K", "prior")
    current_ytd_revenue = fact(600, "2026-04-01", "2026-09-30", "2026-11-04", "6-K", "current")
    current_ytd_income = fact(70, "2026-04-01", "2026-09-30", "2026-11-04", "6-K", "current")
    payload = {"cik": 1973239, "facts": {"us-gaap": {
        "Revenues": concept([annual_revenue, prior_ytd_revenue, current_ytd_revenue,
                             fact(999, "2026-10-01", "2026-10-31", "2026-11-06", "6-K", "news-only")]),
        "NetIncomeLoss": concept([annual_income, prior_ytd_income, current_ytd_income]),
        "Assets": concept([
            {"val": 900, "end": "2026-03-31", "filed": "2026-05-26", "form": "20-F", "accn": "annual"},
            {"val": 950, "end": "2025-09-30", "filed": "2025-11-05", "form": "6-K", "accn": "prior"},
            {"val": 1_100, "end": "2026-09-30", "filed": "2026-11-04", "form": "6-K", "accn": "current"},
        ]),
    }}}

    company = parse_companyfacts(payload)
    rows = build_metrics("ARM", company, date(2026, 11, 9), 12,
                         price_on=lambda _: 100.0, split_factor=lambda _a, _b: 1.0)

    assert [p.end.isoformat() for p in company.periods] == ["2026-09-30", "2026-03-31", "2025-09-30"]
    assert all(p.accn != "news-only" for p in company.periods)
    assert rows[0].revenue == 1_150
    assert rows[0].net_income == 130
    assert rows[0].filing_date == "2026-11-04"


def test_foreign_share_basis_uses_same_filing_and_float_date_close():
    measured = date(2026, 3, 31)
    filed = date(2026, 5, 26)
    cover = Fact(None, measured, 1_000, filed, "20-F", "annual")
    def company(float_value):
        return Company(cik=1, cover_shares=[cover],
                       public_float=[Fact(None, measured, float_value, filed, "20-F", "annual")])

    assert implied_ads_price_ratio(company(100_000), filed, lambda day: 100.0) == 1.0
    assert implied_ads_price_ratio(company(20_000), filed, lambda day: 100.0) == 0.2
    assert implied_ads_price_ratio(company(100_000), date(2026, 5, 25), lambda day: 100.0) is None
    assert implied_ads_price_ratio(Company(cik=1, cover_shares=[cover]), filed,
                                   lambda day: 100.0) is None
    assert implied_ads_price_ratio(company(100_000), filed, lambda day: None) is None
    newer_unmatched = Fact(None, date(2026, 4, 30), 100_000,
                           date(2026, 6, 1), "20-F", "other-filing")
    with_newer = company(100_000)
    with_newer.public_float.append(newer_unmatched)
    assert implied_ads_price_ratio(with_newer, date(2026, 6, 1), lambda day: 100.0) is None


def test_restatement_is_visible_once_filed():
    """A restated figure is real information the day it becomes public, so the
    later filing wins from that date on and not before."""
    company = Company(cik=1, facts={"Revenues": [
        Fact(start=date(2026, 1, 1), end=date(2026, 3, 31), val=100.0,
             filed=date(2026, 4, 25), form="10-Q", accn="a"),
        Fact(start=date(2026, 1, 1), end=date(2026, 3, 31), val=90.0,
             filed=date(2026, 8, 10), form="10-Q/A", accn="c"),
    ]})
    key = (date(2026, 1, 1), date(2026, 3, 31))
    assert pit_values(company, ["Revenues"], date(2026, 7, 1))[key] == 100.0
    assert pit_values(company, ["Revenues"], date(2026, 9, 1))[key] == 90.0


def test_current_consensus_is_excluded_from_historical_runs():
    """Today's price targets applied to a date two months back would invent a
    result that could never have existed."""
    ev = collect("TEST", date(2026, 6, 30), FakeClient())
    assert ev.is_historical
    assert ev.consensus is None
    assert any(c.code == "consensus_excluded" for c in ev.caveats)
    assert not any(fid.startswith("est.") for fid in ev.facts)


def test_current_run_does_use_consensus(as_of):
    ev = collect("TEST", as_of, FakeClient())
    assert not ev.is_historical
    assert ev.consensus is not None
    assert ev.value("est.target_mean") == 140.0


def test_a_filing_index_entry_from_the_future_is_dropped():
    """The SEC submissions index is fetched as it stands today. Its newest entry
    can postdate a historical run, and both the freshness check and the event
    check read that field."""
    past = date(2026, 6, 30)
    ev = collect("TEST", past, FakeClient())   # profile says a 10-Q was filed 2026-07-29
    assert (ev.profile or {}).get("latest_report") is None
    assert "after this run" in (ev.profile or {}).get("latest_report_note", "")


def test_a_current_run_keeps_the_filing_index_entry(as_of):
    ev = collect("TEST", as_of, FakeClient())
    assert (ev.profile or {}).get("latest_report", {}).get("filed") == "2026-07-29"


def test_future_foreign_report_metadata_is_dropped_from_historical_run():
    profile = {
        "cik": 1, "name": "Foreign Test", "sic": "3674", "exchanges": ["Nasdaq"],
        "latest_report": {"form": "20-F", "filed": "2026-07-29", "period": "2026-03-31"},
        "latest_foreign_report": {
            "form": "20-F", "filed": "2026-07-29", "period": "2026-03-31",
        },
    }
    ev = collect("TEST", date(2026, 6, 30), FakeClient(profile=profile))
    assert (ev.profile or {}).get("latest_report") is None
    assert (ev.profile or {}).get("latest_foreign_report") is None


def test_latest_index_report_ignores_ordinary_6k():
    recent = {
        "form": ["6-K", "20-F"],
        "filingDate": ["2026-11-06", "2026-06-24"],
        "reportDate": ["2026-10-01", "2026-03-31"],
    }
    assert _latest_report({"filings": {"recent": recent}}) == {
        "form": "20-F", "filed": "2026-06-24", "period": "2026-03-31",
    }


# ---------------------------------------------------------------------------
# A split the feed forgot to adjust
# ---------------------------------------------------------------------------

def _price_bars(closes, start=date(2024, 7, 8)):
    from edgedesk.models import Price
    return [Price(open=c, high=c * 1.01, low=c * 0.99, close=c, volume=1000,
                  time=(start + timedelta(days=i)).isoformat() + "T04:00:00Z")
            for i, c in enumerate(closes)]


def test_a_split_the_feed_missed_is_restated_and_reported():
    """Broadcom, July 2024: 1,669 then 168 in an 'adjusted' series."""
    from edgedesk.providers.splits import repair_unadjusted_splits
    bars = _price_bars([1700.0, 1712.0, 1669.0, 168.0, 166.0])
    fixed, notes = repair_unadjusted_splits(bars, [(date(2024, 7, 11), 10.0)], "AVGO")
    assert [round(b.close, 1) for b in fixed] == [170.0, 171.2, 166.9, 168.0, 166.0]
    assert fixed[0].volume == 10000 and fixed[3].volume == 1000
    assert notes == [{"ex_date": "2024-07-11", "ratio": 10.0, "bars_restated": 3}]
    worst = min(b.close / a.close - 1 for a, b in zip(fixed, fixed[1:]))
    assert worst > -0.05, "no 90% crash may survive"


def test_a_split_the_feed_did_adjust_is_left_alone():
    from edgedesk.providers.splits import repair_unadjusted_splits
    bars = _price_bars([170.0, 171.2, 166.9, 168.0, 166.0])
    fixed, notes = repair_unadjusted_splits(bars, [(date(2024, 7, 11), 10.0)], "AVGO")
    assert fixed == bars and notes == []


def test_a_real_crash_on_a_split_date_is_not_mistaken_for_a_missed_split():
    """Down 35% on the ex-date of a correctly adjusted 10:1 is a crash, not a miss."""
    from edgedesk.providers.splits import repair_unadjusted_splits
    bars = _price_bars([170.0, 171.0, 169.0, 110.0, 108.0])
    fixed, notes = repair_unadjusted_splits(bars, [(date(2024, 7, 11), 10.0)], "X")
    assert fixed == bars and notes == []
