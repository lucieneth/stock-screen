// app.js — fetches data/latest.json and renders the dashboard. (Phase 3)
//
// SECURITY: this file is public. No API keys, ever. It only reads the
// pipeline-generated data/latest.json.

// Repo that backs the watchlist issue form. Change if you fork.
const REPO = "OkPeach/stock-screen";

const VERDICT_CLASS = {
  "WATCH-BUY": "buy",
  "NEUTRAL": "neutral",
  "WATCH-SELL": "sell",
};

let allTickers = [];
// key "SYMBOL:metric" -> { points:[[period,val]], benchmark, label, unit }
const chartData = {};

function manage(action) {
  const t = document.getElementById("manage-ticker").value.trim().toUpperCase();
  if (!t) {
    document.getElementById("manage-ticker").focus();
    return;
  }
  const params = new URLSearchParams({
    template: "watchlist.yml",
    title: `[watchlist] ${action} ${t}`,
    action: action === "add" ? "Add" : "Remove",
    tickers: t,
  });
  window.open(`https://github.com/${REPO}/issues/new?${params}`, "_blank", "noopener");
}

async function load() {
  const status = document.getElementById("status");
  try {
    const resp = await fetch("data/latest.json", { cache: "no-store" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    allTickers = Array.isArray(data.tickers) ? data.tickers : [];
    renderUpdated(data);
    renderChanges(data.changes);
    status.style.display = "none";
    apply();
  } catch (err) {
    status.textContent =
      "No data yet. Run the pipeline (or the GitHub Action) to generate data/latest.json. " +
      `(${err.message})`;
  }
}

async function loadTrackRecord() {
  const el = document.getElementById("track-record");
  try {
    const resp = await fetch("data/track_record.json", { cache: "no-store" });
    if (!resp.ok) return;
    renderTrack(el, await resp.json());
  } catch { /* track record is optional */ }
}

function pct(v) { return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(1)}%`; }

function renderTrack(el, data) {
  const horizons = data.horizons || [];
  if (!horizons.length) return;
  const parts = [];
  for (const h of horizons) {
    const hz = (data.by_horizon || {})[String(h)];
    if (!hz) continue;
    const base = hz.baseline_avg_return;
    const chips = [];
    for (const v of ["WATCH-BUY", "WATCH-SELL"]) {
      const s = (hz.verdicts || {})[v];
      if (!s || !s.n) continue;
      const cls = v === "WATCH-BUY" ? "buy" : "sell";
      if (!s.confident) {
        chips.push(`<span class="tr-chip ${cls}">${v === "WATCH-BUY" ? "Buy" : "Sell"}: building (n=${s.n})</span>`);
      } else {
        const good = s.hit_rate >= 0.5;
        chips.push(`<span class="tr-chip ${cls}" title="avg ${pct(s.avg_return)} vs basket ${base != null ? pct(base) : "—"}">` +
          `${v === "WATCH-BUY" ? "Buy" : "Sell"}: <strong class="${good ? "tr-good" : "tr-bad"}">${Math.round(s.hit_rate * 100)}% hit</strong> · ${pct(s.avg_return)} (basket ${base != null ? pct(base) : "—"}) · n=${s.n}</span>`);
      }
    }
    if (chips.length) parts.push(`<div class="tr-row"><span class="tr-h">${h}d</span>${chips.join("")}</div>`);
  }
  if (!parts.length) {
    el.innerHTML = `<span class="tr-title">Track record</span> <span class="tr-muted">building — grading verdicts as horizons elapse</span>`;
  } else {
    el.innerHTML = `<span class="tr-title">Track record</span>${parts.join("")}` +
      `<span class="tr-muted">forward return after verdict, vs holding the whole watchlist (“basket”)</span>`;
  }
  el.hidden = false;
}

function renderUpdated(data) {
  const el = document.getElementById("updated");
  if (!data.generated_at) return;
  const when = new Date(data.generated_at);
  const sample = data.sample ? " · sample data" : "";
  el.textContent = `Updated ${when.toLocaleString()}${sample}`;
}

function apply() {
  const q = document.getElementById("search").value.trim().toUpperCase();
  const vf = document.getElementById("verdict-filter").value;
  const sort = document.getElementById("sort").value;

  let rows = allTickers.filter((t) => {
    if (q && !(t.symbol || "").toUpperCase().includes(q)) return false;
    if (vf !== "ALL" && t.verdict !== vf) return false;
    return true;
  });

  const num = (v) => (typeof v === "number" ? v : -Infinity);
  const cmp = {
    "composite-desc": (a, b) => num(b.composite) - num(a.composite),
    "composite-asc": (a, b) => num(a.composite) - num(b.composite),
    "symbol-asc": (a, b) => (a.symbol || "").localeCompare(b.symbol || ""),
    "change-desc": (a, b) => num(b.change_pct) - num(a.change_pct),
    "change-asc": (a, b) => num(a.change_pct) - num(b.change_pct),
  }[sort];
  rows.sort(cmp);

  render(rows);
}

function fmtPct(v) {
  if (typeof v !== "number") return "—";
  return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function fmtScore(v) {
  return typeof v === "number" ? v.toFixed(2) : "—";
}

function render(rows) {
  const container = document.getElementById("cards");
  container.innerHTML = "";
  if (!rows.length) {
    container.innerHTML = '<p class="placeholder">No tickers match the current filter.</p>';
    return;
  }

  for (const t of rows) {
    const card = document.createElement("article");
    card.className = "card";

    if (t.error) {
      card.classList.add("card-error");
      card.innerHTML = `<div class="card-head"><span class="ticker">${esc(t.symbol)}</span>
        <span class="badge err">ERROR</span></div><p class="err-msg">${esc(t.error)}</p>`;
      container.appendChild(card);
      continue;
    }

    const vclass = VERDICT_CLASS[t.verdict] || "neutral";
    const changeClass = (t.change_pct || 0) >= 0 ? "up" : "down";

    const flags = (t.flags || []).map((f) => `<span class="flag">${esc(f)}</span>`).join("");
    const s = t.scores || {};

    const sector = t.sector && t.sector !== "Unknown" ? esc(t.sector) : "";
    const peerNote = (t.peers_in_sector || 0) > 0
      ? `vs ${t.peers_in_sector} industry peers`
      : "no peer data";
    const history = t.history || {};
    const funRows = (t.fundamentals || []).map((m) => {
      const tone = m.tone || "neutral";
      const benchTitle = m.sector_benchmark != null
        ? `${m.benchmark_source === "sector" ? "sector" : "peer"} benchmark ${m.sector_benchmark}`
        : "";
      const word = m.word
        ? `<span class="word ${tone}" title="${benchTitle}">${esc(m.word)}</span>`
        : `<span class="word none">—</span>`;
      const pts = history[m.key];
      let labelCell = esc(m.label);
      if (pts && pts.length > 1) {
        const ck = `${t.symbol}:${m.key}`;
        chartData[ck] = { points: pts, benchmark: m.sector_benchmark, label: m.label, unit: m.display };
        labelCell = `<button class="chart-btn" data-ck="${ck}" title="Show history" aria-label="Show ${esc(m.label)} history">📈</button> ${esc(m.label)}`;
      }
      return `<tr><td>${labelCell}</td><td class="mval">${esc(m.display)}</td><td>${word}</td></tr>`;
    }).join("");
    const funBlock = (t.fundamentals || []).length
      ? `<details>
           <summary>Fundamentals (${peerNote})</summary>
           <table class="fundamentals"><tbody>${funRows}</tbody></table>
         </details>`
      : "";

    card.innerHTML = `
      <div class="card-head">
        <span class="ticker">${esc(t.symbol)}</span>
        <span class="badge ${vclass}">${esc(t.verdict || "—")}</span>
      </div>
      ${sector ? `<div class="sector">${esc(t.company || "")}${t.company ? " · " : ""}${sector}</div>` : ""}
      <div class="price-row">
        <span class="price">${typeof t.price === "number" ? "$" + t.price.toFixed(2) : "—"}</span>
        <span class="change ${changeClass}">${fmtPct(t.change_pct)}</span>
        ${sparklineSvg(t.spark)}
      </div>
      ${earningsChip(t.next_earnings)}
      <div class="composite">Composite <strong>${fmtScore(t.composite)}</strong>
        <span class="coverage">coverage ${fmtScore(t.coverage)}</span></div>
      <div class="subscores">
        <span title="Fundamentals">F ${fmtScore(s.fundamentals)}</span>
        <span title="Technicals">T ${fmtScore(s.technicals)}</span>
        <span title="Sentiment">S ${fmtScore(s.sentiment)}</span>
      </div>
      ${flags ? `<div class="flags">${flags}</div>` : ""}
      ${aiBlock(t.ai)}
      ${funBlock}
      <details>
        <summary>Why? (${(t.reasons || []).length} reasons)</summary>
        ${groupedReasons(t.reasons)}
      </details>`;
    container.appendChild(card);
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Tiny ~3-month price line next to the quote; colored by period direction.
function sparklineSvg(closes) {
  if (!Array.isArray(closes) || closes.length < 2) return "";
  const W = 96, H = 28, n = closes.length;
  let lo = Math.min(...closes), hi = Math.max(...closes);
  if (lo === hi) { lo -= 1; hi += 1; }
  const x = (i) => (i * W) / (n - 1);
  const y = (v) => 2 + (1 - (v - lo) / (hi - lo)) * (H - 4);
  const pts = closes.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`).join(" ");
  const up = closes[n - 1] >= closes[0];
  const periodPct = ((closes[n - 1] / closes[0] - 1) * 100).toFixed(1);
  return `<svg viewBox="0 0 ${W} ${H}" class="spark" role="img"
    aria-label="3-month price trend ${periodPct}%"><title>~3 months: ${periodPct}%</title>
    <polyline points="${pts}" fill="none" stroke="${up ? "var(--up)" : "var(--down)"}" stroke-width="1.5"/></svg>`;
}

// "Earnings in Nd" chip; highlighted when imminent.
function earningsChip(dateStr) {
  if (!dateStr) return "";
  const days = Math.ceil((new Date(dateStr + "T00:00:00Z") - Date.now()) / 86400000);
  if (isNaN(days) || days < 0 || days > 120) return "";
  const label = days === 0 ? "Earnings today" : `Earnings in ${days}d`;
  return `<div class="earnings ${days <= 7 ? "soon" : ""}" title="Next earnings: ${esc(dateStr)}">📅 ${label}</div>`;
}

// Pre-generated "Ask AI" buy/not-buy panel (no live call — see ai_summary.py).
function aiBlock(ai) {
  if (!ai || !ai.bull || !ai.bear) return "";
  const src = ai.source === "deterministic" ? "rule-based summary" : `AI · ${esc(ai.source)}`;
  return `<details class="ai">
    <summary>🤖 Ask AI — why buy / why not</summary>
    <div class="ai-body">
      <p class="ai-bull"><span class="ai-tag up">Bull</span> ${esc(ai.bull)}</p>
      <p class="ai-bear"><span class="ai-tag down">Bear</span> ${esc(ai.bear)}</p>
      <p class="ai-src">${src} · grounded in screener data · not financial advice</p>
    </div></details>`;
}

// "Why?" reasons grouped under dimension headers instead of [prefix] strings.
function groupedReasons(reasons) {
  const groups = {};
  const order = [];
  for (const r of reasons || []) {
    const m = /^\[(\w+)\]\s*(.*)$/.exec(r);
    const dim = m ? m[1] : "other";
    const text = m ? m[2] : r;
    if (!groups[dim]) { groups[dim] = []; order.push(dim); }
    groups[dim].push(text);
  }
  return order.map((dim) =>
    `<div class="reason-group"><h4>${esc(dim)}</h4>
       <ul class="reasons">${groups[dim].map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>`
  ).join("");
}

// "What changed since the last run" strip.
function renderChanges(list) {
  const el = document.getElementById("changes");
  if (!el) return;
  if (!Array.isArray(list)) return;          // older payloads: leave hidden
  if (!list.length) {
    el.innerHTML = `<span class="ch-title">Since last run</span><span class="tr-muted">no changes</span>`;
    el.hidden = false;
    return;
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

// Clicking a change chip filters the dashboard to that ticker.
document.getElementById("changes").addEventListener("click", (e) => {
  const chip = e.target.closest(".ch-chip");
  if (!chip) return;
  const search = document.getElementById("search");
  search.value = search.value === chip.dataset.sym ? "" : chip.dataset.sym;
  apply();
});

// Build a small SVG line chart of a metric's quarterly history.
function chartSvg(data) {
  const W = 300, H = 130, padL = 8, padR = 8, padT = 18, padB = 22;
  const vals = data.points.map((p) => p[1]);
  const ys = data.benchmark != null ? vals.concat([data.benchmark]) : vals;
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (lo === hi) { lo -= 1; hi += 1; }
  const n = data.points.length;
  const x = (i) => padL + (i * (W - padL - padR)) / (n - 1);
  const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);
  const line = data.points.map((p, i) => `${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");
  const first = data.points[0][0], last = data.points[n - 1][0];
  const bench = data.benchmark != null
    ? `<line x1="${padL}" y1="${y(data.benchmark).toFixed(1)}" x2="${W - padR}" y2="${y(data.benchmark).toFixed(1)}"
         stroke="#f1c40f" stroke-dasharray="4 3" stroke-width="1"/>
       <text x="${W - padR}" y="${(y(data.benchmark) - 3).toFixed(1)}" class="c-bench" text-anchor="end">benchmark ${esc(data.benchmark)}</text>`
    : "";
  const lastPt = `<circle cx="${x(n - 1).toFixed(1)}" cy="${y(data.points[n - 1][1]).toFixed(1)}" r="2.5" fill="#4f8cff"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" class="chart-svg" role="img" aria-label="${esc(data.label)} history">
    <text x="${padL}" y="12" class="c-title">${esc(data.label)} · last ${n} quarters</text>
    <text x="${padL}" y="${padT - 4}" class="c-axis">${hi.toFixed(1)}</text>
    <text x="${padL}" y="${H - padB + 12}" class="c-axis">${lo.toFixed(1)}</text>
    ${bench}
    <polyline points="${line}" fill="none" stroke="#4f8cff" stroke-width="1.5"/>
    ${lastPt}
    <text x="${padL}" y="${H - 4}" class="c-axis">${esc(first)}</text>
    <text x="${W - padR}" y="${H - 4}" class="c-axis" text-anchor="end">${esc(last)}</text>
  </svg>`;
}

// Expand/collapse a chart row beneath the clicked metric.
document.getElementById("cards").addEventListener("click", (e) => {
  const btn = e.target.closest(".chart-btn");
  if (!btn) return;
  const row = btn.closest("tr");
  const next = row.nextElementSibling;
  if (next && next.classList.contains("chart-row")) {
    next.remove();
    btn.classList.remove("open");
    return;
  }
  const data = chartData[btn.dataset.ck];
  if (!data) return;
  const tr = document.createElement("tr");
  tr.className = "chart-row";
  tr.innerHTML = `<td colspan="3">${chartSvg(data)}</td>`;
  row.after(tr);
  btn.classList.add("open");
});

for (const id of ["search", "verdict-filter", "sort"]) {
  document.getElementById(id).addEventListener("input", apply);
}
document.getElementById("add-btn").addEventListener("click", () => manage("add"));
document.getElementById("remove-btn").addEventListener("click", () => manage("remove"));
load();
loadTrackRecord();
