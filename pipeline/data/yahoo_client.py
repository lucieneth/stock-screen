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


def _range_for(days: int) -> str:
    for cap, label in ((150, "6mo"), (340, "1y"), (680, "2y"), (1700, "5y")):
        if days <= cap:
            return label
    return "10y"


def get_ohlcv(symbol: str, days: int = 400, session: requests.Session | None = None) -> dict:
    """Daily OHLCV as a Finnhub-shaped dict (oldest -> newest)."""
    session = session or requests.Session()
    params = {"range": _range_for(days), "interval": "1d"}
    backoff = 1.0
    last_exc: Exception | None = None
    for attempt in range(3):
        # Rotate hosts between attempts — datacenter IPs (e.g. GitHub Actions
        # runners) sometimes get bot-walled on one edge but not the other.
        base = BASE_URLS[attempt % len(BASE_URLS)]
        try:
            resp = session.get(base + symbol, params=params, headers=HEADERS, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code in (401, 403, 429, 999) or resp.status_code >= 500:
            # 401/403/999 are Yahoo's bot-wall responses; worth retrying on the
            # other host rather than failing immediately.
            last_exc = YahooError(f"HTTP {resp.status_code}")
            time.sleep(backoff)
            backoff *= 2
            continue
        if not resp.ok:
            raise YahooError(f"HTTP {resp.status_code}: {resp.text[:160]}")
        return _parse(resp.json(), symbol, days)
    raise YahooError(f"{symbol}: failed after retries: {last_exc}")


def get_many(symbols: list[str], days: int = 400, workers: int = 4) -> dict[str, dict]:
    """Fetch OHLCV for many symbols in parallel (Yahoo has no rate limit).

    Returns {symbol: ohlcv_dict} where a failed fetch is {"error": "..."} so the
    caller can degrade per-ticker rather than abort.
    """
    def one(sym: str):
        try:
            return sym, get_ohlcv(sym, days=days)
        except YahooError as exc:
            return sym, {"error": str(exc)}
    if not symbols:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(symbols))) as ex:
        return dict(ex.map(one, symbols))


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
