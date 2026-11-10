"""ETF evidence is judged as a fund, never as a company with missing accounts."""

from __future__ import annotations

from datetime import date

import pytest
import pandas as pd

from edgedesk import reports, run as run_mod
from edgedesk.providers.client import DataClient
from edgedesk.providers.yahoo import YahooClient
from tests.conftest import FakeClient

AS_OF = date(2026, 9, 15)

ETF_PROFILE = {
    "cik": 1848758,
    "name": "NEOS S&P 500 High Income ETF",
    "sic": None,
    "sic_description": None,
    "exchanges": ["Cboe BZX"],
    "security_type": "ETF",
    "latest_report": None,
}

ETF_FUND = {
    "category": "Derivative Income",
    "family": "Neos Funds",
    "legal_type": "Exchange Traded Fund",
    "description": "S&P 500 stocks with a call-options strategy.",
    "expense_ratio": 0.0068,
    "category_expense_ratio": 0.008784,
    "total_assets": 12_465_787_904,
    "nav_price": 52.322,
    "market_price": 52.20,
    "stock_position": 0.978,
    "bond_position": 0.0,
    "cash_position": 0.0238,
    "top_holdings": [
        {"symbol": "AAPL", "name": "Apple Inc", "weight": 0.0767},
        {"symbol": "NVDA", "name": "NVIDIA Corp", "weight": 0.0758},
        {"symbol": "MSFT", "name": "Microsoft Corp", "weight": 0.0591},
        {"symbol": "AMZN", "name": "Amazon.com Inc", "weight": 0.0360},
        {"symbol": "GOOGL", "name": "Alphabet Inc", "weight": 0.0249},
    ],
    "sector_weightings": {"technology": 0.3843, "financial_services": 0.12},
    "as_of": "2026-09-15",
}


@pytest.fixture
def etf_run(tmp_path, monkeypatch):
    monkeypatch.setattr("edgedesk.paths.RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr("edgedesk.paths.ESTIMATES_DIR", tmp_path / "estimates")
    client = FakeClient(metrics=[], consensus=False, release=False,
                        profile=ETF_PROFILE, fund=ETF_FUND)
    return run_mod.analyze("SPYI", AS_OF, client=client, save=False)


def test_equity_etf_uses_fund_evidence_instead_of_gaap_gate(etf_run):
    ev = etf_run["evidence"]
    caveats = {c["code"]: c for c in ev["caveats"]}
    assert ev["profile"]["security_type"] == "ETF"
    assert ev["fund"]["expense_ratio"] == 0.0068
    assert "no_gaap_fundamentals" not in caveats
    assert "no_sector" not in caveats
    assert "no_consensus" not in caveats
    assert "no_release" not in caveats
    assert caveats["etf_methodology"]["severity"] == "degrade"
    assert ev["quality_score"] > 0


def test_equity_etf_gets_separate_publishable_ratings(etf_run):
    assert etf_run["factors"]["fund"]["coverage"] >= 0.8
    assert etf_run["verdicts"]["swing"]["quality_state"] != "WITHHELD"
    assert etf_run["verdicts"]["long_term"]["quality_state"] != "WITHHELD"
    assert etf_run["verdicts"]["long_term"]["rating"] is not None
    assert etf_run["verdicts"]["long_term"]["methodology"] == "equity_etf"
    assert etf_run["versions"]["rating"] == "etf-1.0.0"
    assert etf_run["versions"]["factors"] == "etf-1.0.0"
    assert etf_run["calibration"]["long_term"]["standing"] == "not measured"
    assert "ETF formula" in etf_run["calibration"]["long_term"]["summary"]


@pytest.mark.parametrize("fmt", ("scorecard", "condensed", "full", "card"))
def test_every_report_discloses_the_etf_methodology(etf_run, fmt):
    text = reports.render(etf_run, fmt)
    assert "ETF methodology used" in text
    assert "does not assess distribution sustainability" in text


def test_historical_etf_does_not_receive_current_fund_holdings(monkeypatch):
    monkeypatch.setattr("edgedesk.evidence.package.last_settled_day",
                        lambda *a, **k: date(2026, 9, 15))
    client = FakeClient(metrics=[], consensus=False, release=False,
                        profile=ETF_PROFILE, fund=ETF_FUND)
    run = run_mod.analyze("SPYI", date(2026, 6, 30), client=client, save=False)
    codes = {c["code"] for c in run["evidence"]["caveats"]}
    assert run["evidence"]["fund"] is None
    assert "fund_data_excluded" in codes
    assert run["verdicts"]["long_term"]["quality_state"] == "WITHHELD"


def test_non_equity_etf_is_withheld_until_it_has_appropriate_model():
    fund = {**ETF_FUND, "stock_position": 0.05, "bond_position": 0.93}
    client = FakeClient(metrics=[], consensus=False, release=False,
                        profile=ETF_PROFILE, fund=fund)
    run = run_mod.analyze("BOND", AS_OF, client=client, save=False)
    caveat = next(c for c in run["evidence"]["caveats"]
                  if c["code"] == "unsupported_etf_type")
    assert caveat["severity"] == "withhold"
    assert run["verdicts"]["long_term"]["quality_state"] == "WITHHELD"


def test_yahoo_adapter_normalizes_etf_structure_without_using_distribution_yield():
    class Funds:
        description = "S&P 500\ufffd stocks with a call-options strategy."
        fund_overview = {"categoryName": "Derivative Income", "family": "Neos Funds",
                         "legalType": "Exchange Traded Fund"}
        asset_classes = {"stockPosition": 0.978, "bondPosition": 0.0,
                         "cashPosition": 0.0238}
        sector_weightings = {"technology": 0.3843}
        fund_operations = pd.DataFrame(
            {"SPYI": [0.0068], "Category Average": [0.008784]},
            index=["Annual Report Expense Ratio"],
        )
        top_holdings = pd.DataFrame(
            {"Name": ["Apple Inc", "NVIDIA Corp"],
             "Holding Percent": [0.0767, 0.0758]},
            index=["AAPL", "NVDA"],
        )

    class Ticker:
        info = {"quoteType": "ETF", "totalAssets": 12_465_787_904,
                "navPrice": 52.322, "regularMarketPrice": 52.20,
                "yield": 0.1215}
        funds_data = Funds()

    fund = YahooClient(ticker_factory=lambda symbol: Ticker()).fund_data("SPYI")
    assert fund["expense_ratio"] == 0.0068
    assert fund["category_expense_ratio"] == 0.008784
    assert fund["total_assets"] == 12_465_787_904
    assert fund["premium_discount"] == pytest.approx((52.20 - 52.322) / 52.322)
    assert fund["top_holdings"][0] == {
        "symbol": "AAPL", "name": "Apple Inc", "weight": 0.0767,
    }
    assert "\ufffd" not in fund["description"]
    assert "yield" not in fund


def test_data_client_adds_yahoo_security_type_to_sec_profile():
    client = DataClient.__new__(DataClient)
    client.edgar = type("Edgar", (), {"profile": lambda self, ticker: {
        "cik": 1848758, "name": "NEOS ETF Trust", "sic": None,
    }})()
    client.yahoo = type("Yahoo", (), {"info": lambda self, ticker: {
        "quoteType": "ETF", "longName": "NEOS S&P 500 High Income ETF",
    }})()
    profile = client.profile("SPYI")
    assert profile["security_type"] == "ETF"
    assert profile["cik"] == 1848758


def test_etf_versions_do_not_invalidate_the_company_formula():
    company = run_mod.analyze("TEST", AS_OF, client=FakeClient(), save=False)
    assert company["versions"]["rating"] == "1.0.0"
    assert company["versions"]["factors"] == "1.1.0"
    assert company["verdicts"]["long_term"]["methodology"] == "company"
    assert company["calibration"]["long_term"]["stale"] is False
