// app.js — renders the screener dashboard from data/*.json. (no build step)
//
// SECURITY: this file is public. No API keys, ever. It only reads the
// pipeline-generated data/latest.json + data/track_record.json.

const REPO = "OkPeach/stock-screen";   // backs the add/remove issue form
const VCLASS = { "WATCH-BUY": "buy", "NEUTRAL": "neutral", "WATCH-SELL": "sell" };

const state = {
  tickers: [],
  search: "",
  verdict: "ALL",
  sort: "composite-desc",
};
let drawerChart = null;

/* ---------------- data load ---------------- */

async function load() {
  const status = document.getElementById("status");
  try {
    const resp = await fetch("data/latest.json", { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    state.tickers = Array.isArray(data.tickers) ? data.tickers : [];
    setUpdated(data);
    renderTape();
    state.briefing = data.briefing || null;
    state.changes = Array.isArray(data.changes) ? data.changes : [];
    renderBrief(data.briefing);
    renderChanges(data.changes);
    status.style.display = "none";
    apply();
  } catch (err) {
    status.textContent = `No data yet — run the pipeline to generate data/latest.json. (${err.message})`;
  }
  loadTrackRecord();
}

async function loadTrackRecord() {
  try {
    const resp = await fetch("data/track_record.json", { cache: "no-store" });
    if (resp.ok) renderHero(await resp.json());
    else renderHero(null);
  } catch { renderHero(null); }
}

/* ---------------- helpers ---------------- */

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const num = (v) => (typeof v === "number" ? v : null);
const fmtPct = (v) => (typeof v === "number" ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : "—");
const fmtScore = (v) => (typeof v === "number" ? v.toFixed(2) : "—");
const fmtMoney = (v) => (typeof v === "number" ? "$" + v.toFixed(2) : "—");
const pct = (v) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`;

// Deterministic gradient monogram per ticker (no external logo service).
function monogram(sym) {
  let h = 0;
  for (const ch of sym || "?") h = (h * 31 + ch.charCodeAt(0)) % 360;
  const h2 = (h + 50) % 360;
  return `<span class="mono-logo" style="background:linear-gradient(135deg,hsl(${h} 70% 48%),hsl(${h2} 75% 38%))" aria-hidden="true">${esc((sym || "?").slice(0, 2))}</span>`;
}

// Daily brief: plain-English summary + buy / sell chips (no neutrals).
function renderBrief(brief) {
  const el = document.getElementById("brief");
  if (!brief || !brief.text) { el.hidden = true; return; }
  const chips = (list, cls) => (list || []).map((p) =>
    `<button class="brief-chip ${cls}" data-sym="${esc(p.symbol)}" title="${esc(p.line || "")}">${esc(p.symbol)}</button>`).join("");
  const buys = brief.buy && brief.buy.length
    ? `<div class="brief-row"><span class="brief-tag up">▲ Buy</span>${chips(brief.buy, "up")}</div>` : "";
  const sells = brief.sell && brief.sell.length
    ? `<div class="brief-row"><span class="brief-tag down">▼ Avoid / trim</span>${chips(brief.sell, "down")}</div>` : "";
  el.innerHTML = `
    <div class="brief-head"><span class="brief-icon">📋</span><h3>Today's brief</h3></div>
    <p class="brief-text">${esc(brief.text)}</p>
    ${buys}${sells}`;
  el.hidden = false;
}

// Scrolling ticker tape (duplicated content for a seamless loop).
function renderTape() {
  const tape = document.getElementById("tape");
  const track = document.getElementById("tape-track");
  const live = state.tickers.filter((t) => !t.error && typeof t.price === "number");
  if (live.length < 2) { tape.hidden = true; return; }
  const item = (t) => {
    const cls = (t.change_pct || 0) >= 0 ? "up" : "down";
    return `<span class="tape-item" data-sym="${esc(t.symbol)}"><span class="ts">${esc(t.symbol)}</span>` +
      `<span class="tp tabular">${fmtMoney(t.price)}</span>` +
      `<span class="tc ${cls} tabular">${fmtPct(t.change_pct)}</span></span>`;
  };
  const half = live.map(item).join("");
  track.innerHTML = half + half;   // two copies -> translateX(-50%) loops cleanly
  tape.hidden = false;
}

// Count numbers up on first paint (subtle, respects reduced motion).
function countUp(el, target, ms = 700) {
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) { el.textContent = target; return; }
  const t0 = performance.now();
  const tick = (now) => {
    const p = Math.min(1, (now - t0) / ms);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function setUpdated(data) {
  const el = document.getElementById("updated");
  if (!data.generated_at) return;
  const when = new Date(data.generated_at);
  el.textContent = `Updated ${when.toLocaleString()}${data.sample ? " · sample" : ""}`;
}

// Tiny inline SVG price line (cards + fundamentals rows).
function sparklineSvg(series, opts = {}) {
  if (!Array.isArray(series) || series.length < 2) return "";
  const W = opts.w || 84, H = opts.h || 30, n = series.length;
  let lo = Math.min(...series), hi = Math.max(...series);
  if (lo === hi) { lo -= 1; hi += 1; }
  const x = (i) => (i * W) / (n - 1);
  const y = (v) => 2 + (1 - (v - lo) / (hi - lo)) * (H - 4);
  const pts = series.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`).join(" ");
  const up = series[n - 1] >= series[0];
  const col = up ? "var(--up)" : "var(--down)";
  return `<svg viewBox="0 0 ${W} ${H}" class="spark" preserveAspectRatio="none" aria-hidden="true">
    <polyline points="${pts}" fill="none" stroke="${col}" stroke-width="1.5"/></svg>`;
}

function scoreBar(letter, v, naLabel) {
  if (naLabel) {   // dimension unavailable this run — don't fake a 0
    return `<div class="bar-row"><span>${letter}</span>
      <div class="bar-track"></div><span class="bar-val na">${esc(naLabel)}</span></div>`;
  }
  const val = num(v) ?? 0;
  const w = Math.min(50, Math.abs(val) * 50);
  const side = val >= 0 ? "pos" : "neg";
  const style = val >= 0 ? `left:50%;width:${w}%` : `right:50%;left:auto;width:${w}%`;
  return `<div class="bar-row"><span>${letter}</span>
    <div class="bar-track"><div class="bar-fill ${side}" style="${style}"></div></div>
    <span class="bar-val">${fmtScore(v)}</span></div>`;
}

// Which dimensions actually produced a signal this run (for honest "n/a" labels).
function dimStatus(t) {
  const d = t.details || {};
  const tech = d.technicals && Object.keys(d.technicals).length ? null : "n/a";
  const sent = d.sentiment || {};
  let s = null;
  if (!sent.source || sent.source === "none") s = "n/a";
  else if (sent.source === "vader_headlines" && sent.deviation === undefined) s = "building";
  return { tech, sent: s };
}

function earningsDays(dateStr) {
  if (!dateStr) return null;
  const d = Math.ceil((new Date(dateStr + "T00:00:00Z") - Date.now()) / 86400000);
  return (isNaN(d) || d < 0 || d > 120) ? null : d;
}

/* ---------------- hero ---------------- */

function renderHero(track) {
  const el = document.getElementById("hero");
  const live = state.tickers.filter((t) => !t.error);
  if (!live.length) { el.hidden = true; return; }
  const counts = { "WATCH-BUY": 0, "NEUTRAL": 0, "WATCH-SELL": 0 };
  live.forEach((t) => { if (counts[t.verdict] != null) counts[t.verdict]++; });
  const total = live.length;
  const seg = (v, cls) => `<div class="dist-seg ${cls}" style="width:${(counts[v] / total) * 100}%"></div>`;

  el.innerHTML = `
    <div class="hero-card">
      <h3>${total} stocks screened</h3>
      <div class="dist-bar">${seg("WATCH-BUY", "buy")}${seg("NEUTRAL", "neutral")}${seg("WATCH-SELL", "sell")}</div>
      <div class="dist-legend">
        <div><span class="dot buy"></span>Watch-Buy<br><span class="n" data-count="${counts["WATCH-BUY"]}">0</span></div>
        <div><span class="dot neutral"></span>Neutral<br><span class="n" data-count="${counts["NEUTRAL"]}">0</span></div>
        <div><span class="dot sell"></span>Watch-Sell<br><span class="n" data-count="${counts["WATCH-SELL"]}">0</span></div>
      </div>
    </div>
    <div class="hero-card"><h3>Track record</h3>${trackHtml(track)}</div>`;
  el.hidden = false;
  el.querySelectorAll(".n[data-count]").forEach((n) => countUp(n, Number(n.dataset.count)));
}

function trackHtml(track) {
  if (!track || !track.horizons) return `<p class="tr-muted">Building — grades verdicts as horizons elapse.</p>`;
  const rows = [];
  for (const h of track.horizons) {
    const hz = (track.by_horizon || {})[String(h)];
    if (!hz) continue;
    const base = hz.baseline_avg_return;
    const chips = [];
    for (const v of ["WATCH-BUY", "WATCH-SELL"]) {
      const s = (hz.verdicts || {})[v];
      if (!s || !s.n) continue;
      const cls = v === "WATCH-BUY" ? "buy" : "sell";
      const lbl = v === "WATCH-BUY" ? "Buy" : "Sell";
      if (!s.confident) {
        chips.push(`<span class="tr-chip ${cls}">${lbl}: building (n=${s.n})</span>`);
      } else {
        const good = s.hit_rate >= 0.5;
        chips.push(`<span class="tr-chip ${cls}" title="avg ${pct(s.avg_return)} vs basket ${base != null ? pct(base) : "—"}">${lbl}: <span class="${good ? "tr-good" : "tr-bad"}">${Math.round(s.hit_rate * 100)}% hit</span> · ${pct(s.avg_return)} · n=${s.n}</span>`);
      }
    }
    if (chips.length) rows.push(`<div class="tr-line"><span class="tr-h">${h}d</span>${chips.join("")}</div>`);
  }
  if (!rows.length) return `<p class="tr-muted">Building — grades verdicts as horizons elapse.</p>`;
  return `<div class="tr-headline">${rows.join("")}<span class="tr-muted">forward return after verdict, vs holding the whole watchlist.</span></div>`;
}

/* ---------------- changes strip ---------------- */

function renderChanges(list) {
  const el = document.getElementById("changes");
  if (!Array.isArray(list)) { el.hidden = true; return; }
  if (!list.length) {
    el.innerHTML = `<span class="ch-title">Since last run</span><span class="tr-muted">no changes</span>`;
    el.hidden = false; return;
  }
  const chip = (c) => {
    let cls = c.type;
    if (c.type === "verdict") cls += c.to === "WATCH-BUY" ? " up" : c.to === "WATCH-SELL" ? " down" : "";
    if (c.type === "mover") cls += c.text.includes("+") ? " up" : " down";
    return `<button class="ch-chip ${cls}" data-sym="${esc(c.symbol)}">${esc(c.text)}</button>`;
  };
  el.innerHTML = `<span class="ch-title">Since last run</span>${list.map(chip).join("")}`;
  el.hidden = false;
}

/* ---------------- filter + sort + cards ---------------- */

function apply() {
  const q = state.search.trim().toUpperCase();
  let rows = state.tickers.filter((t) => {
    if (q && !(t.symbol || "").toUpperCase().includes(q)) return false;
    if (state.verdict !== "ALL" && t.verdict !== state.verdict) return false;
    return true;
  });
  const n = (v) => (typeof v === "number" ? v : -Infinity);
  const gaps = (t) => (t.error ? 2 : (t.missing && t.missing.length ? 1 : 0));
  const cmp = {
    "composite-desc": (a, b) => n(b.composite) - n(a.composite),
    "composite-asc": (a, b) => n(a.composite) - n(b.composite),
    "symbol-asc": (a, b) => (a.symbol || "").localeCompare(b.symbol || ""),
    "change-desc": (a, b) => n(b.change_pct) - n(a.change_pct),
    "change-asc": (a, b) => n(a.change_pct) - n(b.change_pct),
    // Tickers missing data (or errored) bubble to the front so gaps are obvious.
    "issues-first": (a, b) => gaps(b) - gaps(a) || (a.symbol || "").localeCompare(b.symbol || ""),
  }[state.sort];
  rows.sort(cmp);
  renderCards(rows);
  renderGapsNote();
}

// Flag how many tickers are missing data; one click brings them to the front.
function renderGapsNote() {
  const el = document.getElementById("gaps-note");
  const bad = state.tickers.filter((t) => t.error || (t.missing && t.missing.length));
  if (!bad.length || state.sort === "issues-first") { el.hidden = true; return; }
  el.innerHTML = `⚠ ${bad.length} ticker${bad.length > 1 ? "s" : ""} missing some data ` +
    `(${bad.slice(0, 4).map((t) => esc(t.symbol)).join(", ")}${bad.length > 4 ? "…" : ""}). ` +
    `<button id="gaps-sort" class="linkish">Show these first →</button>`;
  el.hidden = false;
  document.getElementById("gaps-sort").addEventListener("click", () => {
    state.sort = "issues-first";
    document.getElementById("sort").value = "issues-first";
    apply();
  });
}

function renderCards(rows) {
  const c = document.getElementById("cards");
  c.innerHTML = "";
  if (!rows.length) { c.innerHTML = `<p class="status">No tickers match the filter.</p>`; return; }
  rows.forEach((t, i) => {
    const el = cardEl(t);
    el.style.animationDelay = `${Math.min(i * 45, 450)}ms`;   // staggered entrance
    c.appendChild(el);
  });
}

function cardEl(t) {
  const card = document.createElement("article");
  card.className = "card";
  if (t.error) {
    card.innerHTML = `<div class="card-head"><div><span class="ticker">${esc(t.symbol)}</span></div>
      <span class="badge err">Error</span></div><div class="name">${esc(t.error)}</div>`;
    return card;
  }
  const vcls = VCLASS[t.verdict] || "neutral";
  card.classList.add(vcls);
  const changeCls = (t.change_pct || 0) >= 0 ? "up" : "down";
  const s = t.scores || {};
  const ed = earningsDays(t.next_earnings);
  let flags = (t.flags || []).slice(0, 3).map((f) => `<span class="flag">${esc(f.replace(/_/g, " "))}</span>`).join("");
  if (Array.isArray(t.missing) && t.missing.length) {
    flags = `<span class="flag gap" title="These fields couldn't be fetched this run — see the run log">⚠ partial data: ${esc(t.missing.join(", "))}</span>` + flags;
  }
  const hasAi = t.ai && t.ai.bull;
  const ds = dimStatus(t);

  card.innerHTML = `
    <div class="card-head">
      <div class="head-id">${monogram(t.symbol)}<div>
        <span class="ticker">${esc(t.symbol)}</span>
        <div class="name">${esc(t.company || "")}${t.company && t.sector && t.sector !== "Unknown" ? " · " : ""}${t.sector && t.sector !== "Unknown" ? esc(t.sector) : ""}</div>
      </div></div>
      <span class="badge ${vcls}">${esc(t.verdict || "—")}</span>
    </div>
    <div class="price-row">
      <span class="price tabular">${fmtMoney(t.price)}</span>
      <span class="change ${changeCls} tabular">${fmtPct(t.change_pct)}</span>
      ${sparklineSvg(t.spark)}
    </div>
    <div class="meta-row">
      <span class="composite-pill">Composite <strong>${fmtScore(t.composite)}</strong></span>
      ${ed != null ? `<span class="earnings ${ed <= 7 ? "soon" : ""}">📅 ${ed === 0 ? "Earnings today" : "Earnings in " + ed + "d"}</span>` : ""}
    </div>
    <div class="bars">
      ${scoreBar("F", s.fundamentals)}${scoreBar("T", s.technicals, ds.tech)}${scoreBar("S", s.sentiment, ds.sent)}
    </div>
    ${flags ? `<div class="flags">${flags}</div>` : ""}
    <div class="card-cta">${hasAi ? `<span class="ai-hint">🤖 Ask AI</span>` : "<span></span>"}<span>Details →</span></div>`;

  card.addEventListener("click", () => openDrawer(t.symbol));
  return card;
}

/* ---------------- detail drawer ---------------- */

function openDrawer(symbol) {
  const t = state.tickers.find((x) => x.symbol === symbol);
  if (!t || t.error) return;
  const s = t.scores || {};
  const vcls = VCLASS[t.verdict] || "neutral";
  const changeCls = (t.change_pct || 0) >= 0 ? "up" : "down";

  const ai = t.ai && t.ai.bull ? `<div class="section"><h3>In plain English</h3>
      <div class="ai-card">
        ${t.ai.takeaway ? `<p class="ai-take">${esc(t.ai.takeaway)}</p>` : ""}
        <p class="ai-bull"><span class="ai-tag up">Good</span>${esc(t.ai.bull)}</p>
        <p class="ai-bear"><span class="ai-tag down">Risks</span>${esc(t.ai.bear)}</p>
        <p class="ai-src">${t.ai.source === "deterministic" ? "auto-summary" : "AI · " + esc(t.ai.source)} · based on the data above · not financial advice</p>
      </div></div>` : "";

  const history = t.history || {};
  const funRows = (t.fundamentals || []).map((m) => {
    const tone = m.tone || "neutral";
    const word = m.word ? `<span class="word ${tone}">${esc(m.word)}</span>` : `<span class="word none">—</span>`;
    const h = history[m.key];
    const spark = h && h.length > 1 ? sparklineSvg(h.map((p) => p[1]), { w: 60, h: 18 }) : "";
    return `<tr><td>${esc(m.label)}</td><td class="mval tabular">${esc(m.display)}</td>
      <td class="mspark">${spark}</td><td>${word}</td></tr>`;
  }).join("");
  const peerNote = (t.peers_in_sector || 0) > 0 ? `vs ${t.peers_in_sector} industry peers` : "no peer data";
  const fundamentals = funRows ? `<div class="section"><h3>Fundamentals (${peerNote})</h3>
      <table class="fundamentals"><tbody>${funRows}</tbody></table></div>` : "";

  const reasons = groupedReasons(t.reasons);

  const drawer = document.getElementById("detail");
  drawer.innerHTML = `
    <div class="drawer-head">
      <div>
        <h2>${monogram(t.symbol)}${esc(t.symbol)} <span class="badge ${vcls}">${esc(t.verdict || "—")}</span></h2>
        <div class="name">${esc(t.company || "")}${t.sector && t.sector !== "Unknown" ? " · " + esc(t.sector) : ""}</div>
        <div class="price-row"><span class="price tabular">${fmtMoney(t.price)}</span>
          <span class="change ${changeCls} tabular">${fmtPct(t.change_pct)}</span></div>
      </div>
      <button class="close-btn" id="drawer-close" aria-label="Close">✕</button>
    </div>
    <div class="drawer-body">
      <div class="section"><h3>Price · ~3 months</h3><div class="chart-box"><canvas id="price-chart"></canvas></div></div>
      <div class="section"><h3>Signal breakdown</h3><div class="bars">
        ${scoreBar("F", s.fundamentals)}${scoreBar("T", s.technicals, dimStatus(t).tech)}${scoreBar("S", s.sentiment, dimStatus(t).sent)}
        <div class="bar-row"><span>Σ</span><div class="bar-track"><div class="bar-fill ${(t.composite||0)>=0?"pos":"neg"}" style="${(t.composite||0)>=0?`left:50%;width:${Math.min(50,Math.abs(t.composite||0)*50)}%`:`right:50%;left:auto;width:${Math.min(50,Math.abs(t.composite||0)*50)}%`}"></div></div><span class="bar-val">${fmtScore(t.composite)}</span></div>
      </div></div>
      ${ai}
      ${fundamentals}
      <div class="section"><h3>Why?</h3>${reasons}</div>
    </div>`;

  document.getElementById("scrim").hidden = false;
  drawer.hidden = false;
  document.getElementById("drawer-close").addEventListener("click", closeDrawer);
  drawPriceChart(t);
}

function drawPriceChart(t) {
  const canvas = document.getElementById("price-chart");
  if (!canvas || !Array.isArray(t.spark) || t.spark.length < 2 || typeof Chart === "undefined") return;
  const up = t.spark[t.spark.length - 1] >= t.spark[0];
  const col = up ? getCss("--up") : getCss("--down");
  const ctx = canvas.getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, 200);
  grad.addColorStop(0, col + "55");
  grad.addColorStop(1, col + "00");
  if (drawerChart) drawerChart.destroy();
  // Soft neon glow under the price line.
  const glow = {
    id: "glow",
    beforeDatasetsDraw(c) { c.ctx.save(); c.ctx.shadowColor = col; c.ctx.shadowBlur = 14; },
    afterDatasetsDraw(c) { c.ctx.restore(); },
  };
  drawerChart = new Chart(ctx, {
    type: "line",
    plugins: [glow],
    data: {
      labels: t.spark.map((_, i) => i),
      datasets: [{ data: t.spark, borderColor: col, backgroundColor: grad, fill: true,
                   borderWidth: 2, pointRadius: 0, tension: 0.25 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: {
        callbacks: { title: () => "", label: (c) => "$" + c.parsed.y.toFixed(2) } } },
      scales: {
        x: { display: false },
        y: { position: "right", grid: { color: getCss("--border") },
             ticks: { color: getCss("--muted"), callback: (v) => "$" + v } },
      },
    },
  });
}

function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function groupedReasons(reasons) {
  const groups = {}, order = [];
  for (const r of reasons || []) {
    const m = /^\[(\w+)\]\s*(.*)$/.exec(r);
    const dim = m ? m[1] : "other", text = m ? m[2] : r;
    if (!groups[dim]) { groups[dim] = []; order.push(dim); }
    groups[dim].push(text);
  }
  if (!order.length) return `<p class="tr-muted">No reasons recorded.</p>`;
  return order.map((dim) =>
    `<div class="reason-group"><h4>${esc(dim)}</h4><ul class="reasons">${groups[dim].map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`).join("");
}

function closeDrawer() {
  document.getElementById("detail").hidden = true;
  document.getElementById("scrim").hidden = true;
  if (drawerChart) { drawerChart.destroy(); drawerChart = null; }
}

/* ---------------- watchlist add/remove (GitHub issue) ---------------- */

function manage(action) {
  const t = document.getElementById("manage-ticker").value.trim().toUpperCase();
  if (!t) { document.getElementById("manage-ticker").focus(); return; }
  const params = new URLSearchParams({
    template: "watchlist.yml", title: `[watchlist] ${action} ${t}`,
    action: action === "add" ? "Add" : "Remove", tickers: t,
  });
  window.open(`https://github.com/${REPO}/issues/new?${params}`, "_blank", "noopener");
}

/* ---------------- theme ---------------- */

function initTheme() {
  const saved = localStorage.getItem("theme");
  if (saved) document.documentElement.dataset.theme = saved;
}
function toggleTheme() {
  const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
  if (drawerChart) drawerChart.update();
}

/* ---------------- wiring ---------------- */

document.getElementById("search").addEventListener("input", (e) => { state.search = e.target.value; apply(); });
document.getElementById("sort").addEventListener("change", (e) => { state.sort = e.target.value; apply(); });
document.getElementById("verdict-pills").addEventListener("click", (e) => {
  const pill = e.target.closest(".pill");
  if (!pill) return;
  state.verdict = pill.dataset.v;
  document.querySelectorAll("#verdict-pills .pill").forEach((p) => p.classList.toggle("active", p === pill));
  apply();
});
document.getElementById("changes").addEventListener("click", (e) => {
  const chip = e.target.closest(".ch-chip");
  if (!chip) return;
  const search = document.getElementById("search");
  search.value = search.value === chip.dataset.sym ? "" : chip.dataset.sym;
  state.search = search.value; apply();
});
document.getElementById("add-btn").addEventListener("click", () => manage("add"));
document.getElementById("remove-btn").addEventListener("click", () => manage("remove"));
document.getElementById("scrim").addEventListener("click", closeDrawer);
document.getElementById("tape").addEventListener("click", (e) => {
  const item = e.target.closest(".tape-item");
  if (item) openDrawer(item.dataset.sym);
});
document.getElementById("brief").addEventListener("click", (e) => {
  const chip = e.target.closest(".brief-chip");
  if (chip) openDrawer(chip.dataset.sym);
});
document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

/* ===================== Screener Assistant (chat) ===================== */
// Answers from the data already loaded (no key needed). If you paste your own
// free Gemini/Groq key (stored only in localStorage), open-ended questions go
// to the LLM with the screener data as context.

const SUGGESTIONS = ["What's the best stock to buy?", "What should I avoid?", "What changed today?", "Biggest movers?"];

function liveT() { return state.tickers.filter((t) => !t.error); }
function topBuys(n) { return liveT().filter((t) => t.verdict === "WATCH-BUY").sort((a, b) => (b.composite || 0) - (a.composite || 0)).slice(0, n); }
function topSells(n) { return liveT().filter((t) => t.verdict === "WATCH-SELL").sort((a, b) => (a.composite || 0) - (b.composite || 0)).slice(0, n); }
function tChip(t) { return `<button class="t-link" data-sym="${esc(t.symbol)}">${esc(t.symbol)}</button>`; }
function findT(q) {
  const up = q.toUpperCase();
  return liveT().find((t) => new RegExp(`\\b${t.symbol}\\b`).test(up))
      || liveT().find((t) => (t.company || "").toUpperCase().split(/[\s,]+/).some((w) => w.length > 2 && up.includes(w.toUpperCase())));
}
function gist(t) {
  if (t.ai && t.ai.takeaway) return esc(t.ai.takeaway);
  const good = (t.fundamentals || []).filter((m) => m.tone === "good").map((m) => m.label);
  return good.length ? `strong on ${esc(good.slice(0, 3).join(", "))}` : "a balanced profile";
}
function describe(t) {
  const s = t.scores || {};
  return `${tChip(t)} — <b>${esc(t.verdict || "—")}</b> at ${fmtMoney(t.price)} (${fmtPct(t.change_pct)}).<br>` +
    `${t.ai && t.ai.takeaway ? esc(t.ai.takeaway) + "<br>" : ""}` +
    `<span class="muted">Fundamentals ${fmtScore(s.fundamentals)} · Technicals ${fmtScore(s.technicals)} · Sentiment ${fmtScore(s.sentiment)}</span>`;
}

function answerLocally(q) {
  const s = q.toLowerCase().trim();
  const T = findT(q);

  if (/(best|top|which|what).*(buy|pick)|buy now|to buy|should i buy/.test(s) || s === "buy") {
    const b = topBuys(3);
    if (!b.length) return "Nothing is screening as a <b>WATCH-BUY</b> right now — mostly neutral.";
    const top = b[0];
    const rest = b.slice(1);
    return `The strongest buy-screen right now is ${tChip(top)} (composite <b>${fmtScore(top.composite)}</b>).<br>` +
      `Why: ${gist(top)}.` + (rest.length ? `<br>Also screening as buys: ${rest.map(tChip).join(" ")}` : "") +
      `<br><span class="muted">A filter to investigate — not advice.</span>`;
  }
  if (/(avoid|sell|trim|worst|stay away|not buy)/.test(s)) {
    const v = topSells(3);
    if (!v.length) return "Nothing is screening as a <b>WATCH-SELL</b> right now.";
    return `Screening as ones to avoid/trim: ${v.map(tChip).join(" ")}.<br>` +
      `Weakest is ${tChip(v[0])} — ${gist(v[0])} is the concern.`;
  }
  if (/(chang|flip|new|today)/.test(s)) {
    if (state.briefing && state.briefing.text) return esc(state.briefing.text);
    if (state.changes && state.changes.length) return state.changes.slice(0, 6).map((c) => "• " + esc(c.text)).join("<br>");
    return "No notable changes since the last run.";
  }
  if (/(mover|moved|biggest|gainer|loser|up today|down today)/.test(s)) {
    const m = liveT().filter((t) => typeof t.change_pct === "number").sort((a, b) => Math.abs(b.change_pct) - Math.abs(a.change_pct)).slice(0, 4);
    return m.length ? "Biggest moves today:<br>" + m.map((t) => `${tChip(t)} ${fmtPct(t.change_pct)}`).join("<br>") : "No price moves to report.";
  }
  if (/(cheap|value|low p\/?e|undervalued)/.test(s)) {
    const withPe = liveT().map((t) => ({ t, pe: (t.fundamentals || []).find((m) => m.key === "pe")?.value })).filter((x) => typeof x.pe === "number");
    withPe.sort((a, b) => a.pe - b.pe);
    return withPe.length ? "Lowest P/E (cheapest by earnings):<br>" + withPe.slice(0, 4).map((x) => `${tChip(x.t)} ${x.pe.toFixed(1)}×`).join("<br>") : "No P/E data available.";
  }
  if (/how many|count|breakdown/.test(s)) {
    const c = { "WATCH-BUY": 0, NEUTRAL: 0, "WATCH-SELL": 0 };
    liveT().forEach((t) => { if (c[t.verdict] != null) c[t.verdict]++; });
    return `Of ${liveT().length} screened: <b>${c["WATCH-BUY"]}</b> buy, ${c.NEUTRAL} neutral, <b>${c["WATCH-SELL"]}</b> sell.`;
  }
  if (T && /(why|about|tell me|how|is|should|good|bad|\?)/.test(s)) return describe(T);
  if (T) return describe(T);
  return null; // no local match
}

function buildLLMContext() {
  const lines = liveT().slice(0, 30).map((t) => {
    const s = t.scores || {};
    const fl = (t.fundamentals || []).filter((m) => m.word).map((m) => `${m.label} ${m.word}`).slice(0, 4).join(", ");
    return `${t.symbol} (${t.company || ""}, ${t.sector || ""}): ${t.verdict} composite ${fmtScore(t.composite)}; ` +
      `F ${fmtScore(s.fundamentals)} T ${fmtScore(s.technicals)} S ${fmtScore(s.sentiment)}; price ${fmtMoney(t.price)} ${fmtPct(t.change_pct)}; ` +
      `${fl}${t.ai && t.ai.takeaway ? "; " + t.ai.takeaway : ""}`;
  });
  return lines.join("\n");
}

async function answerLLM(q) {
  const raw = localStorage.getItem("ai_key");
  if (!raw) return null;
  const idx = raw.indexOf(":");
  const prov = raw.slice(0, idx), key = raw.slice(idx + 1);
  const prompt = `You are a stock-screener assistant. Answer the user's question using ONLY the data below. ` +
    `Be concise and plain-spoken. This is decision-support, not financial advice.\n\nDATA:\n${buildLLMContext()}\n\nQUESTION: ${q}`;
  try {
    if (prov === "gemini") {
      const r = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key=${encodeURIComponent(key)}`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }) });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      return esc(j.candidates?.[0]?.content?.parts?.[0]?.text || "").replace(/\n/g, "<br>");
    }
    const r = await fetch("https://api.groq.com/openai/v1/chat/completions",
      { method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + key }, body: JSON.stringify({ model: "llama-3.3-70b-versatile", messages: [{ role: "user", content: prompt }] }) });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    return esc(j.choices?.[0]?.message?.content || "").replace(/\n/g, "<br>");
  } catch (e) {
    return `Couldn't reach the AI (${esc(e.message)}). Check your key via ⚙.`;
  }
}

function chatPush(role, html) {
  const log = document.getElementById("chat-log");
  const div = document.createElement("div");
  div.className = "msg " + role;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

async function chatAsk(q) {
  q = q.trim();
  if (!q) return;
  chatPush("user", esc(q));
  const local = answerLocally(q);
  if (local) { chatPush("bot", local); return; }
  if (localStorage.getItem("ai_key")) {
    const thinking = chatPush("bot", '<span class="dots">thinking…</span>');
    thinking.innerHTML = (await answerLLM(q)) || "I couldn't answer that from the data.";
    document.getElementById("chat-log").scrollTop = 1e9;
  } else {
    chatPush("bot", `I can answer that with an AI key. Try: <i>${esc(SUGGESTIONS[0])}</i>, or click ⚙ to add a free Gemini/Groq key for open-ended questions.`);
  }
}

function chatInit() {
  const fab = document.getElementById("chat-fab");
  const panel = document.getElementById("chat");
  const sug = document.getElementById("chat-suggest");
  sug.innerHTML = SUGGESTIONS.map((q) => `<button class="sug" data-q="${esc(q)}">${esc(q)}</button>`).join("");
  let greeted = false;
  fab.addEventListener("click", () => {
    const open = panel.hidden;
    panel.hidden = !open;
    fab.classList.toggle("active", open);
    if (open && !greeted) {
      greeted = true;
      chatPush("bot", "Hi! Ask me about today's screen — e.g. <i>what's the best stock to buy?</i> I answer from the loaded data.");
    }
    if (open) document.getElementById("chat-input").focus();
  });
  document.getElementById("chat-close").addEventListener("click", () => { panel.hidden = true; fab.classList.remove("active"); });
  document.getElementById("chat-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const inp = document.getElementById("chat-input");
    chatAsk(inp.value); inp.value = "";
  });
  sug.addEventListener("click", (e) => { const b = e.target.closest(".sug"); if (b) chatAsk(b.dataset.q); });
  document.getElementById("chat-log").addEventListener("click", (e) => {
    const b = e.target.closest(".t-link"); if (b) openDrawer(b.dataset.sym);
  });
  document.getElementById("chat-key").addEventListener("click", () => {
    const cur = localStorage.getItem("ai_key") ? " (a key is set)" : "";
    const v = prompt(`Paste your own free API key for open-ended chat${cur}.\nFormat:  gemini:YOUR_KEY   or   groq:YOUR_KEY\n(stored only in this browser; leave blank to clear)`);
    if (v === null) return;
    if (!v.trim()) { localStorage.removeItem("ai_key"); chatPush("bot", "AI key cleared — I'll keep answering from the data."); return; }
    if (!/^(gemini|groq):.+/i.test(v.trim())) { chatPush("bot", "That didn't look right — use <code>gemini:KEY</code> or <code>groq:KEY</code>."); return; }
    localStorage.setItem("ai_key", v.trim().toLowerCase().startsWith("groq") ? "groq:" + v.trim().slice(5) : "gemini:" + v.trim().slice(7));
    chatPush("bot", "Key saved (in this browser only). Now you can ask open-ended questions too.");
  });
}

initTheme();
chatInit();
load();
