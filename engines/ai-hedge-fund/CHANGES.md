# Changes in this copy

This folder is a snapshot of [virattt/ai-hedge-fund](https://github.com/virattt/ai-hedge-fund)
(MIT, see `LICENSE`), changed for ai-research-desk. The main changes:

- **Free data stack** (`hedge_fund/data/free/`), used when no Financial Datasets key is set:
  SEC EDGAR fundamentals (point-in-time by filing date), Alpaca prices, splits and Benzinga news
  (SIP, then IEX, then Yahoo with no keys), Yahoo analyst consensus. Missed splits are repaired;
  convertible notes count as debt; a missing capex figure is unknown, not zero.
- **Verdicts** (`hedge_fund/verdict/`): a per-ticker desk rating from the analysts' votes and a
  research manager's written view, with data caveats such as stale fundamentals. If every analyst
  abstains, the manager withholds the rating instead of inventing a Hold from missing evidence.
- **Caching:** entries reaching today expire after six hours, so new filings are seen.
- **Strategy:** `street-consensus`.
- **Persona answers that are not clean JSON** are still read: a lenient decoder (raw line breaks,
  braces inside the reasoning), the stated signal and confidence kept when they are unambiguous, then one
  retry before abstaining. When an analyst sits in several strategies, a call with a view wins over an
  abstention in the per-ticker verdict.
- **Structured persona answers:** a client that offers `complete_json` (the desk's subscription
  client) is asked with the signal schema, so an answer cannot stop mid-JSON.
- **Backtest entry points** removed from the TUI and CLI: LLM backtests before a model's training
  cutoff are look-ahead biased, so the desk is forward-tested only.
