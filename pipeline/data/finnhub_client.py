"""Finnhub client — quotes, news, OHLCV. (Phase 1)

Reads the API key from the FINNHUB_API_KEY environment variable, which is
supplied by GitHub Actions secrets. Never hard-code keys here, and never write
this file's output into docs/ without stripping secrets (there are none in the
data payload — only the key in the request URL, which we never persist).

Run as a script to smoke-test a single ticker:

    FINNHUB_API_KEY=xxxx python -m pipeline.data.finnhub_client AAPL

Finnhub endpoints used (free tier, https://finnhub.io/docs/api):
  - /quote          current price snapshot
  - /stock/candle   daily OHLCV  (note: candle access is gated on some free
                    plans; we surface that as an error field rather than crash)
  - /company-news   recent headlines (used for sentiment in Phase 2)
"""
from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone

import requests

BASE_URL = "https://finnhub.io/api/v1"
DEFAULT_TIMEOUT = 15  # seconds


class FinnhubError(RuntimeError):
    """Raised when the Finnhub API returns an error or no usable data."""


def _api_key() -> str:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        raise FinnhubError(
            "FINNHUB_API_KEY is not set. Export it (or supply it via GitHub "
            "Actions secrets) before running the pipeline."
        )
    return key


def _get(session: requests.Session, path: str, params: dict, *, retries: int = 3) -> dict | list:
    """GET a Finnhub endpoint with the API token, with basic retry on 429/5xx."""
    params = {**params, "token": _api_key()}
    url = f"{BASE_URL}{path}"
    backoff = 1.0
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:  # network error
            last_exc = exc
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            # rate-limited or server error -> back off and retry
            last_exc = FinnhubError(f"{path} -> HTTP {resp.status_code}")
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 403:
            raise FinnhubError(
                f"{path} -> HTTP 403 (no access on this API plan). "
                "Some Finnhub free plans gate /stock/candle."
            )
        if not resp.ok:
            raise FinnhubError(f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    raise FinnhubError(f"{path} failed after {retries} attempts: {last_exc}")


def get_quote(symbol: str, session: requests.Session | None = None) -> dict:
    """Current price snapshot: c=current, d=change, dp=%change, h/l/o/pc, t=ts."""
    session = session or requests.Session()
    data = _get(session, "/quote", {"symbol": symbol})
    if not isinstance(data, dict) or data.get("c") in (None, 0):
        raise FinnhubError(f"No quote data for {symbol!r} (got {data!r}).")
    return data


def get_ohlcv(
    symbol: str,
    days: int = 400,
    resolution: str = "D",
    session: requests.Session | None = None,
) -> dict:
    """Daily OHLCV candles for the last `days` days.

    Returns the raw Finnhub candle object: keys t,o,h,l,c,v and status `s`.
    `days=400` gives enough history for SMA200 (Phase 2 technicals).
    """
    session = session or requests.Session()
    now = datetime.now(tz=timezone.utc)
    start = now - timedelta(days=days)
    data = _get(
        session,
        "/stock/candle",
        {
            "symbol": symbol,
            "resolution": resolution,
            "from": int(start.timestamp()),
            "to": int(now.timestamp()),
        },
    )
    if isinstance(data, dict) and data.get("s") == "no_data":
        raise FinnhubError(f"No candle data for {symbol!r} in the last {days} days.")
    return data


def get_company_news(
    symbol: str,
    days: int = 7,
    session: requests.Session | None = None,
) -> list[dict]:
    """Recent company headlines over the last `days` days (most-recent first)."""
    session = session or requests.Session()
    now = datetime.now(tz=timezone.utc).date()
    start = now - timedelta(days=days)
    data = _get(
        session,
        "/company-news",
        {"symbol": symbol, "from": start.isoformat(), "to": now.isoformat()},
    )
    if not isinstance(data, list):
        raise FinnhubError(f"Unexpected news payload for {symbol!r}: {data!r}")
    return data


def get_company_metrics(symbol: str, session: requests.Session | None = None) -> dict:
    """Full /stock/metric payload -> {"metric": {...}, "series": {...}}.

    `metric` holds the current values; `series` holds historical quarterly/annual
    time-series (used for the fundamentals charts). Both are free-tier.
    """
    session = session or requests.Session()
    data = _get(session, "/stock/metric", {"symbol": symbol, "metric": "all"})
    if not isinstance(data, dict):
        raise FinnhubError(f"Unexpected metric payload for {symbol!r}: {data!r}")
    return {
        "metric": data.get("metric") if isinstance(data.get("metric"), dict) else {},
        "series": data.get("series") if isinstance(data.get("series"), dict) else {},
    }


def get_basic_financials(symbol: str, session: requests.Session | None = None) -> dict:
    """Company basic financials (the `metric` sub-object of /stock/metric)."""
    return get_company_metrics(symbol, session=session)["metric"]


def get_peers(symbol: str, session: requests.Session | None = None) -> list[str]:
    """Industry peer tickers (/stock/peers) — the real peer group, free tier."""
    session = session or requests.Session()
    data = _get(session, "/stock/peers", {"symbol": symbol})
    return [s for s in data if isinstance(s, str)] if isinstance(data, list) else []


def get_profile(symbol: str, session: requests.Session | None = None) -> dict:
    """Company profile (/stock/profile2) -> sector/industry/name (free tier)."""
    session = session or requests.Session()
    data = _get(session, "/stock/profile2", {"symbol": symbol})
    if not isinstance(data, dict):
        return {}
    return {
        "sector": data.get("finnhubIndustry"),  # Finnhub's closest field
        "industry": data.get("finnhubIndustry"),
        "companyName": data.get("name"),
    }


def get_news_sentiment(symbol: str, session: requests.Session | None = None) -> dict:
    """Finnhub news-sentiment (/news-sentiment).

    Note: this endpoint is premium on current Finnhub plans and commonly
    returns HTTP 403/401 on free keys. Callers should treat absence as
    "no sentiment data" rather than an error (see checks/sentiment.py).
    """
    session = session or requests.Session()
    data = _get(session, "/news-sentiment", {"symbol": symbol})
    if not isinstance(data, dict):
        raise FinnhubError(f"Unexpected sentiment payload for {symbol!r}: {data!r}")
    return data


def fetch_ticker(symbol: str, news_days: int = 7, ohlcv_days: int = 400) -> dict:
    """Aggregate quote + OHLCV + news for one ticker into a single dict.

    OHLCV and news failures are captured as `error` fields rather than aborting
    the whole fetch, so a candle-access limitation doesn't block the quote.
    """
    session = requests.Session()
    symbol = symbol.upper()
    result: dict = {"symbol": symbol, "fetched_at": datetime.now(tz=timezone.utc).isoformat()}

    result["quote"] = get_quote(symbol, session=session)

    try:
        result["ohlcv"] = get_ohlcv(symbol, days=ohlcv_days, session=session)
    except FinnhubError as exc:
        result["ohlcv"] = {"error": str(exc)}

    try:
        bundle = get_company_metrics(symbol, session=session)
        result["financials"] = bundle["metric"]
        result["series"] = bundle["series"]
    except FinnhubError as exc:
        result["financials"] = {"error": str(exc)}
        result["series"] = {}

    try:
        result["sentiment"] = get_news_sentiment(symbol, session=session)
    except FinnhubError as exc:
        result["sentiment"] = {"error": str(exc)}

    try:
        news = get_company_news(symbol, days=news_days, session=session)
        # Trim to the fields we actually use downstream, keep payload small.
        result["news"] = [
            {
                "datetime": item.get("datetime"),
                "headline": item.get("headline"),
                "source": item.get("source"),
                "url": item.get("url"),
                "summary": item.get("summary"),
            }
            for item in news[:50]
        ]
    except FinnhubError as exc:
        result["news"] = {"error": str(exc)}

    return result


def main(argv: list[str]) -> int:
    symbol = argv[1] if len(argv) > 1 else "AAPL"
    try:
        data = fetch_ticker(symbol)
    except FinnhubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
