"""sentiment_baseline.py: deriving per-ticker baselines from history snapshots."""
import json

from pipeline import sentiment_baseline as sb


def _snap(path, rows):
    path.write_text(json.dumps({"tickers": rows}))


def _vader_row(symbol, avg, key="raw"):
    return {"symbol": symbol, "details": {"sentiment": {"source": "vader_headlines", key: avg}}}


def test_median_baseline_requires_min_obs(tmp_path):
    for i in range(5):
        _snap(tmp_path / f"2026-01-0{i + 1}.json", [_vader_row("AAPL", 0.30 + i * 0.01)])
    _snap(tmp_path / "2026-01-06.json", [_vader_row("MSFT", 0.5)])  # only 1 obs

    out = sb.load_baselines(tmp_path, min_obs=5)
    assert out["AAPL"] == 0.32          # median of 0.30..0.34
    assert "MSFT" not in out            # below min_obs


def test_legacy_avg_field_supported(tmp_path):
    for i in range(5):
        _snap(tmp_path / f"2026-01-0{i + 1}.json", [_vader_row("AAPL", 0.25, key="avg")])
    assert sb.load_baselines(tmp_path, min_obs=5)["AAPL"] == 0.25


def test_non_vader_and_malformed_rows_ignored(tmp_path):
    rows = [
        {"symbol": "X", "details": {"sentiment": {"source": "finnhub_news_sentiment"}}},
        {"symbol": "Y", "details": {}},
        {"symbol": "Z", "error": "rate limited"},
    ]
    for i in range(5):
        _snap(tmp_path / f"2026-01-0{i + 1}.json", rows)
    assert sb.load_baselines(tmp_path, min_obs=5) == {}


def test_window_uses_most_recent_snapshots(tmp_path):
    # 10 snapshots; window of 5 must only see the last five values.
    for i in range(10):
        val = 0.1 if i < 5 else 0.9
        _snap(tmp_path / f"2026-01-{i + 1:02d}.json", [_vader_row("AAPL", val)])
    out = sb.load_baselines(tmp_path, min_obs=5, max_snapshots=5)
    assert out["AAPL"] == 0.9
