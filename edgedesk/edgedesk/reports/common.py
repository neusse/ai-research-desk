"""Formatting shared by every report, so the three cannot drift apart.

The rule that makes three formats safe is that they are three renderings of one
run file, never three analyses. Every number any of them prints comes through
the helpers here, so a change to how a percentage is shown changes it
everywhere, and a figure that is missing looks missing in all three.
"""

from __future__ import annotations

# The families in the order a reader wants them, not alphabetical.
FAMILY_ORDER = ("quality", "growth", "valuation", "momentum", "relative_strength",
                "technical", "earnings", "risk")
HORIZON_ORDER = ("swing", "long_term")
WINDOWS = ("1m", "3m", "6m", "12m")


def foreign_filer_methodology(ev: dict) -> str | None:
    """A prominent annotation when 20-F/6-K normalization was used."""
    caveat = next((c for c in (ev.get("caveats") or [])
                   if c.get("code") == "foreign_filer_normalized"), None)
    if not caveat:
        return None
    report = ((ev.get("profile") or {}).get("latest_foreign_report") or {})
    annual = report.get("form") or "20-F/40-F"
    return (f"Foreign-filer normalization used. Standard US GAAP facts from the issuer's "
            f"annual {annual} and financial 6-K filings were converted into point-in-time TTM "
            "periods. Interim 6-K disclosure is less uniform than a 10-Q, so data quality and "
            "rating confidence are degraded.")


def price(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def pct(v, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:+.{digits}f}%"


def pct0(v, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"


def num(v, digits: int = 2) -> str:
    return "n/a" if v is None else f"{v:,.{digits}f}"


def money(v) -> str:
    """Large dollar figures the way a human says them."""
    if v is None:
        return "n/a"
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= cut:
            return f"${v / cut:,.2f}{suffix}"
    return f"${v:,.0f}"


def fact_value(fact: dict) -> str:
    """One evidence fact, formatted by its own declared unit."""
    unit, value = fact.get("unit"), fact.get("value")
    if value is None:
        return "n/a"
    if unit == "usd":
        return money(value) if abs(value) >= 1e6 else f"${value:,.2f}"
    if unit == "price":
        return price(value)
    if unit == "pct":
        return pct0(value)
    if unit == "x":
        return f"{value:,.2f}x"
    if unit == "count":
        return f"{value:,.0f}"
    return str(value)


def signal_value(signal: dict) -> str:
    """A factor signal's raw input, which has no unit field to lean on.

    Fractions are shown as percentages and multiples as multiples, decided by
    the signal's own key rather than by guessing from magnitude.
    """
    raw = signal.get("raw")
    if raw is None:
        return "n/a"
    if not isinstance(raw, (int, float)):
        return str(raw)
    key = signal.get("key", "")
    if any(k in key for k in ("margin", "roe", "roa", "growth", "ret_", "spy", "sector",
                              "yield", "surprise", "drawdown", "volatility", "atr_pct",
                              "vs_sma", "from_high", "gap")):
        return pct(raw) if raw < 0 or "growth" in key or "ret_" in key else pct0(raw)
    if any(k in key for k in ("ratio", "coverage", "leverage", "peg", "pe", "trend")):
        return f"{raw:,.2f}x"
    if "range_position" in key or "stack" in key:
        return f"{raw:,.2f}"
    return f"{raw:,.2f}"


def rating_line(verdict: dict) -> str:
    """The one-line verdict, identical wherever it appears."""
    if verdict.get("quality_state") == "WITHHELD":
        return "RATING WITHHELD"
    return (f"{verdict.get('rating')} ({num(verdict.get('score'), 1)}/100), "
            f"{verdict.get('conviction')} conviction"
            + ("" if verdict.get("quality_state") == "VALID" else ", DEGRADED"))


def top_contributors(run: dict, horizon: str, n: int = 3,
                     worst: bool = False) -> list[dict]:
    """The families that actually moved this horizon's score.

    Ranked by weight times distance from neutral, not by raw score: a family
    scoring 95 on a 5% weight did less to the verdict than one scoring 30 on a
    25% weight, and a "why" that ignores that is decoration.
    """
    verdict = (run.get("verdicts") or {}).get(horizon) or {}
    rows = []
    for c in verdict.get("contributions") or []:
        score = c.get("score")
        if score is None:
            continue
        rows.append({**c, "pull": (score - 50) * c.get("weight", 0)})
    rows.sort(key=lambda r: r["pull"], reverse=not worst)
    return [r for r in rows if (r["pull"] < 0) == worst][:n]


def family_signals(run: dict, key: str, available_only: bool = True) -> list[dict]:
    fam = (run.get("factors") or {}).get(key) or {}
    signals = fam.get("signals") or []
    return [s for s in signals if s.get("score") is not None] if available_only else signals


def strongest_signals(run: dict, key: str, n: int = 2, worst: bool = False) -> list[dict]:
    signals = sorted(family_signals(run, key),
                     key=lambda s: s["score"], reverse=not worst)
    return signals[:n]


def partial_label(missing) -> str | None:
    """How a report names a synthesis written without the bull or bear case (R2-02)."""
    return f"Partial: the {' and '.join(missing)} case was unavailable" if missing else None
