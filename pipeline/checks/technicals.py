"""Technicals check -> score in -1..+1 plus reasons/flags. (Phase 2)

Indicators are computed directly with pandas (SMA50/200, RSI14, MACD) rather
than pandas-ta: that package is unavailable for Python 3.11 here and its 0.3.x
line breaks under NumPy 2.x. The math below is standard and dependency-light.

Input is a Finnhub candle dict: {"c": [...closes...], "s": "ok", ...}.
"""
from __future__ import annotations

import pandas as pd


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(100)  # all-gains window -> RSI 100


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line


def check(ohlcv: dict, cfg: dict) -> dict:
    """Return {score, reasons, flags, metrics} for the technicals dimension."""
    reasons: list[str] = []
    flags: list[str] = []

    if not isinstance(ohlcv, dict) or "error" in ohlcv:
        msg = ohlcv.get("error") if isinstance(ohlcv, dict) else "no OHLCV"
        return {"score": 0.0, "reasons": [f"technicals: unavailable ({msg})"], "flags": [], "metrics": {}}

    closes = ohlcv.get("c") or []
    fast_w = int(cfg.get("sma_fast", 50))
    slow_w = int(cfg.get("sma_slow", 200))
    if len(closes) < slow_w:
        return {
            "score": 0.0,
            "reasons": [f"technicals: only {len(closes)} candles (<{slow_w} needed)"],
            "flags": [],
            "metrics": {"candles": len(closes)},
        }

    close = pd.Series(closes, dtype="float64")
    sma_fast = close.rolling(fast_w).mean().iloc[-1]
    sma_slow = close.rolling(slow_w).mean().iloc[-1]
    rsi = float(_rsi(close).iloc[-1])
    macd_line, signal_line = _macd(close)
    macd_v, sig_v = float(macd_line.iloc[-1]), float(signal_line.iloc[-1])
    metrics = {
        "sma_fast": round(float(sma_fast), 2),
        "sma_slow": round(float(sma_slow), 2),
        "rsi": round(rsi, 1),
        "macd": round(macd_v, 3),
        "macd_signal": round(sig_v, 3),
    }

    score = 0.0

    # 12-1 momentum (heaviest weight — the best-evidenced technical factor):
    # return from ~12 months ago to ~1 month ago, skipping the last month to
    # avoid short-term reversal. Scaled so `mom_full_scale` (default 30%) earns
    # the full ±0.4 contribution.
    lookback = int(cfg.get("mom_lookback", 252))
    skip = int(cfg.get("mom_skip", 21))
    full_scale = float(cfg.get("mom_full_scale", 0.30))
    if len(closes) >= lookback + skip and closes[-lookback] > 0:
        mom = closes[-skip] / closes[-lookback] - 1.0
        contrib = max(-1.0, min(1.0, mom / full_scale)) * 0.4
        score += contrib
        reasons.append(f"12-1 momentum {mom * 100:+.0f}% ({'positive' if mom > 0 else 'negative'})")
        metrics["momentum_12_1"] = round(mom, 4)
    else:
        reasons.append(f"12-1 momentum unavailable (<{lookback + skip} candles)")

    # Trend: SMA50 vs SMA200.
    if sma_fast > sma_slow:
        score += 0.3
        reasons.append(f"SMA{fast_w} ({sma_fast:.2f}) above SMA{slow_w} ({sma_slow:.2f}) — uptrend")
    else:
        score -= 0.3
        reasons.append(f"SMA{fast_w} ({sma_fast:.2f}) below SMA{slow_w} ({sma_slow:.2f}) — downtrend")

    # RSI(14) — deliberately small: daily RSI mean-reversion is weak evidence,
    # so extremes nudge the score and mostly serve as flags.
    oversold = float(cfg.get("rsi_oversold", 30))
    overbought = float(cfg.get("rsi_overbought", 70))
    if rsi < oversold:
        score += 0.15
        reasons.append(f"RSI {rsi:.0f} < {oversold:.0f} (oversold)")
        flags.append("rsi_oversold")
    elif rsi > overbought:
        score -= 0.15
        reasons.append(f"RSI {rsi:.0f} > {overbought:.0f} (overbought)")
        flags.append("rsi_overbought")
    else:
        reasons.append(f"RSI {rsi:.0f} (neutral)")

    # MACD crossover.
    if macd_v > sig_v:
        score += 0.15
        reasons.append("MACD above signal (bullish)")
    else:
        score -= 0.15
        reasons.append("MACD below signal (bearish)")

    score = max(-1.0, min(1.0, score))
    return {"score": round(score, 3), "reasons": reasons, "flags": flags, "metrics": metrics}
