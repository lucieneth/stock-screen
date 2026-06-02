"""Peer-group benchmarks for fundamentals.

For each watchlist company we pull its real industry peers from Finnhub
(/stock/peers), fetch each peer's basic financials, and take the median per
metric. That's a far less biased benchmark than "other companies that happen to
be on your watchlist". For P/E we additionally layer FMP's whole-sector value
when available.

Peer fundamentals change quarterly, so results are cached on disk (committed by
the workflow) and only refetched past the TTL — keeping us well under the free
60-calls/min Finnhub limit in steady state.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from pipeline import metrics
from pipeline.data import finnhub_client as fh

CACHE_PATH = Path(__file__).resolve().parent.parent / "cache" / "benchmarks.json"
TTL_DAYS = 10
MAX_PEERS = 12


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def _fresh(entry: dict, ttl_days: int) -> bool:
    try:
        ts = datetime.fromisoformat(entry["ts"])
    except (KeyError, ValueError):
        return False
    return (datetime.now(tz=timezone.utc) - ts).days < ttl_days


def build_benchmarks(symbols: list[str], *, ttl_days: int = TTL_DAYS, pause: float = 0.3) -> dict[str, dict]:
    """Return {symbol: {"values":{k:median}, "source":{k:"peers"}, "peers":n}}."""
    cache = _load_cache()
    out: dict[str, dict] = {}
    peer_metrics_memo: dict[str, dict] = {}  # peer symbol -> extracted metrics (this run)

    for sym in symbols:
        entry = cache.get(sym)
        if entry and _fresh(entry, ttl_days):
            out[sym] = {"values": entry["values"], "source": {k: "peers" for k in entry["values"]},
                        "peers": entry.get("peers", 0)}
            continue

        try:
            peers = fh.get_peers(sym)
        except fh.FinnhubError:
            peers = []
        peers = [p for p in dict.fromkeys(peers) if p != sym][:MAX_PEERS]

        collected: dict[str, list[float]] = {}
        for p in peers:
            m = peer_metrics_memo.get(p)
            if m is None:
                try:
                    m = metrics.extract(fh.get_basic_financials(p), None)
                except fh.FinnhubError:
                    m = {}
                peer_metrics_memo[p] = m
                time.sleep(pause)  # be gentle with the 60/min free tier
            for k, v in m.items():
                collected.setdefault(k, []).append(v)

        values = {k: round(metrics._median(vs), 4) for k, vs in collected.items() if vs}
        cache[sym] = {"ts": datetime.now(tz=timezone.utc).isoformat(), "peers": len(peers), "values": values}
        out[sym] = {"values": values, "source": {k: "peers" for k in values}, "peers": len(peers)}

    _save_cache(cache)
    return out
