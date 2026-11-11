/* The Desk dashboard.
   Routes (all in the query string, so every view has a link):
     ?t=ECG&d=2026-09-15                 a finished run (snapshot)
     ?t=ECG&d=...&h=swing                ... on another horizon
     ?t=ECG&d=...&panel=debate:as_they_ran:0 | agent:quant:news
     ?t=ECG&d=...&doc=memo|quant|vets|edge   the reader
     ?live=<run id>                      a run in progress
   A run uses one, two or three teams (Quant desk, Veterans, Edge Desk), with or without a
   debate. Everything drawn comes from the server (server.py), which reads what the desk wrote. */

"use strict";

const $app = document.getElementById("app");
const PHONE = () => innerWidth <= 700;
const saved = (() => { try { return JSON.parse(localStorage.getItem("desk.form") || "{}"); } catch { return {}; } })();
const S = { runs: [], live: [], run: null, profile: {}, liveState: null, poll: null, drawer: false,
            own: false, engines: { quant: true, vets: true, edge: false, ...(saved.engines || {}) }, debate: saved.debate !== false,
            plan: null, planInfo: null };
// Which subscription answers the model calls. The form defaults to the settings (DESK_PLAN) and
// can switch one run at a time; every run records the plan it used.
const PLAN_NAME = { claude: "Claude", chatgpt: "ChatGPT", api: "API key" };
const planNow = () => S.plan || S.planInfo?.default || "claude";
const planModels = p => { const m = S.planInfo?.models?.[p]; return m ? `${m.strong} and ${m.fast}` : ""; };

/* ------------------------------------------------------------------ helpers */
const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const rc = r => ({ buy: "buy", overweight: "ow", hold: "hold", underweight: "uw", sell: "sell" }[String(r || "").toLowerCase()] || "none");
const words = s => (s || "").split(/\s+/).filter(Boolean).length;
const readMin = w => Math.max(1, Math.round(w / 230));
const mmss = s => s == null ? "" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const dur = s => s == null ? "" : s >= 60 ? `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s` : `${Math.round(s)}s`;
const money = (x, d = 2) => x == null ? "n/a" : "$" + Number(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const big = x => x == null ? "n/a" : x >= 1e12 ? `$${(x / 1e12).toFixed(2)}T` : x >= 1e9 ? `$${(x / 1e9).toFixed(2)}B` : `$${(x / 1e6).toFixed(0)}M`;
const fmtDate = (d, withYear = true) => d ? new Date(d + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", ...(withYear ? { year: "numeric" } : {}) }) : "";
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
const list = xs => xs.length < 2 ? xs.join("") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];
/* Model and tool text becomes HTML here, so it is sanitized. Stripping <script> alone left onerror=
   and friends, and script on this page can call the server. No DOMPurify means no HTML at all. */
const md = s => {
  const src = String(s || "");
  if (!window.marked || !window.DOMPurify) return `<pre>${esc(src)}</pre>`;
  return DOMPurify.sanitize(marked.parse(src)).replace(/<table>/g, '<div class="tbl"><table>').replace(/<\/table>/g, "</table></div>");
};
/* Enough of the opening to carry the point: whole sentences until about 90 characters. */
const firstSentence = s => {
  const parts = String(s || "").split(/(?<=[.!?])\s+(?=[A-Z$("])/);
  let out = "";
  for (const p of parts) { out += (out ? " " : "") + p; if (out.length >= 90) break; }
  return out.trim();
};
/* ---- what a team gave up or had disputed in the debate, marked inside its own report --------
   A team giving a point up records a concession, not a proof the point was wrong.
   withdrawn  = the report's own team accepted the point: struck through.
   challenged = another team disputed it and the owner held: flagged, never struck.
   `isMd` text is markdown source (the tags pass through marked); otherwise it is plain text. */
const fixesFor = (run, team) => (run?.corrections || []).map((c, i) => ({ ...c })).filter(c => c.team === team).map((c, i) => ({ ...c, n: i + 1 }));
function mark(run, team, src, isMd) {
  let out = isMd ? String(src || "") : esc(src);
  for (const c of [...fixesFor(run, team)].sort((a, b) => b.quote.length - a.quote.length)) {
    const q = isMd ? c.quote : esc(c.quote), at = out.indexOf(q);
    if (at < 0) continue;
    const tip = esc(`${c.status === "withdrawn" ? "Withdrawn" : "Challenged"} in the debate: ${c.reason}`);
    const tag = c.status === "withdrawn" ? "del" : "mark";
    out = out.slice(0, at) + `<${tag} class="fix ${c.status}" title="${tip}">${q}</${tag}><sup class="fixn">${c.n}</sup>` + out.slice(at + q.length);
  }
  return out;
}
function fixesBox(run, team) {
  const xs = fixesFor(run, team);
  if (!xs.length) return "";
  return `<aside class="fixes" aria-label="Corrections from the debate"><h5>What the debate changed in this report</h5>
    <p class="fine">Struck text was given up by the ${TEAMS[team].name} itself during the debate. Highlighted text was disputed by another team and the ${TEAMS[team].name} stood by it, so it is flagged, not struck.</p>
    <ol>${xs.map(c => `<li><${c.status === "withdrawn" ? "del" : "mark"} class="fix ${c.status}">${esc(c.quote.replace(/[*_`#|]/g, "").trim())}</${c.status === "withdrawn" ? "del" : "mark"}>
      <span><b>${c.status === "withdrawn" ? "Given up by its own team" : "Disputed"}</b> · ${esc(c.horizon)} debate, round ${c.round}${c.by ? ` · raised by the ${TEAMS[c.by].name}` : ""}. ${esc(c.reason)}</span></li>`).join("")}</ol></aside>`;
}

const Q = () => Object.fromEntries(new URLSearchParams(location.search));
// A run's address: the day's latest is ?t=&d=, any other run of that day adds r (its run id).
const runQ = run => ({ t: run.ticker, d: run.date, ...(run.run_id && !run.latest ? { r: run.run_id } : {}) });
function go(params, replace = false) {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== "")).toString();
  history[replace ? "replaceState" : "pushState"](null, "", qs ? "?" + qs : location.pathname);
  route();
}
/* Every POST carries the token the server hands this page (another site cannot read it). A server
   restart mints a new one, so a refused POST fetches it again and retries once. */
let deskToken = null;
const token = async fresh => {
  if (!deskToken || fresh) deskToken = (await (await fetch("/api/token")).json()).token;
  return deskToken;
};
async function api(path, opts) {
  const post = !!opts && !!opts.method && opts.method !== "GET";
  const send = async fresh => fetch(path, post ? { ...opts, headers: { ...(opts.headers || {}), "X-Desk-Token": await token(fresh) } } : opts);
  let r = await send(false);
  if (post && r.status === 403) r = await send(true);
  const body = await r.json().catch(() => ({ error: `The server answered ${r.status}.` }));
  if (!r.ok || body.error) throw new Error(body.error || `The server answered ${r.status}.`);
  return body;
}
const ico = {
  x: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3 3l8 8M11 3l-8 8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  up: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3.5 8.5L7 5l3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  dn: '<svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M3.5 5.5L7 9l3.5-3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  plus: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 3v10M3 8h10" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>',
  menu: '<svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  arrow: '<svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M4 2.5L7.5 6 4 9.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

/* ------------------------------------------------------------------ the three teams
   Rosters are in the order each engine runs. `step` is what the desk's events carry. */
const roster = rows => rows.map(([id, name, stage, step]) => ({ id, name, stage, step: step || id }));
const TEAMS = {
  quant: { name: "Quant desk", caps: "QUANT DESK", cls: "q", engine: "tape", color: "var(--quant)", line: "#5667D2", tool: "TradingAgents",
    about: "12 agents, about 7 min", parallel: false,
    roster: roster([["market", "Market analyst", "Analysts", "market_report"], ["sentiment", "Sentiment analyst", "Analysts", "sentiment_report"],
      ["news", "News analyst", "Analysts", "news_report"], ["fund", "Fundamentals analyst", "Analysts", "fundamentals_report"],
      ["bull", "Bull researcher", "Research", "bull_history"], ["bear", "Bear researcher", "Research", "bear_history"],
      ["rm", "Research manager", "Research", "investment_plan"], ["trader", "Trader", "Trading", "trader_investment_plan"],
      ["agg", "Aggressive analyst", "Risk", "current_aggressive_response"], ["con", "Conservative analyst", "Risk", "current_conservative_response"],
      ["neu", "Neutral analyst", "Risk", "current_neutral_response"], ["pm", "Portfolio manager", "Decision", "final_trade_decision"]]) },
  vets: { name: "Veterans", caps: "VETERANS", cls: "v", engine: "value", color: "var(--vets)", line: "#C0913A", tool: "ai-hedge-fund",
    about: "5 investors, about 1 min", parallel: true,
    roster: roster([["pead", "Earnings drift", "Signal models"], ["analyst_consensus", "Analyst consensus", "Signal models"],
      ["buffett", "Buffett", "Investors"], ["munger", "Munger", "Investors"], ["graham", "Graham", "Investors"],
      ["lynch", "Lynch", "Investors"], ["druckenmiller", "Druckenmiller", "Investors"], ["research_manager", "Research manager", "Decision"]]) },
  edge: { name: "Edge Desk", caps: "EDGE DESK", cls: "e", engine: "edge", color: "var(--edge)", line: "#A272BC", tool: "Edge Desk",
    about: "scored by formula, about 10 min", parallel: false,
    roster: roster([["evidence", "Evidence package", "Evidence"], ["rating", "Factors and rating", "Rating"],
      ["lens:quality_compounder", "Quality lens", "Written analysis"], ["lens:deep_value", "Deep value lens", "Written analysis"],
      ["lens:growth_at_reasonable_price", "Growth lens", "Written analysis"], ["lens:trend_and_flow", "Trend lens", "Written analysis"],
      ["bull", "Bull case", "Written analysis"], ["bear", "Bear case", "Written analysis"], ["synthesis", "Synthesis", "Written analysis"],
      ["second_look", "Swing second look", "Research"], ["headline_read", "Headline read", "Research"],
      ["filing_research", "Filing research", "Research"]]) },
};
const ORDER = ["quant", "vets", "edge"];
const BY_ENGINE = { tape: "quant", value: "vets", edge: "edge" };
const HZ = { as_they_ran: "As they ran", swing: "Swing", long_term: "Long term" };
const HZ_ORDER = ["as_they_ran", "swing", "long_term"];   // as_they_ran: runs saved before 2026-09-24
/* How a horizon's teams came to agree. Runs before 2026-09-24 stored "High" and "Medium", which read
   as a measured confidence; nothing measured it, so they show as what happened instead. */
const agreement = c => ({ High: "Agreed before debate", Medium: "Agreed after debate" }[c] || c || "");
const ACTIVITY = { market: "Reading six months of bars and the moving averages", sentiment: "Reading what the crowd is saying", news: "Reading the week's company and macro news",
  fund: "Reading the filings and the cash flow statement", bull: "Building the case for", bear: "Building the case against",
  rm: "Weighing the bull against the bear", trader: "Turning the plan into an action and a stop", agg: "Arguing for more risk", con: "Arguing for less risk",
  neu: "Weighing both risk views", pm: "Signing off the final rating", research_manager: "Reading every thesis and the fundamentals",
  evidence: "Collecting prices, filings and estimates, point in time", rating: "Scoring eight factor families and computing levels", synthesis: "Weighing the two cases against the formula's rating" };
/* What a horizon shows when it has no rating. */
const verdictWord = h => h.rating || ({ gridlock: "Gridlock", no_agreement: "Split", different_clocks: "No call", no_view: "No view", withheld: "Withheld" }[h.status] || "No call");

/* ------------------------------------------------------------------ chrome */
function runDots(r) {
  if (r.mode === "reports") return ORDER.filter(e => r.engines.includes(e)).map(e => r.own_ratings[e] ? rc(r.own_ratings[e]) : "none");
  return HZ_ORDER.map(k => r.ratings[k] === undefined ? "none" : r.ratings[k] ? rc(r.ratings[k]) : (r.status?.[k] === "gridlock" || r.status?.[k] === "no_agreement") ? "split" : "none");
}
function sidebar(activeKey) {
  const live = S.live.filter(l => l.exit == null);
  const items = [
    ...live.map(l => ({ key: "live:" + l.id, t: l.ticker, sub: `Running · ${l.own ? "owned" : "new position"}`, live: true, href: { live: l.id } })),
    ...S.runs.map(r => ({ key: `${r.ticker}:${r.date}`, t: r.ticker,
      sub: `${fmtDate(r.date, false)} · ${r.mode === "reports" ? "reports only" : r.owns ? "owned" : "new position"}${r.plan === "chatgpt" ? " · ChatGPT" : ""}`, dots: runDots(r), href: { t: r.ticker, d: r.date } })),
  ];
  const n = ORDER.filter(e => S.engines[e]).length;
  const form = `<form class="newrun" id="newrun">
      <label for="tk">Run a symbol</label>
      <input id="tk" class="tk-in" placeholder="TICKER" autocomplete="off" spellcheck="false" maxlength="6">
      <div class="seg" role="group" aria-label="Position">
        <button type="button" data-own="0" class="${S.own ? "" : "on"}">New position</button><button type="button" data-own="1" class="${S.own ? "on" : ""}">Owned</button></div>
      <div class="seg plan-seg" role="group" aria-label="AI plan">
        ${["claude", "chatgpt"].map(p => `<button type="button" data-plan="${p}" class="${planNow() === p ? "on" : ""}" title="${esc(planModels(p))}">${PLAN_NAME[p]}</button>`).join("")}</div>
      <p class="plan-hint">AI plan: ${PLAN_NAME[planNow()]}${planModels(planNow()) ? ` · ${esc(planModels(planNow()))}` : ""}${S.plan && S.plan !== S.planInfo?.default ? " (this run only)" : ""}</p>
      <div class="pick" role="group" aria-label="Teams">${ORDER.map(e => `<label class="${TEAMS[e].cls}"><input type="checkbox" data-engine="${e}" ${S.engines[e] ? "checked" : ""}><span>${TEAMS[e].name}<small>${TEAMS[e].about}</small></span></label>`).join("")}</div>
      <label class="deb ${n < 2 ? "off" : ""}"><input type="checkbox" data-debate ${S.debate && n > 1 ? "checked" : ""} ${n < 2 ? "disabled" : ""}>
        <span>Debate the ratings<small>${n < 2 ? "Needs at least two teams" : "Compare by horizon, debate, write a memo"}</small></span></label>
      <button class="go" type="submit" ${n ? "" : "disabled"}>${n === 0 ? "Pick a team" : n === 1 ? `Run ${TEAMS[ORDER.find(e => S.engines[e])].name}` : `Run ${n} teams`}</button>
      <p class="form-err" id="formerr" role="alert"></p>
    </form>`;
  return `<aside class="side ${S.drawer ? "open" : ""}" aria-label="Runs">
    <div class="wordmark">THE DESK<small>Research only</small></div>${form}
    <a class="week-link ${activeKey === "week" ? "on" : ""}" href="?view=week" data-nav>Weekly review<small>latest call per symbol</small></a>
    <div class="runs-h">Runs</div>
    ${items.length ? `<ul class="runs">${items.map(i => `<li><a class="run ${i.key === activeKey ? "on" : ""}" href="?${new URLSearchParams(i.href)}" data-nav>
        <span class="t">${esc(i.t)}</span>
        <span class="dots">${i.live ? '<i class="dot live"></i>' : i.dots.map(d => `<i class="dot ${d}"></i>`).join("")}</span>
        <span class="d">${esc(i.sub)}</span></a></li>`).join("")}</ul>`
      : `<p class="empty-runs">No runs yet. Enter a symbol above and the teams start on it.</p>`}
  </aside>${S.drawer ? '<div class="scrim" data-close-drawer></div>' : ""}`;
}
function railHtml(active) {
  return `<nav class="rail" aria-label="Runs"><div class="wordmark">D</div>
    <a class="rail-b add" href="?" data-nav aria-label="Run a symbol">${ico.plus}</a>
    ${S.runs.slice(0, 12).map(r => `<a class="rail-b ${`${r.ticker}:${r.date}` === active ? "on" : ""}" href="?${new URLSearchParams({ t: r.ticker, d: r.date })}" data-nav>${esc(r.ticker)}</a>`).join("")}</nav>`;
}
function phoneTop() {
  return PHONE() ? `<div class="phone-top"><button class="icon-b" data-open-drawer aria-label="Runs">${ico.menu}</button>
    <div class="wordmark">THE DESK</div><button class="icon-b dark" data-open-drawer aria-label="Run a symbol">${ico.plus}</button></div>` : "";
}
function strip({ ticker, price, owns, live, stopped, cancelled, clock, chips, prog, reports, tools }) {
  return `${phoneTop()}<div class="strip"><div class="strip-in">
    <div class="who"><span class="tk">${esc(ticker)}</span>${price ? `<span class="px">${money(price)}</span>` : ""}</div>
    <div class="meta">${reports ? "" : `<span class="tag pos">${owns ? "Owned" : "New position"}</span>`}
      ${live ? `<span class="tag live">Live ${clock}</span><button class="cancel" data-cancel>Cancel run</button>` : cancelled ? `<span class="tag">Cancelled at ${clock}</span>` : stopped ? `<span class="tag">Stopped at ${clock}</span>` : `<span class="tag">${reports ? "Reports only" : "Snapshot"}</span>`}</div>
    ${prog && !PHONE() ? prog : ""}
    ${tools || ""}
    <div class="chips">${chips}</div>
    ${prog && PHONE() ? prog : ""}
  </div></div>`;
}

/* ------------------------------------------------------------------ symbol card */
// run: a saved run being shown (snapshot or PDF). Yahoo's profile is fetched now, so its
// time-sensitive fields would read as the run's own; the run's P/E is used where it was saved and
// everything else is marked as today's (audit R2-15, Stage B6).
function symbolCard(t, runPrice, runAnchors, run = null) {
  const p = S.profile[t];
  if (!p) return `<section class="sym loading" aria-label="Company"><div class="sym-logo"><span>${esc(t[0])}</span></div><div class="sym-main"><div class="sym-name">${esc(t)}</div><div class="sym-sub">Loading company details...</div></div></section>`;
  if (p.error) return `<section class="sym" aria-label="Company"><div class="sym-logo"><span>${esc(t[0])}</span></div><div class="sym-main"><div class="sym-name">${esc(t)}</div><div class="sym-sub">${esc(p.error)}</div></div></section>`;
  const price = runPrice ?? p.price;
  const lo = runAnchors?.low_52w ?? p.low_52w, hi = runAnchors?.high_52w ?? p.high_52w;
  const pos = lo != null && hi != null && price != null && hi > lo ? Math.min(100, Math.max(0, (price - lo) / (hi - lo) * 100)) : null;
  const days = p.next_earnings ? Math.round((new Date(p.next_earnings + "T12:00:00") - new Date()) / 864e5) : null;
  const div = p.dividend_yield ? `${(p.dividend_yield * 100).toFixed(2)}%` : "None";
  const today = w => run ? ["today", w].filter(Boolean).join(" · ") : w;
  const runPe = run?.valuation?.pe_now;
  const pe = !run ? ["P/E", p.pe_trailing ? p.pe_trailing.toFixed(1) + "x" : "n/a", p.pe_forward ? `${p.pe_forward.toFixed(1)}x forward` : "trailing"]
    : ["P/E", runPe ? runPe.toFixed(1) + "x" : "n/a", runPe ? "at the run's close" : "not saved with the run"];
  const stats = [
    ["Market cap", big(p.market_cap), today(p.exchange || "")],
    pe,
    ["Next earnings", p.next_earnings ? fmtDate(p.next_earnings) : "Not announced", p.next_earnings
      ? (run ? "today's calendar" : `in ${days} days`) + (p.earnings_estimated ? " · estimated" : " · confirmed") : today("")],
    ["Dividend yield", div, today(p.dividend_rate ? `${money(p.dividend_rate)} a year` : p.dividend_yield ? "" : "Pays no dividend")],
    ["Beta", p.beta != null ? p.beta.toFixed(2) : "n/a", today(p.avg_volume ? `${(p.avg_volume / 1e6).toFixed(2)}M avg volume` : "")],
  ];
  const range = pos == null ? "" : `<div class="st rng"><div class="k">52-week range</div>
      <div class="rbar"><i style="left:${pos}%"></i></div>
      <div class="rlab"><span>${money(lo)}</span><span>${money(hi)}</span></div>
      <div class="w">${((price - hi) / hi * 100).toFixed(1)}% from the high</div></div>`;
  const fetched = p.fetched_at ? new Date(p.fetched_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "";
  return `<section class="sym" aria-label="Company">
    <div class="sym-logo"><img src="/api/logo?t=${encodeURIComponent(t)}" alt="" onerror="this.remove()"><span>${esc(t[0])}</span></div>
    <div class="sym-main">
      <div class="sym-name">${esc(p.name)}</div>
      <div class="sym-sub">${[p.sector && `<b>${esc(p.sector)}</b>`, p.industry && esc(p.industry)].filter(Boolean).join('<span class="sep">/</span>')}
        ${p.hq ? `<span class="dotsep"></span>${esc(p.hq)}` : ""}${p.employees ? `<span class="dotsep"></span>${Number(p.employees).toLocaleString()} employees` : ""}
        ${p.website ? `<span class="dotsep"></span><a href="${esc(p.website)}" target="_blank" rel="noopener">${esc(p.website.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, ""))}</a>` : ""}</div>
      ${p.summary ? `<p class="sym-desc" id="symdesc">${esc(p.summary)}</p><button class="more" data-more>More</button>` : ""}
    </div>
    <div class="sym-stats">${stats.map(([k, v, w]) => `<div class="st"><div class="k">${k}</div><div class="v">${esc(v)}</div><div class="w">${esc(w)}</div></div>`).join("")}${range}</div>
    <p class="sym-src">Company data from Yahoo${fetched ? `, fetched ${fetched}` : ""}. ${run
      ? `The price, 52-week range and P/E are the run's (${esc(fmtDate(run.date))}); figures marked today are not saved with the run.`
      : `The price and 52-week range are ${runAnchors ? "the run's settled close" : "current"}.`}</p>
  </section>`;
}
async function loadProfile(t) {
  if (!t || t in S.profile) return;
  S.profile[t] = null;
  try { S.profile[t] = await api(`/api/profile?t=${encodeURIComponent(t)}`); }
  catch (e) { S.profile[t] = { error: "Company details are unavailable right now: " + e.message }; }
  render();
}

/* ------------------------------------------------------------------ snapshot: the call */
function chart(run, h) {
  const bars = run.bars || [];
  if (bars.length < 5) return `<div class="chart-empty">No price history was available for the chart.</div>`;
  const lv = Object.fromEntries((h?.memo.levels || []).map(l => [l.key, l.values]));
  const levelVals = Object.values(lv).flat().filter(v => v > 0);
  const closes = bars.map(b => b.c);
  const lo0 = Math.min(...closes, ...levelVals), hi0 = Math.max(...closes, ...levelVals), pad = (hi0 - lo0) * .06;
  const lo = lo0 - pad, hi = hi0 + pad;
  const W = 640, H = 230, pl = 4, pr = 70, pt = 8, pb = 20;
  const x = i => pl + (i / (closes.length - 1)) * (W - pl - pr);
  const y = v => pt + (1 - (v - lo) / (hi - lo)) * (H - pt - pb);
  const path = closes.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const band = (a, b, f) => `<rect x="${pl}" y="${y(Math.max(a, b))}" width="${W - pl - pr}" height="${Math.max(2, Math.abs(y(a) - y(b)))}" fill="${f}"/>`;
  const ln = (v, c) => `<line x1="${pl}" x2="${W - pr}" y1="${y(v)}" y2="${y(v)}" stroke="${c}" stroke-width="1.2" stroke-dasharray="4 4"/>`;
  const labels = [];
  const lbl = (v, c, t) => labels.push([y(v), c, t]);
  const mid = a => (a[0] + a[a.length - 1]) / 2;
  let marks = "";
  if (lv.entry_zone?.length) { marks += lv.entry_zone.length > 1 ? band(lv.entry_zone[0], lv.entry_zone[1], "rgba(27,119,71,.12)") : ln(lv.entry_zone[0], "#1B7747"); lbl(mid(lv.entry_zone), "#1B7747", "Entry"); }
  if (lv.trim?.length) { marks += lv.trim.length > 1 ? band(lv.trim[0], lv.trim[1], "rgba(192,145,58,.16)") : ln(lv.trim[0], "#C0913A"); lbl(mid(lv.trim), "#8E6112", "Trim"); }
  if (lv.first_target?.length) { if (lv.first_target.length > 1) marks += band(lv.first_target[0], lv.first_target[1], "rgba(27,119,71,.07)"); marks += ln(mid(lv.first_target), "#1B7747"); lbl(mid(lv.first_target), "#1B7747", "Target"); }
  if (lv.stop?.length) { marks += ln(lv.stop[0], "#AC3E36"); lbl(lv.stop[0], "#AC3E36", "Stop"); }
  labels.sort((a, b) => a[0] - b[0]);
  for (let i = 1; i < labels.length; i++) if (labels[i][0] - labels[i - 1][0] < 12) labels[i][0] = labels[i - 1][0] + 12;
  const n = closes.length - 1;
  const month = d => new Date(d + "T12:00:00").toLocaleDateString("en-US", { month: "short" });
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(run.ticker)} daily closes${h ? ` with the ${esc(h.label)} levels` : ""}">
    ${marks}
    <path d="${path}" fill="none" stroke="#14171C" stroke-width="1.6" stroke-linejoin="round"/>
    <circle cx="${x(n)}" cy="${y(closes[n])}" r="4" fill="#14171C"/>
    ${labels.map(([yy, c, t]) => `<text x="${W - pr + 8}" y="${yy + 3.5}" font-family="JetBrains Mono" font-size="10" fill="${c}">${t}</text>`).join("")}
    <text x="${pl}" y="${H - 3}" font-family="Archivo" font-size="10" fill="#A7ADB6">${month(bars[0].d)}</text>
    <text x="${W - pr - 22}" y="${H - 3}" font-family="Archivo" font-size="10" fill="#A7ADB6">${month(bars[n].d)}</text>
  </svg>`;
}

function valuationPanel(run) {
  const v = run.valuation || {}, oh = v.own_history || {}, peg = v.peg || {}, st = v.street || {};
  const row = (k, sm, val, cls = "") => `<div class="row"><span class="k">${k}<small>${sm}</small></span><span class="v ${cls}">${val}</span></div>`;
  const vs = x => x == null || !run.close ? "" : x > run.close ? "r-ow" : "r-uw";
  return `<div class="valu">
    ${row("Price", v.pe_now ? `${v.pe_now.toFixed(1)} times trailing earnings` : "last settled close", money(run.close))}
    ${oh.fair_mid ? row("Own-history fair value", `median P/E ${oh.median_pe} · range ${money(oh.fair_low)} to ${money(oh.fair_high)}`, money(oh.fair_mid), vs(oh.fair_mid)) : ""}
    ${peg.fair_value ? row("PEG at 1", `EPS compounding ${(peg.eps_cagr * 100).toFixed(1)}% a year${peg.capped ? ", P/E capped at 30" : ""}`, money(peg.fair_value), vs(peg.fair_value)) : ""}
    ${st.target_mean ? row("Street mean target", `${st.analysts} analysts · low ${money(st.target_low, 0)}, high ${money(st.target_high, 0)}`, money(st.target_mean), vs(st.target_mean)) : ""}
  </div>`;
}

function howItEnded(h) {
  const d = h.debate;
  if (h.status === "withheld") return "the data does not support a rating, for any team";
  if (h.status === "gridlock") return "two against one, and nobody moved";
  if (h.status === "no_agreement") return "the teams did not agree";
  if (h.status === "different_clocks") return "the teams answered for different timeframes";
  if (h.status === "single") return "only one team rates this horizon";
  if (d) return `after a debate: the ${list(d.conceded.map(t => TEAMS[t].name))} conceded`;
  return h.voters.length > 2 ? "all three teams agreed at the first reading" : "both teams agreed at the first reading";
}

function callSection(run, h) {
  const m = h.memo;
  const levels = m.levels.map(l => `<div class="${l.key}"><div class="k">${l.label}</div><div class="p">${esc(/^\d/.test(l.price) ? "$" + l.price : l.price)}</div><div class="w">${esc(l.reason)}</div></div>`).join("");
  const lists = [["Why", "var(--ow)", m.reasons], ["Risks", "var(--uw)", m.risks], ["What would change the view", "var(--ink)", m.watch]];
  const three = PHONE()
    ? lists.map(([t, c, xs], i) => `<details class="acc" ${i ? "" : "open"}><summary><b>${t}</b><span>${xs.length}</span></summary><ul>${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details>`).join("")
    : `<div class="three">${lists.map(([t, c, xs]) => `<div><h4><i style="background:${c}"></i>${t}</h4><ul>${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>`).join("")}</div>`;
  const tabs = run.horizons.length > 1 ? `<div class="tabs" role="tablist">${run.horizons.map(x => `<button role="tab" data-h="${x.key}" class="${x.key === h.key ? "on" : ""}">${x.label}<b class="r-${rc(x.rating)}">${esc(verdictWord(x))}</b></button>`).join("")}</div>` : "";
  const stands = !h.rating && h.debate ? `<div class="stands">${Object.entries(h.debate.stands).map(([t, r]) => `<span>${TEAMS[t].name}<b class="r-${rc(r)}">${esc(r)}</b></span>`).join("")}</div>` : "";
  return `<section aria-label="The call">${tabs}
    <div class="call"><span class="big r-${rc(h.rating)}">${esc(verdictWord(h).toUpperCase())}</span>
      <span class="conv">${h.rating ? `<b>${esc(agreement(h.conviction))}</b> · ` : ""}${howItEnded(h)}</span></div>
    ${stands}
    <h2 class="headline">${esc(m.headline)}</h2>
    <p class="summary">${esc(m.summary)}</p>
    ${m.manager_view ? `<p class="summary" style="margin-top:12px"><b>The desk's own view.</b> ${esc(m.manager_view)}</p>` : ""}
    <div class="action ${rc(h.rating)}"><span class="pos">${run.owns ? "Owned" : "New position"}</span><p><b>${esc(h.action)}.</b> ${esc(m.action_note)}</p></div>
    ${levels ? `<div class="levels">${levels}</div>` : ""}
    <div class="evi"><div><div class="chart">${chart(run, h)}</div><div class="chart-cap">Six months of settled closes. The levels drawn are this horizon's; switch horizon and they move.</div></div>${valuationPanel(run)}</div>
    ${m.valuation_view ? `<p class="valview"><b>Valuation.</b> ${esc(m.valuation_view)}</p>` : ""}
    ${h.note ? `<p class="hz-note">${esc(h.note)}.</p>` : ""}
    ${lists.some(([, , xs]) => xs.length) ? three : ""}</section>`;
}

function disagreement(run) {
  const debated = run.horizons.filter(h => h.debate), agreed = run.horizons.filter(h => !h.debate);
  if (!debated.length) return `<section class="sec" aria-label="Where they disagreed"><div class="eyebrow"><b>Where they disagreed</b></div>
    <p class="agreed-line">Nowhere that needed a debate${agreed.some(h => h.status === "different_clocks") ? " (where the teams answered for different timeframes, they were not compared)" : ""}.</p></section>`;
  return `<section class="sec" aria-label="Where they disagreed">
    <div class="eyebrow"><b>Where they disagreed</b><span>${debated.map(h => h.label).join(", ")}: the horizon${debated.length > 1 ? "s" : ""} that went to debate</span></div>
    ${debated.map(h => `<div class="dis">
      ${h.memo.crux ? `<p class="issue"><small>The issue · ${esc(h.label)}</small>${esc(h.memo.crux)}</p>` : `<p class="issue"><small>${esc(h.label)}</small>${esc(h.how)}</p>`}
      <div class="ex">${h.debate.turns.map((t, i) => `<div class="exrow link ${TEAMS[t.team].cls}" role="button" tabindex="0" data-panel="debate:${h.key}:${i}"><span class="rd">Round ${t.round}</span>
        <span class="tm"><i></i>${TEAMS[t.team].name}${t.decision === "concede" ? '<span class="conceded">Conceded</span>' : ""}</span>
        <span class="mv">${t.decision === "concede" ? "Adopts" : "Defends"} <b>${esc(t.rating)}</b>. ${esc(firstSentence(t.argument))}</span></div>`).join("")}</div>
      <div class="dis-foot"><span class="outcome">${h.rating ? `Agreed after debate: <b class="r-${rc(h.rating)}">${esc(h.rating)}</b>`
        : h.status === "gridlock" ? "Gridlock: two against one, and nobody moved. No rating is published" : "No agreement: the memo sets out the question instead of picking a side"}</span>
        <button class="btn" data-panel="debate:${h.key}:0">Read the debate ${ico.arrow}</button></div></div>`).join("")}
    ${agreed.length ? `<p class="agreed-line">${agreed.map(h => h.label).join(" and ")}: not debated.</p>` : ""}
  </section>`;
}

/* ------------------------------------------------------------------ snapshot: the teams */
function agentRows(run, team) {
  let stage = "";
  return `<ul class="alist">${run[team].agents.map(a => {
    const head = a.stage !== stage ? `<li class="st">${esc(stage = a.stage)}</li>` : "";
    const conf = team === "vets" && a.call ? `<span class="conf ${a.call === "bullish" || a.call === "overweight" || a.call === "buy" ? "bull" : a.call === "bearish" ? "bear" : "neu"}">${esc(cap(a.call))}${a.confidence ? `, ${Math.round(a.confidence)}% confidence` : ""}</span>` : "";
    return head + `<li><button data-panel="agent:${team}:${a.id}" class="${a.saved === false ? "unsaved" : ""}"><span class="an">${esc(a.name)}${conf}</span>
      <span class="at">${a.took != null && team !== "vets" ? mmss(a.took) : ""}</span><span class="ag">${a.saved === false ? (team === "edge" ? "Not kept: it failed its checks" : "Not saved by this run") : esc(a.gist)}</span></button></li>`;
  }).join("")}</ul>`;
}

function teamCard(run, team) {
  const t = run[team], T = TEAMS[team];
  const meta = [team === "quant" ? "12 agents" : team === "vets" ? "5 investors, 2 signal models, a manager" : "evidence, 8 factor families, written analysis",
    T.tool !== T.name ? T.tool : null, t.seconds ? dur(t.seconds) : null, t.reused ? "reused report" : null].filter(Boolean).join(" · ");
  const votes = t.votes ? ` · ${t.votes.bullish} bullish, ${t.votes.bearish} bearish, ${t.votes.neutral} neutral` : "";
  return `<article class="team ${T.cls}"><div class="team-h"><div class="n">${T.caps}</div><div class="m">${meta}</div>
    <div class="tr r-${rc(t.rating)}">${esc((t.rating || "Withheld").toUpperCase())}</div><div class="tf">${esc(t.timeframe || "")}${votes}</div>
    <p class="case"><b>${team === "quant" ? "The decision" : team === "vets" ? "Main case" : "How it was scored"}</b>${esc(t.case)}</p></div>
    ${agentRows(run, team)}<a class="team-doc" href="?${new URLSearchParams({ ...runQ(run), doc: team })}" data-nav>Read the ${T.name}'s full report ${ico.arrow}${fixesFor(run, team).length ? `<small>${fixesFor(run, team).length} statement${fixesFor(run, team).length > 1 ? "s" : ""} marked after the debate</small>` : ""}</a><button class="phone-more" data-panel="agent:${team}:${t.agents[0]?.id}">See all ${t.agents.length} ${team === "vets" ? "members" : team === "quant" ? "agents" : "steps"} ${ico.arrow}</button></article>`;
}

function mergeSvg(teams, pending) {
  if (teams.length < 2) return "";
  const xs = teams.map((_, i) => (i + .5) / teams.length * 100);
  return `<div class="merge" aria-hidden="true"><svg viewBox="0 0 100 62" preserveAspectRatio="none">${teams.map((t, i) =>
    `<path d="M${xs[i]} 0 C ${xs[i]} 34, 50 26, 50 62" fill="none" stroke="${pending ? "#C7CCD4" : TEAMS[t].line}" stroke-width="2" vector-effect="non-scaling-stroke" ${pending ? 'stroke-dasharray="4 4"' : ""}/>`).join("")}</svg></div>`;
}

function horizonRows(teams, rows) {
  /* rows: [{label, out:html, pending, cells:{team: {rating, tf, out?} | null | "none" | "withheld"}}]
     null means still working, so only the live view should pass it. A finished run passes "withheld"
     for a team that ran but refused to rate (Edge Desk on stale data): it is not waiting. */
  const cell = (t, c) => c === "none" ? `<div class="cell ${TEAMS[t].cls} out"><div class="who">${TEAMS[t].name}</div><div class="tf">does not rate this horizon</div></div>`
    : c === "withheld" ? `<div class="cell ${TEAMS[t].cls} out"><div class="who">${TEAMS[t].name}</div><div class="tf">rating withheld, so no vote</div></div>`
    : c === "noview" ? `<div class="cell ${TEAMS[t].cls} out"><div class="who">${TEAMS[t].name}</div><div class="tf">no view on this horizon, so no vote</div></div>`
    : `<div class="cell ${TEAMS[t].cls} ${c?.out ? "out" : ""}"><div class="who">${TEAMS[t].name}</div>${c ? `<div class="rt r-${rc(c.rating)}">${esc(c.rating)}</div><div class="tf">${esc(c.tf || "")}</div>` : '<div class="tf">waiting</div>'}</div>`;
  return `<div class="hz" style="--n:${teams.length}">${rows.map(r => `<div class="hzrow ${r.pending ? "pending" : ""}">
    <div class="lbl">${esc(r.label)}<span>${r.out}</span></div>${teams.map(t => cell(t, r.cells[t])).join("")}</div>`).join("")}</div>`;
}

function teamsSection(run) {
  const teams = run.engines;
  const rows = run.horizons.map(h => ({
    label: h.label,
    out: `${h.debate ? "Debated" : h.status === "different_clocks" ? "Different clocks" : h.status === "single" ? "One view" : "Agreed"} · <b class="r-${rc(h.rating)}">${esc(verdictWord(h))}</b>`,
    cells: Object.fromEntries(teams.map(t => [t, h.restated[t] && !h.restated[t].rating ? "noview" : h.restated[t] ? { rating: h.restated[t].rating, tf: h.key === "as_they_ran" ? h.restated[t].timeframe : h.restated[t].own ? "its own rating" : h.sub, out: !h.voters.includes(t) }
      : (t === "edge" && h.key === "swing") ? "none" : run[t] && !run[t].rating ? "withheld" : null])),
  }));
  return `<section class="sec" aria-label="How the teams got there">
    <div class="eyebrow"><b>${run.mode === "reports" ? (teams.length > 1 ? "The teams" : "The team") : "How the teams got there"}</b><span>${run.mode === "reports" ? "Open any step to read what it wrote"
      : "Each team rated the stock alone, then the ratings were compared horizon by horizon"}</span></div>
    <div class="teams" style="--n:${teams.length}">${teams.map(t => teamCard(run, t)).join("")}</div>
    ${rows.length ? mergeSvg(teams, false) + horizonRows(teams, rows) : ""}</section>`;
}

function sideBySide(run) {
  const teams = run.engines;
  return `<section aria-label="Ratings side by side"><div class="eyebrow"><b>${teams.length > 1 ? "Ratings side by side" : "The rating"}</b><span>${teams.length > 1 ? "No debate was asked for, so each rating stands on its own and there is no combined call" : "One team, so there is nothing to compare it with"}</span></div>
    <div class="side-by" style="--n:${teams.length}">${teams.map(t => { const x = run[t], sig = x.swing;
      return `<div class="sb ${TEAMS[t].cls}"><div class="n">${TEAMS[t].caps}</div><div class="r r-${rc(x.rating)}">${esc(x.rating || "Withheld")}</div>
        <div class="tf">${esc(x.timeframe || "its own timeframe")}</div><p>${esc(x.case)}</p>${sig?.signal ? `<span class="sig">SWING SETUP ${esc(sig.signal)}</span>` : ""}</div>`; }).join("")}</div>
    ${run.bars?.length > 5 ? `<div class="evi" style="margin-top:16px"><div><div class="chart">${chart(run, null)}</div><div class="chart-cap">Six months of settled closes.</div></div>${valuationPanel(run)}</div>` : ""}
  </section>`;
}

function reportDocs(run) {
  const qw = run.quant ? Object.values(run.quant.reports).reduce((n, s) => n + words(s), 0) : 0;
  return [
    run.mode !== "reports" && ["memo", "Combined memo", "var(--ink)", `The desk's conclusion for each horizon, with valuation and levels · ${words(run.memo_md).toLocaleString()} words`],
    run.quant && ["quant", "Quant desk, as TradingAgents wrote it", "var(--quant)", `Analysts to portfolio manager, in its five stages · ${qw.toLocaleString()} words`],
    run.vets && ["vets", "Veterans, as ai-hedge-fund wrote it", "var(--vets)", "Desk vote, 5 investors, 2 signal models, research manager"],
    run.edge && ["edge", "Edge Desk, its full report", "var(--edge)", `Ratings and how they were reached, levels, factors, written analysis, evidence · ${words(run.edge.report_md).toLocaleString()} words`],
  ].filter(Boolean);
}
function reportsSection(run) {
  const link = doc => "?" + new URLSearchParams({ ...runQ(run), doc });
  return `<section class="sec" aria-label="Full reports">
    <div class="eyebrow"><b>Full reports</b><span>${run.mode === "reports" ? "Each team's report as its own tool wrote it" : "The memo that combines them, and each team's report as its own tool wrote it"}</span></div>
    <div class="docs">${reportDocs(run).map(([k, n, c, m]) => `<a class="doc" href="${link(k)}" data-nav><i style="background:${c}"></i><span><span class="n">${n}</span><span class="m">${m}</span></span><span class="go2">Read</span></a>`).join("")}</div>
    <p class="runfoot">Run ${fmtDate(run.date)}${run.generated_at ? `, finished ${new Date(run.generated_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}` : ""}${run.billing === "max" ? ` · Claude plan, nothing billed${run.notional ? ` (API equivalent $${run.notional.toFixed(2)})` : ""}` : run.billing === "chatgpt" ? " · ChatGPT plan, nothing billed" : run.costs != null ? ` · API cost $${run.costs.toFixed(2)}` : ""}${run.seconds ? ` · ${dur(run.seconds)}` : ""}</p>
  </section>`;
}

/* ------------------------------------------------------------------ first screen (2026-10-05)
   Claude's layout from the side-by-side prototype: both horizons as peers, the action for your
   position on the first screen, every warning computed from the saved run, voters as chips, Edge's
   steps counted apart, earlier runs of the symbol beside the chart, disclaimers once in the footer. */
const RATING_ORDER = ["Sell", "Underweight", "Hold", "Overweight", "Buy"];
const FLAG_WORD = { high: "Evidence", warn: "Check", info: "Note" };
function runFlags(run) {
  const out = [...(run.flags || [])];
  const p = S.profile[run.ticker], ne = p && !p.error ? p.next_earnings : null;
  if (ne && run.horizons.some(h => h.key === "swing")) {
    const days = (new Date(ne + "T12:00:00") - new Date(run.date + "T12:00:00")) / 864e5;
    if (days >= 0 && days <= 56) out.push({ kind: "earnings", level: "info",
      text: `Next earnings ${fmtDate(ne)} fall inside the swing window (today's calendar, not saved with the run).` });
  }
  return out;
}
function flagList(flags) {
  return `<div class="fs-flags">${flags.length ? flags.map(f => `<div class="fs-flag ${f.level}"><b>${FLAG_WORD[f.level]}</b>${esc(f.text)}</div>`).join("")
    : `<div class="fs-flag clear"><b>Clear</b>No evidence warnings on this run.</div>`}</div>`;
}
// A team's vote as it started, and where the debate left it when it moved: "Hold → Underweight".
const moved = (h, t) => { const a = h.restated[t]?.rating, b = h.debate?.stands?.[t]; return esc(a || "") + (b && a && b !== a ? ` &rarr; ${esc(b)}` : ""); };
function voterChips(run, h) {
  return run.engines.map(t => {
    const r = h.restated[t];
    if (h.voters.includes(t)) return `<span class="fs-chip ${TEAMS[t].cls}">${TEAMS[t].name} ${moved(h, t)}</span>`;
    const why = t === "edge" && h.key === "swing" ? "does not vote on swing" : r && !r.rating ? "no view" : run[t] && !run[t].rating ? "withheld" : h.status === "withheld" ? "not asked" : "no vote";
    return `<span class="fs-chip ${why === "does not vote on swing" ? "na" : "off"}">${TEAMS[t].name}: ${why}</span>`;
  }).join("");
}
function horizonCard(run, h) {
  const eligible = run.engines.filter(t => !(t === "edge" && h.key === "swing")).length;
  const lv = h.memo.levels.map(l => `<tr title="${esc(l.reason)}"><td>${l.label}</td><td>${esc(/^\d/.test(l.price) ? "$" + l.price : l.price)}</td></tr>`).join("");
  const story = h.status === "withheld" ? esc(h.how)
    : h.debate ? `Debated: ${h.debate.turns.length} turn${h.debate.turns.length > 1 ? "s" : ""}, ${h.rating ? "agreement reached" : "no agreement"}.`
    : h.voters.length > 1 ? "No debate: every voter started on the same side." : h.voters.length === 1 ? "One voter, nothing to compare." : "No voters.";
  return `<div class="fs-card ${h.key === Q().h ? "on" : ""}"><div class="fs-lab">${esc(h.label)} <span>${esc(h.sub)}</span></div>
    <div class="fs-big r-${rc(h.rating)}">${esc(verdictWord(h))}</div>
    <div class="fs-agree">${h.rating ? esc(agreement(h.conviction)) + " · " : ""}${h.voters.length} of ${eligible} eligible team${eligible === 1 ? "" : "s"} voted</div>
    <div class="fs-voters">${voterChips(run, h)}</div>
    <div class="fs-act">${esc(h.action)}</div>
    ${h.memo.action_note ? `<p class="fs-actnote">${esc(h.memo.action_note)}</p>` : ""}
    ${lv ? `<table class="fs-lv">${lv}</table>` : ""}
    <div class="fs-why">${story}</div></div>`;
}
function edgeCoverage(e) {
  const n = st => e.agents.filter(a => a.state === st).length;
  const bits = [`${n("ok")} ok`];
  if (n("failed")) bits.push(`<b class="bad">${n("failed")} failed</b>`);
  if (n("unavailable")) bits.push(`<b class="meh">${n("unavailable")} unavailable</b>`);
  if (n("skipped")) bits.push(`${n("skipped")} skipped`);
  const title = e.agents.filter(a => a.state !== "ok").map(a => `${a.name}: ${a.state}${a.error ? " (" + a.error + ")" : ""}`).join("; ");
  return `<span class="fs-cov" title="${esc(title)}">${bits.join(" · ")}</span>`;
}
function teamMatrix(run) {
  const sw = run.horizons.find(h => h.key === "swing"), lt = run.horizons.find(h => h.key === "long_term");
  const vote = (h, t) => {
    if (!h) return '<span class="fs-cov">n/a</span>';
    const r = h.restated[t];
    if (!r) return `<span class="fs-cov">${t === "edge" && h.key === "swing" ? "does not vote" : h.status === "withheld" ? "withheld" : "no vote"}</span>`;
    if (!r.rating) return '<span class="fs-cov">no view</span>';
    const end = h.debate?.stands?.[t];
    return `<b class="r-${rc(r.rating)}">${esc(r.rating)}</b>${end && end !== r.rating ? ` &rarr; <b class="r-${rc(end)}">${esc(end)}</b> <span class="fs-cov">after debate</span>` : r.own ? "" : ' <span class="fs-cov">restated</span>'}`;
  };
  const rows = run.engines.map(t => {
    const T = run[t];
    const extra = t === "edge" ? `<br>${edgeCoverage(T)}${T.basis && T.rating ? `<br><span class="fs-cov">long term from its ${esc(T.basis)}${T.expected_return != null ? `, expected ${(T.expected_return * 100).toFixed(1)}% a year` : ""}</span>` : ""}`
      : t === "vets" && T.caveats?.length ? `<br><span class="fs-cov" title="${esc(T.caveats.join(" "))}">${T.caveats.length} data caveat${T.caveats.length > 1 ? "s" : ""}</span>` : "";
    return `<tr><td><span class="fs-chip ${TEAMS[t].cls}">${TEAMS[t].name}</span>${extra}</td><td><b class="r-${rc(T.rating)}">${esc(T.rating || "Withheld")}</b><br><span class="fs-cov">${esc(T.timeframe || "clock not stated")}</span></td><td>${vote(sw, t)}</td><td>${vote(lt, t)}</td></tr>`;
  }).join("");
  return `<table class="fs-mx"><thead><tr><th>Team</th><th>Own call</th><th>Swing vote</th><th>Long-term vote</th></tr></thead><tbody>${rows}</tbody></table>`;
}
function historyTable(run) {
  const hs = run.history || [];
  if (!hs.length) return `<p class="fs-cov">No other run of ${esc(run.ticker)} on file.</p>`;
  const word = (x, k) => x.ratings[k] || (x.status?.[k] ? verdictWord({ status: x.status[k] }) : x.mode === "reports" ? "reports only" : "");
  return `<table class="fs-hist"><thead><tr><th>Run</th><th>Long term</th><th>Swing</th><th class="num">Close then</th><th class="num">Since</th></tr></thead><tbody>${hs.map(x => {
    const chg = x.close && run.close ? (run.close / x.close - 1) * 100 : null;
    const href = { t: run.ticker, d: x.date, ...(x.run_id && !x.latest ? { r: x.run_id } : {}) };
    const when = fmtDate(x.date) + (x.time && x.date === run.date ? ` ${x.time}` : "") + (x.date === run.date && x.latest ? " (latest)" : "");
    return `<tr><td><a href="?${new URLSearchParams(href)}" data-nav>${esc(when)}</a></td><td class="r-${rc(x.ratings.long_term)}">${esc(word(x, "long_term"))}</td><td class="r-${rc(x.ratings.swing)}">${esc(word(x, "swing"))}</td><td class="num">${money(x.close)}</td><td class="num">${chg == null ? "" : (chg >= 0 ? "+" : "") + chg.toFixed(1) + "%"}</td></tr>`;
  }).join("")}</tbody></table>`;
}
function valuationLine(run) {
  const v = run.valuation || {}, oh = v.own_history || {}, peg = v.peg || {}, st = v.street || {};
  const parts = [oh.fair_mid && `Own-history fair value <span class="mono">${money(oh.fair_low)} / ${money(oh.fair_mid)} / ${money(oh.fair_high)}</span>`,
    peg.fair_value && `PEG = 1 <span class="mono">${money(peg.fair_value)}</span>`,
    st.target_mean && `Street <span class="mono">${money(st.target_low, 0)} / ${money(st.target_mean, 0)} / ${money(st.target_high, 0)}</span>`,
    v.pe_now && `P/E then <span class="mono">${v.pe_now.toFixed(1)}x</span>`].filter(Boolean);
  return parts.length ? `<p class="fs-val">${parts.join(" · ")}. Three different methods, not three votes.</p>` : "";
}
function firstScreen(run, key) {
  const name = S.profile[run.ticker]?.name || "";
  const e = run.edge, edgeOff = e && e.price_day && run.price_day && e.price_day !== run.price_day;
  // "As they ran" was retired on 2026-09-24; older runs still carry it, and each team's own call is
  // in the teams table, so the first screen shows only the two horizons the desk rates now.
  const hzs = run.horizons.filter(x => x.key !== "as_they_ran");
  const h = hzs.find(x => x.key === key) || hzs.find(x => x.key === "long_term") || hzs[0];
  const tabs = hzs.filter(x => x.memo.reasons.length).map(x => `<button data-h="${x.key}" aria-pressed="${x.key === h.key}">${esc(x.label)}</button>`).join("");
  const swing = run.horizons.find(x => x.key === "swing");
  return `<section class="fs" aria-label="The call">
    <div class="fs-head"><div><div class="fs-tick">${esc(run.ticker)}</div><div class="fs-co">${esc(name)}</div>
      <div class="fs-pills"><span class="fs-pill ${run.owns ? "own" : ""}">${run.owns ? "Owned" : "New position"}</span>${run.engines.map(t => `<span class="fs-chip ${TEAMS[t].cls}">${TEAMS[t].name}</span>`).join("")}</div></div>
      <div class="fs-meta"><div class="fs-px">${money(run.close)} <span>settled close ${fmtDate(run.price_day || run.date)}</span></div>
        ${edgeOff ? `<div class="fs-warnline">Edge Desk priced from ${fmtDate(e.price_day)} at ${money(e.close)}</div>` : ""}
        <div>Run ${fmtDate(run.date)}${run.generated_at ? ", " + new Date(run.generated_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : ""}</div>
        ${run.plan ? `<div class="fs-plan">${PLAN_NAME[run.plan.name] || run.plan.name} plan${run.plan.models?.length ? ` · ${esc(run.plan.models.filter(Boolean).join(", "))}` : ""}</div>` : ""}</div></div>
    ${flagList(runFlags(run))}
    <div class="fs-hz">${run.horizons.filter(x => x.key !== "as_they_ran").map(x => horizonCard(run, x)).join("")}</div>
    ${h && h.memo.reasons.length ? `<h4 class="fs-h">What carried it <span class="fs-tabs">${tabs}</span></h4>
      <p class="fs-headline">${esc(h.memo.headline)}</p>
      <div class="fs-two"><div><h4 class="fs-h">Why</h4><ul>${h.memo.reasons.slice(0, 4).map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>
        <div><h4 class="fs-h">Risks</h4><ul>${h.memo.risks.slice(0, 4).map(x => `<li>${esc(x)}</li>`).join("")}</ul></div></div>
      ${h.memo.watch.length ? `<p class="fs-val"><b>Review when:</b> ${esc(h.memo.watch[0])}</p>` : ""}` : ""}
    <h4 class="fs-h">The teams: own call, then the vote</h4>${teamMatrix(run)}
    <div class="fs-two fs-gap"><div><h4 class="fs-h">Other runs of ${esc(run.ticker)}</h4>${historyTable(run)}</div>
      <div><h4 class="fs-h">Six months${swing ? ", with the swing levels" : ""}</h4><div class="chart">${chart(run, swing || run.horizons[0])}</div></div></div>
    <h4 class="fs-h">Valuation references</h4>${valuationLine(run)}
  </section>`;
}
function weekPage() {
  const seen = new Set();
  const latest = S.runs.filter(r => r.mode !== "reports" && Object.keys(r.ratings || {}).length).filter(r => seen.has(r.ticker) ? false : seen.add(r.ticker));
  const rank = r => (r.flags || []).some(f => f.level === "high") ? 0 : (r.flags || []).some(f => f.level === "warn") ? 1 : 2;
  const NAMES = { withheld_lt: "Long term withheld", stale: "Stale fundamentals", prices: "Mixed price dates", edge_withheld: "Edge withheld",
    partial: "Partial Edge analysis", unavailable: "Edge source unavailable", figures: "Unverified debate figures" };
  const rows = latest.sort((a, b) => rank(a) - rank(b)).map(r => {
    const prev = S.runs.find(x => x.ticker === r.ticker && x.date < r.date && Object.keys(x.ratings || {}).length);
    const word = k => r.ratings[k] || (r.status?.[k] ? verdictWord({ status: r.status[k] }) : "n/a");
    const move = k => {
      if (!prev) return '<span class="fs-chg">first run</span>';
      const now = r.ratings[k], then = prev.ratings[k];
      if (!now || !then) return "";
      const d = RATING_ORDER.indexOf(now) - RATING_ORDER.indexOf(then);
      return d > 0 ? `<span class="fs-chg up" title="was ${esc(then)}">&#9650;</span>` : d < 0 ? `<span class="fs-chg down" title="was ${esc(then)}">&#9660;</span>` : '<span class="fs-chg">same</span>';
    };
    const fl = (r.flags || []).map(f => `<div><span class="fs-dot ${f.level}"></span>${esc(NAMES[f.kind] || f.kind)}</div>`).join("") || '<span class="fs-cov">none</span>';
    return `<tr class="fs-row" data-go="${esc(r.ticker)}|${esc(r.date)}"><td><b>${esc(r.ticker)}</b><br><span class="fs-cov">${fmtDate(r.date)}</span></td>
      <td><b class="r-${rc(r.ratings.long_term)}">${esc(word("long_term"))}</b> ${move("long_term")}</td>
      <td><b class="r-${rc(r.ratings.swing)}">${esc(word("swing"))}</b> ${move("swing")}</td><td>${fl}</td><td class="num">${money(r.close)}</td></tr>`;
  }).join("");
  return `<section class="fs"><div class="fs-tick" style="font-size:24px">Weekly review</div>
    <p class="fs-co" style="margin:4px 0 14px">The latest run of each symbol, evidence problems first. Arrows compare with that symbol's previous run. Click a row to open it.</p>
    ${rows ? `<table class="fs-wk"><thead><tr><th>Symbol</th><th>Long term</th><th>Swing</th><th>Evidence</th><th class="num">Close</th></tr></thead><tbody>${rows}</tbody></table>` : '<p class="fs-cov">No debated runs yet.</p>'}
  </section>`;
}

function snapshotPage(run, key) {
  const head = symbolCard(run.ticker, run.close, run.anchors, run);
  if (run.mode === "reports" || !run.horizons.length) return head + sideBySide(run) + teamsSection(run) + reportsSection(run);
  // The first screen leads; the debate, the teams and the reports follow, then the company card.
  return firstScreen(run, key) + disagreement(run) + teamsSection(run) + reportsSection(run)
    + `<section class="sec" aria-label="Company"><div class="eyebrow"><b>The company</b></div>${head}</section>`;
}

/* ------------------------------------------------------------------ panels */
function panel(run, spec) {
  if (!spec) return "";
  const [kind, a, ...rest] = spec.split(":"), b = rest.join(":");
  const close = `<button class="p-btn" data-close-panel aria-label="Close">${ico.x}</button>`;
  if (kind === "debate") {
    const h = run.horizons.find(x => x.key === a);
    if (!h || !h.debate) return "";
    return `<aside class="panel" aria-label="The debate"><div class="p-top"><div class="crumb"><i style="background:var(--ink)"></i>${esc(run.ticker)} · ${fmtDate(run.date, false)} · ${esc(h.label)}</div>
      <div class="p-row"><div><div class="p-title">The debate</div><div class="p-meta">${h.debate.turns.length} turns over ${Math.max(...h.debate.turns.map(t => t.round))} rounds · ${h.debate.conceded.length ? `the ${list(h.debate.conceded.map(t => TEAMS[t].name))} conceded` : "nobody conceded"}</div></div>
        <div class="p-btns">${close}</div></div></div>
      <div class="p-body">
        ${h.memo.crux ? `<p class="note lead"><b>The issue</b>${esc(h.memo.crux)}</p>` : ""}
        ${h.debate.turns.map((t, i) => `<div class="dturn ${TEAMS[t.team].cls}" id="turn${i}"><div class="dh"><span class="tn">${TEAMS[t.team].name}${t.decision === "concede" ? '<span class="conceded">Conceded</span>' : ""}</span><span class="rd">Round ${t.round}</span></div>
          <div class="dmv">${t.decision === "concede" ? "Adopts" : "Defends"} <b>${esc(t.rating)}</b></div><p>${esc(t.argument)}</p>
          ${t.changed ? `<p class="note"><b>What changed its mind</b>${esc(t.changed)}</p>` : ""}
          ${t.holes.length ? `<div class="note"><b>${t.decision === "concede" ? "What it still doubts" : "Holes it found in the other reports"}</b><ul>${t.holes.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
          ${t.evidence.length ? `<div class="note"><b>Evidence the others lacked</b><ul>${t.evidence.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
          ${t.note ? `<p class="note"><b>Moderator</b>${esc(t.note)}</p>` : ""}</div>`).join("")}
        <p class="dout">${h.rating ? `Agreed after debate: <b>${esc(h.rating)}</b>.` : h.status === "gridlock" ? "Gridlock: two against one, and nobody moved." : "No agreement."} A concession adopts another team's rating exactly; nobody splits the difference, and nobody has to concede.</p>
      </div></aside>`;
  }
  if (kind === "agent" && run[a]) {
    const team = a, T = TEAMS[team], agents = run[team].agents, i = Math.max(0, agents.findIndex(x => x.id === b)), ag = agents[i];
    const prev = agents[i - 1], next = agents[i + 1];
    let body;
    if (team === "quant") {
      const text = run.quant.reports[ag.id];
      body = text ? `<h5>In one line</h5><p class="takeaway">${esc(ag.gist)}</p><h5>What it wrote</h5><div class="prose small">${md(mark(run, "quant", text, true))}</div>`
        : `<div class="missing"><b>This run did not save the ${esc(ag.name.toLowerCase())}'s own text.</b>Its arguments survive in the ${ag.stage === "Research" ? "research manager's" : "portfolio manager's"} decision. Runs from now on keep it.</div>`;
    } else if (team === "edge") {
      body = ag.saved === false ? `<div class="missing"><b>This section was not kept.</b>${esc(ag.error || "It failed Edge Desk's checks twice (length, or a figure that is not in the evidence).")}</div>`
        : `<h5>In one line</h5><p class="takeaway">${esc(ag.gist)}</p>${ag.text ? `<h5>What it wrote</h5><p class="ptext">${mark(run, "edge", ag.text, false)}</p>` : `<p class="fine">This step is computed, not written. The numbers are in Edge Desk's full report.</p>`}`;
    } else if (ag.id === "research_manager") {
      const v = run.vets.verdict;
      body = `<h5>Summary</h5><p class="takeaway">${esc(v.summary)}</p><h5>Thesis</h5><p class="ptext">${esc(v.thesis)}</p>
        ${[["Bull case", v.bull_case], ["Bear case", v.bear_case], ["What would change the view", v.what_would_change]].map(([t, xs]) => xs?.length ? `<h5>${t}</h5><ul class="kp">${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "").join("")}`;
    } else {
      body = `<h5>Call</h5><p class="takeaway">${esc(cap(ag.call))}${ag.confidence ? `, ${Math.round(ag.confidence)}% confidence` : ""}${ag.value != null ? ` · signal ${ag.value >= 0 ? "+" : ""}${ag.value.toFixed(2)}` : ""}</p>
        <h5>Reasoning</h5><p class="ptext">${mark(run, "vets", ag.text || "No view: nothing in the window to act on.", false)}</p>`;
    }
    const doc = "?" + new URLSearchParams({ ...runQ(run), doc: team }) + (team === "quant" ? "#" + ag.id : "");
    return `<aside class="panel" aria-label="${esc(ag.name)}"><div class="p-top"><div class="crumb"><i style="background:${T.color}"></i>${esc(run.ticker)} · ${T.name} · ${esc(ag.stage)}</div>
      <div class="p-row"><div><div class="p-title">${esc(ag.name)}</div><div class="p-meta">${ag.at != null ? `Finished at ${mmss(ag.at)}` : "Finished"}${ag.took != null && team !== "vets" ? ` · took ${mmss(ag.took)}` : ""}${ag.words ? ` · ${ag.words.toLocaleString()} words` : ""}</div></div>
        <div class="p-btns"><button class="p-btn" ${prev ? `data-panel="agent:${team}:${prev.id}"` : "disabled"} aria-label="Previous">${ico.up}</button><button class="p-btn" ${next ? `data-panel="agent:${team}:${next.id}"` : "disabled"} aria-label="Next">${ico.dn}</button>${close}</div></div></div>
      <div class="p-body">${body}
        <div class="fullrep"><div><b>Read the whole ${T.name} report</b><span>In the reader, with contents</span></div><a class="btn" href="${doc}" data-nav>Open ${ico.arrow}</a></div></div>
      <div class="p-foot"><span>${prev ? `<kbd>&uarr;</kbd>${esc(prev.name)}` : ""}</span><span>${next ? `${esc(next.name)}<kbd>&darr;</kbd>` : ""}</span></div></aside>`;
  }
  return "";
}

/* ------------------------------------------------------------------ reader */
const QDOC = [["I. Analyst team", ["market", "sentiment", "news", "fund"]], ["II. Research team", ["bull", "bear", "rm"]],
              ["III. Trading team", ["trader"]], ["IV. Risk management", ["agg", "con", "neu"]], ["V. Portfolio management", ["pm"]]];

function sectionsOf(markdown, kind, stage, run, team) {
  const parts = String(markdown || "").split(/^## /m); parts.shift();
  const secs = parts.map((p, i) => { const nl = p.indexOf("\n"); return { id: "s" + i, t: p.slice(0, nl).trim(), b: p.slice(nl + 1) }; });
  return { toc: secs.map(s => `<a href="#${s.id}" data-sec="${s.id}">${esc(s.t.replace(/:.*/, ""))}<small>${readMin(words(s.b))}m</small></a>`).join(""),
    secs: secs.map(s => `<section class="rsec ${kind}" id="${s.id}"><div class="rsec-h"><span class="stage">${stage}</span><h3>${esc(s.t)}</h3><span class="wc">${words(s.b).toLocaleString()} words</span></div><div class="prose">${md(team ? mark(run, team, s.b, true) : s.b)}</div></section>`).join("") };
}

function docBody(run, doc) {
  let d;
  if (doc === "quant") {
    const qa = Object.fromEntries(run.quant.agents.map(a => [a.id, a]));
    const qw = Object.values(run.quant.reports).reduce((n, s) => n + words(s), 0);
    d = { kind: "quant", brief: true,
      toc: QDOC.map(([g, ids]) => `<div class="grp">${g}</div>` + ids.map(id => { const a = qa[id], w = words(run.quant.reports[id]);
        return `<a href="#${id}" data-sec="${id}" class="${w ? "" : "miss"}">${esc(a.name)}<small>${w ? readMin(w) + "m" : "not saved"}</small></a>`; }).join("")).join(""),
      secs: QDOC.map(([g, ids]) => ids.map((id, i) => { const a = qa[id], text = run.quant.reports[id], w = words(text);
        return `<section class="rsec q" id="${id}"><div class="rsec-h"><span class="stage">${g.split(". ")[0]}.${i + 1} · ${g.split(". ")[1]}</span><h3>${esc(a.name)}</h3><span class="wc">${w ? `${w.toLocaleString()} words · ${readMin(w)} min` : "not saved"}</span></div>
          ${w ? `<p class="gist">${esc(a.gist)}</p><div class="prose">${md(mark(run, "quant", text, true))}</div>` : `<div class="missing"><b>This run did not save the ${esc(a.name.toLowerCase())}'s own text.</b>TradingAgents writes it, but this run only kept the stage reports. Its arguments survive in the ${id === "bull" || id === "bear" ? "research manager's decision (II.3)" : "portfolio manager's decision (V.1)"}. Runs from now on keep it.</div>`}</section>`; }).join("")).join(""),
      head: `<div class="rd-kick" style="color:var(--quant)"><i style="background:var(--quant)"></i>Quant desk<span>TradingAgents · standalone report</span></div>
        <h1 class="rd-title">${esc(run.ticker)}: <span class="r-${rc(run.quant.rating)}">${esc((run.quant.rating || "").toUpperCase())}</span></h1>
        <p class="rd-dek">Twelve agents in five stages: four analysts report, a bull and a bear argue, a manager decides, a trader plans, three risk analysts push back, and the portfolio manager signs off.</p>
        <div class="rd-facts"><span><b>12</b> agents</span><span><b>5</b> stages</span>${run.quant.seconds ? `<span><b class="mono">${dur(run.quant.seconds)}</b></span>` : ""}<span><b class="mono">${qw.toLocaleString()}</b> words · ${readMin(qw)} min read</span><span class="mono">${esc(run.date)}${run.close ? " · close " + money(run.close) : ""}</span></div>
        <a class="jump" href="#pm">Start with the decision ${ico.arrow}</a>` };
  } else if (doc === "vets") {
    const v = run.vets, vd = v.verdict, votes = v.votes || {}, tot = (votes.bullish || 0) + (votes.bearish || 0) + (votes.neutral || 0) + (votes.no_view || 0) || 1;
    const card = a => `<div class="persona"><div class="ph"><span class="pn">${esc(a.name)}</span><span class="pc ${a.call === "no view" ? "none" : esc(a.call)}">${esc(cap(a.call))}</span>
        <span class="pv">${a.confidence ? Math.round(a.confidence) + "% confidence · " : ""}signal ${a.value >= 0 ? "+" : ""}${(a.value ?? 0).toFixed(2)}</span></div><p>${mark(run, "vets", a.text || "No view: nothing in the window to act on.", false)}</p></div>`;
    const ul = (h, c, xs) => xs?.length ? `<h4><i style="background:${c}"></i>${h}</h4><ul class="prose">${xs.map(x => `<li>${mark(run, "vets", x, false)}</li>`).join("")}</ul>` : "";
    const an = run.anchors || {}, val = run.valuation || {};
    const kv = [["20-day average", money(an.sma_20)], ["50-day average", money(an.sma_50)], ["200-day average", money(an.sma_200)],
      ["20-day range", `${money(an.low_20d)} to ${money(an.high_20d)}`], ["52-week range", `${money(an.low_52w)} to ${money(an.high_52w)}`],
      ["Average true range, 14 days", money(an.atr_14)], ["P/E now", val.pe_now ? val.pe_now.toFixed(1) + "x" : "n/a"],
      ["Own-history fair value", val.own_history ? `${money(val.own_history.fair_low)} / ${money(val.own_history.fair_mid)} / ${money(val.own_history.fair_high)}` : "n/a"],
      ["PEG at 1", money(val.peg?.fair_value)], ["Street targets", val.street ? `${money(val.street.target_low)} / ${money(val.street.target_mean)} / ${money(val.street.target_high)}` : "n/a"]];
    const SECS = [
      ["vote", "Desk vote", `<p class="gist">${votes.bullish} bullish, ${votes.bearish} bearish, ${votes.neutral} neutral, ${votes.no_view} with no view. Desk score ${v.score >= 0 ? "+" : ""}${(v.score ?? 0).toFixed(2)}, which bands to ${esc(v.desk_rating)}.</p>
        <div class="vote"><div class="vbar"><i style="width:${votes.bullish / tot * 100}%;background:var(--ow)"></i><i style="width:${votes.neutral / tot * 100}%;background:#AEB4BD"></i><i style="width:${votes.bearish / tot * 100}%;background:var(--uw)"></i></div>
        <div class="vleg"><span><b>${votes.bullish}</b>bullish</span><span><b>${votes.neutral}</b>neutral</span><span><b>${votes.bearish}</b>bearish</span><span><b>${votes.no_view}</b>no view</span></div>
        <p class="fine">One vote per analyst. Buy at +0.50 or more, Overweight from +0.20, Hold between -0.20 and +0.20, Underweight to -0.50, Sell below.</p></div>`],
      ["investors", "Investors", `<p class="gist">Five investor personas read the same fundamentals and price history, each in their own style.</p>${v.agents.filter(a => a.stage === "Investors").map(card).join("")}`],
      ["signals", "Signal models", `<p class="gist">Two rules-based models with no LLM: drift after an earnings surprise, and the Street's consensus.</p>${v.agents.filter(a => a.stage === "Signal models").map(card).join("")}`],
      ["manager", "Research manager", `<p class="gist">${esc(vd.summary)}</p><div class="prose"><p>${mark(run, "vets", vd.thesis, false)}</p></div>
        <div class="vlist">${ul("Bull case", "var(--ow)", vd.bull_case)}${ul("Bear case", "var(--uw)", vd.bear_case)}${ul("What would change the view", "var(--ink)", vd.what_would_change)}${ul("Headlines it read", "var(--ink-3)", vd.headlines)}</div>
        <p class="fine mono">Rating ${esc(vd.rating)} · ${vd.confidence}% confidence · ${esc(vd.model || "")}</p>`],
      ["fundamentals", "Fundamentals", `<p class="gist">What every persona was given: the company, its filings and the trailing numbers, from SEC EDGAR.</p><div class="prose">${String(v.fundamentals || "").split(/\n+/).map(l => `<p>${esc(l)}</p>`).join("")}</div>`],
      ["levels", "Price and valuation", `<p class="gist">Last settled close ${money(run.close)}${an.pct_from_52w_high != null ? `, ${Math.abs(an.pct_from_52w_high * 100).toFixed(1)}% below the 52-week high` : ""}.</p><table class="kv">${kv.map(([k, x]) => `<tr><td>${k}</td><td>${x}</td></tr>`).join("")}</table>`],
    ];
    d = { kind: "vets",
      toc: SECS.map(([id, n]) => `<a href="#${id}" data-sec="${id}">${n}</a>`).join(""),
      secs: SECS.map(([id, n, b], i) => `<section class="rsec v" id="${id}"><div class="rsec-h"><span class="stage">${i + 1} · ai-hedge-fund</span><h3>${n}</h3></div>${b}</section>`).join(""),
      head: `<div class="rd-kick" style="color:var(--vets)"><i style="background:var(--vets)"></i>Veterans<span>ai-hedge-fund · standalone report</span></div>
        <h1 class="rd-title">${esc(run.ticker)}: <span class="r-${rc(v.rating)}">${esc((v.rating || "").toUpperCase())}</span></h1>
        <p class="rd-dek">${esc(vd.summary)}</p>
        <div class="rd-facts"><span><b>5</b> investors</span><span><b>2</b> signal models</span><span><b>1</b> manager</span>${v.seconds ? `<span><b class="mono">${v.seconds}s</b></span>` : ""}</div>` };
  } else if (doc === "edge") {
    const e = run.edge, body = sectionsOf(e.report_md, "e", "Edge Desk", run, "edge");
    d = { kind: "edge", ...body,
      head: `<div class="rd-kick" style="color:var(--edge)"><i style="background:var(--edge)"></i>Edge Desk<span>its full report · scored by formula, explained by a model</span></div>
        <h1 class="rd-title">${esc(run.ticker)}: <span class="r-${rc(e.rating)}">${esc((e.rating || "Withheld").toUpperCase())}</span></h1>
        <p class="rd-dek">Long term, 1 year or more. The rating comes from a formula over a point-in-time evidence package, and every report prints what has been measured about it. The written analysis explains the evidence; it cannot change a number.</p>
        <div class="rd-facts">${e.score != null ? `<span>Score <b class="mono">${e.score.toFixed(1)}</b> of 100</span>` : ""}<span><b>${esc(e.conviction || "")}</b> conviction</span>${e.swing?.signal ? `<span>Swing setup <b>${esc(e.swing.signal)}</b>, no rating</span>` : ""}<span><b class="mono">${words(e.report_md).toLocaleString()}</b> words · ${readMin(words(e.report_md))} min read</span></div>` };
  } else {
    d = { kind: "memo", ...sectionsOf(run.memo_md, "", "Combined memo"),
      head: `<div class="rd-kick"><i style="background:var(--ink)"></i>Combined memo<span>the desk's conclusion · ${run.engines.length} teams</span></div>
        <h1 class="rd-title">${esc(run.ticker)}</h1>
        <p class="rd-dek">Each horizon's rating, the valuation panel, the reasoning, the risks and the levels, written after the teams rated${run.horizons.some(h => h.debate) ? " and the debate ended" : ""}.</p>
        <div class="rd-facts">${run.horizons.map(h => `<span>${esc(h.label)} <b class="r-${rc(h.rating)}">${esc(verdictWord(h))}</b></span>`).join("")}<span><b class="mono">${words(run.memo_md).toLocaleString()}</b> words · ${readMin(words(run.memo_md))} min read</span></div>` };
  }
  d.fixes = doc === "memo" ? "" : fixesBox(run, doc);
  return d;
}

function readerPage(run, doc) {
  const docs = reportDocs(run);
  if (!docs.some(d => d[0] === doc)) doc = docs[0][0];
  const d = docBody(run, doc);
  const base = { ...runQ(run) };
  const short = { memo: "Combined memo", quant: "Quant desk", vets: "Veterans", edge: "Edge Desk" };
  return `${phoneTop()}<div class="rd-top"><div class="rd-top-in">
      <a class="rd-back" href="?${new URLSearchParams(base)}" data-nav>${ico.arrow} ${esc(run.ticker)} · ${fmtDate(run.date, false)}</a>
      <nav class="docsw" aria-label="Report">${docs.map(([k, , c]) => `<a href="?${new URLSearchParams({ ...base, doc: k })}" data-nav class="${k === doc ? "on" : ""}"><i style="background:${c}"></i>${short[k]}</a>`).join("")}</nav>
    </div><div class="rd-prog ${d.kind}"><i id="prog"></i></div></div>
    <div class="reader"><nav class="toc ${d.kind}" aria-label="Sections"><h6>Contents</h6>${d.toc}${d.brief ? `<div class="toc-tools"><label class="switch"><input type="checkbox" data-brief> Summaries only</label></div>` : ""}</nav>
      <article id="art">${d.brief ? `<label class="switch brief-m"><input type="checkbox" data-brief> Summaries only</label>` : ""}<header class="rd-head">${d.head}</header>${d.fixes}${d.secs}
      <p class="foot">Research only. The desk places no orders and is not financial advice.</p></article></div>`;
}

function wireReader() {
  const art = document.getElementById("art"); if (!art) return;
  const links = [...document.querySelectorAll(".toc a[data-sec]")], secs = links.map(a => document.getElementById(a.dataset.sec));
  const onScroll = () => {
    const bar = document.getElementById("prog"); if (!bar) return removeEventListener("scroll", onScroll);
    const h = document.documentElement.scrollHeight - innerHeight;
    bar.style.width = (h > 0 ? scrollY / h * 100 : 0) + "%";
    let cur = 0; secs.forEach((s, i) => { if (s && s.getBoundingClientRect().top < 140) cur = i; });
    links.forEach((a, i) => a.classList.toggle("on", i === cur));
  };
  addEventListener("scroll", onScroll, { passive: true }); onScroll();
  document.querySelectorAll("[data-brief]").forEach(b => b.addEventListener("change", e => {
    art.classList.toggle("brief", e.target.checked); document.querySelectorAll("[data-brief]").forEach(x => x.checked = e.target.checked); onScroll(); }));
  document.querySelectorAll(".rsec").forEach(s => s.addEventListener("click", () => {
    if (!art.classList.contains("brief")) return;
    art.classList.remove("brief"); document.querySelectorAll("[data-brief]").forEach(x => x.checked = false); s.scrollIntoView(); onScroll(); }));
  if (location.hash) { const t = document.getElementById(location.hash.slice(1)); if (t) setTimeout(() => t.scrollIntoView(), 30); }
}

/* ------------------------------------------------------------------ print view (the PDF)
   ?print=brief  the symbol, every horizon's call with its levels, the debates in short, the teams
   ?print=full   adds the full debate transcripts and each team's complete report, corrections marked */
function printPage(run, kind) {
  const full = kind === "full", reports = run.mode === "reports" || !run.horizons.length;
  const stamp = `${fmtDate(run.date)}${run.generated_at ? ", " + new Date(run.generated_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) : ""}`;
  const cover = `<header class="pr-cover"><div class="pr-mark">THE DESK<small>Research only</small></div>
    <h1>${esc(run.ticker)}<span>${esc(S.profile[run.ticker]?.name || "")}</span></h1>
    <p class="pr-sub">${full ? "Full report" : "Summary report"} · run ${stamp} · ${run.owns ? "owned" : "new position"} · ${list(run.engines.map(e => TEAMS[e].name))}${reports ? " · no debate" : ""}</p>
    ${reports ? "" : `<table class="pr-glance"><thead><tr><th>Horizon</th><th>Rating</th><th>Agreement</th><th>Action</th></tr></thead><tbody>${run.horizons.map(h =>
      `<tr><td>${esc(h.label)}<small>${esc(h.sub)}</small></td><td><b class="r-${rc(h.rating)}">${esc(verdictWord(h))}</b></td><td>${esc(agreement(h.conviction))}</td><td>${esc(h.action)}</td></tr>`).join("")}</tbody></table>`}</header>`;
  const debateBrief = h => !h.debate ? "" : `<div class="dis pr-keep"><p class="issue"><small>The debate · ${esc(h.label)}</small>${esc(h.memo.crux || h.how)}</p>
      <div class="ex">${h.debate.turns.map(t => `<div class="exrow ${TEAMS[t.team].cls}"><span class="rd">Round ${t.round}</span><span class="tm"><i></i>${TEAMS[t.team].name}${t.decision === "concede" ? '<span class="conceded">Conceded</span>' : ""}</span>
        <span class="mv">${t.decision === "concede" ? "Adopts" : "Defends"} <b>${esc(t.rating)}</b>. ${esc(full ? t.argument : firstSentence(t.argument))}${full && t.changed ? `<br><i>What changed its mind:</i> ${esc(t.changed)}` : ""}${full && t.holes.length ? `<br><i>${t.decision === "concede" ? "Still doubts" : "Holes it found"}:</i> ${t.holes.map(esc).join(" ")}` : ""}</span></div>`).join("")}</div>
      <div class="dis-foot"><span class="outcome">${esc(h.how)}.</span></div></div>`;
  if (!full && !reports) {
    // The two-page brief (2026-10-05): the same first screen as the dashboard, then each debate in
    // a few lines. The full report keeps the long layout below.
    const debates = run.horizons.filter(h => h.debate && h.key !== "as_they_ran").map(h => `<div class="pr-deb pr-keep"><b>${esc(h.label)} debate.</b>
      ${esc(h.memo.crux || "")} ${h.debate.turns.map(t => `${TEAMS[t.team].name} ${t.decision === "concede" ? "conceded to" : "held"} ${esc(t.rating)}`).join("; ")}.</div>`).join("");
    const struck = (run.corrections || []).filter(c => c.status === "withdrawn").length, disputed = (run.corrections || []).length - struck;
    return `<div class="print brief2"><div class="pr-mark">THE DESK<small>Summary report · ${stamp}</small></div>
      ${firstScreen(run, "long_term")}
      ${debates ? `<h4 class="fs-h">The debates</h4>${debates}` : ""}
      ${run.corrections?.length ? `<p class="fs-val">After the debates, ${struck} statement${struck === 1 ? " was" : "s were"} given up by ${struck === 1 ? "its" : "their"} own team and ${disputed} ${disputed === 1 ? "was" : "were"} disputed; the full report marks each one.</p>` : ""}
      <p class="foot">Research output for the investor's own decision. The desk places no orders and this is not financial advice. Agreement labels describe what happened in the debate, not how often the desk is right. Generated by The Desk, ${stamp}.</p></div>`;
  }
  const horizons = reports ? sideBySide(run) : run.horizons.map((h, i) => `<div class="${i ? "pr-break" : ""}">${callSection({ ...run, horizons: [h] }, h)}${debateBrief(h)}</div>`).join("");
  const allFixes = run.engines.map(t => fixesBox(run, t)).join("");
  const teams = `<section class="sec pr-break"><div class="eyebrow"><b>The teams</b></div>
    <div class="side-by" style="--n:${run.engines.length}">${run.engines.map(t => `<div class="sb ${TEAMS[t].cls}"><div class="n">${TEAMS[t].caps}</div><div class="r r-${rc(run[t].rating)}">${esc(run[t].rating || "Withheld")}</div><div class="tf">${esc(run[t].timeframe || "")}</div><p>${esc(run[t].case)}</p></div>`).join("")}</div>
    ${reports ? "" : horizonRows(run.engines, run.horizons.map(h => ({ label: h.label, out: `<b class="r-${rc(h.rating)}">${esc(verdictWord(h))}</b>`,
      cells: Object.fromEntries(run.engines.map(t => [t, h.restated[t] && !h.restated[t].rating ? "noview" : h.restated[t] ? { rating: h.restated[t].rating, tf: h.key === "as_they_ran" ? h.restated[t].timeframe : "", out: !h.voters.includes(t) } : (t === "edge" && h.key === "swing") ? "none" : run[t] && !run[t].rating ? "withheld" : null])) })))}
    ${allFixes ? `<div class="pr-keep" style="margin-top:22px">${allFixes}</div>` : ""}</section>`;
  const appendix = !full ? "" : run.engines.map(t => { const d = docBody(run, t);
    return `<section class="pr-doc pr-break"><header class="rd-head">${d.head.replace(/<a class="jump"[\s\S]*?<\/a>/, "")}</header>${d.fixes}${d.secs}</section>`; }).join("");
  return `<div class="print ${full ? "full" : "brief"}">${cover}${symbolCard(run.ticker, run.close, run.anchors, run)}${horizons}${teams}${appendix}
    <p class="foot">Research output for the investor's own decision. The desk places no orders and this is not financial advice. ${run.billing === "max" ? "" : ""}Generated by The Desk, ${stamp}.</p></div>`;
}

/* ------------------------------------------------------------------ live */
function newLiveState(id) {
  const team = () => ({ started: false, reused: false, done: {}, rating: undefined, at: null, finished: false });
  return { id, from: 0, events: [], elapsed: 0, running: true, exit: null, log: "", ticker: "", date: "", own: false,
    engines: ["quant", "vets"], debate: true, team: { quant: team(), vets: team(), edge: team() }, hz: {}, finished: false };
}
function applyEvent(L, e) {
  const team = BY_ENGINE[e.engine], T = team && L.team[team];
  switch (e.type) {
    case "run_started": L.planned = e.horizons || []; if (e.engines) L.engines = e.engines; if (e.debate != null) L.debate = e.debate; break;
    case "engine_started": T.started = true; T.startAt = e.at; break;
    case "engine_reused": Object.assign(T, { started: true, reused: true, rating: e.rating, at: e.at, finished: true }); break;
    case "engine_done": Object.assign(T, { rating: e.rating, at: e.at, finished: true }); break;
    case "agent_done": if (T && e.step) T.done[e.step] = { at: e.at, text: e.text, call: e.call, full: e.full, failed: !!e.failed }; break;
    case "timeframes": L.timeframes = { quant: e.tape, vets: e.value, edge: e.edge }; break;
    case "horizon_started": L.hz[e.horizon] = { ratings: {}, turns: [], started: true }; break;
    case "horizon_rating": (L.hz[e.horizon] ||= { ratings: {}, turns: [] }).ratings[team] = { rating: e.rating, tf: e.timeframe, out: e.voting === false }; break;
    case "debate_started": L.hz[e.horizon].debating = true; break;
    case "debate_skipped": L.hz[e.horizon].skipped = e.text; break;
    case "debate_turn": L.hz[e.horizon].turns.push({ team, round: e.round, decision: e.decision, rating: e.rating, text: e.text }); break;
    case "horizon_outcome": Object.assign(L.hz[e.horizon], { outcome: e.rating, status: e.status, conviction: e.conviction, action: e.action, how: e.text, debating: false, decided: true }); break;
    case "memo_written": L.hz[e.horizon].headline = e.text; break;
    case "run_finished": L.finished = true; break;
  }
  L.events.push(e);
}

async function pollLive() {
  const L = S.liveState; if (!L) return;
  try {
    const r = await api(`/api/status?id=${encodeURIComponent(L.id)}&from=${L.from}`);
    L.error = null;
    Object.assign(L, { elapsed: r.elapsed, running: r.running, exit: r.exit, log: r.log || "", ticker: r.ticker, date: r.date, own: r.own, plan: r.plan, cancelled: !!r.cancelled });
    if (!L.events.length) { L.engines = r.engines || L.engines; L.debate = r.debate !== false; }
    r.events.forEach(e => { applyEvent(L, e); L.from = e.seq; });
    loadProfile(L.ticker);
    if (!r.running && r.exit === 0) {
      clearInterval(S.poll); S.poll = null;
      await loadRuns();
      return go({ t: L.ticker, d: L.date }, true);
    }
    if (!r.running) { clearInterval(S.poll); S.poll = null; }
  } catch (e) {
    // A laptop wake, a busy local server, or a dashboard restart can make one status request
    // fail while the research process continues.  Keep polling: a completed memo is durable and
    // the server can reconcile it on a later request.
    L.error = e.message;
  }
  render();
}

function liveTeamCol(L, team) {
  const T = L.team[team], C = TEAMS[team], failed = !L.running && L.exit && !T.finished;
  const allDone = L.engines.every(e => L.team[e].finished);
  if (T.finished) {
    const tf = L.timeframes?.[team];
    return `<div class="col ${C.cls}"><div class="col-h"><span class="n">${C.caps}</span><span class="c">${T.reused ? "reused today's report" : "done in " + mmss((T.at || 0) - (T.startAt || 0))}</span></div>
      <div class="verdict-card"><div class="l">Team rating</div><div class="tr r-${rc(T.rating)}">${esc((T.rating || "Withheld").toUpperCase())}</div>
        ${tf ? `<div class="tf">${esc(tf)}</div>` : ""}
        ${!allDone ? `<div class="wait">Waiting for ${list(L.engines.filter(e => !L.team[e].finished).map(e => "the " + TEAMS[e].name))}.${L.debate ? " The horizons start when every team has rated." : ""}</div>` : ""}</div></div>`;
  }
  const done = C.roster.filter(a => T.done[a.step]), pending = C.roster.filter(a => !T.done[a.step]);
  const working = !T.started || failed ? [] : !C.parallel ? pending.slice(0, 1) : pending.filter(a => a.stage !== "Decision").length ? pending.filter(a => a.stage !== "Decision") : pending.slice(0, 1);
  const queued = pending.filter(a => !working.includes(a));
  const line = d => d.text || (d.call ? cap(d.call.toLowerCase()) + "." : "No view.");
  return `<div class="col ${C.cls}"><div class="col-h"><span class="n">${C.caps}</span><span class="c">${T.started ? `${done.length} of ${C.roster.length}` : "starting"}</span></div>
    ${done.length ? `<ul class="done">${done.slice(-4).map(a => `<li><button data-live-agent="${team}:${a.step}"><span class="ck${T.done[a.step].failed ? " x" : ""}">${T.done[a.step].failed ? "&times;" : "&#10003;"}</span><span class="dn"><b>${esc(a.name)}</b>${esc(line(T.done[a.step]))}</span><span class="dt">${mmss(T.done[a.step].at)}</span></button></li>`).join("")}
      ${done.length > 4 ? `<li class="earlier">and ${done.length - 4} earlier</li>` : ""}</ul>` : ""}
    ${C.parallel && working.length > 1 ? `<div class="active"><div class="an">${working.length} investors<span>working in parallel</span></div><p>${esc(working.map(a => a.name).join(", "))}</p></div>`
      : working.map(a => `<div class="active"><div class="an">${esc(a.name)}<span>working</span></div><p>${ACTIVITY[a.id] || (a.id.startsWith("lens:") ? "Reading the evidence through this investor's eyes" : "Working")}...</p></div>`).join("")}
    ${failed ? `<div class="active failed"><div class="an">${esc(pending[0]?.name || "This team")}<span>${L.cancelled ? "cancelled" : "stopped"}</span></div><p>${L.cancelled ? "Stopped when you cancelled the run." : "The run ended before this team finished. The log in the banner above says why."}</p></div>` : ""}
    ${queued.length && !failed ? `<div class="next"><b>Up next:</b> ${queued.slice(0, 3).map(a => a.name).join(", ")}${queued.length > 3 ? `, and ${queued.length - 3} more` : ""}</div>` : ""}
  </div>`;
}

function livePage(L) {
  const t = L.ticker || "...", clock = mmss(L.elapsed), teams = L.engines;
  const allRated = teams.every(e => L.team[e].finished);
  const failed = !L.running && L.exit;
  const finishedTeams = teams.filter(e => L.team[e].finished);
  const prog = `<div class="progress">${teams.map(e => { const T = L.team[e], n = TEAMS[e].roster.length, k = T.finished ? n : Object.keys(T.done).length;
    return `<div class="pg ${TEAMS[e].cls}"><b>${TEAMS[e].name}</b> ${T.finished ? "done" : `${k} of ${n}`}<span class="bar"><i style="width:${k / n * 100}%"></i></span></div>`; }).join("")}</div>`;
  const chips = !L.debate ? teams.map(e => { const T = L.team[e];
      return T.finished ? `<div class="hchip"><div class="l">${TEAMS[e].name}</div><div class="r r-${rc(T.rating)}">${esc(T.rating || "Withheld")}</div></div>` : `<div class="hchip empty"><div class="l">${TEAMS[e].name}</div><div class="r">Working</div></div>`; }).join("")
    : (L.planned?.length ? L.planned : HZ_ORDER).map(k => { const h = L.hz[k];
      if (h?.decided) return `<div class="hchip"><div class="l">${HZ[k]}</div><div class="r r-${rc(h.outcome)}">${esc(verdictWord({ rating: h.outcome, status: h.status }))}</div></div>`;
      if (h?.debating) return `<div class="hchip debating"><div class="l">${HZ[k]}</div><div class="r">Debating</div></div>`;
      return `<div class="hchip empty"><div class="l">${HZ[k]}</div><div class="r">${h ? "Comparing" : "Not yet"}</div></div>`; }).join("");
  const banner = L.cancelled ? `<div class="banner"><span class="mk">&times;</span>
      <div><b>You cancelled this run</b><span>${finishedTeams.length ? `${cap(list(finishedTeams.map(e => "the " + TEAMS[e].name)))} had already finished; that report is saved and will be reused if you run ${esc(t)} again today.` : "Every team was stopped and nothing was saved."}</span></div>
      <div class="acts"><button class="btn" data-rerun>Run again</button></div></div>`
    : failed ? `<div class="banner"><span class="mk">!</span>
      <div><b>The run stopped${allRated ? " after every team rated" : finishedTeams.length ? ` before the ${list(teams.filter(e => !L.team[e].finished).map(e => TEAMS[e].name))} finished` : ""}</b>
      <span>${finishedTeams.length ? "Whatever finished is saved. Running again reuses it, so only the unfinished part starts over." : "Nothing was saved. Running again starts over."}</span></div>
      <div class="acts"><button class="btn dark" data-rerun>Run again</button></div>
      ${L.log ? `<details class="log"><summary>Show the last lines of the log</summary><pre>${esc(L.log)}</pre></details>` : ""}</div>` : "";
  const errorBanner = L.error ? `<div class="banner"><span class="mk">!</span><div><b>Lost track of this run</b><span>${esc(L.error)}</span></div></div>` : "";
  const feed = [...L.events].filter(e => e.type === "agent_done").reverse();
  const stillWorking = teams.filter(e => !L.team[e].finished).map(e => "the " + TEAMS[e].name);
  const now = `${L.plan ? `<p class="plan-live">Running on the <b>${PLAN_NAME[L.plan] || L.plan} plan</b>${planModels(L.plan) ? ` (${esc(planModels(L.plan))})` : ""}</p>` : ""}<section aria-label="Now"><div class="eyebrow"><b>${allRated ? (teams.length > 1 ? "Every team has rated" : "The team has rated") : "Now"}</b><span>${allRated ? (L.debate ? "Their ratings are now compared, one horizon at a time" : "Saving the reports")
      : failed ? `Stopped at ${clock}` : finishedTeams.length ? `${cap(list(finishedTeams.map(e => "the " + TEAMS[e].name)))} ${finishedTeams.length > 1 ? "have" : "has"} rated; ${list(stillWorking)} ${stillWorking.length > 1 ? "are" : "is"} still working` : teams.length > 1 ? "Every team is working" : "Working"}</span></div>
    <div class="now" style="--n:${teams.length}">${teams.map(e => liveTeamCol(L, e)).join("")}</div></section>`;
  let convergence = "";
  if (allRated && L.debate) {
    const rows = (L.planned?.length ? L.planned : HZ_ORDER).map(k => { const h = L.hz[k], started = h && Object.keys(h.ratings).length;
      return { label: HZ[k], pending: !started,
        cells: Object.fromEntries(teams.map(e => [e, h?.ratings[e] && !h.ratings[e].rating ? "noview" : h?.ratings[e] ? { rating: h.ratings[e].rating, tf: h.ratings[e].tf || "", out: h.ratings[e].out } : (e === "edge" && k === "swing" && started) ? "none" : null])),
        out: h?.decided ? `${h.turns.length ? "Debated" : "Agreed"} · <b class="r-${rc(h.outcome)}">${esc(verdictWord({ rating: h.outcome, status: h.status }))}</b>`
          : h?.debating ? `<b class="deb">Debating, turn ${h.turns.length + 1}</b>` : h ? "Comparing" : "Next" }; });
    const deb = Object.entries(L.hz).filter(([, h]) => h.turns.length || h.debating);
    convergence = `<section aria-label="Horizons">${mergeSvg(teams, false)}${horizonRows(teams, rows)}
      ${deb.map(([k, h]) => `<div class="dis" style="margin-top:16px"><p class="issue"><small>${h.debating ? "Debating now" : "Debated"} · ${HZ[k]}</small>${h.debating ? "The teams gave different sides, so each manager defends or concedes. Nobody has to concede." : esc(h.how || "")}</p>
        <div class="ex">${h.turns.map(x => `<div class="exrow ${TEAMS[x.team].cls}"><span class="rd">Round ${x.round}</span><span class="tm"><i></i>${TEAMS[x.team].name}${x.decision === "concede" ? '<span class="conceded">Conceded</span>' : ""}</span>
          <span class="mv">${x.decision === "concede" ? "Adopts" : "Defends"} <b>${esc(x.rating)}</b>. ${esc(firstSentence(x.text))}</span></div>`).join("")}
        ${h.debating && L.running ? `<div class="exrow"><span class="rd"></span><span class="tm"></span><span class="mv live-reply">Replying<span class="typing"><i></i><i></i><i></i></span></span></div>` : ""}</div></div>`).join("")}
      ${Object.values(L.hz).some(h => h.headline) ? `<div class="feed memo-feed">${Object.entries(L.hz).filter(([, h]) => h.headline).map(([k, h]) => `<div class="fi"><span class="t">memo</span><span class="w"><span>${HZ[k]}<small class="r-${rc(h.outcome)}">${esc(verdictWord({ rating: h.outcome, status: h.status }))}</small></span></span><span class="x">${esc(h.headline)}</span><span></span></div>`).join("")}</div>` : ""}
      <p class="later">${L.running ? "The page turns into the finished snapshot when the last memo is written." : ""}</p></section>`;
  }
  const feedSec = !(allRated && L.debate) ? `<section class="sec" aria-label="Latest findings"><div class="eyebrow"><b>Latest findings</b><span>${feed.length ? `${feed.length} so far, newest first. Open any one to read it in full.` : "Newest first, across the teams"}</span></div>
    <div class="feed scroll">${feed.length ? feed.map(e => { const tm = BY_ENGINE[e.engine], who = TEAMS[tm].roster.find(a => a.step === e.step);
      return `<button class="fi ${TEAMS[tm].cls}" data-live-agent="${tm}:${e.step}"><span class="t">${mmss(e.at)}</span><span class="w"><i></i><span>${TEAMS[tm].name}<small>${esc(who?.name || e.who)}</small></span></span><span class="x">${esc(e.text || (e.call ? cap(e.call.toLowerCase()) + "." : "No view."))}</span><span class="o">Open</span></button>`; }).join("")
      : `<div class="empty-feed">The first findings show up here as each agent finishes.</div>`}</div></section>` : "";
  return { strip: strip({ ticker: t, price: null, owns: L.own, live: !failed, stopped: !!failed, cancelled: L.cancelled, clock, chips, prog: failed ? "" : prog, reports: !L.debate }),
    body: symbolCard(t, null, null) + errorBanner + banner + now + convergence + feedSec };
}

function liveAgentPanel(L, spec) {
  const [team, ...rest] = spec.split(":"), step = rest.join(":"), T = L.team[team];
  const a = TEAMS[team]?.roster.find(x => x.step === step), d = T?.done[step];
  if (!a || !d) return "";
  return `<aside class="panel" aria-label="${esc(a.name)}"><div class="p-top"><div class="crumb"><i style="background:${TEAMS[team].color}"></i>${esc(L.ticker)} · Live · ${TEAMS[team].name} · ${esc(a.stage)}</div>
    <div class="p-row"><div><div class="p-title">${esc(a.name)}</div><div class="p-meta">Finished at ${mmss(d.at)}${d.call ? " · " + esc(d.call) : ""}</div></div><div class="p-btns"><button class="p-btn" data-close-panel aria-label="Close">${ico.x}</button></div></div></div>
    <div class="p-body"><h5>In one line</h5><p class="takeaway">${esc(d.text || (d.call ? cap(d.call.toLowerCase()) + "." : "No view."))}</p>
      ${d.full ? `<h5>What it wrote</h5><div class="prose small">${md(d.full)}</div>` : `<p class="fine">The full text opens in the finished run.</p>`}</div></aside>`;
}

function runTools(run) {
  const link = doc => "?" + new URLSearchParams({ ...runQ(run), doc });
  const pdf = kind => `/api/pdf?${new URLSearchParams({ ...runQ(run), kind })}`;
  return `<div class="tools">
    <details class="menu"><summary class="tool">Reports ${ico.dn}</summary><div class="pop">${reportDocs(run).map(([k, n, c]) => `<a href="${link(k)}" data-nav><i style="background:${c}"></i>${n}</a>`).join("")}</div></details>
    <details class="menu"><summary class="tool">Download PDF ${ico.dn}</summary><div class="pop wide">
      <a href="${pdf("brief")}" download data-pdf><b>Summary</b><small>The call for each horizon with its levels and chart, the debate in short, the teams' ratings. About a dozen pages.</small></a>
      <a href="${pdf("full")}" download data-pdf><b>Everything</b><small>The summary plus the full debate and each team's complete report, with every statement a team gave up or had disputed in the debate marked.</small></a>
      <p class="fine" id="pdfnote">Made on this computer. It takes a few seconds.</p></div></details></div>`;
}

/* ------------------------------------------------------------------ render */
function render() {
  const q = Q();
  document.body.classList.toggle("drawer-open", S.drawer);
  const foot = `<p class="foot">Research only. The desk places no orders and is not financial advice.</p>`;
  if (q.live) {
    const L = S.liveState;
    if (!L) { $app.innerHTML = `<div class="app">${sidebar()}<main><div class="page"><p class="loading-msg">Connecting to the run...</p></div></main></div>`; return; }
    const { strip: st, body } = livePage(L);
    const pan = q.panel ? liveAgentPanel(L, q.panel) : "";
    $app.innerHTML = `<div class="app ${pan && !PHONE() ? "reading" : ""}">${pan && !PHONE() ? railHtml() : sidebar("live:" + L.id)}<main>${st}<div class="page">${body}${foot}</div></main>${pan}</div>`;
    return;
  }
  if (q.t && q.d) {
    const run = S.run;
    if (!run || run.ticker !== q.t || run.date !== q.d) {
      $app.innerHTML = `<div class="app">${sidebar(`${q.t}:${q.d}`)}<main><div class="page"><p class="loading-msg">${S.loadError ? esc(S.loadError) : `Loading ${esc(q.t)}...`}</p></div></main></div>`;
      return;
    }
    if (q.print) {
      document.title = `${run.ticker} ${run.date} ${q.print === "full" ? "full report" : "summary"}`;
      document.body.classList.add("printing");
      $app.innerHTML = printPage(run, q.print);
      document.querySelectorAll("details").forEach(x => x.open = true);
      document.getElementById("symdesc")?.classList.add("open");
      return;
    }
    if (q.doc) {
      $app.innerHTML = `<div class="app">${sidebar(`${run.ticker}:${run.date}`)}<main>${readerPage(run, q.doc)}</main></div>`;
      wireReader();
      return;
    }
    const reports = run.mode === "reports" || !run.horizons.length;
    const key = q.h && run.horizons.some(h => h.key === q.h) ? q.h : (run.horizons.find(h => h.key === "long_term") || run.horizons[0])?.key;
    const chips = reports ? run.engines.map(e => `<div class="hchip"><div class="l">${TEAMS[e].name}</div><div class="r r-${rc(run[e].rating)}">${esc(run[e].rating || "Withheld")}</div></div>`).join("")
      : run.horizons.map(h => `<button class="hchip ${h.key === key ? "on" : ""}" data-h="${h.key}"><div class="l">${h.label}</div><div class="r r-${rc(h.rating)}">${esc(verdictWord(h))}</div></button>`).join("");
    const pan = panel(run, q.panel);
    $app.innerHTML = `<div class="app ${pan && !PHONE() ? "reading" : ""}">${pan && !PHONE() ? railHtml(`${run.ticker}:${run.date}`) : sidebar(`${run.ticker}:${run.date}`)}
      <main>${strip({ ticker: run.ticker, price: run.close, owns: run.owns, live: false, chips, reports, tools: runTools(run) })}<div class="page">${snapshotPage(run, key)}${foot}</div></main>${pan}</div>`;
    return;
  }
  if (q.view === "week") {
    $app.innerHTML = `<div class="app">${sidebar("week")}<main>${phoneTop()}<div class="page">${weekPage()}${foot}</div></main></div>`;
    return;
  }
  const lead = r => r.ratings.long_term || r.ratings.swing || Object.values(r.own_ratings || {})[0];
  $app.innerHTML = `<div class="app">${sidebar()}<main>${phoneTop()}<div class="page home">
    <h1 class="home-h">Run a symbol, or open a past run.</h1>
    <p class="home-p">Pick the teams: the Quant desk (TradingAgents), the Veterans (ai-hedge-fund) and Edge Desk. With a debate, their ratings are compared for each horizon, argued where they differ, and written up as a memo. Without one, you get each team's report as it stands.</p>
    ${S.runs.length ? `<div class="docs">${S.runs.slice(0, 8).map(r => `<a class="doc" href="?${new URLSearchParams({ t: r.ticker, d: r.date })}" data-nav><i style="background:var(--${rc(lead(r)) === "none" ? "ink-3" : rc(lead(r))})"></i><span><span class="n">${esc(r.ticker)}</span><span class="m">${fmtDate(r.date)} · ${r.mode === "reports"
      ? ORDER.filter(e => r.engines.includes(e)).map(e => `${TEAMS[e].name} ${esc(r.own_ratings[e] || "withheld")}`).join(" · ")
      : HZ_ORDER.filter(k => r.ratings[k] !== undefined).map(k => `${HZ[k]} ${esc(r.ratings[k] || verdictWord({ status: r.status?.[k] }))}`).join(" · ")}</span></span><span class="go2">Open</span></a>`).join("")}</div>` : ""}
    </div></main></div>`;
}

/* ------------------------------------------------------------------ routing and events */
async function loadRuns() {
  try { const r = await api("/api/runs"); S.runs = r.runs; S.live = r.live; S.planInfo = r.plan || S.planInfo; } catch (e) { S.runs = S.runs || []; }
}
async function route() {
  const q = Q();
  S.loadError = null;
  if (q.live) {
    if (!S.liveState || S.liveState.id !== q.live) {
      clearInterval(S.poll);
      S.liveState = newLiveState(q.live);
      await pollLive();
      if (S.liveState?.running) S.poll = setInterval(pollLive, 1500);
    }
    return render();
  }
  clearInterval(S.poll); S.poll = null; S.liveState = null;
  if (q.t && q.d && (!S.run || S.run.ticker !== q.t || S.run.date !== q.d || (S.run.shownR || "") !== (q.r || ""))) {
    render();
    try { S.run = await api(`/api/run?t=${encodeURIComponent(q.t)}&d=${encodeURIComponent(q.d)}${q.r ? `&r=${encodeURIComponent(q.r)}` : ""}`); S.run.shownR = q.r || ""; }
    catch (e) { S.loadError = `Could not open ${q.t} for ${q.d}: ${e.message}`; }
    loadProfile(q.t);
  }
  render();
  if (!q.doc && !location.hash) scrollTo(0, 0);
}
const startRun = (ticker, own, engines, debate, plan = planNow()) => api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticker, own, engines, debate, plan }) });

document.addEventListener("click", async e => {
  const nav = e.target.closest("a[data-nav]");
  if (nav && !e.metaKey && !e.ctrlKey) {
    e.preventDefault(); S.drawer = false;
    const u = new URL(nav.href);
    history.pushState(null, "", u.search + u.hash); route(); return;
  }
  const hb = e.target.closest("[data-h]");
  if (hb) return go({ ...Q(), h: hb.dataset.h, panel: null }, true);
  const row = e.target.closest("[data-go]");
  if (row) { const [t, d] = row.dataset.go.split("|"); return go({ t, d }); }
  const pb = e.target.closest("[data-panel]");
  if (pb) {
    const spec = pb.dataset.panel;
    go({ ...Q(), panel: spec }, true);
    if (spec.startsWith("debate")) document.getElementById("turn" + spec.split(":")[2])?.scrollIntoView({ block: "start" });
    return;
  }
  const la = e.target.closest("[data-live-agent]");
  if (la) return go({ ...Q(), panel: la.dataset.liveAgent }, true);
  if (e.target.closest("[data-close-panel]")) return go({ ...Q(), panel: null }, true);
  if (e.target.closest("[data-open-drawer]")) { S.drawer = true; return render(); }
  if (e.target.closest("[data-close-drawer]")) { S.drawer = false; return render(); }
  const seg = e.target.closest("[data-own]");
  if (seg) { S.own = seg.dataset.own === "1"; document.querySelectorAll("[data-own]").forEach(b => b.classList.toggle("on", b === seg)); return; }
  const planBtn = e.target.closest("[data-plan]");
  if (planBtn) { S.plan = planBtn.dataset.plan; return render(); }
  if (e.target.closest("[data-more]")) { const d = document.getElementById("symdesc"); d.classList.toggle("open"); e.target.textContent = d.classList.contains("open") ? "Less" : "More"; return; }
  const pdfLink = e.target.closest("[data-pdf]");
  if (pdfLink) { const n = document.getElementById("pdfnote"); if (n) n.textContent = "Preparing the PDF. The download starts by itself in a few seconds."; return; }
  if (!e.target.closest("details.menu")) document.querySelectorAll("details.menu[open]").forEach(d => d.open = false);
  if (e.target.closest("[data-cancel]")) {
    const L = S.liveState; if (!L) return;
    if (!confirm(`Cancel the ${L.ticker} run? Teams still working are stopped; reports already finished are kept.`)) return;
    e.target.closest("[data-cancel]").disabled = true;
    try { await api("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: L.id }) }); await pollLive(); await loadRuns(); render(); }
    catch (err) { alert(err.message); }
    return;
  }
  if (e.target.closest("[data-rerun]")) {
    const L = S.liveState;
    try { const r = await startRun(L.ticker, L.own, L.engines, L.debate, L.plan || planNow()); await loadRuns(); S.liveState = null; go({ live: r.id }); } catch (err) { alert(err.message); }
  }
});
document.addEventListener("change", e => {
  const eng = e.target.closest("[data-engine]"), deb = e.target.closest("[data-debate]");
  if (!eng && !deb) return;
  if (eng) S.engines[eng.dataset.engine] = eng.checked;
  if (deb) S.debate = deb.checked;
  try { localStorage.setItem("desk.form", JSON.stringify({ engines: S.engines, debate: S.debate })); } catch {}
  const typed = document.getElementById("tk")?.value || "";
  render();
  const tk = document.getElementById("tk"); if (tk) tk.value = typed;
});
document.addEventListener("submit", async e => {
  if (e.target.id !== "newrun") return;
  e.preventDefault();
  const tk = document.getElementById("tk").value.trim().toUpperCase(), err = document.getElementById("formerr");
  const engines = ORDER.filter(x => S.engines[x]);
  if (!/^[A-Z.]{1,6}$/.test(tk)) { err.textContent = "Enter a ticker symbol, like ECG."; return; }
  if (!engines.length) { err.textContent = "Pick at least one team."; return; }
  err.textContent = "Starting...";
  try {
    const r = await startRun(tk, S.own, engines, S.debate && engines.length > 1);
    await loadRuns(); S.drawer = false; go({ live: r.id });
  } catch (x) { err.textContent = x.message; }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && Q().panel) { go({ ...Q(), panel: null }, true); return; }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches?.(".exrow.link")) { e.preventDefault(); e.target.click(); return; }
  if (!Q().panel || !Q().panel.startsWith("agent") || !["ArrowUp", "ArrowDown"].includes(e.key) || e.target.closest("input")) return;
  const b = document.querySelector(`.p-btns [aria-label="${e.key === "ArrowUp" ? "Previous" : "Next"}"][data-panel]`);
  if (b) { e.preventDefault(); b.click(); }
});
addEventListener("popstate", route);
let lastPhone = PHONE();
addEventListener("resize", () => { if (PHONE() !== lastPhone) { lastPhone = PHONE(); render(); } });

(async () => {
  await loadRuns();
  const q = Q();
  if (!q.t && !q.live && S.live.some(l => l.exit == null)) return go({ live: S.live.find(l => l.exit == null).id }, true);
  route();
  setInterval(async () => { if (!Q().live) { const before = JSON.stringify(S.live); await loadRuns(); if (JSON.stringify(S.live) !== before && !Q().doc) render(); } }, 15000);
})();
