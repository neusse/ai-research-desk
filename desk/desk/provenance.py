"""What produced a team's report, so a saved report is reused only for the same inputs.

Every team report the desk saves carries `inputs`: the plan, the settled session the team priced
from, every research setting the team reads, the ai-hedge-fund fund's contents, and a digest of
the source code that ran. A later run that day reuses a saved report only when its inputs are
identical; a Claude report is never reused for a ChatGPT run, a before-close report never for an
after-close run, and a report saved before this existed (no `inputs`) is never reused at all.

Settings are taken by prefix (TRADINGAGENTS_*, HEDGE_FUND_*, EDGE_DESK_*), not from a hand-kept
list, so a new knob counts the day the engine adds it; output locations and anything secret are
left out. A setting that is unset and one set to its default value count as different: that
reruns a team once, which is the safe mistake. Source identity is a content digest of each
package, so a ZIP install or an uncommitted edit is seen too; when it cannot be computed, reuse
is refused rather than assumed safe.

Values recorded are model names, plan names, dates, settings and hashes: no keys or tokens.
"""

from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

from desk.config import AIHF_DIR, EDGE_DIR, TA_DIR

VERSION = 3
EDGE_ENV = Path.home() / ".edge-desk" / ".env"
MANDATES = Path.home() / ".hedge-fund" / "mandates"
DESK_PACKAGE = Path(__file__).resolve().parent

PREFIX = {"quant": "TRADINGAGENTS_", "vets": "HEDGE_FUND_", "edge": "EDGE_DESK_"}
SHARED = ("ALPACA_DATA_FEED", "DESK_LLM")                    # every team's data feed and call path
CHATGPT = ("DESK_CHATGPT_STRONG", "DESK_CHATGPT_FAST", "DESK_CHATGPT_STRONG_EFFORT",
           "DESK_CHATGPT_FAST_EFFORT", "DESK_CHATGPT_EFFORT")
# Where output goes, and secrets: neither changes what a team concludes. A credential is a KEY,
# SECRET, PASSWORD or a singular TOKEN (ACCESS_TOKEN); a TOKENS budget (MAX_TOKENS) is a research
# setting and is kept (Codex G2).
_SKIP = re.compile(r"(KEY|TOKEN(?!S)|SECRET|PASSWORD|CREDENTIAL|_DIR|_PATH|_FILE|VAULT)", re.I)
# Data sources an engine picks by which credentials exist. The choice is recorded, never the value.
_ALPACA_PAIRS = (("alpaca_paper_api_key", "alpaca_paper_secret"),
                 ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"), ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"),
                 ("ALPACA_TRADING_KEY", "ALPACA_TRADING_SECRET"))
SOURCE = {"quant": lambda: TA_DIR / "tradingagents", "vets": lambda: AIHF_DIR / "hedge_fund",
          "edge": lambda: EDGE_DIR / "edgedesk"}
_SOURCE_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".toml", ".md", ".txt", ".j2", ".jinja"}


def expected(engine: str, plan: str, session: str, mandate: str | None = None, env=None) -> dict:
    """The inputs a run of `engine` would have right now."""
    out = {"v": VERSION, "plan": plan, "session": session, "settings": settings(engine, plan, env),
           "data": data_sources(engine, env),
           "code": {"desk": source_digest(DESK_PACKAGE), "engine": source_digest(SOURCE[engine]())}}
    if engine == "vets":
        out["mandate"] = {"name": mandate, "sha256": mandate_hash(mandate)}
    return out


def settings(engine: str, plan: str, env=None) -> dict:
    """Every setting the team reads that can change what it writes, as the team will see it."""
    env = os.environ if env is None else env

    def wanted(k: str) -> bool:
        return ((k.startswith(PREFIX[engine]) or k in SHARED or (plan == "chatgpt" and k in CHATGPT))
                and not _SKIP.search(k))

    values = {k: v for k, v in env.items() if v and wanted(k)}
    if engine == "edge":
        # Edge Desk's own settings file wins over inherited values (edge_runner.edge_settings_win).
        for k, v in _dotenv(EDGE_ENV).items():
            if wanted(k):
                if v:
                    values[k] = v
                else:
                    values.pop(k, None)
    return dict(sorted(values.items()))


def data_sources(engine: str, env=None) -> dict:
    """Which evidence sources the engine will resolve to, from which credentials are present: a
    new Financial Datasets key moves the Veterans off the free stack, Alpaca keys decide between
    Alpaca and Yahoo prices, FRED and Alpha Vantage keys add TradingAgents data. Labels only."""
    env = dict(os.environ if env is None else env)
    if engine == "edge":
        env.update({k: v for k, v in _dotenv(EDGE_ENV).items() if v})
    if engine == "quant":
        return {"fred": bool(env.get("FRED_API_KEY")), "alpha_vantage": bool(env.get("ALPHA_VANTAGE_API_KEY"))}
    out = {"alpaca": _has_alpaca(env)}
    if engine == "vets":
        # hedge_fund/data/factory.py: data_provider(), without importing the engine.
        explicit = (env.get("HEDGE_FUND_DATA_PROVIDER") or "").strip().lower()
        out["provider"] = ("financialdatasets" if explicit in ("fd", "financialdatasets", "financial_datasets")
                           else explicit or ("financialdatasets" if env.get("FINANCIAL_DATASETS_API_KEY") else "free"))
    return out


def _has_alpaca(env) -> bool:
    sources = [env]
    if env.get("ALPACA_ENV_FILE"):
        sources.append(_dotenv(Path(env["ALPACA_ENV_FILE"])))
    return any(s.get(k) and s.get(sec) for s in sources for k, sec in _ALPACA_PAIRS)


def differs(saved: dict | None, wanted: dict) -> str | None:
    """Why a saved report cannot stand in for a new run, or None when it can."""
    if not saved:
        return "it records no inputs (saved before reports carried them)"
    if None in (wanted.get("code") or {"desk": None}).values():
        return "this install's source version cannot be read, so nothing is reused"
    for key, label in (("v", "provenance version"), ("plan", "plan"), ("session", "price session"),
                       ("settings", "research settings"), ("data", "data sources"), ("mandate", "fund"),
                       ("code", "source code")):
        if saved.get(key) != wanted.get(key):
            return f"its {label} differs"
    return None


def mandate_hash(name: str | None) -> str | None:
    path = MANDATES / f"{name}.yaml" if name else None
    if not path or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


@lru_cache(maxsize=None)
def source_digest(folder: Path) -> str | None:
    """A sha256 over the package's source files (paths and contents), or None if it is missing.
    Content, not git: a ZIP install has no history and an uncommitted edit leaves HEAD alone."""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    h = hashlib.sha256()
    files = sorted(p for p in folder.rglob("*")
                   if p.is_file() and p.suffix.lower() in _SOURCE_SUFFIXES
                   and not {"__pycache__", "tests", ".venv", "node_modules"} & set(p.relative_to(folder).parts))
    for p in files:
        h.update(p.relative_to(folder).as_posix().encode())
        h.update(b"\0")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))      # a CRLF checkout is the same code
        h.update(b"\0")
    return h.hexdigest() if files else None


def _dotenv(path: Path) -> dict:
    if not path.exists():
        return {}
    from dotenv import dotenv_values
    return dotenv_values(path)
