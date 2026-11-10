"""Horizon ratings and the managers' debate.

For each horizon, each desk's manager first restates its own desk's rating for
that horizon, from its own report only. The two engines were not built around
a horizon, so without this step they argue past each other (a broken chart vs a
compounding business). If the restated ratings land on different sides, the
managers debate:

Each turn a manager either DEFENDS (finding a specific hole in the other desk's
report, or bringing evidence from its own report the other did not consider)
or CONCEDES (adopting the other rating, naming exactly what changed its mind).
Two rules are enforced in code, not just asked for in the prompt:
- a concession adopts another manager's current rating on the other side exactly, and says what
  changed its mind;
- a defense cannot drift to a compromise rating on another side (e.g. from
  Overweight to Hold): splitting the difference is not agreement.
A turn that breaks either rule is sent back once with the problem named. It is never repaired: a
second bad answer is recorded as the manager holding its rating, with a note, because rewriting it
into a rating the manager did not choose would manufacture an agreement.
The Super Manager never breaks the tie; an unresolved debate is reported as such.
The investor's position is never shown here: the analysis stays blind to it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from desk import figures as figs
from desk.views import VIEWS
from desk.config import CALLS
from desk.llm import DeskLLM, cached_text, text
from desk.ratings import RATINGS, most_conservative, normalize, side
from desk.valuation import render_text as valuation_text

DESKS = {"A": "TradingAgents", "B": "ai-hedge-fund", "C": "Edge Desk"}

NO_VIEW = "No view"
HORIZON_RATING_SCHEMA = {
    "type": "object",
    "properties": {"rating": {"type": "string", "enum": [*RATINGS, NO_VIEW]}, "rationale": {"type": "string"}},
    "required": ["rating", "rationale"],
    "additionalProperties": False,
}

DEBATE_RULES = """ROLE: you are one of the desks' portfolio managers. For the horizon below, the desks'
ratings landed on different sides. A Super Manager will write the final memo but will not
decide for you: the managers either reach agreement or clearly record why they cannot. Holding
your position to the end is a legitimate outcome when your evidence supports it.

Rules:
- Use only the desk reports, the shared computed data, and the debate so far.
- Judge for the stated horizon, weighting evidence as it says.
- Each turn, DEFEND or CONCEDE.
- To defend, do at least one of: (a) name a specific hole in another desk's report: a
  factual error, a misread or stale number, missing data, or a conclusion its own evidence
  does not support; (b) bring evidence from your own report that the others did not
  consider, and explain why it changes the conclusion. Cite the report section.
- Answer the other managers' latest points directly. Do not repeat earlier arguments
  without new substance.
- Concede only when the other side has shown a specific error in your reasoning, or
  material evidence you lacked, that changes the conclusion. Name exactly what changed your
  mind. Do not concede to be agreeable, and do not hold out from pride: the investor is
  best served by the better-supported view.
- Conceding means adopting another manager's current rating exactly. Compromise ratings are
  not allowed: if you defend, keep a rating on your current side.
- When you concede, list in gives_up the sentences from YOUR OWN desk's report that you no
  longer stand by, each copied exactly (at most 3). Leave it empty when you defend, or when
  you change your rating without disowning anything your report says.
- Every figure you cite must appear in a desk report or the shared computed data. If you
  compute one, give the inputs. A figure found in no report is flagged to the other managers.
- Be concise: at most 180 words of argument per turn."""

TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["defend", "concede"]},
        "rating": {"type": "string", "enum": list(RATINGS)},
        "holes_in_other_report": {"type": "array", "items": {"type": "string"}},
        "new_evidence": {"type": "array", "items": {"type": "string"}},
        "argument": {"type": "string"},
        "what_changed_my_mind": {"type": "string"},
        "gives_up": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "rating", "holes_in_other_report", "new_evidence", "argument",
                 "what_changed_my_mind", "gives_up"],
    "additionalProperties": False,
}


@dataclass
class Turn:
    round: int
    desk: str
    decision: str
    rating: str
    argument: str
    holes_in_other_report: list[str] = field(default_factory=list)
    new_evidence: list[str] = field(default_factory=list)
    what_changed_my_mind: str = ""
    note: str = ""
    gives_up: list[str] = field(default_factory=list)     # own-report sentences a concession disowns


@dataclass
class DebateResult:
    turns: list[Turn]
    outcome: str                      # "agreed" | "gridlock" (2 vs 1 held) | "no_agreement"
    ratings: dict[str, str]           # each desk's rating when the debate ended
    conceded_by: str | None = None    # the last desk to concede
    agreed_rating: str | None = None
    conceded: list[str] = field(default_factory=list)      # every desk that conceded, in order

    def to_dict(self) -> dict:
        return {**asdict(self), "turns": [asdict(t) for t in self.turns]}


def horizon_text(key: str) -> str:
    v = VIEWS[key]
    if not v["weighting"]:
        return f"VIEW: {v['label']}. Each desk's own call on its own stated timeframe."
    return f"HOLDING PERIOD: {v['label']}. {v['weighting']}"


def reports_block(ticker: str, as_of: str, reports: dict[str, dict], shared: dict | None = None) -> str:
    """Every selected desk's full work plus shared computed data. Identical for every call
    (cached). `reports` maps desk code (A TradingAgents, B ai-hedge-fund, C Edge Desk) to its
    payload. The shared data comes from ai-hedge-fund's runner when it ran, else from Edge Desk,
    which computes the same anchors and valuation panel; pass `shared` to keep it when `reports`
    holds a single desk (own_block)."""
    ta, aihf, edge = reports.get("A"), reports.get("B"), reports.get("C")
    shared = shared if shared is not None else (aihf or edge or {})
    close = shared.get("last_close")
    anchors = shared.get("anchors") or {}
    parts = [f"STOCK: {ticker}   DATE: {as_of}"
             + (f"   LAST SETTLED CLOSE: ${close:,.2f}" if isinstance(close, (int, float)) else "")]
    if shared:
        parts += ["", "=== SHARED COMPUTED DATA (not any desk's opinion) ===",
                  "Valuation panel:", valuation_text(shared.get("valuation") or {}),
                  "Price anchors (daily bars): " + ", ".join(f"{k} {val}" for k, val in anchors.items())]
    if ta:
        r = ta["reports"]
        parts += [
            "", f"=== DESK A: TradingAgents   RATING: {ta['rating']} ===",
            "(Market, fundamentals, news and social sentiment analysts; bull/bear research debate;",
            " trader; risk debate; portfolio manager. The desk did not target a specific horizon.)",
            "", "--- Portfolio manager final decision ---", r.get("final_trade_decision", ""),
            "", "--- Research manager plan ---", r.get("investment_plan", ""),
            "", "--- Trader plan ---", r.get("trader_investment_plan", ""),
            "", "--- Market (technical) report ---", r.get("market_report", ""),
            "", "--- Fundamentals report ---", r.get("fundamentals_report", ""),
            "", "--- News report ---", r.get("news_report", ""),
            "", "--- Sentiment report ---", r.get("sentiment_report", ""),
        ]
    if aihf:
        v = aihf["verdict"]
        parts += [
            "", f"=== DESK B: ai-hedge-fund   RATING: {aihf.get('rating') or 'Withheld'} ===",
            "(Investor-persona analysts over SEC filings, post-earnings drift, Street consensus;",
            " research manager verdict. The desk did not target a specific horizon.)",
            "", "--- Research manager verdict ---",
            f"Rating {v.get('rating') or 'Withheld'} at "
            f"{str(v['confidence']) + '%' if v.get('confidence') is not None else 'n/a'} confidence; mechanical desk score "
            f"{v.get('desk_score')} ({v.get('desk_rating')}).",
            f"Summary: {v.get('summary') or ''}",
            f"Thesis: {v.get('thesis') or ''}",
            "Bull case:", *[f"- {x}" for x in v.get("bull_case") or []],
            "Bear case:", *[f"- {x}" for x in v.get("bear_case") or []],
            "What would change the call:", *[f"- {x}" for x in v.get("what_would_change") or []],
            "Data caveats:", *([f"- {x}" for x in v.get("data_caveats") or []] or ["- none"]),
            "Recent headlines:", *([f"- {x}" for x in v.get("headlines") or []] or ["- none"]),
            "", "--- Analyst calls ---",
        ]
        for p_ in aihf["personas"]:
            confidence = f", {p_['confidence']:.0f}% confidence" if isinstance(p_.get("confidence"), (int, float)) else ""
            parts += [f"[{p_['model']}] {str(p_['call']).upper()}{confidence}", p_.get("reasoning") or "", ""]
        parts += ["--- Fundamentals the analysts saw ---", aihf.get("fundamentals", "")]
    if edge:
        sig = edge.get("swing") or {}
        parts += [
            "", f"=== DESK C: Edge Desk   LONG-TERM RATING: {edge.get('rating') or 'WITHHELD'} "
                f"({edge.get('rating_basis') or 'factor screen'}) ===",
            "(A deterministic engine: a point-in-time evidence package, then a long-term business case",
            " (owner earnings, a reverse DCF, three five-year scenarios, an expected annual return against",
            " a 10% hurdle) beside a factor screen. Its long-term rating is for 1 year or more. It does not",
            " VOTE on the swing horizon: its swing call gives an entry, an invalidation and a size, but was",
            " measured and not shown to pick winners, so it is offered below as evidence.)",
            f"Swing call: {sig.get('signal') or 'n/a'}. {sig.get('why') or ''}",
            "", "--- Edge Desk full report ---", edge.get("report_md") or "",
        ]
    return "\n".join(parts)


def own_block(ticker: str, as_of: str, reports_by_desk: dict[str, dict], desk: str) -> str:
    """One desk's report and the shared computed data, nothing from the other desks (audit A02,
    R2-07). The shared data is computed, not any desk's opinion, so every manager may see it."""
    shared = reports_by_desk.get("B") or reports_by_desk.get("C") or {}
    return reports_block(ticker, as_of, {desk: reports_by_desk[desk]}, shared=shared)


def rate_for_horizon(llm: DeskLLM, reports: str, desk: str, horizon: str, desk_rating: str) -> dict:
    """A separate question, not a revision: how does this desk rate the stock for this holding
    period? `reports` must be this desk's own_block: the restatement is blind to the other desks,
    so the first reading of each horizon is independent and only the debate compares them. A
    manager whose evidence does not speak to the period answers No view, and does not vote."""
    prompt = "\n".join([
        f"ROLE: you are the manager of DESK {desk} ({DESKS[desk]}). Your desk's own rating is "
        f"{desk_rating}, made on whatever timeframe your work naturally targets.",
        "",
        horizon_text(horizon),
        "",
        "Answer a new question: for an investor holding over exactly this period, what is your "
        "rating? You have your own desk's report and the shared computed data. It may match your "
        "desk's own rating or differ; say what the evidence supports for this period. If your "
        f"report has no evidence that bears on this period, answer \"{NO_VIEW}\" and say why: "
        "a guess is worse than no vote. Rationale: at most 60 words. JSON only.",
    ])
    data = llm.json([cached_text(reports), text(prompt)], HORIZON_RATING_SCHEMA, max_tokens=4000, model=CALLS["horizon_rating"][0], effort=CALLS["horizon_rating"][1])
    rationale = str(data.get("rationale", "")).strip()
    if str(data.get("rating", "")).strip().lower() == NO_VIEW.lower():
        return {"rating": None, "no_view": True, "rationale": rationale}
    return {"rating": normalize(data.get("rating")) or desk_rating, "rationale": rationale}


def run_debate(llm: DeskLLM, reports: str, horizon: str, start: dict[str, str],
               max_rounds: int, say=print, on_turn=None) -> DebateResult:
    """Two or three managers. It ends the moment every rating is on the same side; otherwise
    it runs its rounds and records where each manager stood. Nobody is made to concede."""
    ratings = dict(start)
    order = list(ratings)
    turns: list[Turn] = []
    conceded: list[str] = []
    citable = figs.known(reports)

    for round_no in range(1, max_rounds + 1):
        for desk in order:
            say(f"    Round {round_no}: {DESKS[desk]} manager ({ratings[desk]}) responding...")
            last = round_no == max_rounds and desk == order[-1]
            prompt = (DEBATE_RULES + "\n\n" + horizon_text(horizon) + "\n\n"
                      + _turn_prompt(desk, ratings, turns, round_no, max_rounds, last))
            turn = _take_turn(llm, reports, prompt, desk, round_no, ratings, say, citable)
            turns.append(turn)
            ratings[desk] = turn.rating
            say(f"      -> {turn.decision.upper()} at {turn.rating}")
            if turn.decision == "concede":
                conceded.append(desk)
            if on_turn:
                on_turn(turn)
            if len({side(r) for r in ratings.values()}) == 1:
                return DebateResult(turns, "agreed", dict(ratings),
                                    conceded_by=conceded[-1] if conceded else desk,
                                    agreed_rating=most_conservative(list(ratings.values())),
                                    conceded=conceded)
    sides = [side(r) for r in ratings.values()]
    majority = len(ratings) >= 3 and max(sides.count(x) for x in set(sides)) > len(sides) / 2
    return DebateResult(turns, "gridlock" if majority else "no_agreement", dict(ratings), conceded=conceded)


def transcript(turns: list[Turn]) -> str:
    lines = []
    for t in turns:
        lines.append(f"[Round {t.round}, DESK {t.desk} ({DESKS[t.desk]}), {t.decision.upper()}, "
                     f"rating {t.rating}]")
        lines.append(f"Argument: {t.argument}")
        if t.holes_in_other_report:
            lines += ["Holes found in the other report:", *[f"- {h}" for h in t.holes_in_other_report]]
        if t.new_evidence:
            lines += ["Evidence the other desk did not consider:", *[f"- {e}" for e in t.new_evidence]]
        if t.what_changed_my_mind:
            lines.append(f"What changed my mind: {t.what_changed_my_mind}")
        if t.gives_up:
            lines += ["Statements from its own report it gives up:", *[f"- {g}" for g in t.gives_up]]
        if t.note:
            lines.append(f"(Moderator note: {t.note})")
        lines.append("")
    return "\n".join(lines).strip()


EDGE_ROLE = ("Your desk's long-term rating is its business case, computed from filings and not written by "
             "a model: owner earnings after stock pay, earnings-quality flags, an eight-filing trajectory, a "
             "reverse DCF for the growth the price needs, and bear, base and bull five-year paths weighted "
             "into an expected annual return against a 10% hurdle (Buy at 13% or more, Hold within 3 points "
             "of the hurdle, else Sell). Argue from those numbers and from the assumptions printed with "
             "them. Be candid about its limits: the scenario assumptions are judgement, it has only been "
             "measured on 24 companies, and it is a valuation discipline that sold expensive leaders too "
             "early in that sample. Conceding means you judge the other evidence outweighs it in this "
             "case. It does not change the desk's published number.")


def _turn_prompt(desk: str, ratings: dict, turns: list[Turn], round_no: int, max_rounds: int,
                 last: bool = False) -> str:
    others = [d for d in ratings if d != desk]
    lines = [
        f"You are the manager of DESK {desk} ({DESKS[desk]}). Your current rating for this horizon: "
        f"{ratings[desk]}. " + " ".join(f"The manager of DESK {o} ({DESKS[o]}) rates it {ratings[o]}."
                                        for o in others),
        *([EDGE_ROLE] if desk == "C" else []),
        "",
        "DEBATE SO FAR:",
        transcript(turns) if turns else
        "(none) You open the debate: lead with the strongest hole in another desk's report or the "
        "strongest evidence from your report that the others did not consider.",
        "",
        f"This is round {round_no} of {max_rounds}.",
    ]
    if last:
        lines.append("This is the last turn of the debate. If you still disagree, say precisely what "
                     "evidence would settle it.")
    lines.append("Respond with JSON only.")
    return "\n".join(lines)


def _ask(llm: DeskLLM, reports: str, prompt: str) -> dict:
    return llm.json([cached_text(reports), text(prompt)], TURN_SCHEMA, max_tokens=8000,
                    model=CALLS["debate_turn"][0], effort=CALLS["debate_turn"][1])


def _unsupported(data: dict, citable: set[float] | None) -> list[str]:
    """Figures in a turn's prose that appear in no report."""
    if citable is None:
        return []
    prose = [data.get("argument"), data.get("what_changed_my_mind"),
             *(data.get("holes_in_other_report") or []), *(data.get("new_evidence") or [])]
    return figs.unverified("\n".join(str(x) for x in prose if x), citable)


def _take_turn(llm: DeskLLM, reports: str, prompt: str, desk: str, round_no: int, ratings: dict,
               say=print, citable: set[float] | None = None) -> Turn:
    """One manager's turn, checked in code and never repaired. A turn that breaks a rule is sent
    back once with the problem named. If the second answer is no better, the manager is recorded
    as holding its rating: picking a rating for it would manufacture an agreement it never made.
    A turn quoting figures found in no report is also sent back once; if it still does, it stands
    with a moderator note naming them, so nobody concedes to a number they could not check."""
    data = _ask(llm, reports, prompt)
    problem = _problem(data, desk, ratings)
    loose = [] if problem else _unsupported(data, citable)
    if not problem and not loose:
        return _turn(data, desk, round_no)
    if not problem:
        say(f"      (figures found in no report: {', '.join(loose)}; asking again)")
        again = _ask(llm, reports, prompt + "\n\n" + (
            f"YOUR PREVIOUS ANSWER QUOTED FIGURES THAT APPEAR IN NO REPORT: {', '.join(loose)}. "
            "Answer again. Use only figures from the desk reports or the shared computed data, or "
            "give the inputs of any figure you compute. Respond with JSON only."))
        if _problem(again, desk, ratings):
            return _turn(data, desk, round_no, note=_figure_note(loose))
        still = _unsupported(again, citable)
        return _turn(again, desk, round_no,
                     note=_figure_note(still) if still else "first answer quoted figures found in no report; asked again")
    say(f"      (turn rejected: {problem}; asking again)")
    data = _ask(llm, reports, prompt + "\n\n" + (
        f"YOUR PREVIOUS ANSWER WAS REJECTED: {problem}. Answer again. Either concede, adopting "
        f"exactly {_either(_concede_options(desk, ratings))} and saying what changed your mind, or "
        f"defend with a rating on your current side ({side(ratings[desk])}). Respond with JSON only."))
    second = _problem(data, desk, ratings)
    if not second:
        return _turn(data, desk, round_no, note=f"first answer rejected ({problem}); asked again")
    return Turn(
        round=round_no, desk=desk, decision="defend", rating=ratings[desk],
        argument=str(data.get("argument", "")).strip(),
        holes_in_other_report=[str(x) for x in data.get("holes_in_other_report") or []],
        new_evidence=[str(x) for x in data.get("new_evidence") or []],
        note=(f"answer rejected twice ({second}), so this manager is recorded as holding "
              f"{ratings[desk]}; the argument is as it wrote it"),
    )


def _figure_note(loose: list[str]) -> str:
    return (f"figures found in no report: {', '.join(loose)}. They may be computed from report "
            "figures or unsupported; treat them as unverified")


def _concede_options(desk: str, ratings: dict) -> list[str]:
    """What a concession may adopt: another manager's current rating, on another side."""
    return sorted({r for d, r in ratings.items() if d != desk and side(r) != side(ratings[desk])},
                  key=RATINGS.index)


def _either(options: list[str]) -> str:
    return " or ".join(options) if options else "another manager's rating"


def _problem(data: dict, desk: str, ratings: dict) -> str | None:
    """Why this turn cannot stand as written, or None."""
    decision, rating = data.get("decision"), normalize(data.get("rating"))
    if decision not in ("defend", "concede"):
        return "the decision must be defend or concede"
    if rating is None:
        return f"the rating must be one of {', '.join(RATINGS)}"
    if decision == "concede":
        options = _concede_options(desk, ratings)
        if rating not in options:
            return (f"a concession adopts another manager's current rating exactly "
                    f"({_either(options)}), not {rating}")
        if not str(data.get("what_changed_my_mind") or "").strip():
            return "a concession must say what changed your mind"
    elif side(rating) != side(ratings[desk]):
        return (f"a defense keeps a rating on your current side ({side(ratings[desk])}); moving to "
                f"{rating} is a compromise, which is not allowed")
    return None


def _turn(data: dict, desk: str, round_no: int, note: str = "") -> Turn:
    """A turn that passed _problem, exactly as the manager gave it."""
    decision = data["decision"]
    return Turn(
        round=round_no, desk=desk, decision=decision, rating=normalize(data["rating"]),
        argument=str(data.get("argument", "")).strip(),
        holes_in_other_report=[str(x) for x in data.get("holes_in_other_report") or []],
        new_evidence=[str(x) for x in data.get("new_evidence") or []],
        what_changed_my_mind=str(data.get("what_changed_my_mind", "")).strip() if decision == "concede" else "",
        note=note,
        gives_up=[str(x).strip() for x in data.get("gives_up") or [] if str(x).strip()] if decision == "concede" else [],
    )
