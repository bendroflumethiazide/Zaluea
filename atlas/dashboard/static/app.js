"use strict";

const POLL_MS = 2000;
const $ = (id) => document.getElementById(id);

const ICON = {
  scan: '<path d="M11 4a7 7 0 1 0 4.9 12l4.1 4" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
  reject: '<path d="M6 6l12 12M18 6 6 18" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/>',
  shadow: '<path d="M4 16c3-6 6-6 8-2s5 4 8-2" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
  risk: '<path d="M12 3 5 6v6c0 4 3 7 7 9 4-2 7-5 7-9V6Z" fill="none" stroke="currentColor" stroke-width="2"/><path d="m9 12 2 2 4-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  recon: '<path d="M4 12a8 8 0 0 1 14-5l2-2v6h-6l2-2a6 6 0 0 0-10 3M20 12a8 8 0 0 1-14 5l-2 2v-6h6l-2 2a6 6 0 0 0 10-3" fill="currentColor"/>',
  health: '<path d="M3 12h4l2-5 4 10 2-5h6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
  control: '<circle cx="12" cy="12" r="3" fill="currentColor"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/>',
  ok: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="m8 12 3 3 5-6" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
  warn: '<path d="M12 4 2.5 20h19Z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M12 10v4M12 17h.01" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
  fail: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/><path d="m9 9 6 6m0-6-6 6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>',
};
const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">${ICON[name] || ICON.scan}</svg>`;

const KIND_LABEL = {
  scan: "Scan", reject: "No trade", shadow: "Simulated", risk: "Risk check", recon: "Reconciliation",
  health: "Health", control: "Your change", stop: "Emergency stop",
};

let S = null;
let scannerCat = "all";
let activityKind = "all";
let seenFeed = new Set();
let firstLoad = true;
let approvalShown = null;
let approvalDeadline = 0;

// ---------- formatting ----------
function money(c, { sign = false } = {}) {
  if (c === null || c === undefined) return "n/a";
  const v = Math.abs(c) / 100;
  const s = v.toLocaleString(undefined, { style: "currency", currency: "USD" });
  if (!sign || c === 0) return c < 0 ? "-" + s : s;
  return (c > 0 ? "+" : "-") + s;
}
const pct = (x) => (x == null ? "–" : `${Number(x).toFixed(1)}%`);
function timeAgo(t) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - t));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}
const clockTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function setText(id, t) { const el = $(id); if (el && el.textContent !== t) el.textContent = t; }
function setHTML(el, html) { if (el.innerHTML !== html) el.innerHTML = html; }
function announce(msg) {
  const a = $("announcer");
  a.textContent = "";
  setTimeout(() => { a.textContent = msg; }, 60);
}

// ---------- top bar & hero ----------
function renderTop() {
  setText("clock", S.clock.et);
  const stopped = S.controls.emergency_stop;
  const btn = $("stop-btn");
  btn.setAttribute("aria-pressed", String(stopped));
  btn.disabled = S.preview === "partial";
  btn.title = btn.disabled ? "Read-only view: the stop control connects to the gateway in Phase 7" : "";
  setText("stop-label", stopped ? "TRADING STOPPED · CLEAR…" : "STOP ALL NEW TRADING");
  $("hero-status").parentElement.querySelector(".live-dot").dataset.state = stopped ? "stopped" : "live";
  const scanning = S.scanner.length;
  setText("hero-status", stopped
    ? "All new trading stopped"
    : `Scanning ${scanning} markets · ${S.controls.authorized_today ? "live trading authorized" : "no live trading today"}`);
}

// ---------- account ----------
function renderAccount() {
  const a = S.account, c = S.controls;
  const chip = $("auth-chip");
  chip.className = "chip " + (c.emergency_stop ? "chip-bad" : c.authorized_today ? "chip-good" : "chip-warn");
  setText("auth-chip", c.emergency_stop ? "Emergency stop on"
    : c.authorized_today ? "Live trading authorized today" : "Live trading not authorized today");
  const rows = [
    ["Available cash", money(a.cash_c)], ["Portfolio value", money(a.portfolio_c)],
    ["Open exposure", money(a.exposure_c)], ["Purchases today", money(a.purchases_c)],
    ["Fees today", money(a.fees_c)], ["Realized P&L", money(a.realized_c, { sign: true }), a.realized_c],
    ["Unrealized P&L", money(a.unrealized_c, { sign: true }), a.unrealized_c],
    ["Remaining allowance", money(a.remaining_c)], ["Reserved for orders", money(a.reserved_c)],
  ];
  setHTML($("kpis"), rows.map(([k, v, n]) =>
    `<div class="kpi"><dt>${k}</dt><dd class="${n > 0 ? "pos" : n < 0 ? "neg" : ""}">${v}</dd></div>`).join(""));
}

function renderBudget() {
  const L = S.limits, sp = S.usage.spend_c, res = S.account.reserved_c;
  const total = L.daily_spend_c;
  const used = sp.kalshi + sp.stocks + sp.etfs;
  setText("budget-spent", money(used));
  setText("budget-of", `spent of ${money(total)} · ${money(Math.max(0, total - used - res))} left`);
  setText("budget-day", `Accounting day ${S.clock.accounting_day} (New York time)`);
  const segs = [["kalshi", "Kalshi", sp.kalshi], ["stocks", "Stocks", sp.stocks], ["etfs", "ETFs", sp.etfs], ["reserved", "Reserved", res]];
  const bar = $("budget-bar");
  bar.replaceChildren(...segs.filter(s => s[2] > 0).map(([k, , v]) => {
    const el = document.createElement("span");
    el.className = `seg-${k}`;
    el.style.width = `${(100 * v / total).toFixed(2)}%`;  // CSSOM is allowed by the CSP
    return el;
  }));
  setText("budget-bar-label", `Budget used: ${segs.map(([, n, v]) => `${n} ${money(v)}`).join(", ")}, of ${money(total)}.`);
  const cat = L.category_c;
  setHTML($("budget-legend"), segs.map(([k, n, v]) =>
    `<li><span class="swatch seg-${k}" aria-hidden="true"></span>${n}: ${money(v)}${cat[k] ? ` <span class="muted">(ceiling ${money(cat[k])})</span>` : ""}</li>`).join(""));
}

// ---------- limits & health ----------
function limitRow(name, used, max, fmt, note) {
  const ratio = max ? used / max : 0;
  return `<li class="limit-row"><span>${name}${note ? ` <span class="muted small">${note}</span>` : ""}</span>
    <span class="val">${fmt(used)} / ${fmt(max)}</span>
    <meter min="0" max="${max}" low="${max * 0.6}" high="${max * 0.85}" optimum="0" value="${used}"
      aria-label="${esc(name)}: ${esc(fmt(used))} of ${esc(fmt(max))} (${Math.round(ratio * 100)}%)"></meter></li>`;
}
function renderLimits() {
  const L = S.limits, U = S.usage, sp = U.spend_c;
  const m = (c) => money(c), n = (x) => String(x);
  setHTML($("limits"), [
    limitRow("Daily spending", sp.kalshi + sp.stocks + sp.etfs, L.daily_spend_c, m, "all modules"),
    limitRow("Daily realized loss", U.daily_loss_c, L.max_daily_loss_c, m),
    limitRow("Open exposure", S.account.exposure_c, L.max_market_c * L.max_positions, m),
    limitRow("Concurrent positions", U.positions, L.max_positions, n),
    limitRow("Open entry orders", U.open_orders, L.max_open_orders, n),
    limitRow("Consecutive losses", U.consec_losses, L.max_consec_losses, n),
  ].join("") + `<li class="limit-row"><span>Largest single purchase</span><span class="val">${money(L.max_order_c)}</span></li>
    <li class="limit-row"><span>Largest exposure per market</span><span class="val">${money(L.max_market_c)}</span></li>`);
}
function renderHealth() {
  const word = { ok: "Healthy", warn: "Attention", fail: "Failed" };
  setHTML($("health"), S.health.map(h =>
    `<li><span class="ico st-${h.status}">${svg(h.status)}</span>
      <span class="name">${esc(h.name)}<span class="det">${esc(h.detail)}</span></span>
      <span class="st st-${h.status}">${word[h.status]}</span></li>`).join(""));
}

// ---------- controls ----------
const CONTROLS = [
  { key: "master", label: "Master trading switch", hint: "Runs ATLAS's scanners and strategy loops." , key_: true },
  { key: "authorized_today", label: "Authorize today's trading budget", hint: "OFF by default. Expires at midnight New York time and is OFF again after any restart.", key_: true,
    confirmOn: { title: "Authorize today's trading budget?", body: "ATLAS may open real positions today, within all limits ($20 daily spend, $2 per purchase, $3 daily loss).", word: "AUTHORIZE" } },
  { key: "require_approval", label: "Require my approval for every live trade", hint: "ON by default. Each approval is tied to one exact order and expires after 60 seconds.", key_: true,
    confirmOff: { title: "Stop asking before each trade?", body: "ATLAS would place live orders without asking you first, still within every hard limit.", word: "AUTONOMOUS" } },
  { key: "module:kalshi", label: "Kalshi", hint: "15-minute crypto and other prediction markets." },
  { key: "module:esports", label: "E-Sports", hint: "Disabled until a reliable live game-state feed exists." },
  { key: "module:stocks", label: "Stocks", hint: "Long-only, liquid listed stocks via Alpaca." },
  { key: "module:etfs", label: "ETFs", hint: "Checked for overlap with your stock holdings." },
  { key: "paper", label: "Paper trading", hint: "Simulated orders, kept fully separate from real money." },
  { key: "shadow", label: "Shadow trading", hint: "Runs continuously on live data with simulated fills." },
];
function controlValue(key) {
  if (key.startsWith("module:")) return S.controls.modules[key.split(":")[1]];
  return S.controls[key];
}
function buildControls() {
  $("controls").innerHTML = CONTROLS.map((c, i) => `
    <div class="control${c.key_ ? " key" : ""}">
      <span class="lbl" id="ctl-l-${i}">${c.label}</span>
      <span class="switch-wrap">
        <span class="onoff" id="ctl-s-${i}" aria-hidden="true"></span>
        <button type="button" class="switch" role="switch" aria-checked="false" id="ctl-${i}"
          aria-labelledby="ctl-l-${i}" aria-describedby="ctl-h-${i}" data-key="${c.key}"></button>
      </span>
      <p class="hint" id="ctl-h-${i}">${c.hint}</p>
    </div>`).join("");
  CONTROLS.forEach((c, i) => $(`ctl-${i}`).addEventListener("click", () => onToggle(c, i)));
}
function renderControls() {
  CONTROLS.forEach((c, i) => {
    const on = !!controlValue(c.key);
    const b = $(`ctl-${i}`);
    b.setAttribute("aria-checked", String(on));
    setText(`ctl-s-${i}`, on ? "ON" : "OFF");
    b.disabled = S.preview === "partial" || (c.key === "authorized_today" && S.controls.emergency_stop && !on);
  });
}
async function onToggle(c, i) {
  const next = !controlValue(c.key);
  const conf = next ? c.confirmOn : c.confirmOff;
  if (conf && !(await confirmDialog(conf))) { $(`ctl-${i}`).focus(); return; }
  await send(c.key, next);
  $(`ctl-${i}`).focus();
}

// ---------- approval ----------
function renderApproval() {
  const a = S.approval, box = $("approval");
  const key = a ? a.market + a.qty + a.entry_c : "none";
  if (key === approvalShown) return;
  approvalShown = key;
  if (!a) {
    box.innerHTML = `<p class="empty">Nothing waiting. Trades that need your approval appear here, with every detail and a 60-second timer.</p>`;
    return;
  }
  approvalDeadline = Date.now() + a.expires_in * 1000;
  box.innerHTML = `
    <article class="approval" aria-labelledby="ap-title">
      <div class="approval-top">
        <h3 id="ap-title">Buy ${a.qty} × ${esc(a.side)} · ${esc(a.market)}</h3>
        ${a.example ? '<p class="chip chip-warn">Example only</p>' : ""}
      </div>
      <dl>
        <div><dt>Market</dt><dd>${esc(a.category)}</dd></div>
        <div><dt>Entry price</dt><dd>${a.entry_c}¢ each</dd></div>
        <div><dt>Purchase cost</dt><dd>${money(a.cost_c)}</dd></div>
        <div><dt>Fees</dt><dd>${money(a.fee_c)}</dd></div>
        <div><dt>Worst-case loss</dt><dd class="neg">${money(a.worst_c)}</dd></div>
        <div><dt>Estimated probability</dt><dd>${pct(a.prob_pct)}</dd></div>
        <div><dt>Fair value</dt><dd>${a.fair_pct.toFixed(1)}¢</dd></div>
        <div><dt>Expected value</dt><dd class="${a.ev_c > 0 ? "pos" : "neg"}">${money(a.ev_c, { sign: true })}</dd></div>
        <div><dt>Budget left after</dt><dd>${money(a.remaining_c - a.cost_c - a.fee_c)}</dd></div>
        <div><dt>Current exposure</dt><dd>${money(a.exposure_c)}</dd></div>
      </dl>
      <p class="reason"><strong>Why:</strong> ${esc(a.reason)}</p>
      <p class="reason"><strong>Exit plan:</strong> ${esc(a.exit)}</p>
      <div class="actions">
        <button type="button" class="btn btn-primary" id="ap-yes">Approve this exact order</button>
        <button type="button" class="btn btn-danger" id="ap-no">Reject</button>
        <span class="muted countdown" id="ap-timer"></span>
      </div>
    </article>`;
  $("ap-yes").addEventListener("click", () => send("approve_example", true));
  $("ap-no").addEventListener("click", () => send("reject_example", true));
}
function tickApproval() {
  const t = $("ap-timer");
  if (!t) return;
  const left = Math.max(0, Math.round((approvalDeadline - Date.now()) / 1000));
  t.textContent = left ? `Expires in ${left} s` : "Expired: a new approval would be needed";
}

// ---------- scanner ----------
function renderScanner() {
  if ($("scanner-note")) setText("scanner-note", S.scanner_note || "Sample rows (preview).");
  const verdictTxt = { no_trade: "No trade", shadow: "Shadow only", trade: "Trade" };
  const rows = S.scanner.filter(r => scannerCat === "all" || r.category === scannerCat);
  setHTML($("scanner"), rows.length ? rows.map(r => {
    const isPred = r.category === "kalshi" || r.category === "esports";
    const dash = "–";
    const price = r.price == null ? dash : isPred ? `${Number(r.price).toFixed(1)}¢` : money(Math.round(r.price * 100));
    const model = r.model_pct == null ? dash : `${Number(r.model_pct).toFixed(1)}%`;
    const spread = r.spread == null ? dash : isPred ? `${Number(r.spread).toFixed(1)}¢` : `$${Number(r.spread).toFixed(2)}`;
    return `<tr><td>${esc(r.market)}<span class="sub">${esc({ kalshi: "Kalshi", esports: "E-Sports", stocks: "Stock", etfs: "ETF" }[r.category])}</span></td>
      <td>${esc(r.side)}</td><td class="num">${price}</td><td class="num">${model}</td><td class="num">${spread}</td>
      <td class="num">${r.depth_usd == null ? "–" : "$" + Number(r.depth_usd).toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
      <td><span class="verdict v-${r.verdict}">${verdictTxt[r.verdict]}</span></td><td class="why">${esc(r.reason)}</td></tr>`;
  }).join("") : `<tr><td colspan="8" class="muted">No markets in this category.</td></tr>`);
}

// ---------- correlation ----------
function renderCorrelation() {
  const c = S.correlation;
  setHTML($("sectors"), c.sectors.map(s =>
    `<li><span>${esc(s.name)}</span><span class="val">${s.pct}%</span>
      <meter min="0" max="100" value="${s.pct}" aria-label="${esc(s.name)}: ${s.pct}% of exposure"></meter></li>`).join(""));
  setHTML($("groups"), c.groups.map(g =>
    `<li><strong>${esc(g.name)}</strong> <span class="muted small">(correlation ${g.corr.toFixed(2)})</span><br>
      <span class="small">${g.members.map(esc).join(", ")} · combined ${money(g.combined_c)} of ${money(g.limit_c)} limit</span></li>`).join(""));
  setHTML($("overlaps"), c.overlaps.map(o => `<li>${esc(o.a)} holds ${o.pct}% ${esc(o.b)}${o.b === "QQQ" ? " (index overlap)" : ""}</li>`).join("")
    + c.redundant.map(r => `<li><strong>Redundant:</strong> ${esc(r)}</li>`).join(""));
  setText("corr-window", `Correlation window: ${c.window}. Past correlation can break down in unusual markets.`);
}

// ---------- shadow ----------
function renderShadow() {
  const s = S.shadow;
  const pnlEl = $("shadow-sub");
  if (s.real) {
    setText("shadow-balance", money(s.pnl_c, { sign: true }));
    const state = s.running ? `updated ${s.age_s} s ago` : `NOT RUNNING (last update ${Math.round(s.age_s / 60)} min ago; start: python -m atlas.shadow --data &lt;folder&gt;)`;
    pnlEl.innerHTML = `Simulated P&L of <strong>${esc(s.account)}</strong>, ${state}. Live public data, simulated money; resting orders fill only when real trades sell through them.`;
  } else {
    setText("shadow-balance", money(s.balance_c));
    pnlEl.innerHTML = `Simulated P&L <strong class="${s.pnl_c >= 0 ? "pos" : "neg"}">${money(s.pnl_c, { sign: true })}</strong> from a ${money(s.start_c)} simulated start. Not real money.`;
  }
  const pts = s.curve_c.length > 1 ? s.curve_c : [s.curve_c[0] || 0, s.curve_c[0] || 0];
  const lo = Math.min(...pts), hi = Math.max(...pts), span = Math.max(1, hi - lo);
  const xy = pts.map((v, i) => [(i / (pts.length - 1)) * 300, 72 - ((v - lo) / span) * 64]);
  const d = xy.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  setHTML($("spark"), `<defs><linearGradient id="sg" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#5fd4ff" stop-opacity=".35"/><stop offset="1" stop-color="#5fd4ff" stop-opacity="0"/></linearGradient></defs>
    <path d="${d} L300 80 L0 80 Z" fill="url(#sg)"/><path d="${d}" fill="none" stroke="#5fd4ff" stroke-width="2.2" vector-effect="non-scaling-stroke"/>`);
  setText("spark-cap", s.real
    ? `Cumulative simulated P&L over ${s.trades} settled trades: low ${money(lo)}, high ${money(hi)}.`
    : `Simulated balance over the last ${pts.length} trades: low ${money(lo)}, high ${money(hi)}, now ${money(s.balance_c)}.`);
  const pct = (v) => v === null || v === undefined ? "–" : `${v}%`;
  $("shadow-accounts").hidden = $("shadow-accounts-heading").hidden = !s.accounts;
  if (s.accounts) setHTML($("shadow-accounts"), s.accounts.map(a => {
    const strat = a.name.split("/")[0].replace("crypto_", "").replace(/_/g, " ");
    const title = a.type === "maker" ? "Resting bids" : "Buy at the ask";
    const prof = a.name.endsWith("/as_live") ? "real limits" : "research, no daily stops";
    const notes = [a.open ? `${a.open} order resting now` : "",
      a.missed ? `unfilled orders would have won ${pct(a.missed_win_rate)} of ${a.missed}` : "",
      a.pnl_no_maker_fee ? `${esc(a.pnl_no_maker_fee)} if makers pay no fee` : "",
      a.rejected ? `${a.rejected} refused by limits` : ""].filter(Boolean).join(" · ");
    return `<li><p class="sim-title"><strong>${title}</strong> <span class="muted small">${esc(strat)} · ${prof}</span></p>
      <dl class="sim-stats"><div><dt>Orders</dt><dd>${a.orders}</dd></div><div><dt>Filled</dt><dd>${pct(a.fill_rate)}</dd></div>
      <div><dt>Settled</dt><dd>${a.settled}</dd></div><div><dt>Won</dt><dd>${pct(a.win_rate)}</dd></div>
      <div><dt>P&amp;L</dt><dd>${esc(a.pnl)}</dd></div></dl>${notes ? `<p class="muted small">${notes}</p>` : ""}</li>`;
  }).join(""));
  setHTML($("shadow-kpis"), [["Trades", s.trades], ["Won", s.wins], ["Lost", s.losses],
    ["Win rate", s.trades ? `${Math.round(100 * s.wins / s.trades)}%` : "–"], ["Sim. fees", money(s.fees_c)], ["Max drawdown", money(s.max_dd_c)]]
    .map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join(""));
  setHTML($("shadow-pos"), s.positions.length ? s.positions.map(p =>
    p.mark_c === null || p.mark_c === undefined
      ? `<li>${p.qty} × ${esc(p.side)} · ${esc(p.market)} · cost ${p.entry_c}¢ each, held to settlement (sim.)</li>`
      : `<li>${p.qty} × ${esc(p.side)} · ${esc(p.market)} · entry ${p.entry_c}¢, now ${p.mark_c}¢ <span class="${p.mark_c >= p.entry_c ? "pos" : "neg"}">(${money((p.mark_c - p.entry_c) * p.qty, { sign: true })} sim.)</span></li>`).join("")
    : `<li class="muted">No simulated positions.</li>`);
  setText("shadow-vs", `Shadow vs live: ${s.vs_live}`);
}

// ---------- positions, activity, summary ----------
function renderPositions() {
  setHTML($("positions"), S.positions.length ? `<ul class="plain">${S.positions.map(p =>
    `<li>${p.qty} × ${esc(String(p.side).toUpperCase())} · ${esc(p.market)} · cost basis ${money(p.basis_c)}</li>`).join("")}</ul>` :
    `<p class="empty">No open real positions. ATLAS will only open one after you authorize today's budget${S.controls.require_approval ? " and approve the trade" : ""}.</p>`);
}
let activityAll = false;
const ACTIVITY_SHORT = 12;
function renderActivity() {
  const all = S.feed.filter(e => activityKind === "all" || e.kind === activityKind);
  const rows = activityAll ? all : all.slice(0, ACTIVITY_SHORT);
  const more = $("activity-more");
  more.hidden = all.length <= ACTIVITY_SHORT;
  more.setAttribute("aria-expanded", String(activityAll));
  setText("activity-more", activityAll ? "Show fewer" : `Show all ${all.length}`);
  setHTML($("activity"), rows.length ? rows.map(e =>
    `<tr><td>${clockTime(e.t)}</td><td><span class="k-${e.kind}">${esc(KIND_LABEL[e.kind] || e.kind)}</span>: ${esc(e.title)}</td><td class="why">${esc(e.detail)}</td></tr>`).join("")
    : `<tr><td colspan="3" class="muted">Nothing in this category yet.</td></tr>`);
}
function renderSummary() {
  const s = S.summary;
  setHTML($("summary"), [
    ["Purchases", money(s.purchases_c)], ["Fees", money(s.fees_c)],
    ["Realized P&L", money(s.realized_c, { sign: true })], ["Unrealized P&L", money(s.unrealized_c, { sign: true })],
    ["Trades (won / lost)", `${s.trades} (${s.wins} / ${s.losses})`], ["Safety interventions", s.interventions],
    ["Best strategy", esc(s.best)], ["Worst strategy", esc(s.worst)],
  ].map(([k, v]) => `<div class="kpi"><dt>${k}</dt><dd${String(v).length > 16 ? ' class="long"' : ""}>${v}</dd></div>`).join(""));
}

// ---------- feed ----------
function renderFeed() {
  const list = $("feed");
  const items = S.feed.slice(0, 25);
  const fresh = [];
  list.innerHTML = items.map(e => {
    const isNew = !seenFeed.has(e.id) && !firstLoad;
    if (!seenFeed.has(e.id)) { seenFeed.add(e.id); if (!firstLoad) fresh.push(e); }
    return `<li class="feed-item${isNew ? " new" : ""}"><span class="ico k-${e.kind}">${svg(e.kind)}</span>
      <div><div class="ttl">${esc(e.title)}</div><div class="det">${esc(e.detail)}</div>
      <time datetime="${new Date(e.t * 1000).toISOString()}">${timeAgo(e.t)}</time></div></li>`;
  }).join("");
  const important = fresh.filter(e => e.important);
  if (important.length) announce(important.map(e => `${e.title}. ${e.detail}`).join(" "));
}

// ---------- dialog ----------
function confirmDialog({ title, body, word }) {
  return new Promise((resolve) => {
    const dlg = $("confirm"), input = $("confirm-input"), err = $("confirm-error");
    setText("confirm-title", title);
    setText("confirm-body", body);
    $("confirm-typed").hidden = !word;
    setText("confirm-word", word || "");
    input.value = ""; input.removeAttribute("aria-invalid"); err.hidden = true;
    const done = (v) => { dlg.close(); cleanup(); resolve(v); };
    const onSubmit = (e) => {
      e.preventDefault();
      if (word && input.value.trim().toUpperCase() !== word) {
        input.setAttribute("aria-invalid", "true");
        err.textContent = `Type ${word} exactly to confirm.`; err.hidden = false; input.focus();
        return;
      }
      done(true);
    };
    const onCancel = () => done(false);
    const onEsc = (e) => { e.preventDefault(); done(false); };
    function cleanup() {
      $("confirm-form").removeEventListener("submit", onSubmit);
      $("confirm-cancel").removeEventListener("click", onCancel);
      dlg.removeEventListener("cancel", onEsc);
    }
    $("confirm-form").addEventListener("submit", onSubmit);
    $("confirm-cancel").addEventListener("click", onCancel);
    dlg.addEventListener("cancel", onEsc);
    dlg.showModal();
    (word ? input : $("confirm-cancel")).focus();
  });
}

// ---------- plumbing ----------
async function send(action, value) {
  try {
    const r = await fetch("/api/control", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, value }),
    });
    const d = await r.json();
    announce(d.message || (r.ok ? "Done." : "That didn't work."));
    await refresh();
  } catch {
    announce("Could not reach the ATLAS dashboard server.");
  }
}

async function onStop() {
  if (!S) return;
  if (!S.controls.emergency_stop) { await send("emergency_stop", true); $("stop-btn").focus(); return; }
  const ok = await confirmDialog({
    title: "Clear the emergency stop?",
    body: "New trading stays blocked until you also authorize today's budget again.",
    word: "RESUME",
  });
  if (ok) await send("emergency_stop", false);
  $("stop-btn").focus();
}

function wireFilters(groupId, attr, setter) {
  $(groupId).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-" + attr + "]");
    if (!b) return;
    $(groupId).querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
    setter(b.dataset[attr]);
  });
}

let failures = 0;
async function refresh() {
  try {
    const r = await fetch("/api/state", { cache: "no-store" });
    S = await r.json();
    failures = 0;
    const partial = S.preview === "partial";
    setText("env-chip", partial ? "Safety data: real · markets: sample" : S.preview ? "Preview · sample data" : "Connected");
    $("preview-note").innerHTML = S.real_error
      ? `<strong>Problem:</strong> ${esc(S.real_error)}`
      : partial
        ? "<strong>Read-only view of your ATLAS data.</strong> Budget, limits, system health, authorization status, positions and activity are real. The market scanner and shadow trading panels show live data when their programs are running; the correlation panel is still sample data. Controls are read-only until the dashboard connects to the execution gateway (Phase 7)."
        : "<strong>Preview.</strong> Everything below is sample data. This dashboard is not connected to Kalshi or Alpaca and cannot place orders.";
    renderTop(); renderAccount(); renderBudget(); renderLimits(); renderHealth(); renderControls();
    renderApproval(); renderScanner(); renderCorrelation(); renderShadow(); renderPositions();
    renderActivity(); renderSummary(); renderFeed();
    firstLoad = false;
  } catch {
    failures += 1;
    setText("hero-status", "Can't reach the ATLAS dashboard server");
    if (failures === 2) announce("Lost connection to the ATLAS dashboard server.");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  buildControls();
  $("stop-btn").addEventListener("click", onStop);
  wireFilters("scanner-filters", "cat", (v) => { scannerCat = v; if (S) renderScanner(); });
  wireFilters("activity-filters", "kind", (v) => { activityKind = v; if (S) renderActivity(); });
  $("activity-more").addEventListener("click", () => { activityAll = !activityAll; if (S) renderActivity(); });
  refresh();
  setInterval(refresh, POLL_MS);
  setInterval(tickApproval, 1000);
});
