"""Yahoo Finance chart client — keyless daily OHLCV.

Finnhub gates /stock/candle on free plans and FMP's free key is unreliable, so
this is the primary price source: Yahoo's public chart JSON endpoint, no API key
and no per-minute quota. We call the endpoint directly (not via the yfinance
library, which the project avoids) and parse it ourselves.

Returns a Finnhub-shaped candle dict so checks/technicals.py works unchanged:
    {"s": "ok", "t": [...iso dates...], "o/h/l/c/v": [... oldest -> newest ...]}

    python -m pipeline.data.yahoo_client AAPL
"""
from __future__ import annotations

import sys
import json
import time
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

BASE_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/",
    "https://query2.finance.yahoo.com/v8/finance/chart/",
)
BASE_URL = BASE_URLS[0]  # kept for callers/tests that reference it
# Yahoo rejects the default python-requests UA; present as a browser.
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
DEFAULT_TIMEOUT = 15


class YahooError(RuntimeError):
    """Raised when Yahoo returns an error or no usable price data."""


def _retry_wait(resp, backoff: float, cap: float = 15.0) -> float:
    """Wait this long before retrying: Retry-After header if larger, capped.

    The cap is deliberately low — the bot-wall sends inflated Retry-After
    values, and with ~19 symbols a per-request minute would add 20+ minutes
    to the run. Symbols that stay walled fall through to FMP / stale cache.
    """
    try:
        retry_after = float(resp.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        retry_after = 0.0
    return min(max(retry_after, backoff), cap)


def _range_for(days: int) -> str:
    for cap, label in ((150, "6mo"), (340, "1y"), (680, "2y"), (1700, "5y")):
        if days <= cap:
            return label
    return "10y"


def get_ohlcv(symbol: str, days: int = 400, session: requests.Session | None = None,
              attempts: int = 3) -> dict:
    """Daily OHLCV as a Finnhub-shaped dict (oldest -> newest)."""
    session = session or requests.Session()
    params = {"range": _range_for(days), "interval": "1d"}
    backoff = 1.0
    last_exc: Exception | None = None
    for attempt in range(attempts):
        # Rotate hosts between attempts — datacenter IPs (e.g. GitHub Actions
        # runners) sometimes get bot-walled on one edge but not the other.
        base = BASE_URLS[attempt % len(BASE_URLS)]
        try:
            resp = session.get(base + symbol, params=params, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(backoff)
                backoff *= 2
            continue
        if resp.status_code in (401, 403, 429, 999) or resp.status_code >= 500:
            # 401/403/999 are Yahoo's bot-wall responses; worth retrying on the
            # other host rather than failing immediately.
            last_exc = YahooError(f"HTTP {resp.status_code}")
            if attempt + 1 < attempts:
                time.sleep(_retry_wait(resp, backoff))
                backoff *= 2
            continue
        if not resp.ok:
            raise YahooError(f"HTTP {resp.status_code}: {resp.text[:160]}")
        return _parse(resp.json(), symbol, days)
    raise YahooError(f"{symbol}: failed after retries: {last_exc}")


def get_many(symbols: list[str], days: int = 400, workers: int = 2,
             jitter: float = 0.6, second_chance_wait: float = 45.0) -> dict[str, dict]:
    """Fetch OHLCV for many symbols, gently (Yahoo rate-limits datacenter IPs).

    Small worker pool + per-request jitter avoids the burst-429 that otherwise
    rate-limits the whole GitHub Actions runner. Returns {symbol: ohlcv_dict};
    a failed fetch is {"error": "..."} so callers degrade per-ticker.

    If the parallel pass leaves failures, one slow serial pass retries them
    after `second_chance_wait` seconds — Yahoo's burst-429 usually clears
    within a minute, and a missed symbol here costs a day of dashboard gaps.

    Both passes stop paying per-symbol retry waits once several symbols in a
    row have failed: at that point the runner IP is walled and waiting only
    stretches the job (FMP / stale cache pick up the slack downstream).
    """
    wall = {"strikes": 0}   # consecutive failures; shared, races are benign

    def one(sym: str):
        if jitter:
            time.sleep(random.uniform(0, jitter))
        try:
            result = get_ohlcv(sym, days=days, attempts=1 if wall["strikes"] >= 4 else 3)
            wall["strikes"] = 0
            return sym, result
        except YahooError as exc:
            wall["strikes"] += 1
            return sym, {"error": str(exc)}
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as ex:
        out = dict(ex.map(one, symbols))
    failed = [sym for sym, v in out.items() if "error" in v]
    if failed and second_chance_wait > 0:
        time.sleep(second_chance_wait)
        misses = 0
        for sym in failed:
            time.sleep(random.uniform(0.5, 1.5))  # serial + paced, no burst
            try:
                out[sym] = get_ohlcv(sym, days=days, attempts=2)
                misses = 0
            except YahooError as exc:
                out[sym] = {"error": str(exc)}
                misses += 1
                if misses >= 3:
                    break   # wall hasn't lifted — keep the recorded errors
    return out


def _parse(payload: dict, symbol: str, days: int) -> dict:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise YahooError(f"{symbol}: {chart['error']}")
    results = chart.get("result") or []
    if not results:
        raise YahooError(f"{symbol}: empty chart result")
    res = results[0]
    timestamps = res.get("timestamp") or []
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    if not timestamps or not closes:
        raise YahooError(f"{symbol}: no candles returned")

    o, h, l, c, t, v = [], [], [], [], [], []
    opens, highs, lows, vols = (quote.get("open") or []), (quote.get("high") or []), \
        (quote.get("low") or []), (quote.get("volume") or [])
    for i, ts in enumerate(timestamps):
        close = closes[i] if i < len(closes) else None
        if close is None:  # Yahoo nulls out holidays / halts
            continue
        t.append(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"))
        c.append(float(close))
        o.append(_num(opens, i, close))
        h.append(_num(highs, i, close))
        l.append(_num(lows, i, close))
        v.append(_num(vols, i, 0))

    if not c:
        raise YahooError(f"{symbol}: no non-null closes")
    sl = slice(-days, None)
    return {"s": "ok", "t": t[sl], "o": o[sl], "h": h[sl], "l": l[sl], "c": c[sl], "v": v[sl]}


def _num(arr, i, default):
    if i < len(arr) and arr[i] is not None:
        try:
            return float(arr[i])
        except (TypeError, ValueError):
            return default
    return default


def main(argv: list[str]) -> int:
    symbol = (argv[1] if len(argv) > 1 else "AAPL").upper()
    try:
        data = get_ohlcv(symbol)
    except YahooError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"symbol": symbol, "candles": len(data["c"]),
                      "first": data["t"][0], "last": data["t"][-1],
                      "last_close": data["c"][-1]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
