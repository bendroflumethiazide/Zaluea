"use strict";

const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);

const ICONS = {
  check: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  cross: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>',
  dots:  '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>',
  pause: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="currentColor"><rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/></svg>',
  alert: '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 7v6M12 17h.01"/></svg>',
  eye:   '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
};

let last = { status: null, tradeCount: null };
let settingsFilled = false;
let formDirty = false;

// ---------- helpers ----------
function money(x) {
  if (x === null || x === undefined || Number.isNaN(x)) return "–";
  const s = Math.abs(x).toLocaleString(undefined, { style: "currency", currency: "USD" });
  if (Math.abs(x) < 0.005) return s;
  return (x > 0 ? "+" : "-") + s;
}
function cents(p) {
  return p === null || p === undefined ? "–" : `${(p * 100).toFixed(1)}¢`;
}
function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function duration(sec) {
  sec = Math.max(0, Math.round(sec));
  const m = Math.floor(sec / 60), s = sec % 60;
  return m ? `${m} min ${s} s` : `${s} s`;
}
function announce(msg) {
  const el = $("announcer");
  el.textContent = "";
  // Small delay so repeated messages are still read out.
  setTimeout(() => { el.textContent = msg; }, 50);
}
function setText(id, text) {
  const el = $(id);
  if (el.textContent !== text) el.textContent = text;
}

// ---------- status ----------
function describeStatus(st) {
  if (!st.updated) {
    return { tone: "bad", icon: "alert", title: "The bot isn't running",
             detail: "Start it from a terminal with: python bot.py" };
  }
  if (!st.running) {
    return { tone: "bad", icon: "alert", title: "The bot has stopped responding",
             detail: `Last heard from ${clock(st.updated)}. Start it again with: python bot.py` };
  }
  if (st.position) {
    return { tone: "good", icon: "check", title: "Holding a trade",
             detail: (st.check && st.check.note) || "Waiting for this window to finish." };
  }
  if (st.stopped_by_user) {
    return { tone: "warn", icon: "pause", title: "Paused",
             detail: "No new trades until you resume." };
  }
  if (st.paused === "Paused by you") {
    // You just resumed; the bot notices within about 5 seconds.
    return { tone: "neutral", icon: "eye", title: "Resuming…",
             detail: "The bot will pick this up within a few seconds." };
  }
  if (st.paused) {
    return { tone: "warn", icon: "pause", title: "Paused for today", detail: st.paused };
  }
  if (st.error) {
    return { tone: "warn", icon: "alert", title: "Watching, with a connection problem",
             detail: "The bot will keep retrying. Details: " + st.error };
  }
  const c = st.check || {};
  if (c.stage === "checking") {
    return { tone: "good", icon: "eye", title: "Looking for a safe trade",
             detail: "Safety checks are running for this window." };
  }
  return { tone: "neutral", icon: "eye", title: "Watching the market",
           detail: c.note || "Waiting for the next window." };
}

function renderStatus(st) {
  const s = describeStatus(st);
  const icon = $("status-icon");
  icon.dataset.tone = s.tone;
  icon.innerHTML = ICONS[s.icon];
  setText("status-title", s.title);
  setText("status-detail", s.detail);
  if (last.status !== null && last.status !== s.title) announce(`Status: ${s.title}. ${s.detail}`);
  last.status = s.title;

  const btn = $("toggle-btn");
  btn.disabled = false;
  setText("toggle-btn", st.stopped_by_user ? "Resume trading" : "Pause trading");
  btn.dataset.action = st.stopped_by_user ? "resume" : "pause";

  const badge = $("mode-badge");
  const mode = st.mode || "unknown";
  badge.dataset.mode = mode;
  setText("mode-badge", {
    paper: "Practice mode: no real money",
    demo: "Demo exchange: fake money",
    live: "LIVE: real money",
    unknown: "Mode unknown",
  }[mode]);

  setText("updated", st.updated ? `Last update from the bot: ${clock(st.updated)}.` : "");
}

// ---------- today ----------
function renderToday(st) {
  const t = st.today || {};
  const pnl = $("stat-pnl");
  setText("stat-pnl", money(t.pnl ?? 0));
  pnl.className = (t.pnl ?? 0) > 0.004 ? "pos" : (t.pnl ?? 0) < -0.004 ? "neg" : "";
  setText("stat-trades", String(t.trades ?? 0));
  const cap = st.settings ? st.settings.max_daily_spend : null;
  setText("stat-spent", cap ? `${money(t.spent ?? 0).replace("+", "")} of ${money(cap).replace("+", "")}` : "–");
  setText("stat-wins", String(t.wins ?? 0));
  setText("stat-losses", String(t.losses ?? 0));
}

// ---------- market ----------
function renderMarket(st) {
  const m = st.market;
  const has = m && m.ticker && st.running;
  $("market").hidden = !has;
  $("market-empty").hidden = !!has;
  if (!has) return;
  setText("mkt-title", `Will BTC be up at ${clock(m.close_ts)}? (window ${clock(m.open_ts)} to ${clock(m.close_ts)})`);
  setText("mkt-strike", m.strike ? m.strike.toLocaleString(undefined, { style: "currency", currency: "USD" }) : "–");
  setText("mkt-yes", cents(m.yes_ask));
  setText("mkt-no", m.yes_bid !== null && m.yes_bid !== undefined ? cents(1 - m.yes_bid) : "–");
  const left = m.close_ts - st.now;
  setText("mkt-left", duration(left));
  const elapsed = Math.min(15, Math.max(0, (st.now - m.open_ts) / 60));
  const bar = $("mkt-progress");
  bar.value = elapsed;
  bar.setAttribute("aria-valuetext", `${Math.floor(elapsed)} of 15 minutes done`);
}

// ---------- safety checks ----------
function renderChecks(st) {
  const c = st.check || {};
  const list = $("checks");
  let note = "";
  let items = [];
  if (!st.running) {
    note = "Checks appear while the bot is running.";
  } else if (st.position || c.stage === "holding") {
    note = "Checks are done for this window: the bot is holding a trade and watching its stop-loss.";
  } else if (c.stage === "checking" && c.guards) {
    const allOk = c.guards.every((g) => g.ok === true);
    note = allOk ? "All checks passed." :
      `Checking the favorite, ${(c.side || "").toUpperCase() === "YES" ? "Up (YES)" : "Down (NO)"}, at minute ${Math.floor(c.minute)}.`;
    items = c.guards;
  } else {
    note = c.note || "Checks run in minutes 10 to 14 of each 15-minute window.";
  }
  setText("checks-note", note);

  const html = items.map((g) => {
    const state = g.ok === true ? "pass" : g.ok === false ? "fail" : "wait";
    const label = { pass: "Passed", fail: "Not met", wait: "Checking" }[state];
    const icon = { pass: ICONS.check, fail: ICONS.cross, wait: ICONS.dots }[state];
    return `<li class="check" data-state="${state}">${icon}<div>
      <span class="check-name">${escapeHtml(g.name)}</span>:
      <span class="check-state">${label}</span>
      <div class="muted small">${escapeHtml(g.detail || "")}</div></div></li>`;
  }).join("");
  if (list.innerHTML !== html) list.innerHTML = html;
}

// ---------- trades ----------
function renderTrades(st) {
  const trades = st.trades || [];
  $("trades-wrap").hidden = trades.length === 0;
  $("trades-empty").hidden = trades.length > 0;
  const outcome = { won: "Won", lost: "Lost", stop: "Sold early (stop-loss)" };
  const html = trades.map((t) => `<tr>
      <td>${clock(t.ts)}${t.mode !== "paper" ? ` <span class="small">(${escapeHtml(t.mode)})</span>` : ""}</td>
      <td>${t.side === "yes" ? "Up (YES)" : "Down (NO)"}</td>
      <td class="num">${t.count}</td>
      <td class="num">${cents(t.price)}</td>
      <td>${outcome[t.outcome] || t.outcome}</td>
      <td class="num ${t.pnl > 0 ? "pos" : "neg"}">${money(t.pnl)}</td>
    </tr>`).join("");
  const body = $("trades");
  if (body.innerHTML !== html) body.innerHTML = html;

  if (last.tradeCount !== null && trades.length > last.tradeCount && trades[0]) {
    const t = trades[0];
    announce(`Trade finished: ${(outcome[t.outcome] || t.outcome).toLowerCase()}, ${money(t.pnl)}.`);
  }
  last.tradeCount = trades.length;
}

// ---------- settings ----------
function fillSettings(st) {
  if (!st.settings || formDirty) return;
  if (settingsFilled && document.activeElement && document.activeElement.form === $("settings-form")) return;
  for (const k of Object.keys(FIELD_RULES)) {
    const el = $(k);
    if (el.value === "" || !settingsFilled) el.value = st.settings[k];
  }
  settingsFilled = true;
}

const FIELD_LABELS = {
  stake: "Amount per trade",
  max_daily_spend: "Daily spending limit",
  max_daily_loss: "Daily loss limit",
  max_trades: "Trades per day",
};
const FIELD_RULES = {
  stake: { min: 1, max: 25, whole: false },
  max_daily_spend: { min: 1, max: 500, whole: false },
  max_daily_loss: { min: 1, max: 100, whole: false },
  max_trades: { min: 1, max: 50, whole: true },
};

function validate(values) {
  const errors = {};
  for (const [k, r] of Object.entries(FIELD_RULES)) {
    const raw = values[k];
    const v = Number(raw);
    if (raw === "" || Number.isNaN(v)) errors[k] = `${FIELD_LABELS[k]} must be a number.`;
    else if (r.whole && !Number.isInteger(v)) errors[k] = `${FIELD_LABELS[k]} must be a whole number.`;
    else if (v < r.min || v > r.max) errors[k] = `${FIELD_LABELS[k]} must be between ${r.min} and ${r.max}.`;
  }
  return errors;
}

function showErrors(errors) {
  const summary = $("form-errors");
  const list = $("form-error-list");
  list.innerHTML = "";
  for (const k of Object.keys(FIELD_RULES)) {
    const input = $(k);
    const msg = $(`${k}-error`);
    if (errors[k]) {
      input.setAttribute("aria-invalid", "true");
      msg.textContent = errors[k];
      msg.hidden = false;
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = `#${k}`;
      a.textContent = errors[k];
      a.addEventListener("click", (e) => { e.preventDefault(); input.focus(); });
      li.appendChild(a);
      list.appendChild(li);
    } else {
      input.removeAttribute("aria-invalid");
      msg.hidden = true;
      msg.textContent = "";
    }
  }
  const any = Object.keys(errors).length > 0;
  summary.hidden = !any;
  if (any) summary.focus();
}

async function onSave(e) {
  e.preventDefault();
  const values = {};
  for (const k of Object.keys(FIELD_RULES)) values[k] = $(k).value.trim();
  const errors = validate(values);
  showErrors(errors);
  setText("save-msg", "");
  if (Object.keys(errors).length) return;
  try {
    const res = await post("/api/settings", values);
    const data = await res.json();
    if (!res.ok) { showErrors(data.errors || {}); return; }
    formDirty = false;
    setText("save-msg", "Settings saved. The bot uses them within a few seconds.");
    announce("Settings saved.");
  } catch {
    setText("save-msg", "Could not reach the dashboard server. Is it still running?");
    announce("Could not save settings.");
  }
}

// ---------- pause / resume ----------
async function onToggle() {
  const btn = $("toggle-btn");
  const action = btn.dataset.action === "resume" ? "resume" : "pause";
  btn.disabled = true;
  try {
    const res = await post(`/api/${action}`, {});
    if (!res.ok) throw new Error();
    announce(action === "pause" ? "Trading paused." : "Trading resumed.");
    await refresh();
  } catch {
    announce("Could not change the pause setting.");
  } finally {
    btn.disabled = false;
    btn.focus();
  }
}

// ---------- plumbing ----------
function post(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

let failures = 0;
async function refresh() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    const st = await res.json();
    failures = 0;
    renderStatus(st);
    renderToday(st);
    renderMarket(st);
    renderChecks(st);
    renderTrades(st);
    fillSettings(st);
  } catch {
    failures += 1;
    if (failures === 2) announce("Lost connection to the dashboard server.");
    setText("status-title", "Can't reach the dashboard server");
    setText("status-detail", "Start it again with: python dashboard.py");
    $("status-icon").dataset.tone = "bad";
    $("status-icon").innerHTML = ICONS.alert;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("toggle-btn").addEventListener("click", onToggle);
  $("settings-form").addEventListener("submit", onSave);
  $("settings-form").addEventListener("input", () => { formDirty = true; });
  refresh();
  setInterval(refresh, POLL_MS);
});
