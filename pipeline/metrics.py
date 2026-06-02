"""Fundamental metric registry + extraction/normalization + peer labelling.

Finnhub reports ratio-style metrics as percentages (44.0) while FMP reports them
as fractions (0.44) — and some keys share a name across providers. So we keep
each provider's dict separate and normalize by *provenance* (a Finnhub percent
stays, an FMP fraction is ×100) rather than guessing from the magnitude.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    finnhub: list[str]              # Finnhub basic-financials keys (percent form)
    fmp: list[str]                  # FMP ratios/key-metrics keys (fraction form)
    direction: str                  # 'lower_better' | 'higher_better' | 'neutral'
    vocab: str                      # 'valuation' | 'higher' | 'lower' | 'neutral'
    unit: str = ""                  # "" | "%" | "x"
    pct: bool = False               # ratio shown as a percent (needs FMP ×100)
    de_like: bool = False           # debt/equity may arrive in percent form


METRICS: list[Metric] = [
    Metric("pe", "P/E", ["peTTM", "peBasicExclExtraTTM", "peNormalizedAnnual"], ["peRatioTTM", "priceEarningsRatioTTM"], "lower_better", "valuation", "x"),
    Metric("pb", "P/B", ["pbQuarterly", "pbAnnual"], ["priceToBookRatioTTM", "pbRatioTTM"], "lower_better", "valuation", "x"),
    Metric("ps", "P/S", ["psTTM", "psAnnual"], ["priceToSalesRatioTTM"], "lower_better", "valuation", "x"),
    Metric("gross_margin", "Gross margin", ["grossMarginTTM", "grossMarginAnnual"], ["grossProfitMarginTTM"], "higher_better", "higher", "%", pct=True),
    Metric("net_margin", "Net margin", ["netProfitMarginTTM", "netProfitMarginAnnual"], ["netProfitMarginTTM"], "higher_better", "higher", "%", pct=True),
    Metric("roe", "ROE", ["roeTTM", "roeAnnual"], ["returnOnEquityTTM"], "higher_better", "higher", "%", pct=True),
    Metric("roa", "ROA", ["roaTTM", "roaAnnual"], ["returnOnAssetsTTM"], "higher_better", "higher", "%", pct=True),
    Metric("current_ratio", "Current ratio", ["currentRatioQuarterly", "currentRatioAnnual"], ["currentRatioTTM"], "higher_better", "higher", "x"),
    Metric("debt_to_equity", "Debt/Equity", ["totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual", "longTermDebt/equityQuarterly"], ["debtEquityRatioTTM", "debtToEquityTTM"], "lower_better", "lower", "x", de_like=True),
    Metric("rev_growth", "Rev growth YoY", ["revenueGrowthTTMYoy", "revenueGrowthQuarterlyYoy"], ["revenueGrowth"], "higher_better", "higher", "%", pct=True),
    Metric("dividend_yield", "Dividend yield", ["dividendYieldIndicatedAnnual", "currentDividendYieldTTM"], ["dividendYielTTM", "dividendYieldTTM"], "neutral", "neutral", "%", pct=True),
    Metric("fcf", "Free cash flow", ["freeCashFlowTTM", "freeCashFlowAnnual"], ["freeCashFlowTTM", "freeCashFlowPerShareTTM"], "higher_better", "higher", ""),
]

BY_KEY = {m.key: m for m in METRICS}

# metric key -> Finnhub /stock/metric `series.quarterly` field (for charts).
SERIES_KEYS = {
    "pe": "pe", "pb": "pb", "ps": "ps",
    "gross_margin": "grossMargin", "net_margin": "netMargin",
    "roe": "roe", "roa": "roa",
    "current_ratio": "currentRatio", "debt_to_equity": "totalDebtToEquity",
}


def history_from_series(series: dict | None, max_points: int = 16) -> dict[str, list]:
    """Build {metric_key: [[period, value], ...]} from a Finnhub series block."""
    if not isinstance(series, dict):
        return {}
    quarterly = series.get("quarterly") if isinstance(series.get("quarterly"), dict) else {}
    out: dict[str, list] = {}
    for key, field in SERIES_KEYS.items():
        pts = quarterly.get(field)
        if not isinstance(pts, list) or not pts:
            continue
        series_pts = []
        for p in pts[-max_points:]:
            if isinstance(p, dict) and p.get("v") is not None and p.get("period"):
                try:
                    series_pts.append([p["period"], round(float(p["v"]), 3)])
                except (TypeError, ValueError):
                    continue
        if series_pts:
            out[key] = series_pts
    return out


def _raw(d: dict, keys: list[str]):
    for k in keys:
        v = d.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def extract(finnhub_fin: dict | None, fmp_fin: dict | None = None) -> dict[str, float]:
    """Pull metrics from the two provider dicts, normalizing by source."""
    fh = finnhub_fin if isinstance(finnhub_fin, dict) and "error" not in finnhub_fin else {}
    fp = fmp_fin if isinstance(fmp_fin, dict) and "error" not in fmp_fin else {}
    out: dict[str, float] = {}
    for m in METRICS:
        v = _raw(fh, m.finnhub)
        from_fmp = False
        if v is None:
            v = _raw(fp, m.fmp)
            from_fmp = True
        if v is None:
            continue
        if m.de_like and v > 5:          # 150 (percent) -> 1.5
            v = v / 100
        elif m.pct and from_fmp:         # FMP fraction -> percent
            v = v * 100
        out[m.key] = v
    return out


def fmt(m: Metric, value: float) -> str:
    if value is None:
        return "—"
    if m.unit == "%":
        return f"{value:.1f}%"
    if m.unit == "x":
        return f"{value:.1f}×"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.2f}"


# --- sector-relative labelling ------------------------------------------------

_BAND = 0.10  # within ±10% of the peer benchmark counts as "in line"

_TONE = {
    "cheap": "good", "strong": "good", "lean": "good",
    "expensive": "bad", "weak": "bad", "negative": "bad", "heavy": "bad",
    "in line": "neutral", "high": "neutral", "low": "neutral",
}

# (below-benchmark word, above-benchmark word) per vocabulary
_WORDS = {
    "valuation": ("cheap", "expensive"),   # lower is cheaper
    "higher": ("weak", "strong"),          # higher is better
    "lower": ("lean", "heavy"),            # lower is better (e.g. debt)
    "neutral": ("low", "high"),
}


def label(m: Metric, value: float, benchmark: float) -> str | None:
    if benchmark is None or benchmark == 0:
        return None
    if m.vocab == "valuation" and value <= 0:
        return "negative"
    below, above = _WORDS[m.vocab]
    if value < benchmark * (1 - _BAND):
        return below
    if value > benchmark * (1 + _BAND):
        return above
    return "in line"


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def annotate_records(records: list[dict], benchmarks: dict[str, dict] | None = None) -> None:
    """Attach a sector-relative `fundamentals` list to each record in place.

    benchmarks: {symbol: {"values": {key: float}, "source": {key: str}, "peers": int}}
    where the benchmark per metric is the median of the company's real industry
    peers (and, for P/E, optionally the whole-sector value). Metrics without a
    benchmark are shown without a label.
    """
    benchmarks = benchmarks or {}
    for r in records:
        if "error" in r:
            continue
        bench = benchmarks.get(r["symbol"], {})
        bvals = bench.get("values", {})
        bsrc = bench.get("source", {})
        vals = r.get("_metric_values", {})
        out = []
        for m in METRICS:
            if m.key not in vals:
                continue
            value = vals[m.key]
            benchmark = bvals.get(m.key)
            word = label(m, value, benchmark) if benchmark is not None else None
            out.append({
                "key": m.key, "label": m.label,
                "value": round(value, 4), "display": fmt(m, value),
                "sector_benchmark": round(benchmark, 4) if benchmark is not None else None,
                "benchmark_source": bsrc.get(m.key, "peers") if benchmark is not None else None,
                "word": word, "tone": _TONE.get(word) if word else None,
            })
        r["fundamentals"] = out
        r["peers_in_sector"] = bench.get("peers", 0)
        r.pop("_metric_values", None)
