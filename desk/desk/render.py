"""Turn a desk run into a markdown memo, a JSON bundle, and an Obsidian note."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from desk.config import QMD, VAULT_MEMOS_DIR
from desk.debate import DESKS
from desk.views import VIEWS

_EXEC_SUMMARY = re.compile(r"\*\*Executive Summary\*\*:\s*(.+?)(?:\n\s*\n|\*\*Investment Thesis)", re.S)
_LEVEL_NAMES = (("entry_zone", "Entry zone"), ("stop", "Stop / thesis break"),
                ("first_target", "First target"), ("trim", "Trim / take profit"))
_TARGET_VERB = {"raised": "raised to", "lowered": "lowered to", "new": "set at", "kept": "kept at"}
_ANCHOR_NAMES = (("last_close", "Last settled close"), ("sma_20", "20-day average"),
                 ("sma_50", "50-day average"), ("sma_200", "200-day average"), ("high_20d", "20-day high"),
                 ("low_20d", "20-day low"), ("high_52w", "52-week high"), ("low_52w", "52-week low"),
                 ("atr_14", "Avg daily range (ATR 14)"))


_NAMES = {"A": "quant desk", "B": "veterans", "C": "edge desk"}


_RANGE_DASH = re.compile(r"(?<=[\d$%x])\s*[\u2013\u2014]\s*(?=[\d$])")
_OTHER_DASH = re.compile(r"\s*[\u2013\u2014]\s*")


def plain_dashes(text: str) -> str:
    """House style: no em or en dashes in anything the reader sees. Team reports are stored verbatim
    (the fact check matches quotes against them character for character), so only what is rendered
    for reading is cleaned: a dash between figures becomes a hyphen, any other becomes a comma."""
    return _OTHER_DASH.sub(", ", _RANGE_DASH.sub("-", text))


def markdown(bundle: dict) -> str:
    ticker, as_of = bundle["ticker"], bundle["date"]
    ta, aihf, edge = bundle.get("tradingagents"), bundle.get("ai_hedge_fund"), bundle.get("edge_desk")
    shared = aihf or edge or {}
    horizons = bundle["horizons"]
    close = shared.get("last_close")

    out = [f"# {ticker} desk memo, {as_of}" if horizons else f"# {ticker} desk reports, {as_of}", "",
           f"Position: **{'you own it' if bundle['owns'] else 'not owned (new position)'}**"
           + (f"  |  Last settled close ${close:,.2f}" if isinstance(close, (int, float)) else "")
           + (f" ({bundle['price_session']}, the one close every team used)" if bundle.get("price_session") else ""), ""]
    if horizons:
        out += ["## At a glance", "", "| Verdict | Rating | Agreement | Action for you |", "|---|---|---|---|"]
        for key, h in horizons.items():
            rating = h["outcome"]["rating"]
            label = VIEWS[key]["label"]
            status = h["outcome"].get("status")
            shown = rating.upper() if rating else {"gridlock": "GRIDLOCK", "withheld": "WITHHELD"}.get(
                status, "NO CONSENSUS")
            out.append(f"| {label} | **{shown}** | {h['outcome']['conviction']} | {h['action']} |")
        out.append("")
        native = bundle.get("native") or {}
        if native:
            out += ["_Each team's own call, on its own clock (provenance, not compared): "
                    + ", ".join(f"{_NAMES[d]} {v.get('rating') or 'withheld'} ({v.get('timeframe') or 'clock not stated'})"
                                for d, v in native.items()) + "._", ""]
    else:
        out += ["_The engines were run without a debate: each report stands on its own and there is no "
                "combined rating._", ""]
    if edge and edge.get("methodology"):
        out += ["## Methodology annotation", "",
                f"- **Edge Desk:** {edge['methodology']}", ""]
    out += valuation_section(shared.get("valuation") or {})

    for key, h in horizons.items():
        out += horizon_section(key, h)

    out += ["## Desk reports" + (" (before horizons)" if horizons else ""), "",
            "| Desk | Engine rating | Core call |", "|---|---|---|"]
    if ta:
        out.append(f"| TradingAgents | {ta['rating']} | {_one_line(ta_summary(ta))} |")
    if aihf:
        v = aihf["verdict"]
        rating = aihf.get("rating") or "Withheld"
        confidence = f" ({v['confidence']:.0f}%)" if rating != "Withheld" and isinstance(v.get("confidence"), (int, float)) else ""
        reason = v.get("summary") or (v.get("data_caveats") or ["No analyst call was available."])[-1]
        out.append(f"| ai-hedge-fund | {rating}{confidence} | {_one_line(reason)} |")
    if edge:
        sig = edge.get("swing") or {}
        score = f" ({edge['score']:.1f}/100)" if isinstance(edge.get("score"), (int, float)) else ""
        out.append(f"| Edge Desk | {edge.get('rating') or 'Withheld'}{score}, long term | "
                   f"{_one_line('Swing setup ' + str(sig.get('signal') or 'n/a') + ': ' + str(sig.get('why') or ''))} |")
    out.append("")

    anchors = shared.get("anchors") or {}
    if anchors:
        out += ["<details><summary>Computed price anchors</summary>", "", "| Anchor | Value |", "|---|---|"]
        out += [f"| {name} | {anchors[k]} |" for k, name in _ANCHOR_NAMES if anchors.get(k) is not None]
        out += ["", "</details>", ""]

    costs = bundle["costs"]
    parts = [f"{label} {_usd(costs.get(k))}" for k, label, present in
             (("tradingagents", "TradingAgents", ta), ("ai_hedge_fund", "ai-hedge-fund", aihf),
              ("edge_desk", "Edge Desk", edge)) if present]
    if horizons:
        parts.append(f"horizon ratings + debates + memos {_usd(costs['desk'])}")
    billing = " All on the Max plan: nothing billed." if costs.get("billing") == "max" else ""
    out += ["---", f"Cost: {_usd(costs['total'])} ({', '.join(parts)}).{billing} Files: `{bundle['files']['json']}`",
            "", "_Research output for the investor's own decision; not investment advice._", ""]
    return plain_dashes("\n".join(out))


def valuation_section(panel: dict) -> list[str]:
    if not panel:
        return []
    eps = f"{panel['eps_ttm']:.2f}" if isinstance(panel.get("eps_ttm"), (int, float)) else "n/a"
    out = ["## Valuation", "", f"Price ${panel['price']}  |  TTM EPS {eps}  |  P/E now {panel['pe_now']}", "",
           "| Method | Fair value | Price vs it |", "|---|---|---|"]
    h = panel.get("own_history")
    if h:
        out.append(f"| Own-history multiple (median P/E {h['median_pe']}, {h['periods']} quarters) | "
                   f"{h['fair_low']} / **{h['fair_mid']}** / {h['fair_high']} | {h['price_vs_mid']:+.1%} vs mid |")
    p = panel.get("peg")
    if p and p.get("fair_value"):
        out.append(f"| PEG = 1 (EPS growth {p['eps_cagr']:.1%} a year{', P/E capped at 30' if p['capped'] else ''}) | "
                   f"**{p['fair_value']}** | {p['price_vs_fair']:+.1%} |")
    elif p:
        out.append(f"| PEG = 1 | n/a | {p['note']} |")
    s = panel.get("street")
    if s:
        out.append(f"| Street ({s['analysts']} analysts, {s['rating']}) | low {s['target_low']} / "
                   f"**mean {s['target_mean']}** / high {s['target_high']} | mean {s['upside_to_mean']:+.1%} |")
    t = panel.get("target_changes") or {}
    if t.get("count"):
        out += ["", f"Recent price-target actions: {t['count']} ({t['raised']} raised, {t['lowered']} lowered)"]
        out += [f"- {c['date']} {c['firm']}: {c['action']}{' ' + c['rating'] if c['rating'] else ''}, "
                f"target {_TARGET_VERB.get(c['direction'], 'at')} {c['target']:g}" for c in t["latest"]]
    out.append("")
    return out


def horizon_section(key: str, h: dict) -> list[str]:
    result, memo = h["outcome"], h["memo"]
    if result.get("status") == "withheld":         # desk/eligibility.py: nothing was rated
        return [f"## {VIEWS[key]['label']}: WITHHELD", "", f"> {memo['headline']}", "",
                memo["summary"], "", f"**Action for you: {h['action']}.**", ""]
    rating = (result["rating"].upper() if result["rating"] else
              "GRIDLOCK" if result.get("status") == "gridlock" else "NO CONSENSUS")
    out = [f"## {VIEWS[key]['label']}: {rating} ({result['conviction']})", "",
           f"> {memo['headline']}", "", f"_{result['how']}._",
           *([f"_{result['note']}._"] if result.get("note") else []), "",
           f"**Action for you: {h['action']}.** {memo['action_note']}", "",
           "### Summary", memo["summary"], "", "### Valuation view", memo["valuation_view"], "",
           "### Each desk's rating", *[f"- {DESKS[d]}: **{r['rating'] or 'No view'}**"
                                       + (f" ({r['timeframe']})" if r.get("timeframe") else "")
                                       + f". {r['rationale']}"
                                    for d, r in h["restated"].items()], ""]
    if h.get("debate"):
        out += ["### Debate", ""]
        for t in h["debate"]["turns"]:
            out.append(f"**Round {t['round']}, {DESKS[t['desk']]}: {t['decision']} at {t['rating']}.** "
                       f"{t['argument']}")
            if t["what_changed_my_mind"]:
                out.append(f"  - What changed its mind: {t['what_changed_my_mind']}")
            if t["note"]:
                out.append(f"  - Moderator: {t['note']}")
            out.append("")
    if memo.get("disagreement_crux"):
        out += ["### The crux", memo["disagreement_crux"], ""]
    if memo.get("super_manager_view"):
        out += ["### Super Manager view", memo["super_manager_view"], ""]
    out += ["### Why", *[f"- {x}" for x in memo["key_reasons"]], "",
            "### Risks", *[f"- {x}" for x in memo["key_risks"]], "",
            "### Price levels", "", "| Level | Price | Why |", "|---|---|---|"]
    out += [f"| {name} | {_level_price(memo['levels'][k])} | {_one_line(_level_why(memo['levels'][k]))} |"
            for k, name in _LEVEL_NAMES]
    out += ["", "### What to watch", *[f"- {x}" for x in memo["what_to_watch"]], ""]
    return out


def vault_note(bundle: dict, memo_md: str) -> str:
    horizons = bundle["horizons"]
    # Long term leads, unless it was withheld (no levels): then the first horizon that has them.
    rated = [h for h in horizons.values() if h["memo"].get("levels")]
    lead = horizons.get("long_term") if horizons.get("long_term", {}).get("memo", {}).get("levels") else \
        (rated[0] if rated else next(iter(horizons.values())))
    front = ["---", "type: thesis", "bucket: investing", "status: active",
             f"created: {bundle['date']}", f"updated: {bundle['date']}", "tags: [investing, desk-memo]",
             f"ticker: {bundle['ticker']}", f"owned: {'true' if bundle['owns'] else 'false'}",
             f"conviction: {lead['outcome']['conviction']}"]
    for key, h in horizons.items():
        front.append(f"rating_{key}: {h['outcome']['rating'] or ('Withheld' if h['outcome'].get('status') == 'withheld' else 'No consensus')}")
    levels = lead["memo"].get("levels") or {}
    if levels:
        front += [f"target: \"{_level_price(levels['first_target'])}\"", f"risk: \"{_level_price(levels['stop'])}\""]
    front += ["---", ""]
    return "\n".join(front) + memo_md


def write_all(bundle: dict, memos_dir: Path, to_vault: bool, say=print, run_dir: Path | None = None) -> dict:
    """The run's own copy (run_dir/memo.json and memo.md, never overwritten) and the day's pointer
    (memos/TICKER-DATE.json and .md, the latest finished run of that day). The pointer is replaced
    only once the run's copy is written, each file in one step, so a failed rerun leaves the
    earlier run as the day's result."""
    stem = f"{bundle['ticker']}-{bundle['date']}"
    memos_dir.mkdir(parents=True, exist_ok=True)
    md_path, json_path = memos_dir / f"{stem}.md", memos_dir / f"{stem}.json"
    bundle["files"] = {"markdown": str(md_path), "json": str(json_path)}
    if run_dir is not None:
        bundle["files"]["run"] = str(run_dir / "memo.json")
    memo_md = markdown(bundle)
    if run_dir is not None:
        _replace(run_dir / "memo.md", memo_md)
        _replace(run_dir / "memo.json", json.dumps(bundle, indent=2))
    paths = {"markdown": md_path, "json": json_path}
    try:
        archive_legacy(memos_dir, stem)
    except OSError as exc:
        # The day's earlier result could not be kept, so it stays the day's result; this run is
        # saved in its own folder and opens from there.
        say(f"  Could not archive the earlier {stem} memo ({exc}); it stays as the day's result.")
        return {"markdown": run_dir / "memo.md", "json": run_dir / "memo.json"} if run_dir else paths
    _replace(md_path, memo_md)
    _replace(json_path, json.dumps(bundle, indent=2))
    if to_vault and VAULT_MEMOS_DIR:
        VAULT_MEMOS_DIR.mkdir(parents=True, exist_ok=True)
        note = VAULT_MEMOS_DIR / f"{bundle['date']} {bundle['ticker']} desk memo.md"
        note.write_text(vault_note(bundle, memo_md), encoding="utf-8")
        paths["vault"] = note
        _reindex_vault(say)
    return paths


LEGACY_SUFFIX = "000000"                    # run ids end in 6 hex digits; zeros mark an archived old run


def archive_legacy(memos_dir: Path, stem: str) -> str | None:
    """Before a new run replaces memos/STEM.json, keep a day's result saved before runs had ids.

    It moves, as a copy, into memos/runs/STEM-HHMMSS-000000/ (memo.json with that run id, memo.md,
    and the old run folder's team reports, events and bars), so it opens like any other run of
    the day. Idempotent, and the folder appears in one rename, so a failure leaves no half copy
    and the old pointer untouched. Returns the run id, or None when there was nothing to keep."""
    old_json = memos_dir / f"{stem}.json"
    try:
        old = json.loads(old_json.read_text(encoding="utf-8")) if old_json.exists() else None
    except json.JSONDecodeError:
        old = None
    if not old or old.get("run_id"):
        return None
    hhmmss = (old.get("generated_at") or "")[11:19].replace(":", "")
    rid = f"{hhmmss if len(hhmmss) == 6 and hhmmss.isdigit() else '000000'}-{LEGACY_SUFFIX}"
    dest = memos_dir / "runs" / f"{stem}-{rid}"
    if (dest / "memo.json").exists():
        return rid
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    legacy_dir = memos_dir / "runs" / stem
    if legacy_dir.is_dir():
        shutil.copytree(legacy_dir, tmp)
    else:
        tmp.mkdir(parents=True)
    (tmp / "memo.json").write_text(json.dumps({**old, "run_id": rid, "archived_from": f"{stem}.json"}, indent=2),
                                   encoding="utf-8")
    old_md = memos_dir / f"{stem}.md"
    if old_md.exists():
        shutil.copy2(old_md, tmp / "memo.md")
    os.replace(tmp, dest)
    return rid


def _replace(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def ta_summary(ta: dict) -> str:
    decision = ta["reports"].get("final_trade_decision", "")
    match = _EXEC_SUMMARY.search(decision)
    return match.group(1).strip() if match else decision[:400]


def _reindex_vault(say) -> None:
    if not QMD or not QMD.exists():
        return
    try:
        for step in ("update", "embed"):
            subprocess.run([str(QMD), step], capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"  (vault search re-index skipped: {exc})")


def _level_price(lv: dict) -> str:
    """Checked levels were computed in code; older bundles carry the model's own text, marked."""
    if lv.get("status") == "withheld":
        return "Withheld"
    return lv["price"] if lv.get("status") == "checked" else f"{lv['price']} (unchecked)"


def _level_why(lv: dict) -> str:
    if lv.get("status") == "withheld":
        return f"{lv.get('why')}. {lv.get('reason') or ''}".strip()
    return f"{lv['reason']} ({lv['how']})" if lv.get("how") else lv["reason"]


def _one_line(value: str, limit: int = 260) -> str:
    flat = " ".join(str(value).split()).replace("|", "/")
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _usd(value) -> str:
    return "n/a (reused)" if value is None else f"${value:.2f}"


def stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
