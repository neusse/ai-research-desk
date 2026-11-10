"""From factor families to a rating, by an explicit rule.

The rating is arithmetic, not judgement. A horizon is a set of weights over the
families; the weighted score falls in a band; the band is the rating. Written
down like this, a disagreement becomes a disagreement about a weight or a
breakpoint, which is a conversation worth having, instead of a disagreement
about what a model felt, which is not.

Three deliberate choices:

* **Risk is not in the sum.** Folding risk into the score makes a risky good
  business and a safe mediocre one indistinguishable. Risk shapes conviction
  and the levels instead, and it is always reported in its own direction.
* **The horizons are separate questions**, not one score read twice. A broken
  chart matters over six weeks and is noise over five years, so the weights
  differ and the two ratings are allowed to disagree.
* **Coverage can withhold the rating.** If too much of the weighted evidence is
  missing, the honest output is no rating, not a confident-looking number built
  from whatever happened to be available.

RATING_VERSION changes whenever a weight or a band moves. The bands here are
uncalibrated starting values: Phase C measures whether higher bands actually
produce better forward outcomes and moves them on evidence, and until it has,
every report says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from edgedesk.evidence.package import Evidence
from edgedesk.factors.families import ETF_FACTORS_VERSION, FACTORS_VERSION, Family

RATING_VERSION = "1.0.0"
ETF_RATING_VERSION = "etf-1.0.0"

VALID, DEGRADED, WITHHELD = "VALID", "DEGRADED", "WITHHELD"

HORIZONS = {
    "swing": {
        "label": "Swing (2 to 6 weeks)",
        "question": "Held for two to six weeks, is this worth owning now?",
        "weights": {
            "relative_strength": 0.25,
            "momentum": 0.22,
            "technical": 0.20,
            "earnings": 0.15,
            "valuation": 0.08,
            "quality": 0.05,
            "growth": 0.05,
        },
    },
    "long_term": {
        "label": "Long term (1 year or more)",
        "question": "Held for a year or more, is this worth owning now?",
        "weights": {
            "quality": 0.26,
            "growth": 0.24,
            "valuation": 0.24,
            "earnings": 0.08,
            "momentum": 0.07,
            "relative_strength": 0.06,
            "technical": 0.05,
        },
    },
}

ETF_HORIZONS = {
    "swing": {
        "label": "Swing (2 to 6 weeks)",
        "question": "Held for two to six weeks, is this equity ETF worth owning now?",
        "weights": {
            "relative_strength": 0.35,
            "momentum": 0.30,
            "technical": 0.25,
            "fund": 0.10,
        },
    },
    "long_term": {
        "label": "Long term (1 year or more)",
        "question": "Held for a year or more, is this equity ETF worth owning now?",
        "weights": {
            "fund": 0.50,
            "relative_strength": 0.25,
            "momentum": 0.15,
            "technical": 0.10,
        },
    },
}

# Same five tiers both upstream projects use, so their output stays comparable.
BANDS = (
    (75.0, "Buy"),
    (60.0, "Overweight"),
    (42.0, "Hold"),
    (28.0, "Underweight"),
    (0.0, "Sell"),
)

# Below this share of the weighted evidence, no rating is published at all.
MIN_COVERAGE = 0.55
# Below this, the rating is published but marked degraded.
GOOD_COVERAGE = 0.80


@dataclass
class Verdict:
    horizon: str
    label: str
    score: float | None
    rating: str | None
    quality_state: str
    conviction: str
    coverage: float
    risk_score: float | None
    contributions: list[dict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    action: str | None = None
    methodology: str = "company"

    def to_dict(self) -> dict:
        is_etf = self.methodology == "equity_etf"
        return {
            "horizon": self.horizon, "label": self.label, "score": self.score,
            "rating": self.rating, "quality_state": self.quality_state,
            "conviction": self.conviction, "coverage": self.coverage,
            "risk_score": self.risk_score, "action": self.action,
            "contributions": self.contributions, "reasons": self.reasons,
            "methodology": self.methodology,
            "rating_version": ETF_RATING_VERSION if is_etf else RATING_VERSION,
            "factors_version": ETF_FACTORS_VERSION if is_etf else FACTORS_VERSION,
        }


def band_for(score: float) -> str:
    for floor, label in BANDS:
        if score >= floor:
            return label
    return BANDS[-1][1]


def rate(ev: Evidence, families: dict[str, Family], horizon: str,
         owns: bool = False) -> Verdict:
    """Score one horizon and decide whether it may be published."""
    is_etf = (ev.profile or {}).get("security_type") == "ETF"
    spec = (ETF_HORIZONS if is_etf else HORIZONS)[horizon]
    methodology = "equity_etf" if is_etf else "company"
    weights: dict[str, float] = spec["weights"]

    contributions: list[dict] = []
    weighted_sum = 0.0
    live_weight = 0.0
    for key, weight in sorted(weights.items(), key=lambda kv: -kv[1]):
        fam = families.get(key)
        available = fam is not None and fam.available
        contributions.append({
            "family": key,
            "label": fam.label if fam else key,
            "weight": weight,
            "score": fam.score if fam else None,
            "coverage": fam.coverage if fam else 0.0,
            "contribution": round(fam.score * weight, 2) if available else None,
        })
        if available:
            # A family that could only compute part of itself carries only that
            # part of its weight, so thin evidence dilutes rather than pretends.
            effective = weight * fam.coverage
            weighted_sum += fam.score * effective
            live_weight += effective

    total_weight = sum(weights.values())
    coverage = round(live_weight / total_weight, 4) if total_weight else 0.0
    score = round(weighted_sum / live_weight, 2) if live_weight > 0 else None

    risk_family = families.get("risk")
    risk_score = risk_family.score if risk_family and risk_family.available else None

    state, reasons = _quality_state(ev, coverage)
    if state == WITHHELD or score is None:
        if score is None and state != WITHHELD:
            state = WITHHELD
            reasons.append("No factor family could be computed, so there is no score to band.")
        return Verdict(horizon=horizon, label=spec["label"], score=score, rating=None,
                       quality_state=WITHHELD, conviction="None", coverage=coverage,
                       risk_score=risk_score, contributions=contributions, reasons=reasons,
                       action="No action: rating withheld", methodology=methodology)

    rating = band_for(score)
    conviction = "Low" if is_etf else _conviction(score, coverage, risk_score, families, weights)
    return Verdict(horizon=horizon, label=spec["label"], score=score, rating=rating,
                   quality_state=state, conviction=conviction, coverage=coverage,
                   risk_score=risk_score, contributions=contributions, reasons=reasons,
                   action=action_for(rating, owns), methodology=methodology)


def _quality_state(ev: Evidence, coverage: float) -> tuple[str, list[str]]:
    """VALID, DEGRADED or WITHHELD, with the reasons spelled out.

    Missing data never silently becomes a neutral score: either enough remains
    to judge on, or the engine says it cannot judge.
    """
    reasons: list[str] = []
    blocking = ev.blocking
    if blocking:
        return WITHHELD, [c.message for c in blocking]
    if coverage < MIN_COVERAGE:
        return WITHHELD, [
            f"Only {coverage:.0%} of the weighted evidence could be computed "
            f"(the floor is {MIN_COVERAGE:.0%}). Insufficient data: rating withheld."
        ]
    if coverage < GOOD_COVERAGE:
        reasons.append(f"{coverage:.0%} of the weighted evidence was available.")
    reasons.extend(c.message for c in ev.degrading)
    return (DEGRADED if reasons else VALID), reasons


def _conviction(score: float, coverage: float, risk_score: float | None,
                families: dict[str, Family], weights: dict[str, float]) -> str:
    """High, Medium or Low, from three things that are not the score itself:
    how complete the evidence was, how much the families agree with each other,
    and how much risk sits underneath."""
    live = [(families[k].score, w) for k, w in weights.items()
            if k in families and families[k].available]
    spread = 0.0
    if len(live) >= 3:
        mean = sum(s * w for s, w in live) / sum(w for _, w in live)
        spread = (sum(w * (s - mean) ** 2 for s, w in live) / sum(w for _, w in live)) ** 0.5

    points = 0
    points += 1 if coverage >= GOOD_COVERAGE else 0
    points += 1 if spread < 18 else 0
    points += 1 if (risk_score is None or risk_score < 55) else 0
    points += 1 if abs(score - 50) >= 15 else 0    # a call, rather than a shrug
    return "High" if points >= 3 else "Medium" if points == 2 else "Low"


def action_for(rating: str, owns: bool) -> str:
    """What to do, given the rating and whether it is already owned.

    Set in code, never by a model, and the ownership flag is never shown to the
    LLM layer: knowing a position exists is how endowment bias gets into a
    research note.
    """
    if owns:
        return {
            "Buy": "Add on strength or into the entry zone",
            "Overweight": "Hold, add on weakness into the entry zone",
            "Hold": "Hold, no action",
            "Underweight": "Trim into strength",
            "Sell": "Exit",
        }[rating]
    return {
        "Buy": "Start a position in the entry zone",
        "Overweight": "Start a smaller position, or wait for the entry zone",
        "Hold": "No action, watch",
        "Underweight": "Avoid",
        "Sell": "Avoid",
    }[rating]
