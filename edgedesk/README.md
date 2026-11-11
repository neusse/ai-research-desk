# Edge Desk

Two calls a name, each with its working and its evidence standing printed beside
it. **Swing (2 days to 8 weeks):** Buy or Sell, and on a Buy the whole trade:
invalidation from the chart, shares from a $1,000 risk budget, two structure
targets, a time limit. **Long term (1 to 5 years):** a business case from the
filings, owner earnings, a reverse DCF for the growth the price needs, and three
five-year scenarios into an expected annual return against a 10% hurdle. A
deterministic core produces every number; an LLM layer on the Claude Max
subscription explains them, reads the filings and the headlines, and may argue,
but never writes a number.

Design record: `docs/` in the Hedge Fund workspace holds the original proposal,
the external review that shaped it, and the spec it started from.

## Status

Everything runs: the deterministic core, four report formats, the calibration,
the strategy research harness, the written analysis with its research layer, and
the weekly rhythm. 252 tests, no network.

**What has been measured matters more than any of the code.** On 229 liquid US
names from 2019 to 2026, with real fills and costs, almost nothing on the chart
predicted the next 1 to 13 weeks; the one thing that held was buying a fast drop
inside a long-term uptrend, modestly. The swing call is built around that and
says so. The long-term business case ordered twelve-month results correctly on
24 names, with the caveats printed on every call. Full record:
`docs/SWING-EVIDENCE-2026-09.md` and "What has been measured" below.

## Run

```bash
.venv/Scripts/edgedesk.exe analyze NVDA                     # one name, now
.venv/Scripts/edgedesk.exe analyze AVGO --llm --format card  # with the written thesis
.venv/Scripts/edgedesk.exe week --out reports --vault        # the weekly job
.venv/Scripts/edgedesk.exe events                            # anything happened since?
.venv/Scripts/edgedesk.exe scan                              # free scores, wide list
.venv/Scripts/edgedesk.exe report AVGO --format full         # re-render, no refetch
.venv/Scripts/edgedesk.exe analyze NVDA --as-of 2026-06-30   # point-in-time
```

Settings live in `~/.edge-desk/.env`. No secrets are stored there: Alpaca
credentials are reached through `ALPACA_ENV_FILE`, which points at the file that
already holds them. `SEC_USER_AGENT` is required (free, no account): the SEC wants a name
and a contact email, `Your Name you@example.com`, and its filing archive refuses requests
without the email.

Who gets looked at lives in `~/.edge-desk/universe.yaml`: `owned` and
`watchlist` get the full treatment, `large_cap` gets a free deterministic score.
Nothing is promoted automatically.

Output goes to `~/.edge-desk/`:

```
runs/YYYY-MM-DD/TICKER.json        the source of truth, one file per run
runs/index.json                    a convenience index, rebuildable
estimates/YYYY-MM-DD/TICKER.json   consensus snapshots, captured from day one
calibration/                       samples and reports from `edgedesk calibrate`
research/                          cached bars, earnings dates, trade lists and reports
                                   from `edgedesk research`; experiments.jsonl logs every try
cache/                             provider caches (EDGAR companyfacts, CIKs)
```

## How it is put together

```
providers/   alpaca, edgar, yahoo, benzinga        ported from ai-hedge-fund's free stack
             plus domestic 10-Q/10-K and US-GAAP-tagged foreign 20-F/6-K facts,
             ETF structure/holdings, filing text and splits
             (repairs the feed's missed splits)
evidence/    package, anchors, decision (valuation at the decision price), valuation,
             business (the long-term case), forward (Street estimates), sentiment,
             sectors, estimates
factors/     scale (the curves), company families plus an equity-ETF fund family
verdict/     swing (the Buy/Sell call and plan), rating (the screen), levels,
             calibration standing, published (what may be shown)
llm/         headless (Max transport), prompts, schema, citations, analysis,
             research (filings, headlines, second look; code-verified)
reports/     scorecard, card, condensed, full, vault; swing, long-term and research blocks
research/    the strategy harness: simulate, plan, evaluate, setups, factor study
calibrate/   harness, outcomes, sample, report
jobs/        cli, week, events, scan, research, universe
```

The flow runs one way. Evidence is collected once for `(ticker, as_of)`; factors
read only the evidence; the verdict reads only the factors; the reports read only
the run file; the LLM layer reads a rendered brief and writes prose beside it.
Nothing downstream can reach back and refetch.

## The rules that matter

**Point in time.** Fundamentals are keyed to SEC filing date, prices stop at the
last settled close (16:15 ET, so every run in a day sees the same numbers),
current analyst consensus is excluded outright from a historical run, and even
the SEC filing index is trimmed to what was filed by the as-of date. Tested in
`tests/test_point_in_time.py`; the calibration harness asserts the same
conditions on every one of its observations.

**Missing is missing.** A figure that cannot be computed is `None`, its signal
abstains, and its family reports reduced coverage. Nothing becomes a zero, and
nothing becomes a neutral score.

**Ratings can be withheld.** Every verdict carries `VALID`, `DEGRADED` or
`WITHHELD`. Withheld when the share count is unresolvable, when EDGAR's
companyfacts feed stops at a quarter older than the newest filed 10-Q, when
there is no usable standard US GAAP data, when price history is too short, or
when under 55% of the weighted evidence could be computed. Foreign filers that
publish standard US GAAP facts are normalized from annual 20-F/40-F and
financial 6-K filings; their rating is marked `DEGRADED` because interim 6-K
disclosure is less uniform than a 10-Q. Every report and the combined desk memo
show a prominent **Foreign-filer normalization used** methodology annotation
when this path is active. A foreign ADS quote is checked against filed public
float and ordinary-share count; if the ratio cannot be confirmed, the rating is
withheld. IFRS-only taxonomies remain withheld. Foreign-filer freshness checks
cover the annual report only: the SEC filing index does not identify which 6-Ks
contain financial statements, so a new interim 6-K cannot trigger a rerun or
expose a lagging companyfacts feed. Semiannual-only filers can have a TTM row
about six months older than a quarterly filer.

**Equity ETFs use a different methodology.** An ETF is detected from Yahoo's
security type and is not sent through the company GAAP gate. Current equity ETFs
are rated from fund structure (fees, assets, reported holdings concentration and
the market price's gap to NAV), market trend and performance relative to SPY.
Every format prints an **ETF methodology used** annotation, and the result is
`DEGRADED` because free holdings data are current-only and the model is not yet
historically calibrated. Distribution yield is never rewarded as investment
return; the model does not claim to measure distribution sustainability.
Historical ETF runs exclude today's fund snapshot, and bond, commodity, cash
and multi-asset ETFs remain `WITHHELD` until they have appropriate benchmarks
and factor curves. ETF conviction is capped at Low while the screen is
unmeasured. The stock swing strategy abstains on ETFs because its backtest does
not cover them.

**Risk points one way.** Higher always means more risk, and risk never enters
the rating: it shapes conviction and the levels.

**The horizons answer different questions.** Swing levels are entry, structural
invalidation and reachable targets. Long-term levels are an accumulation
ceiling, a valuation range, and a thesis break written as business conditions,
because a moving-average break ends a three-week trade and means nothing to a
five-year holding.

**The model writes no numbers.** The LLM layer gets no tools and no network: every call runs
with `--tools ""`, no user or project settings (so no CLAUDE.md or hooks) and no MCP servers, on
both call paths, and a test checks the command. Until 2026-09-25 the default path ran with every
tool available and permissions bypassed, so this sentence was not true there. Any
figure in its prose must match a figure the run holds, by kind as well as by
value, or the call is retried with the offending figures named. A run's numbers
are byte-identical whether the layer ran or not.

**It runs on the subscription.** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
are stripped from the child environment, and a test asserts it. A spent allowance stops the batch at
the first notice (`UsageLimit`, raised in `headless.ask_async`), and the desk refuses to save the
half-written report. Until 2026-09-24 nothing raised it, so the stop never happened.

**Its model is pinned.** Every call names `claude-sonnet-5` (`EDGE_DESK_MODEL` to change). Before
2026-09-24 it named none, so it ran on whatever the user's own Claude Code default was ("sonnet"
at the time); changing that default for everyday coding would have changed the model behind every
report. A test asserts both call paths send the pin.

**Reproducible.** Every run carries a `content_hash` over everything except wall
clocks. The same ticker and as-of date twice must produce the same hash.

## What has been measured

`edgedesk calibrate` runs the deterministic engine across history with no model
anywhere in the path, and asks one question: were higher scores followed by
better outcomes? It does not search for weights, because an optimizer would
trade look-ahead bias for ordinary overfitting.

The first run, 24 large caps, 2024 to 2026, weekly sampling:

| Horizon | Finding |
|---|---|
| Swing (10, 20, 30 days) | **No separation.** Median gaps under 1%, win rates 48% to 53%, adjacent buckets correctly ordered only a quarter to half the time. Worse, the three families carrying two thirds of the weight (relative strength, momentum, technical) go *negative* at 30 days, which is short-term reversal behaving as documented. **So that score is not published.** The swing call that replaced it is built differently; see below. |
| Long term (12 months) | **Exploratory, not independently tested.** Run `wide-v1.1`, factors 1.1.0, re-split 2026-10-05 so a development outcome must close before the 2026-01-02 holdout (audit R2-18): 1,100 completed twelve-month observations remain, all from 2024 decisions. The 52 scoring 80+ beat SPY by a median 21%, but 48 are NVDA. Under 50 (303) lagged by 10% (2% against their sector) with 35% ahead; that survives removing any one ticker, yet only 9 of its 15 tickers lagged. Between 50 and 79 there is no ordering. Weekly windows overlap, so independent results are far fewer than rows. |

Both findings are recorded in `verdict/calibration.py` and printed beside the
rating they describe, so a reader never sees a confident-looking number without
being told what is known about it. The twelve-month window has no held-out
sample yet, because a held-out period cannot contain twelve months of future.

Data fix 2026-10-04 (no factor formula change, so still factors 1.1.0): total debt now
includes convertible notes filed under their own tags (AKAM had $7.56B read as no debt), and
net debt and enterprise value subtract marketable securities as well as cash. This changes the
leverage signal for companies that file only convertible-debt tags, and the calibration rows
for any such company were computed on the old read. A filing with interest expense but no
readable debt figure now withholds the business case (1.1.1) instead of counting no debt.

Factors 1.1.0 (2026-09-19) prices current valuation at the decision date rather
than the filing date. The 1.0.0 sample is void for valuation: its harness divided
a split-adjusted price by as-filed EPS, pricing NVDA before its June 2024 split
at a P/E of 7.5 instead of 75.5. Every 90+ score in that sample was this error.

## Known weaknesses

* **The swing call is a modest, unproven edge.** `verdict/swing.py` publishes Buy or
  Sell with a full plan and says how it got there. Its core (a fast drop inside a
  long-term uptrend, 2 ATR stop, no fixed target) made about +0.26R a trade in both
  samples, with intervals touching zero. Sentiment and analyst inputs have no free
  history and are forward-test only. See `docs/SWING-EVIDENCE-2026-09.md`, including
  what did NOT work: classic trend and momentum readings, four pattern setups, the old
  GREEN/RED signal, tight stops and fixed R targets.
* **The long-term business case is judgement written down.** Its scenario assumptions
  are reasoned constants, not fitted parameters, and its first historical reading is on
  24 names. It is published beside the factor screen so a disagreement is visible.
* **Own-history valuation flatters a de-rated stock.** A company whose multiple
  collapsed looks cheap against its own past indefinitely. Runs flag the gap as
  `multiple_rerated` above 40% and then drop that input from the fair value, the
  trim level, the accumulation ceiling and the valuation factor alike.
* **Quality and growth saturate.** NVDA scores 100 on both, so the curves cannot
  separate an excellent company from a merely very good one.
* **Sector comes from SIC**, a 1980s industrial classification, not GICS. A short
  explicit override table covers the names where the two disagree badly; every
  run records which benchmark it used.
* **Peer comparison needs a peer list** that no free source provides. The sector
  ETF is the stand-in, and the reports say so.
* **The written layer is slow**: about 90 seconds per call, seven calls a name.
  Fine for a weekly job run overnight, wrong for anything interactive.

## Tests

```bash
.venv/Scripts/python.exe -m pytest -q
```

315 tests, no network: the suite runs against a fake client that records every
date boundary it was asked for.
