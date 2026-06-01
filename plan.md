# plan.md — Personal Stock Screener (static webpage on GitHub Pages)

> **For:** one user (you), personal/non-commercial use.
> **Built by:** Claude Code, following the phases in §6.
> **What it does:** A dashboard webpage showing your watchlist scored on fundamentals, technicals, and news/sentiment. Data is refreshed on a schedule by GitHub Actions; the page just displays it.
> **Not financial advice.** Output is decision-support. You make the calls.

---

## 1. The key architectural fact (read first)

GitHub Pages serves **static files only** — no server, no database, no Python at page-load. So the app splits in two:

```
[ GitHub Actions, on a cron schedule ]        [ GitHub Pages, static ]
  Python pipeline runs                          Browser loads index.html
  → calls Finnhub/FMP (keys = secrets)          → fetches docs/data/latest.json
  → scores each ticker                          → renders dashboard
  → writes docs/data/*.json, commits     ─────► → (no keys, no API calls)
  → sends alerts (Telegram/email)
```

**Security rule (non-negotiable):** API keys live **only** in GitHub Actions secrets. They must **never** appear in any file under `docs/` or in client-side JavaScript — the published site is public and anyone can read its source.

---

## 2. Scope

| In scope | Out of scope (v1) |
|---|---|
| Watchlist of ~10–50 US tickers | Real-time/HFT, order execution |
| Fundamentals, technicals, news/sentiment, threshold flags | Live in-browser API calls |
| Scheduled refresh + dashboard | Login/auth (Pages can't do it natively) |
| Alerts sent from the pipeline | Multi-user accounts |

---

## 3. Repo visibility — decide before Phase 0

| Option | Cost | Site | Repo code |
|---|---|---|---|
| **Public repo + Pages** (recommended) | Free; unlimited Actions minutes | Public | Public |
| **Private repo + Pages** | Needs **GitHub Pro**; 2,000 Actions min/mo free | Still public | Private |
| Private repo + **Cloudflare Pages / Vercel** | Free, supports private + access rules | Can be gated | Private |

> Either way the **site URL is public**. Since it shows only stock scores (no keys, no personal money data), a public repo is the simplest free path. If you want the page itself password-gated, use Cloudflare Pages instead of GitHub Pages.

---

## 4. Recommended Data Stack (unchanged — used inside the pipeline)

| Need | Provider | Free tier | Docs |
|---|---|---|---|
| News + **sentiment**, quotes, basic fundamentals | **Finnhub** | 60 calls/min | https://finnhub.io/docs/api |
| Deeper fundamentals fallback | **FMP** | limited free | https://site.financialmodelingprep.com/developer/docs |
| Technicals | **computed locally** (`pandas-ta`) | unlimited | https://github.com/twopirllc/pandas-ta |

**Avoid:** IEX Cloud (shut down Aug 2024); yfinance for anything you rely on (unofficial, breaks).

---

## 5. Repo structure

```
stock-screener/
├── plan.md
├── config.yaml                  # watchlist + thresholds + weights (you edit)
├── requirements.txt
├── pipeline/                    # RUNS IN ACTIONS ONLY — has the API keys
│   ├── data/
│   │   ├── finnhub_client.py    # quotes, news, fundamentals, OHLCV
│   │   └── fmp_client.py
│   ├── checks/
│   │   ├── fundamentals.py
│   │   ├── technicals.py        # pandas-ta
│   │   └── sentiment.py
│   ├── scoring.py               # combine → verdict + reasons
│   ├── notify.py                # Telegram/email from the Action
│   └── run.py                   # orchestrate → writes docs/data/*.json
├── docs/                        # SERVED BY GITHUB PAGES — no keys, ever
│   ├── index.html               # dashboard
│   ├── app.js                   # fetches data/latest.json, renders
│   ├── styles.css
│   └── data/
│       ├── latest.json          # current scores (committed by the Action)
│       └── history/             # dated snapshots for trend view
└── .github/workflows/
    └── refresh.yml              # cron → run pipeline → commit → deploy
```

**Pipeline stack:** Python 3.11+, `requests`, `pandas`, `pandas-ta`, `pyyaml`.
**Frontend stack:** plain HTML/CSS/JS (no build step) — fetches JSON, renders cards/table with sort + filter. Keep it dependency-light.

---

## 6. Build Phases — for Claude Code

Build and verify one phase before the next.

| Phase | Deliverable | Acceptance test |
|---|---|---|
| **0. Skeleton** | repo tree, `config.yaml`, `requirements.txt`, empty `docs/index.html` | repo runs locally; `docs/` shows a placeholder page |
| **1. Pipeline data** | `finnhub_client.py` fetch quote + OHLCV + news for 1 ticker | prints valid JSON for `AAPL` |
| **2. Checks + scoring** | 4 check modules + `scoring.py` → verdict **with reasons** | run on watchlist writes `docs/data/latest.json` |
| **3. Frontend** | `index.html`/`app.js`/`styles.css` reads `latest.json` | open locally → dashboard renders cards, sortable, **no keys in source** |
| **4. Actions cron** | `.github/workflows/refresh.yml`: run pipeline → commit JSON | manual "Run workflow" updates `latest.json` and redeploys |
| **5. Alerts** | `notify.py` fires from the Action (Telegram or email) | a test breach delivers exactly one message; dedupe via committed state |
| **6. Deploy** | Pages enabled on `/docs`; schedule live | site is public, refreshes on cron unattended |

**Tell Claude Code:** "Read `plan.md`. Implement Phase 0, then stop and show me. Wait for my OK before each next phase. Never write any API key into the `docs/` folder."

---

## 7. Scoring logic (in `scoring.py`)

Each check returns a score in **-1…+1** plus flags; thresholds/weights live in `config.yaml`.

```
composite = w_fund*fund + w_tech*tech + w_sent*sent
verdict   = WATCH-BUY | NEUTRAL | WATCH-SELL
```
- **Fundamentals:** e.g. P/E `<25` good / `>40` flag; rev growth YoY `>5%`; D/E `<1.5`; positive FCF.
- **Technicals:** SMA50 vs SMA200 cross; RSI(14) `<30` / `>70`; MACD crossover.
- **Sentiment:** Finnhub news-sentiment score over last 7 days; flag negative spikes.
- **Alerts:** price crosses a set level, daily move `>X%`, new 52-wk high/low.

The dashboard shows the verdict **plus the contributing reasons** — never a bare number.

---

## 8. Deploy & schedule
- **Pages:** Settings → Pages → Source = `Deploy from branch` → `main` → `/docs`.
- **Cron:** in `refresh.yml`, `on: schedule: - cron: "0 21 * * 1-5"` (after US close; cron is UTC). Add `workflow_dispatch` for manual runs.
- **Secrets:** Settings → Secrets and variables → Actions → add `FINNHUB_API_KEY`, etc.
- **Actions cost:** public repos = free unlimited minutes; private = 2,000 min/mo free.

---

## 9. Guardrails
- Free API tiers are **personal/non-commercial** — fine here.
- Cache fundamentals (change quarterly); only quotes/news refresh often → stays under 60/min.
- The site is public; show only scores, never account or position data.
- Verdicts are a **filter to investigate**, not buy/sell instructions. Not financial advice.

---

### Sources
- GitHub Pages — static only, plan availability: https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits
- Finnhub docs: https://finnhub.io/docs/api
- FMP docs: https://site.financialmodelingprep.com/developer/docs
- pandas-ta: https://github.com/twopirllc/pandas-ta
