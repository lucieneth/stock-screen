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
    status.style.display = "none";
    apply();
  } catch (err) {
    status.textContent =
      "No data yet. Run the pipeline (or the GitHub Action) to generate data/latest.json. " +
      `(${err.message})`;
  }
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

    const reasons = (t.reasons || []).map((r) => `<li>${esc(r)}</li>`).join("");
    const flags = (t.flags || []).map((f) => `<span class="flag">${esc(f)}</span>`).join("");
    const s = t.scores || {};

    card.innerHTML = `
      <div class="card-head">
        <span class="ticker">${esc(t.symbol)}</span>
        <span class="badge ${vclass}">${esc(t.verdict || "—")}</span>
      </div>
      <div class="price-row">
        <span class="price">${typeof t.price === "number" ? "$" + t.price.toFixed(2) : "—"}</span>
        <span class="change ${changeClass}">${fmtPct(t.change_pct)}</span>
      </div>
      <div class="composite">Composite <strong>${fmtScore(t.composite)}</strong>
        <span class="coverage">coverage ${fmtScore(t.coverage)}</span></div>
      <div class="subscores">
        <span title="Fundamentals">F ${fmtScore(s.fundamentals)}</span>
        <span title="Technicals">T ${fmtScore(s.technicals)}</span>
        <span title="Sentiment">S ${fmtScore(s.sentiment)}</span>
      </div>
      ${flags ? `<div class="flags">${flags}</div>` : ""}
      <details>
        <summary>Why? (${(t.reasons || []).length} reasons)</summary>
        <ul class="reasons">${reasons}</ul>
      </details>`;
    container.appendChild(card);
  }
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

for (const id of ["search", "verdict-filter", "sort"]) {
  document.getElementById(id).addEventListener("input", apply);
}
document.getElementById("add-btn").addEventListener("click", () => manage("add"));
document.getElementById("remove-btn").addEventListener("click", () => manage("remove"));
load();
