"""FMP client — OHLCV + fundamentals + profile fallback for Finnhub gaps.

Finnhub free keys gate /stock/candle (OHLCV), so technicals come up empty
without a backup. This client pulls the same data from Financial Modeling Prep.

FMP migrated to a "stable" API; legacy /api/v3 paths are often refused on newer
free keys. So every call tries the stable endpoint first and falls back to
legacy, and errors carry the exact reason (status + FMP message) for diagnosis.

Returns shapes the existing checks already understand:
  - get_ohlcv()       -> {"c":[...closes oldest->newest...], "t":[...], "s":"ok"}
  - get_fundamentals() -> raw FMP ratios/key-metrics dict (metrics.extract knows the keys)
  - get_profile()     -> {"sector","industry","companyName"}

Reads FMP_API_KEY from the environment (GitHub Actions secret). Never hard-code.

    FMP_API_KEY=xxxx python -m pipeline.data.fmp_client AAPL
"""
from __future__ import annotations

import os
import sys
import json
import time
from datetime import datetime, timezone

import requests

BASE_STABLE = "https://financialmodelingprep.com/stable"
BASE_LEGACY = "https://financialmodelingprep.com/api/v3"
DEFAULT_TIMEOUT = 15


class FMPError(RuntimeError):
    """Raised when the FMP API returns an error or no usable data."""


def _api_key() -> str:
    key = os.environ.get("FMP_API_KEY")
    if not key:
        raise FMPError("FMP_API_KEY is not set (no env var / Actions secret reaching the job).")
    return key


def _request(session: requests.Session, url: str, params: dict, *, retries: int = 3):
    params = {**params, "apikey": _api_key()}
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
            last_exc = FMPError(f"HTTP {resp.status_code}")
            time.sleep(backoff)
            backoff *= 2
            continue
        if not resp.ok:
            raise FMPError(f"HTTP {resp.status_code}: {resp.text[:160]}")
        data = resp.json()
        if isinstance(data, dict) and (data.get("Error Message") or data.get("message")):
            raise FMPError(str(data.get("Error Message") or data.get("message"))[:160])
        return data
    raise FMPError(f"failed after {retries} attempts: {last_exc}")


def _get(session, stable_path: str, legacy_path: str, params: dict | None = None,
         legacy_params: dict | None = None):
    """Try the stable endpoint, then fall back to legacy; report both errors."""
    session = session or requests.Session()
    try:
        return _request(session, f"{BASE_STABLE}{stable_path}", params or {})
    except FMPError as stable_exc:
        try:
            return _request(session, f"{BASE_LEGACY}{legacy_path}", legacy_params or params or {})
        except FMPError as legacy_exc:
            raise FMPError(f"stable[{stable_path}]: {stable_exc} | legacy[{legacy_path}]: {legacy_exc}")


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _first_row(data) -> dict:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return data if isinstance(data, dict) else {}


def get_ohlcv(symbol: str, days: int = 400, session: requests.Session | None = None) -> dict:
    """Daily candles as a Finnhub-shaped dict (closes oldest -> newest)."""
    session = session or requests.Session()
    data = _get(
        session,
        "/historical-price-eod/full", f"/historical-price-full/{symbol}",
        params={"symbol": symbol},
    )
    # stable -> list of rows; legacy -> {"historical": [...]}
    rows = data.get("historical") if isinstance(data, dict) else data
    if not rows:
        raise FMPError(f"no historical prices for {symbol!r}")
    rows = sorted(rows, key=lambda r: r.get("date", ""))[-days:]  # oldest -> newest
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
    """Merged TTM ratios + key-metrics + revenue growth (raw FMP keys)."""
    session = session or requests.Session()
    metric: dict = {}

    for stable, legacy in (("/ratios-ttm", f"/ratios-ttm/{symbol}"),
                           ("/key-metrics-ttm", f"/key-metrics-ttm/{symbol}")):
        try:
            row = _first_row(_get(session, stable, legacy, params={"symbol": symbol}))
            for k, v in row.items():
                if v is not None and k not in metric:
                    metric[k] = v
        except FMPError:
            pass

    if "freeCashFlowTTM" not in metric and metric.get("freeCashFlowPerShareTTM") is not None:
        metric["freeCashFlowTTM"] = metric["freeCashFlowPerShareTTM"]

    try:
        row = _first_row(_get(session, "/financial-growth", f"/financial-growth/{symbol}",
                              params={"symbol": symbol, "period": "annual", "limit": 1},
                              legacy_params={"period": "annual", "limit": 1}))
        if row.get("revenueGrowth") is not None:
            metric["revenueGrowth"] = row["revenueGrowth"]
    except FMPError:
        pass

    if not metric:
        raise FMPError(f"no usable fundamentals for {symbol!r}")
    return metric


def get_profile(symbol: str, session: requests.Session | None = None) -> dict:
    """Company profile -> {sector, industry, companyName}."""
    session = session or requests.Session()
    row = _first_row(_get(session, "/profile", f"/profile/{symbol}", params={"symbol": symbol}))
    return {
        "sector": row.get("sector"),
        "industry": row.get("industry"),
        "companyName": row.get("companyName"),
    }


def get_sector_pe(sector: str, date: str | None = None, session: requests.Session | None = None) -> float:
    """Whole-sector P/E for `sector` (FMP sector-PE snapshot). May be premium."""
    session = session or requests.Session()
    date = date or datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    data = _get(session, "/sector-pe-snapshot", "/sector_price_earning_ratio",
                params={"date": date, "sector": sector},
                legacy_params={"date": date, "exchange": "NYSE"})
    rows = data if isinstance(data, list) else [data]
    for row in rows:
        if isinstance(row, dict) and str(row.get("sector", "")).lower() == sector.lower():
            pe = _to_float(row.get("pe") or row.get("priceEarningRatio"))
            if pe:
                return pe
    raise FMPError(f"no sector P/E for {sector!r}")


def main(argv: list[str]) -> int:
    symbol = (argv[1] if len(argv) > 1 else "AAPL").upper()
    out: dict = {"symbol": symbol}
    for name, fn in (("ohlcv", lambda: {"candles": len(get_ohlcv(symbol)["c"])}),
                     ("fundamentals", lambda: get_fundamentals(symbol)),
                     ("profile", lambda: get_profile(symbol))):
        try:
            out[name] = fn()
        except FMPError as exc:
            out[name] = {"error": str(exc)}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
