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
from pipeline.checks import fundamentals as fund_check
from pipeline.checks import technicals as tech_check
from pipeline.checks import sentiment as sent_check
from pipeline.checks import alerts as alert_check
from pipeline import scoring

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


def score_one(symbol: str, cfg: dict, use_fmp: bool = True) -> dict:
    """Fetch + run all checks + score a single ticker into a dashboard record.

    Finnhub is the primary source; when its free tier gates OHLCV/fundamentals
    we fall back to FMP so technicals and fundamentals still populate.
    """
    raw = fh.fetch_ticker(symbol)
    thresholds = cfg.get("thresholds", {})
    sources = {"primary": "finnhub"}

    slow_w = int(thresholds.get("technicals", {}).get("sma_slow", 200))
    if use_fmp and _candle_count(raw.get("ohlcv")) < slow_w:
        try:
            raw["ohlcv"] = fmp.get_ohlcv(symbol, days=max(slow_w + 50, 400))
            sources["ohlcv"] = "fmp"
        except fmp.FMPError:
            pass  # keep whatever Finnhub returned (possibly an error dict)

    if use_fmp and _has_error(raw.get("financials")):
        try:
            raw["financials"] = fmp.get_fundamentals(symbol)
            sources["fundamentals"] = "fmp"
        except fmp.FMPError:
            pass

    fundamentals = fund_check.check(raw.get("financials", {}), thresholds.get("fundamentals", {}))
    technicals = tech_check.check(raw.get("ohlcv", {}), thresholds.get("technicals", {}))
    sentiment = sent_check.check(raw.get("sentiment", {}), raw.get("news", []), thresholds.get("sentiment", {}))
    alerts = alert_check.check(raw.get("quote", {}), raw.get("financials", {}), cfg.get("alerts", {}))

    verdict = scoring.score_ticker(
        fundamentals, technicals, sentiment,
        cfg.get("weights", {}), cfg.get("verdict_bands", {}),
    )

    quote = raw.get("quote", {})
    return {
        "symbol": symbol,
        "price": quote.get("c"),
        "change_pct": quote.get("dp"),
        "verdict": verdict["verdict"],
        "composite": verdict["composite"],
        "coverage": verdict["coverage"],
        "scores": verdict["scores"],
        "reasons": verdict["reasons"],
        "flags": sorted(set(verdict["flags"]) | set(alerts.get("flags", []))),
        "alerts": alerts.get("reasons", []),
        "details": {
            "fundamentals": fundamentals.get("metrics", {}),
            "technicals": technicals.get("metrics", {}),
            "sentiment": sentiment.get("metrics", {}),
        },
        "sources": sources,
    }


def _is_rate_limited(error_msg: str) -> bool:
    """True when a ticker failed specifically because of an API rate limit."""
    msg = (error_msg or "").lower()
    return "429" in msg or "rate limit" in msg or "limit reached" in msg


def run(cfg: dict | None = None, rate_limit_cooldown: float = 65.0) -> dict:
    cfg = cfg or load_config()
    watchlist = cfg.get("watchlist", [])
    use_fmp = bool(os.environ.get("FMP_API_KEY"))

    def attempt(symbol: str) -> dict:
        try:
            return score_one(symbol, cfg, use_fmp=use_fmp)
        except fh.FinnhubError as exc:
            return {"symbol": symbol, "error": str(exc)}

    records: list[dict] = []
    for symbol in watchlist:
        records.append(attempt(symbol))
        time.sleep(1.1)  # stay polite vs the 60 calls/min free tier

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


def main() -> None:
    cfg = load_config()
    payload = run(cfg)
    write_outputs(payload)
    ok = sum(1 for r in payload["tickers"] if "error" not in r)
    print(f"Wrote docs/data/latest.json — {ok}/{payload['count']} tickers scored.")


if __name__ == "__main__":
    main()
