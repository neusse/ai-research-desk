"""What the calibration actually found, recorded where the reports can see it.

A rating that has been measured and a rating that has not are different claims,
and a reader cannot tell them apart from the number. So the findings live here,
stamped with the versions they were measured against, and every report prints
the standing of the rating it is showing.

This is updated by hand after a calibration run, deliberately. Wiring the
report to the last calibration's output would mean a thin or broken run could
silently upgrade a rating's standing, and "the computer says it is validated
now" is exactly the claim that should need a person behind it.

Findings are invalidated by a change to the formula OR to the factors under it.
A rating is a function of both, so a finding about rating 1.0.0 on factors 1.0.0
says nothing about rating 1.0.0 on factors 1.1.0. When either version moves, the
reports say the standing is unknown and quote the old finding only as history.

The words are chosen with care. "Validated" is reserved for a result that held on
data the rules never saw, across enough distinct companies that one of them
cannot carry it. Nothing here has met that bar yet, so nothing here uses the word.
"""

from __future__ import annotations

from edgedesk.factors.families import FACTORS_VERSION
from edgedesk.verdict.rating import RATING_VERSION

MEASURED_AGAINST = {"rating": "1.0.0", "factors": "1.1.0"}
MEASURED_ON = "2026-09-19"

# Counts are from the saved sample wide-v1.1.json, run 2026-09-19 on factors 1.1.0, re-split on
# 2026-10-05 with the R2-18 purge: a development outcome counts only if its window closed before
# the 2026-01-02 holdout. The first split kept twelve-month outcomes that ran into the holdout;
# purging left 1,100 of the 1,896 twelve-month rows, all from decisions made in 2024. Before and
# after are in wide-v1.1-report.md and wide-v1.1-purged-report.md. The 1.0.0 sample is void for
# valuation: its harness divided a split-adjusted price by as-filed EPS, which priced NVDA before
# its June 2024 split at a P/E of 7.5 instead of 75.5.
FINDINGS = {
    "swing": {
        "standing": "no separation found",
        "summary": ("No separation found. Across 2,961 swing observations on 24 large caps "
                    "from 2024 to 2026, scores of 70 and above and scores under 50 had "
                    "median excess returns within about half a percent of each other at 10, "
                    "20 and 30 days, and the sign flips between windows. No significance test "
                    "was run; the claim is absence of demonstrated separation."),
        "advice": ("No swing rating is published. The setup read and the levels describe "
                   "today's chart and are not forecasts."),
        "sample": 2961,
    },
    "long_term": {
        "standing": "exploratory, not independently tested",
        "summary": ("The top of the range is one company and the bottom is a weak lean. Of "
                    "1,100 completed twelve-month observations, all from decisions made in "
                    "2024, the 52 scoring 80 or more beat SPY by a median of 21 percent, but 48 "
                    "of them are NVDA; four remain without it. The 303 under 50 lagged SPY by a "
                    "median of 10 percent (2 percent against their sector) with 35 percent "
                    "ahead, which holds with any one ticker removed (minus 5 to minus 13 "
                    "percent), yet only 9 of the 15 tickers in that bucket lagged. Between 50 "
                    "and 79 there is no ordering. The weekly windows overlap heavily, and no "
                    "twelve-month observation is held out yet."),
        "advice": ("Read the score as a consistent screen, not a measured forecast. A high "
                   "score has not been shown to predict anything beyond one stock's run. A "
                   "score under 50 is a mild caution. The middle of the range is unranked."),
        "sample": 1100,
    },
}

_UNKNOWN = {
    "standing": "not measured",
    "summary": ("The formula or its factors have changed since the last calibration, so "
                "nothing has been measured about the current version."),
    "advice": "Treat the rating as a consistent screen until it is measured again.",
    "sample": 0,
}


def _current() -> dict:
    return {"rating": RATING_VERSION, "factors": FACTORS_VERSION}


def standing(horizon: str, methodology: str = "company") -> dict:
    """What is known about this horizon's rating, for the reports to print."""
    if methodology != "company":
        return {**_UNKNOWN, "measured_on": None, "stale": True,
                "summary": "The equity-ETF formula has not been historically measured.",
                "advice": "Treat the ETF rating as a transparent screen, not a forecast."}
    finding = FINDINGS.get(horizon)
    if _current() != MEASURED_AGAINST or not finding:
        out = {**_UNKNOWN, "measured_on": None, "stale": True}
        if finding:
            out["previous"] = {**finding, "measured_on": MEASURED_ON,
                               "measured_against": MEASURED_AGAINST}
            out["summary"] += (" The previous version's result, for history only: "
                               + finding["summary"])
        return out
    return {**finding, "measured_on": MEASURED_ON, "stale": False}


def one_line(horizon: str) -> str:
    """The short form, for a card or a scorecard footer."""
    s = standing(horizon)
    if s["stale"]:
        return "Not measured against the current formula."
    if horizon == "swing":
        return (f"No separation found in {s['sample']:,} observations "
                f"({s['measured_on']}); no swing rating is published.")
    return (f"Exploratory only: {s['sample']:,} completed twelve-month observations "
            f"({s['measured_on']}), one company dominant, none held out.")
