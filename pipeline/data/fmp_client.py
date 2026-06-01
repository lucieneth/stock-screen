"""FMP client — OHLCV + fundamentals fallback for Finnhub's free-tier gaps. (Phase 4)

Finnhub free keys gate /stock/candle (OHLCV) and /news-sentiment, so on a free
plan technicals/fundamentals can come up empty. This client pulls the same data
from Financial Modeling Prep and returns it in shapes the existing checks
already understand:

  - get_ohlcv()       -> {"c":[...closes oldest->newest...], "t":[...], "s":"ok"}
                         (same shape as finnhub_client.get_ohlcv)
  - get_fundamentals() -> a metric dict keyed with the SAME names
                         checks/fundamentals.py looks up (peTTM, ...)

Reads FMP_API_KEY from the environment (GitHub Actions secret). Never hard-code.

    FMP_API_KEY=xxxx python -m pipeline.data.fmp_client AAPL
"""
from __future__ import annotations

import os
import sys
import json
import time

import requests

BASE_URL = "https://financialmodelingprep.com/api/v3"
DEFAULT_TIMEOUT = 15


class FMPError(RuntimeError):
    """Raised when the FMP API returns an error or no usable data."""


def _api_key() -> str:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise FMPError(
            "FMP_API_KEY is not set. Export it (or supply it via GitHub Actions "
            "secrets) before using the FMP fallback."
        )
    return key


def _get(session: requests.Session, path: str, params: dict, *, retries: int = 3):
    params = {**params, "apikey": _api_key()}
    url = f"{BASE_URL}{path}"
    backoff = 1.0
    last_exc: Exception | None = None
    for _ in range(retries):
        try:
            resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            last_exc = exc
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last_exc = FMPError(f"{path} -> HTTP {resp.status_code}")
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code in (401, 403):
            raise FMPError(f"{path} -> HTTP {resp.status_code} (key/plan issue).")
        if not resp.ok:
            raise FMPError(f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if isinstance(data, dict) and data.get("Error Message"):
            raise FMPError(f"{path}: {data['Error Message']}")
        return data
    raise FMPError(f"{path} failed after {retries} attempts: {last_exc}")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_ohlcv(symbol: str, days: int = 400, session: requests.Session | None = None) -> dict:
    """Daily candles as a Finnhub-shaped dict (closes oldest -> newest)."""
    session = session or requests.Session()
    # `timeseries` caps the number of trading days returned.
    data = _get(session, f"/historical-price-full/{symbol}", {"timeseries": days})
    hist = data.get("historical") if isinstance(data, dict) else None
    if not hist:
        raise FMPError(f"No historical prices for {symbol!r} from FMP.")
    rows = list(reversed(hist))  # FMP returns most-recent first
    return {
        "s": "ok",
        "t": [r.get("date") for r in rows],
        "o": [_to_float(r.get("open")) for r in rows],
        "h": [_to_float(r.get("high")) for r in rows],
        "l": [_to_float(r.get("low")) for r in rows],
        "c": [_to_float(r.get("close")) for r in rows],
        "v": [_to_float(r.get("volume")) for r in rows],
    }


def get_fundamentals(symbol: str, session: requests.Session | None = None) -> dict:
    """Fundamentals normalized to the keys checks/fundamentals.py looks up."""
    session = session or requests.Session()
    metric: dict = {}

    try:
        ratios = _get(session, f"/ratios-ttm/{symbol}", {})
        if isinstance(ratios, list) and ratios:
            r = ratios[0]
            pe = r.get("peRatioTTM") or r.get("priceEarningsRatioTTM")
            if pe is not None:
                metric["peTTM"] = pe
            de = r.get("debtEquityRatioTTM") or r.get("debtToEquityTTM")
            if de is not None:
                metric["totalDebt/totalEquityQuarterly"] = de
    except FMPError:
        pass

    try:
        km = _get(session, f"/key-metrics-ttm/{symbol}", {})
        if isinstance(km, list) and km:
            fcf = km[0].get("freeCashFlowPerShareTTM")
            if fcf is not None:
                metric["freeCashFlowTTM"] = fcf
            if "peTTM" not in metric and km[0].get("peRatioTTM") is not None:
                metric["peTTM"] = km[0]["peRatioTTM"]
    except FMPError:
        pass

    try:
        growth = _get(session, f"/financial-growth/{symbol}", {"period": "annual", "limit": 1})
        if isinstance(growth, list) and growth and growth[0].get("revenueGrowth") is not None:
            metric["revenueGrowthTTMYoy"] = growth[0]["revenueGrowth"]
    except FMPError:
        pass

    if not metric:
        raise FMPError(f"No usable fundamentals for {symbol!r} from FMP.")
    return metric


def main(argv: list[str]) -> int:
    symbol = (argv[1] if len(argv) > 1 else "AAPL").upper()
    try:
        out = {
            "symbol": symbol,
            "ohlcv_candles": len(get_ohlcv(symbol)["c"]),
            "fundamentals": get_fundamentals(symbol),
        }
    except FMPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
