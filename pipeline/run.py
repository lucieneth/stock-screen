"""Orchestrator: load config -> fetch -> check -> score -> write docs/data/*.json.

Run from the repo root (needs FINNHUB_API_KEY in the environment):

    FINNHUB_API_KEY=xxxx python -m pipeline.run

Writes:
  docs/data/latest.json              current scores for the whole watchlist
  docs/data/history/YYYY-MM-DD.json  a dated snapshot for the trend view

SECURITY: the API key is read from the environment only and is never written
into the output payloads or anywhere under docs/.
"""
from __future__ import annotations

import os
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from pipeline.data import finnhub_client as fh
from pipeline.data import fmp_client as fmp
from pipeline.data import yahoo_client as yahoo
from pipeline.checks import fundamentals as fund_check
from pipeline.checks import technicals as tech_check
from pipeline.checks import sentiment as sent_check
from pipeline.checks import alerts as alert_check
from pipeline import scoring
from pipeline import metrics
from pipeline import peers
from pipeline import track_record
from pipeline import sentiment_baseline
from pipeline import changes
from pipeline import ai_summary

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
DATA_DIR = REPO_ROOT / "docs" / "data"
HISTORY_DIR = DATA_DIR / "history"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh_:
        return yaml.safe_load(fh_)


def _has_error(obj) -> bool:
    return not isinstance(obj, dict) or "error" in obj or not obj


def _candle_count(ohlcv) -> int:
    return len(ohlcv.get("c", [])) if isinstance(ohlcv, dict) else 0


def assemble_one(symbol: str, cfg: dict, use_fmp: bool = True,
                 sent_baseline: float | None = None) -> dict:
    """Fetch data + run the peer-independent checks for one ticker.

    Produces a partial record with technicals/sentiment/alerts and the raw
    metric values, but NOT the fundamentals score or verdict — those need the
    sector peer benchmarks, which are computed across the whole watchlist first
    (see finalize_one).
    """
    raw = fh.fetch_ticker(symbol)
    thresholds = cfg.get("thresholds", {})
    sources = {"primary": "finnhub", "fmp_enabled": use_fmp}

    # OHLCV: Finnhub gates /stock/candle on free plans, so Yahoo (keyless, no
    # quota) is the primary price source, with FMP as a secondary fallback.
    slow_w = int(thresholds.get("technicals", {}).get("sma_slow", 200))
    want = max(slow_w + 50, 400)
    if _candle_count(raw.get("ohlcv")) < slow_w:
        try:
            raw["ohlcv"] = yahoo.get_ohlcv(symbol, days=want)
            sources["ohlcv"] = "yahoo"
        except yahoo.YahooError as exc:
            sources["ohlcv_error"] = str(exc)
            if use_fmp:
                try:
                    raw["ohlcv"] = fmp.get_ohlcv(symbol, days=want)
                    sources["ohlcv"] = "fmp"
                    sources.pop("ohlcv_error", None)
                except fmp.FMPError as exc2:
                    sources["ohlcv_error"] = f"yahoo: {exc} | fmp: {exc2}"

    # Keep the two providers' fundamentals separate so metrics.extract can
    # normalize by source (Finnhub=percent, FMP=fraction). A merged dict would
    # lose that provenance on shared key names.
    fin_finnhub = raw["financials"] if not _has_error(raw.get("financials")) else {}
    fin_fmp: dict = {}
    if use_fmp:
        try:
            fin_fmp = fmp.get_fundamentals(symbol)
        except fmp.FMPError as exc:
            sources["fundamentals_error"] = str(exc)
    if fin_finnhub and fin_fmp:
        sources["fundamentals"] = "finnhub+fmp"
    elif fin_fmp:
        sources["fundamentals"] = "fmp"
    elif fin_finnhub:
        sources["fundamentals"] = "finnhub"

    # Company sector for peer-relative labelling.
    profile = {}
    if use_fmp:
        try:
            profile = fmp.get_profile(symbol)
        except fmp.FMPError:
            pass
    if not profile.get("sector"):
        try:
            profile = {**profile, **fh.get_profile(symbol)}
        except fh.FinnhubError:
            pass

    # Peer-independent checks now; fundamentals scoring waits for benchmarks.
    merged_fin = {**fin_fmp, **fin_finnhub}
    technicals = tech_check.check(raw.get("ohlcv", {}), thresholds.get("technicals", {}))
    sentiment = sent_check.check(raw.get("sentiment", {}), raw.get("news", []),
                                 thresholds.get("sentiment", {}), baseline=sent_baseline)
    alerts = alert_check.check(raw.get("quote", {}), merged_fin, cfg.get("alerts", {}))

    # Decoration for the dashboard: ~3 months of closes for the sparkline, and
    # the next earnings date. Both best-effort, never block scoring.
    closes = (raw.get("ohlcv") or {}).get("c") or []
    spark = [round(float(c), 2) for c in closes[-63:]]  # ~one quarter of trading days
    try:
        next_earnings = fh.get_next_earnings(symbol)
    except Exception:
        next_earnings = None

    quote = raw.get("quote", {})
    return {
        "symbol": symbol,
        "company": profile.get("companyName"),
        "sector": profile.get("sector") or "Unknown",
        "price": quote.get("c"),
        "change_pct": quote.get("dp"),
        "spark": spark,
        "next_earnings": next_earnings,
        "flags": sorted(alerts.get("flags", [])),
        "alerts": alerts.get("reasons", []),
        "details": {
            "technicals": technicals.get("metrics", {}),
            "sentiment": sentiment.get("metrics", {}),
        },
        # Raw normalized metric values; metrics.annotate_records turns these into
        # the sector-relative `fundamentals` list and then removes this key.
        "_metric_values": metrics.extract(fin_finnhub, fin_fmp),
        "history": metrics.history_from_series(raw.get("series")),
        "sources": sources,
        # Stashed for finalize_one (removed before output).
        "_tech": technicals,
        "_sent": sentiment,
        "_merged_fin": merged_fin,
    }


def finalize_one(record: dict, cfg: dict) -> None:
    """Compute the fundamentals score + composite verdict, in place.

    Runs after metrics.annotate_records has attached sector-relative labels, so
    fundamentals are scored from those same labels (sector-aware), falling back
    to absolute thresholds only when too few peer-benchmarked metrics exist.
    """
    fundamentals = metrics.score_from_labels(record.get("fundamentals", []), record.get("sector"))
    if fundamentals is None:
        fundamentals = fund_check.check(record["_merged_fin"], cfg.get("thresholds", {}).get("fundamentals", {}))

    verdict = scoring.score_ticker(
        fundamentals, record["_tech"], record["_sent"],
        cfg.get("weights", {}), cfg.get("verdict_bands", {}),
    )
    record["verdict"] = verdict["verdict"]
    record["composite"] = verdict["composite"]
    record["coverage"] = verdict["coverage"]
    record["scores"] = verdict["scores"]
    record["reasons"] = verdict["reasons"]
    record["flags"] = sorted(set(record.get("flags", [])) | set(verdict["flags"]))
    for k in ("_tech", "_sent", "_merged_fin"):
        record.pop(k, None)


def _is_rate_limited(error_msg: str) -> bool:
    """True when a ticker failed specifically because of an API rate limit."""
    msg = (error_msg or "").lower()
    return "429" in msg or "rate limit" in msg or "limit reached" in msg


def run(cfg: dict | None = None, rate_limit_cooldown: float = 65.0) -> dict:
    cfg = cfg or load_config()
    watchlist = cfg.get("watchlist", [])
    use_fmp = bool(os.environ.get("FMP_API_KEY"))

    # Per-ticker sentiment baselines from committed snapshots (A3): sentiment is
    # scored as deviation from a ticker's own typical level, not absolute VADER.
    baselines = sentiment_baseline.load_baselines()

    def attempt(symbol: str) -> dict:
        try:
            return assemble_one(symbol, cfg, use_fmp=use_fmp,
                                sent_baseline=baselines.get(symbol))
        except fh.FinnhubError as exc:
            return {"symbol": symbol, "error": str(exc)}

    records: list[dict] = []
    for symbol in watchlist:
        records.append(attempt(symbol))
        # Finnhub calls are paced globally by finnhub_client._throttle(); no
        # extra per-ticker sleep needed.

    # Run-level retry: any ticker that failed *because of* a rate limit gets one
    # more pass after a cooldown that lets the per-minute quota refill. Other
    # errors (bad symbol, no data) are left as-is — retrying them won't help.
    limited = [i for i, r in enumerate(records) if "error" in r and _is_rate_limited(r["error"])]
    if limited:
        print(f"{len(limited)} ticker(s) hit a rate limit; cooling down {rate_limit_cooldown:.0f}s then retrying.")
        time.sleep(rate_limit_cooldown)
        for i in limited:
            records[i] = attempt(records[i]["symbol"])
            time.sleep(1.5)

    # Benchmark fundamentals against each company's real industry peers (cached),
    # then layer the whole-sector P/E from FMP on top of the peer P/E where we
    # can get it. Falls back to peer P/E if FMP is unavailable.
    ok_symbols = [r["symbol"] for r in records if "error" not in r]
    benchmarks = peers.build_benchmarks(ok_symbols)
    if use_fmp:
        sector_pe: dict[str, float | None] = {}
        for r in records:
            if "error" in r:
                continue
            sec = r.get("sector")
            if not sec or sec == "Unknown":
                continue
            if sec not in sector_pe:
                try:
                    sector_pe[sec] = fmp.get_sector_pe(sec)
                except fmp.FMPError:
                    sector_pe[sec] = None
            pe = sector_pe[sec]
            if pe and r["symbol"] in benchmarks:
                benchmarks[r["symbol"]]["values"]["pe"] = round(pe, 4)
                benchmarks[r["symbol"]]["source"]["pe"] = "sector"

    metrics.annotate_records(records, benchmarks)

    # Now that sector-relative labels exist, score fundamentals + composite.
    for r in records:
        if "error" not in r:
            finalize_one(r, cfg)

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "count": len(records),
        "tickers": records,
    }


def write_outputs(payload: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "latest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    (HISTORY_DIR / f"{stamp}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_previous() -> dict | None:
    """The latest.json from the previous run (yesterday's, in the checkout)."""
    try:
        return json.loads((DATA_DIR / "latest.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main() -> None:
    cfg = load_config()
    previous = load_previous()
    payload = run(cfg)
    # "What changed since the last run" — verdict flips, new flags, movers.
    payload["changes"] = changes.diff(previous, payload)
    # Pre-generate the "Ask AI" buy/not-buy summaries (cached; best-effort).
    ai_summary.annotate(payload["tickers"], pause=float(os.environ.get("AI_PAUSE", "4")))
    write_outputs(payload)
    ok = sum(1 for r in payload["tickers"] if "error" not in r)
    print(f"Wrote docs/data/latest.json — {ok}/{payload['count']} tickers scored.")
    # Grade past verdicts against real forward returns (best-effort).
    track_record.update()


if __name__ == "__main__":
    main()
