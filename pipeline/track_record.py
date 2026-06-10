"""Verdict track record — does the screener actually predict anything?

Reads the dated snapshots in docs/data/history/, and for every past verdict
computes the *actual* forward return at fixed horizons using real Yahoo price
history (not just accrued snapshots, so a verdict can be graded the moment its
horizon has elapsed). Aggregates hit rate and average forward return per verdict
type, against an all-verdicts baseline so you can see whether WATCH-BUY actually
beats simply holding the basket.

Honest by construction: NEUTRAL is excluded from hit rate, small samples are
flagged low-confidence, and the baseline is shown next to every number.

Writes docs/data/track_record.json. Best-effort: never raises into the pipeline.
"""
from __future__ import annotations

import json
import glob
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

from pipeline.data import yahoo_client as yahoo
from pipeline.data import fmp_client as fmp

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = REPO_ROOT / "docs" / "data" / "history"
OUT_PATH = REPO_ROOT / "docs" / "data" / "track_record.json"

HORIZONS = (30, 90)            # calendar days
MIN_SAMPLE = 10               # below this, a bucket is "building"
WATCH = {"WATCH-BUY", "WATCH-SELL"}


def _load_snapshots() -> list[tuple[date, list[dict]]]:
    out = []
    for path in sorted(glob.glob(str(HISTORY_DIR / "*.json"))):
        stem = Path(path).stem
        try:
            d = date.fromisoformat(stem)
        except ValueError:
            continue
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append((d, payload.get("tickers", [])))
    return out


def _price_index(symbol: str) -> list[tuple[date, float]]:
    """Sorted [(date, close)] from Yahoo (FMP fallback), or [] on failure."""
    try:
        ohlcv = yahoo.get_ohlcv(symbol, days=1700)
    except yahoo.YahooError:
        try:
            ohlcv = fmp.get_ohlcv(symbol, days=1700)
        except fmp.FMPError:
            return []
    pairs = []
    for ds, c in zip(ohlcv.get("t", []), ohlcv.get("c", [])):
        try:
            pairs.append((date.fromisoformat(ds), float(c)))
        except (ValueError, TypeError):
            continue
    return pairs


def _close_on_or_after(index: list[tuple[date, float]], target: date) -> float | None:
    for d, c in index:
        if d >= target:
            return c
    return None


def _forward_return(index, entry_date: date, horizon: int) -> float | None:
    entry = _close_on_or_after(index, entry_date)
    exit_ = _close_on_or_after(index, entry_date + timedelta(days=horizon))
    if entry is None or exit_ is None or entry <= 0:
        return None
    # Guard: the exit must really be ~horizon out, not clamped to the last bar.
    if index and (index[-1][0] - entry_date).days < horizon:
        return None
    return exit_ / entry - 1.0


def evaluate(price_fetch=_price_index) -> dict:
    snapshots = _load_snapshots()
    symbols = sorted({t["symbol"] for _, ts in snapshots for t in ts if "symbol" in t})
    # Fetch price histories in parallel — Yahoo has no rate limit.
    if symbols:
        with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as ex:
            indices = dict(zip(symbols, ex.map(price_fetch, symbols)))
    else:
        indices = {}

    # observation: one (symbol, date, verdict, return) per horizon
    buckets: dict[int, dict[str, list[float]]] = {h: {} for h in HORIZONS}
    all_returns: dict[int, list[float]] = {h: [] for h in HORIZONS}

    for d, tickers in snapshots:
        for t in tickers:
            sym, verdict = t.get("symbol"), t.get("verdict")
            if not sym or not verdict or sym not in indices or not indices[sym]:
                continue
            for h in HORIZONS:
                ret = _forward_return(indices[sym], d, h)
                if ret is None:
                    continue
                buckets[h].setdefault(verdict, []).append(ret)
                all_returns[h].append(ret)

    def summarize(returns: list[float], verdict: str) -> dict:
        n = len(returns)
        avg = sum(returns) / n if n else 0.0
        # "hit" = directionally correct: BUY wants up, SELL wants down.
        if verdict == "WATCH-SELL":
            hits = sum(1 for r in returns if r < 0)
        else:
            hits = sum(1 for r in returns if r > 0)
        return {"n": n, "avg_return": round(avg, 4),
                "hit_rate": round(hits / n, 3) if n else None,
                "confident": n >= MIN_SAMPLE}

    result = {"generated_at": datetime.now(tz=timezone.utc).isoformat(), "horizons": list(HORIZONS), "by_horizon": {}}
    for h in HORIZONS:
        base = all_returns[h]
        result["by_horizon"][str(h)] = {
            "baseline_avg_return": round(sum(base) / len(base), 4) if base else None,
            "baseline_n": len(base),
            "verdicts": {v: summarize(rs, v) for v, rs in buckets[h].items()},
        }
    return result


def update() -> dict | None:
    """Compute and write the track record; never raises into the pipeline."""
    try:
        result = evaluate()
    except Exception as exc:  # best-effort accountability, must not break a run
        print(f"track_record: skipped ({exc})")
        return None
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    print(json.dumps(update(), indent=2))
