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
from datetime import datetime, date, timezone
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
from pipeline import fetch_cache

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"
DATA_DIR = REPO_ROOT / "docs" / "data"
HISTORY_DIR = DATA_DIR / "history"
FETCH_CACHE_PATH = REPO_ROOT / "cache" / "fetches.json"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh_:
        return yaml.safe_load(fh_)


# How long cached fetches stay fresh (days). Fundamentals/series move
# quarterly, profiles ~never, earnings until the date passes. OHLCV: a stale
# copy (weekend-safe TTL) is a last resort when every live source fails.
TTL_FUND = 7
TTL_PROFILE = 30
TTL_EARN = 5
TTL_OHLCV_STALE = 5

# Only cache the metric keys we actually read (extraction + alerts) and the
# chart series we plot — Finnhub returns hundreds more, and committing those
# daily would bloat the repo for nothing.
_KEEP_METRIC_KEYS = ({k for m in metrics.METRICS for k in m.finnhub}
                     | {"52WeekHigh", "52WeekLow"})


def _slim_bundle(bundle: dict) -> dict:
    metric = {k: v for k, v in (bundle.get("metric") or {}).items() if k in _KEEP_METRIC_KEYS}
    quarterly = (bundle.get("series") or {}).get("quarterly") or {}
    keep_series = set(metrics.SERIES_KEYS.values())
    series = {"quarterly": {k: v[-16:] for k, v in quarterly.items()
                            if k in keep_series and isinstance(v, list)}}
    return {"metric": metric, "series": series}


def _metric_bundle(symbol: str, fc, sources: dict) -> dict:
    """Finnhub /stock/metric (metric + series), cached — the heaviest call."""
    v = fc.get(f"metric:{symbol}", TTL_FUND)
    if v is not None:
        return v
    try:
        v = _slim_bundle(fh.get_company_metrics(symbol))
        fc.set(f"metric:{symbol}", v)
        return v
    except fh.FinnhubError as exc:
        sources["metric_error"] = str(exc)[:160]
        return {"metric": {}, "series": {}}


def _fmp_fundamentals(symbol: str, fc, use_fmp: bool, sources: dict) -> dict:
    if not use_fmp:
        return {}
    v = fc.get(f"fmpfund:{symbol}", TTL_FUND)
    if v is not None:
        return v
    try:
        v = fmp.get_fundamentals(symbol)
        fc.set(f"fmpfund:{symbol}", v)
        return v
    except fmp.FMPError as exc:
        sources["fmp_fund_error"] = str(exc)[:160]
        return {}


def _resolve_ohlcv(symbol: str, prefetched: dict | None, use_fmp: bool, fc,
                   want: int, sources: dict) -> dict:
    """OHLCV fallback chain: Yahoo (prefetched) -> FMP -> stale cache.

    On success the closes are cached so a future all-sources-down run can still
    show a (slightly stale) sparkline and technicals.
    """
    ohlcv = prefetched if isinstance(prefetched, dict) else {"error": "no OHLCV prefetched"}
    if "error" not in ohlcv:
        sources["ohlcv"] = "yahoo"
    else:
        sources["ohlcv_error"] = f"yahoo: {ohlcv['error']}"[:160]
        if use_fmp:
            try:
                ohlcv = fmp.get_ohlcv(symbol, days=want)
                sources["ohlcv"] = "fmp"
            except fmp.FMPError as exc:
                sources["ohlcv_error"] += f" | fmp: {exc}"[:160]
    if "error" not in ohlcv:
        # Persist dates too so track_record can reuse this instead of re-hitting
        # Yahoo (halves the request burst that triggers the 429).
        fc.set(f"ohlcv:{symbol}", {"t": ohlcv.get("t") or [], "c": ohlcv.get("c") or []})
        return ohlcv
    stale = fc.get(f"ohlcv:{symbol}", TTL_OHLCV_STALE)
    if isinstance(stale, dict) and stale.get("c"):
        sources["ohlcv"] = "cache(stale)"
        return {"s": "ok", "t": stale.get("t") or [], "c": stale["c"]}
    return ohlcv  # still the error dict


def _profile(symbol: str, fc, use_fmp: bool) -> dict:
    """Sector/company name, cached long (profiles change ~never)."""
    v = fc.get(f"profile:{symbol}", TTL_PROFILE)
    if v is not None:
        return v
    prof: dict = {}
    if use_fmp:
        try:
            prof = fmp.get_profile(symbol)
        except fmp.FMPError:
            pass
    if not prof.get("sector"):
        try:
            prof = {**prof, **fh.get_profile(symbol)}
        except fh.FinnhubError:
            pass
    if prof.get("sector") or prof.get("companyName"):
        fc.set(f"profile:{symbol}", prof)
    return prof


def _earnings(symbol: str, fc) -> str | None:
    """Next earnings date, cached until it passes."""
    today = date.today().isoformat()
    v = fc.get(f"earn:{symbol}", TTL_EARN)
    if isinstance(v, dict):
        d = v.get("date")
        if not d or d >= today:        # still upcoming (or "none known") -> reuse
            return d
    try:
        d = fh.get_next_earnings(symbol)
    except Exception:
        d = None
    fc.set(f"earn:{symbol}", {"date": d})
    return d


def assemble_one(symbol: str, cfg: dict, use_fmp: bool = True,
                 sent_baseline: float | None = None,
                 ohlcv: dict | None = None, fc=None) -> dict:
    """Fetch data + run the peer-independent checks for one ticker.

    Speed-tuned: OHLCV is pre-fetched in parallel (Yahoo) and passed in; the
    quote is derived from the latest close (cron runs after the US close);
    slow-moving fundamentals / profile / earnings come from the TTL fetch cache;
    and the always-403 Finnhub candle / news-sentiment probes are gone. The only
    fresh Finnhub call left is company news.
    """
    thresholds = cfg.get("thresholds", {})
    sources = {"fmp_enabled": use_fmp}
    fc = fc or fetch_cache.FetchCache(None)   # memory-only if not supplied

    slow_w = int(thresholds.get("technicals", {}).get("sma_slow", 200))
    ohlcv = _resolve_ohlcv(symbol, ohlcv, use_fmp, fc, max(slow_w + 50, 400), sources)

    # Quote derived from daily closes (after-close cron): price = last close,
    # change = close-to-close. If every candle source failed, fall back to a
    # live Finnhub quote (one paced call) so price is never silently absent.
    closes = ohlcv.get("c") or []
    if closes:
        sources["price"] = sources.get("ohlcv", "yahoo")
        price = round(float(closes[-1]), 2)
        change_pct = (round((closes[-1] / closes[-2] - 1) * 100, 2)
                      if len(closes) >= 2 and closes[-2] else None)
    else:
        try:
            q = fh.get_quote(symbol)
            price, change_pct = q.get("c"), q.get("dp")
            sources["price"] = "finnhub_quote"
        except fh.FinnhubError as exc:
            price = change_pct = None
            sources["price_error"] = str(exc)[:160]

    bundle = _metric_bundle(symbol, fc, sources)
    fin_finnhub = bundle.get("metric") or {}
    series = bundle.get("series") or {}
    fin_fmp = _fmp_fundamentals(symbol, fc, use_fmp, sources)
    if fin_finnhub and fin_fmp:
        sources["fundamentals"] = "finnhub+fmp"
    elif fin_fmp:
        sources["fundamentals"] = "fmp"
    elif fin_finnhub:
        sources["fundamentals"] = "finnhub"

    profile = _profile(symbol, fc, use_fmp)

    try:
        news = fh.get_company_news(symbol)
    except fh.FinnhubError as exc:
        news = []
        sources["news_error"] = str(exc)[:160]

    # Peer-independent checks now; fundamentals scoring waits for benchmarks.
    merged_fin = {**fin_fmp, **fin_finnhub}
    technicals = tech_check.check(ohlcv, thresholds.get("technicals", {}))
    # No premium news-sentiment probe — VADER over the free headlines.
    sentiment = sent_check.check({}, news, thresholds.get("sentiment", {}), baseline=sent_baseline)
    alerts = alert_check.check({"c": price, "dp": change_pct}, merged_fin, cfg.get("alerts", {}))

    spark = [round(float(c), 2) for c in closes[-63:]]  # ~one quarter of trading days
    next_earnings = _earnings(symbol, fc)

    # An explicit inventory of what couldn't be fetched, so gaps are visible on
    # the dashboard and in the run log instead of silently shrinking the data.
    missing = [name for name, ok in (
        ("price", price is not None),
        ("ohlcv", bool(closes)),
        ("fundamentals", bool(fin_finnhub or fin_fmp)),
        ("news", bool(news)),
    ) if not ok]

    return {
        "symbol": symbol,
        "company": profile.get("companyName"),
        "sector": profile.get("sector") or "Unknown",
        "price": price,
        "change_pct": change_pct,
        "spark": spark,
        "next_earnings": next_earnings,
        "missing": missing,
        "flags": sorted(alerts.get("flags", [])),
        "alerts": alerts.get("reasons", []),
        "details": {
            "technicals": technicals.get("metrics", {}),
            "sentiment": sentiment.get("metrics", {}),
        },
        # Raw normalized metric values; metrics.annotate_records turns these into
        # the sector-relative `fundamentals` list and then removes this key.
        "_metric_values": metrics.extract(fin_finnhub, fin_fmp),
        "history": metrics.history_from_series(series),
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

    # Slow-moving fetches (fundamentals/profile/earnings) come from this TTL
    # cache; prices are pre-fetched from Yahoo in parallel (no rate limit).
    fc = fetch_cache.FetchCache(FETCH_CACHE_PATH)
    slow_w = int(cfg.get("thresholds", {}).get("technicals", {}).get("sma_slow", 200))
    print(f"fetching prices for {len(watchlist)} tickers (parallel)…", flush=True)
    ohlcv_map = yahoo.get_many(watchlist, days=max(slow_w + 50, 400))

    def attempt(symbol: str) -> dict:
        try:
            return assemble_one(symbol, cfg, use_fmp=use_fmp,
                                sent_baseline=baselines.get(symbol),
                                ohlcv=ohlcv_map.get(symbol), fc=fc)
        except fh.FinnhubError as exc:
            return {"symbol": symbol, "error": str(exc)}

    records: list[dict] = []
    total = len(watchlist)
    for i, symbol in enumerate(watchlist, 1):
        rec = attempt(symbol)
        records.append(rec)
        if "error" in rec:
            state = "ERROR"
        elif rec.get("missing"):
            state = "partial: missing " + ",".join(rec["missing"])
        else:
            state = "ok"
        print(f"[{i}/{total}] {symbol} ({state})", flush=True)
        # Finnhub calls are paced globally by finnhub_client._throttle().

    fc.save()
    gap_counts: dict[str, int] = {}
    for r in records:
        for m in r.get("missing", []):
            gap_counts[m] = gap_counts.get(m, 0) + 1
    if gap_counts:
        print(f"DATA GAPS across {total} tickers: {gap_counts} — see each record's "
              "'sources' for the cause.", flush=True)

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
    print("building peer benchmarks…", flush=True)
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
    print("generating AI summaries…", flush=True)
    ai_summary.annotate(payload["tickers"], pause=float(os.environ.get("AI_PAUSE", "4")))
    write_outputs(payload)
    ok = sum(1 for r in payload["tickers"] if "error" not in r)
    print(f"Wrote docs/data/latest.json — {ok}/{payload['count']} tickers scored.", flush=True)
    # Grade past verdicts against real forward returns (best-effort).
    print("grading track record…", flush=True)
    track_record.update()
    print("done.", flush=True)


if __name__ == "__main__":
    main()
