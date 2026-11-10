# ai-research-desk

[![CI](https://github.com/simonro/ai-research-desk/actions/workflows/ci.yml/badge.svg)](https://github.com/simonro/ai-research-desk/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/simonro/ai-research-desk)](https://github.com/simonro/ai-research-desk/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A research desk for one stock at a time. Three AI research teams study the same company (two
open-source projects, TradingAgents and ai-hedge-fund, adapted here, and Edge Desk), a desk manager compares their calls for two holding periods (swing, 2 days to 8 weeks,
and long term, 1 year or more), the teams debate where they disagree, and you get a short memo
with a rating, the action for your position, price levels and the reasons. It is for people who
pick their own stocks and want a second, third and fourth opinion that shows its work.

- **It runs on your own AI plan, not an API bill.** Model calls go through your Claude plan (the
  `claude` CLI) or your ChatGPT plan (the `codex` CLI). No per-call charges.
- **Numbers come from code, not from a model.** Prices, valuation and the data checks are computed.
  For each memo price level the model only picks a computed reference (a moving average, a recent
  high or low, a fair value, a Street target, optionally offset by ATR) and code works out the price;
  a level nothing computed supports is withheld. Figures quoted in the debate are checked against
  the reports.
- **It refuses rather than guesses.** Stale filings withhold the long-term rating for the whole desk,
  a missing figure is unknown rather than zero, and a failed step is shown as failed.
- **Research only.** It never places an order or connects to a brokerage account.
- **Free and MIT licensed.** No paid tier, no account, no telemetry.

**[Quick start](#quick-start)** · **[Connect your AI plan](#connect-your-ai-plan)** · **[Watch the video](https://youtu.be/3BXuemYYBLg)** · **[Releases](https://github.com/simonro/ai-research-desk/releases)** · **[Privacy](#privacy-and-your-data)**

**What this is not:** not financial advice, not a signal service, and not a validated model. The
ratings have not been shown to beat the market. Every screenshot below is synthetic demo data for
fictitious companies, not anyone's real research or positions.

![The first screen of a run: swing and long-term calls side by side with the action, levels, votes and reasons, for a fictitious company](docs/screenshot-call.png)

## Privacy and your data

ai-research-desk runs on your own computer. It needs no account, sends no telemetry, and the
dashboard listens on `localhost` only.

### What stays local

- Every run: the teams' reports, the debate, the memo and the PDF, saved in `memos/`. Every run is
  kept, including several runs of one stock on the same day.
- Your settings and keys, in `~/.hedge-desk/.env`.

The run files stay local, but the services below do learn which tickers you research.

### External services

**Your AI plan (Anthropic for Claude, OpenAI for ChatGPT)**: receives the prompts, which contain
the ticker, prices, filings figures, headlines and the teams' reports. It never receives your keys.
Whether you own the stock is kept out of every prompt that rates or debates it, so it cannot sway
a rating; it is sent once per horizon to the final memo writer, which explains the action for your
position (the action itself is set in code from the rating and your position). Every call runs with
no tools, no web search, no file access and none of your personal CLI settings.

**Alpaca (optional)**: receives the tickers you run, to return prices, splits and news headlines.
Without Alpaca keys, prices come from **Yahoo Finance**, which receives the tickers.

**SEC EDGAR**: receives the tickers and the contact string you set in `SEC_USER_AGENT`, which SEC
requires. It returns the companies' filings.

### API keys

Keys live in `~/.hedge-desk/.env` on your machine, which is never committed. Do not share or commit
`.env` files, login folders, run memos or screenshots with personal details.

## What's inside

- **Three research teams**: [TradingAgents](https://github.com/TauricResearch/TradingAgents)
  (analysts, a bull and bear debate, a trader and risk managers),
  [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) (investor personas over SEC filings
  with a research manager), and Edge Desk (a point-in-time evidence package, a deterministic
  business case and factor screen, then a written analysis).
- **The desk manager**: restates each team's call for each horizon from its own report only, sends
  disagreements to a debate where a manager defends or concedes (never a compromise), fact-checks
  what did not survive, and writes the memo. A 2 against 1 standoff is reported as gridlock, not
  decided for you.
- **Data checks**: one settled close for every team, a stale-filing rule, unknown values kept unknown.
  Veterans withholds its rating when every analyst abstains. Its company-filing personas do not
  provide an ETF assessment; use Edge Desk's annotated fund analysis for ETF runs.
- **The dashboard**: start a run, watch the teams work live, reopen any past run (a team report from
  earlier the same day is reused only if it came from the same plan, price session, models, fund and
  code), a weekly review of
  the latest call per symbol, and a two-page PDF summary.

## Screenshots

**The call.** Both horizons side by side, with the action for your position, levels, who voted, how
it was decided, the reasons and risks, earlier runs of the same symbol and the chart.

![The first screen of a run](docs/screenshot-call.png)

**Weekly review.** The latest call per symbol, evidence problems first, with arrows against the
previous run. Here a stale filing has withheld one company's long-term rating.

![The weekly review table](docs/screenshot-weekly.png)

**The PDF summary.** Two pages: the same first screen and the debates in a few lines.

![The two-page PDF summary](docs/screenshot-brief.png)

## Watch the video

[![Watch: I Built a Free AI Stock Research Desk Where 3 AIs Argue](docs/video-thumbnail.jpg)](https://youtu.be/3BXuemYYBLg)

Three AI research teams take the same stock, argue it out for a swing and a long-term call, and the desk writes up where they agree and where they do not.

## Quick start

Requirements: [Python 3.12](https://www.python.org/downloads/) (ai-hedge-fund needs numpy 1.x,
which has no wheels for newer Python on Windows), [git](https://git-scm.com/), and a paid AI plan
with its command-line tool (see [Connect your AI plan](#connect-your-ai-plan)).

**Windows, four steps:** clone the repo with `git clone --recurse-submodules` (or download the ZIP
from [Releases](https://github.com/simonro/ai-research-desk/releases)), then:

1. Double-click `setup.bat`, once. It builds one environment per part and creates
   `~/.hedge-desk/.env`.
2. Open `~/.hedge-desk/.env` (`%USERPROFILE%\.hedge-desk\.env`) and set `SEC_USER_AGENT` to your
   name and email. Add Alpaca keys if you have them; they are optional.
3. Log in to your AI plan once, as described in [Connect your AI plan](#connect-your-ai-plan).
4. Double-click `launch.bat`, every time. Then open http://localhost:8790

**Manual setup (Mac, Linux, or if you prefer):**

```bash
git clone --recurse-submodules https://github.com/simonro/ai-research-desk
cd ai-research-desk
python3.12 install.py
# edit ~/.hedge-desk/.env: set SEC_USER_AGENT, and Alpaca keys if you have them
claude                      # log in once, if you use a Claude plan (see below for ChatGPT)
scripts/run-dashboard.sh    # then open http://localhost:8790
```

Every install starts empty: no runs and no demo data. Type a ticker in the dashboard, pick the
teams, and start a run. A run with all three teams and a debate takes about 10 to 30 minutes.

## Connect your AI plan

The desk has no API keys and no per-call bill. It sends every model call through the command-line
tool of an AI subscription you already pay for, so runs count against that plan's usage. You need
one of these, and you can set up both.

**Claude** (the default). Needs a Claude Pro or Max plan.

1. Install [Claude Code](https://docs.claude.com/en/docs/claude-code), which gives you the `claude`
   command.
2. Run `claude` once in a terminal and sign in with your Claude account. Then close it.

That is all: `DESK_PLAN=claude` is the default. The desk removes any Anthropic API key from its
calls, so they always go to your subscription, never to a metered key.

**ChatGPT**. Needs a paid ChatGPT plan that includes Codex.

1. Install the [Codex CLI](https://github.com/openai/codex): `npm install -g @openai/codex` (needs
   [Node.js](https://nodejs.org/)).
2. Log the desk in once. The desk keeps its own Codex login, separate from any you already use, so
   your personal Codex settings never reach the analysis:

```bash
# PowerShell
$env:CODEX_HOME="$HOME\.hedge-desk\codex"; New-Item -ItemType Directory -Force $env:CODEX_HOME | Out-Null; codex login
# Mac or Linux
mkdir -p ~/.hedge-desk/codex && CODEX_HOME=~/.hedge-desk/codex codex login
```

3. To make ChatGPT the default, set `DESK_PLAN=chatgpt` in `~/.hedge-desk/.env`.

**Picking the plan.** The dashboard's run form has a Claude / ChatGPT switch that starts on your
`DESK_PLAN` default; changing it applies to that run only. One plan per run: a run is all Claude or
all ChatGPT, and the memo records which plan and models it used.

**Usage limits.** A full run with all three teams and a debate makes dozens of model calls. On a
smaller plan, such as Claude Pro, one or two full runs can reach your usage limit; larger plans have
more room. Running one or two teams, or turning the debate off, uses less. If the limit is reached,
or your login has expired, the run stops at once and saves nothing, rather than writing a
half-finished memo. Log in again (`claude`, or the `codex login` command above) or wait for the
limit to reset, then start the run again.

## Updating to a new release

Your runs (`memos/`) and your settings (`~/.hedge-desk/.env`) are not part of a release, so an
update never touches them.

**If you cloned with git:** `git pull`, `git submodule update`, then `setup.bat` (or
`python3.12 install.py`) if dependencies changed.

**If you downloaded the ZIP:** unzip the new version into a new folder, copy `memos/` across, and
run the setup once.

## Configuration

All settings live in `~/.hedge-desk/.env`; `.env.example` documents every one.

| Variable | Enables |
|---|---|
| `DESK_PLAN` | `claude` (default) or `chatgpt` |
| `SEC_USER_AGENT` | Required. Your name and email, which SEC asks for |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca prices and news (free accounts work). Without them prices come from Yahoo and there is no news |
| `DESK_CHATGPT_STRONG`, `DESK_CHATGPT_FAST` | The ChatGPT models for the manager and analyst roles |
| `TRADINGAGENTS_*`, `HEDGE_FUND_*`, `EDGE_DESK_MODEL` | The Claude models each team uses |
| `FRED_API_KEY` | Macro data for TradingAgents (free) |
| `DESK_VAULT_DIR` | Also save each memo as a note in a markdown vault such as Obsidian |
| `DESK_MEMOS_DIR` | Where runs are saved (default `memos/`) |

From the command line: `scripts/run-desk.sh MSFT --own --engines quant,vets,edge` (`.bat` on
Windows). `--own MSFT` marks it as a position you hold; a bare `--own` treats every ticker as new.

## How far to trust it

Large language model ratings cannot be backtested honestly: the models have seen the period being
tested. So the desk is forward-tested only, and nothing here has been shown to predict returns. Edge
Desk prints what has been measured about its own formula, and that is a description, not a
validation. Use the desk to see the arguments for and against a stock, then make your own decision.

## Contributing

Bug reports and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers setup, tests and
how to add a plan. Please report security problems privately, as described in
[SECURITY.md](SECURITY.md), not in a public issue.

## More free tools from Tape to Edge

Same rules for each one: free, MIT, runs on your own machine, and the build is on the [YouTube channel](https://www.youtube.com/@tapetoedge).

- [Edge Scanner](https://github.com/simonro/edge-scanner): a real-time intraday stock scanner with a browser dashboard you lay out yourself.
- [Trading Journal AI](https://github.com/simonro/Trading-Journal-AI): a local-first journal. Import your broker CSV, get round-trip trades, KPIs and AI diary analysis.
- [Edge GEX Lab](https://github.com/simonro/Edge-GEX-Lab): a live gamma exposure (GEX) map for SPY, QQQ, SPX, NDX and stocks, with walls, gamma flip and regime.

## Who made this

I built this to get structured, argued second opinions on the stocks I research, without paying per
call on top of the AI plan I already have. Simon, Tape to Edge.

- YouTube: [@tapetoedge](https://www.youtube.com/@tapetoedge)
- X: [@tapetoedge](https://x.com/tapetoedge)

I take no affiliate money from any tool I show, and this project has no paid tier, no account and
no telemetry. If it is useful, fork it.

## License

MIT. See [LICENSE](LICENSE). TradingAgents (Apache-2.0) is included as a git submodule, and
ai-hedge-fund (MIT) as a modified copy listed in `engines/ai-hedge-fund/CHANGES.md`.
