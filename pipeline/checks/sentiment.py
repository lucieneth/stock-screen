"""Sentiment check -> score in -1..+1 plus reasons/flags. (free, baseline-relative)

Financial headlines are structurally positive, so the absolute VADER level is a
constant offset, not a signal (live data: every ticker ~+0.3). The score is
therefore the ticker's *deviation from its own trailing baseline* (see
pipeline/sentiment_baseline.py), scaled by `deviation_gain`. Until a baseline
exists the dimension reports "building" and is excluded from coverage.

Order of preference:
  1. Finnhub /news-sentiment companyNewsScore (premium plans) — absolute.
  2. VADER over free company-news headlines, scored vs own baseline.
  3. Neutral, if there are no headlines at all.
"""
from __future__ import annotations

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Headline terms VADER doesn't weight well out of the box (scale roughly -4..4).
_FINANCE_LEXICON = {
    "beat": 2.0, "beats": 2.0, "tops": 1.8, "surge": 2.5, "surges": 2.5,
    "soar": 2.8, "soars": 2.8, "rally": 1.8, "upgrade": 2.2, "upgraded": 2.2,
    "outperform": 2.0, "record": 1.5, "raises": 1.5, "jumps": 1.8,
    "miss": -2.0, "misses": -2.0, "plunge": -2.8, "plunges": -2.8,
    "slump": -2.2, "downgrade": -2.2, "downgraded": -2.2, "cut": -1.5,
    "cuts": -1.5, "lawsuit": -1.8, "probe": -1.6, "recall": -1.8,
    "warning": -1.5, "bankruptcy": -3.0, "fraud": -3.0, "selloff": -2.2,
    "tumble": -2.2, "tumbles": -2.2, "underperform": -2.0,
}

_analyzer = SentimentIntensityAnalyzer()
_analyzer.lexicon.update(_FINANCE_LEXICON)


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def _from_finnhub(sentiment: dict) -> tuple[float, list[str]] | None:
    if not isinstance(sentiment, dict) or "error" in sentiment:
        return None
    score01 = sentiment.get("companyNewsScore")
    if score01 is None:
        return None
    try:
        s = (float(score01) - 0.5) * 2
    except (TypeError, ValueError):
        return None
    return _clamp(s), [f"News-sentiment score {float(score01):.2f} (Finnhub)"]


def _raw_from_headlines(news: list) -> tuple[float, int, int, int] | None:
    """(avg_compound, n_stories, n_positive, n_negative) or None if no text."""
    if not isinstance(news, list) or not news:
        return None
    compounds = []
    for item in news:
        text = " ".join(str(item.get(f, "")) for f in ("headline", "summary")).strip()
        if text:
            compounds.append(_analyzer.polarity_scores(text)["compound"])
    if not compounds:
        return None
    avg = sum(compounds) / len(compounds)
    pos = sum(1 for c in compounds if c > 0.05)
    neg = sum(1 for c in compounds if c < -0.05)
    return avg, len(compounds), pos, neg


def check(sentiment: dict, news: list | dict, cfg: dict, baseline: float | None = None) -> dict:
    """Return {score, reasons, flags, metrics} for the sentiment dimension."""
    flags: list[str] = []
    neg_spike = float(cfg.get("negative_spike", -0.25))
    gain = float(cfg.get("deviation_gain", 2.5))

    primary = _from_finnhub(sentiment)
    if primary is not None:
        score, reasons = primary
        if score < neg_spike:
            flags.append("negative_sentiment_spike")
            reasons.append("Negative sentiment spike")
        return {"score": round(score, 3), "reasons": reasons, "flags": flags,
                "metrics": {"source": "finnhub_news_sentiment"}}

    local = _raw_from_headlines(news if isinstance(news, list) else [])
    if local is None:
        return {"score": 0.0, "reasons": ["Sentiment: no recent headlines (neutral)"],
                "flags": [], "metrics": {"source": "none"}}

    raw, n, pos, neg = local
    metrics = {"source": "vader_headlines", "stories": n, "raw": round(raw, 3)}

    if baseline is None:
        # No trailing baseline yet — a structurally-positive absolute level is
        # noise, so report "building" and let coverage renormalize.
        return {"score": 0.0,
                "reasons": [f"sentiment: unavailable (baseline building; raw {raw:+.2f} over {n} stories)"],
                "flags": [], "metrics": metrics}

    dev = raw - baseline
    score = _clamp(dev * gain)
    metrics.update({"baseline": round(baseline, 3), "deviation": round(dev, 3)})
    reasons = [f"Headline sentiment {raw:+.2f} vs own baseline {baseline:+.2f} "
               f"(deviation {dev:+.2f}, {n} stories: {pos}+ / {neg}-)"]
    if dev < neg_spike:
        flags.append("negative_sentiment_spike")
        reasons.append("Negative sentiment spike vs baseline")
    return {"score": round(score, 3), "reasons": reasons, "flags": flags, "metrics": metrics}
