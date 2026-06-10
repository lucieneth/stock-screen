"""run.py end-to-end with all providers mocked: fallback order, finalize, retry."""
import math

import pytest

from pipeline import run as runner
from pipeline import peers

CFG = {
    "watchlist": ["AAPL", "JPM"],
    "weights": {"fundamentals": 0.4, "technicals": 0.35, "sentiment": 0.25},
    "verdict_bands": {"watch_buy": 0.25, "watch_sell": -0.25},
    "thresholds": {
        "fundamentals": {"pe_good": 25, "pe_flag": 40, "rev_growth_yoy": 0.05,
                         "debt_to_equity_max": 1.5, "require_positive_fcf": True},
        "technicals": {"sma_fast": 50, "sma_slow": 200, "rsi_oversold": 30, "rsi_overbought": 70},
        "sentiment": {"negative_spike": -0.5},
    },
    "alerts": {"daily_move_pct": 5.0, "flag_52wk_high": True, "flag_52wk_low": True},
}

FIN = {
    "AAPL": {"peTTM": 30, "pbAnnual": 30, "netProfitMarginTTM": 27, "roeTTM": 150,
             "roaTTM": 30, "currentRatioQuarterly": 1.0, "totalDebt/totalEquityQuarterly": 1.0,
             "revenueGrowthTTMYoy": 12, "psTTM": 9, "grossMarginTTM": 45},
    "JPM": {"peTTM": 12, "pbAnnual": 2.0, "netProfitMarginTTM": 33, "roeTTM": 16,
            "revenueGrowthTTMYoy": 8},
}
SECTOR = {"AAPL": "Technology", "JPM": "Banking"}
PEERS = {"AAPL": ["AAPL", "DELL", "HPQ"], "JPM": ["JPM", "BAC", "WFC"]}
PEER_FIN = {
    "DELL": {"peTTM": 18, "netProfitMarginTTM": 5, "roeTTM": 120, "pbAnnual": 40,
             "roaTTM": 6, "currentRatioQuarterly": 0.8, "totalDebt/totalEquityQuarterly": 2.0,
             "revenueGrowthTTMYoy": 5, "psTTM": 1, "grossMarginTTM": 22},
    "HPQ": {"peTTM": 12, "netProfitMarginTTM": 6, "roeTTM": 80, "pbAnnual": 10,
            "roaTTM": 7, "revenueGrowthTTMYoy": 3, "psTTM": 0.7, "grossMarginTTM": 21,
            "currentRatioQuarterly": 0.9},
    "BAC": {"peTTM": 12, "netProfitMarginTTM": 28, "roeTTM": 10, "pbAnnual": 1.1, "revenueGrowthTTMYoy": 4},
    "WFC": {"peTTM": 11, "netProfitMarginTTM": 25, "roeTTM": 11, "pbAnnual": 1.2, "revenueGrowthTTMYoy": 3},
}


def _uptrend(n=260):
    return [80 + i * 0.4 + 1.5 * math.sin(i / 9) for i in range(n)]


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Wire every external provider to deterministic fakes."""
    monkeypatch.setattr(runner.fh, "get_company_metrics", lambda s, **k: {
        "metric": FIN[s],
        "series": {"quarterly": {"pe": [{"period": "2025-09-30", "v": 28}, {"period": "2025-12-31", "v": 30}]}},
    })
    monkeypatch.setattr(runner.fh, "get_company_news", lambda s, **k: [
        {"headline": "Company beats earnings and raises guidance"}])
    monkeypatch.setattr(runner.fh, "get_profile", lambda s, **k: {"sector": SECTOR[s], "companyName": s})
    monkeypatch.setattr(runner.fh, "get_next_earnings", lambda s, **k: "2026-07-30")
    # Yahoo OHLCV is fetched via get_many -> get_ohlcv; mock the leaf.
    monkeypatch.setattr(runner.yahoo, "get_ohlcv", lambda s, **k: {"c": _uptrend(), "s": "ok"})
    fmp_fail = lambda *a, **k: (_ for _ in ()).throw(runner.fmp.FMPError("fmp down"))
    monkeypatch.setattr(runner.fmp, "get_profile", fmp_fail)
    monkeypatch.setattr(runner.fmp, "get_sector_pe", fmp_fail)
    monkeypatch.setattr(peers.fh, "get_peers", lambda s, **k: PEERS[s])
    monkeypatch.setattr(peers.fh, "get_basic_financials", lambda s, **k: PEER_FIN.get(s, {}))
    monkeypatch.setattr(peers, "CACHE_PATH", tmp_path / "benchmarks.json")
    monkeypatch.setattr(runner, "FETCH_CACHE_PATH", tmp_path / "fetches.json")
    monkeypatch.setattr("time.sleep", lambda *a: None)
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    # Pin sentiment baselines (don't read the repo's live history snapshots).
    monkeypatch.setattr(runner.sentiment_baseline, "load_baselines",
                        lambda **k: {"AAPL": 0.1, "JPM": 0.1})


def test_end_to_end_record_shape(wired):
    out = runner.run(CFG)
    assert out["count"] == 2
    for r in out["tickers"]:
        assert "error" not in r
        assert r["verdict"] in ("WATCH-BUY", "NEUTRAL", "WATCH-SELL")
        assert r["sources"]["price"] == "yahoo"          # quote derived from Yahoo closes
        assert r["details"]["technicals"]["sma_fast"] > 0  # technicals actually computed
        assert r["details"]["sentiment"]["baseline"] == 0.1  # baseline-relative sentiment
        assert r["fundamentals"], "sector-relative labels attached"
        assert r["history"]["pe"], "chart history attached"
        assert len(r["spark"]) == 63 and r["spark"][-1] > 0  # sparkline closes
        assert r["next_earnings"] == "2026-07-30"
        # finalize_one must clean up its scratch keys
        for hidden in ("_metric_values", "_tech", "_sent", "_merged_fin"):
            assert hidden not in r


def test_bank_scored_without_balance_sheet_penalty(wired):
    out = runner.run(CFG)
    jpm = next(r for r in out["tickers"] if r["symbol"] == "JPM")
    fund_reasons = [x for x in jpm["reasons"] if x.startswith("[fundamentals]")]
    assert fund_reasons, "fundamentals were scored"
    assert not any("Debt/Equity" in x for x in fund_reasons)
    assert not any("Current ratio" in x for x in fund_reasons)


def test_score_agrees_with_labels(wired):
    """The core A2 invariant: every scored fundamentals reason mirrors a label."""
    out = runner.run(CFG)
    for r in out["tickers"]:
        words = {(m["label"], m["word"]) for m in r["fundamentals"]}
        for reason in r["reasons"]:
            if reason.startswith("[fundamentals]") and " vs " in reason:
                body = reason.removeprefix("[fundamentals] ")
                label, word = body.rsplit(" vs ", 1)[0].rsplit(" ", 1)
                assert (label, word) in words, f"score reason {reason!r} not backed by a label"


def test_yahoo_down_falls_back_to_fmp_candles(wired, monkeypatch):
    yahoo_fail = lambda *a, **k: (_ for _ in ()).throw(runner.yahoo.YahooError("HTTP 999 bot wall"))
    monkeypatch.setattr(runner.yahoo, "get_ohlcv", yahoo_fail)
    monkeypatch.setattr(runner.fmp, "get_ohlcv", lambda s, **k: {"c": _uptrend(), "s": "ok"})
    monkeypatch.setenv("FMP_API_KEY", "x")
    monkeypatch.setattr(runner.fmp, "get_fundamentals",
                        lambda *a, **k: (_ for _ in ()).throw(runner.fmp.FMPError("x")))
    out = runner.run(CFG)
    for r in out["tickers"]:
        assert r["sources"]["ohlcv"] == "fmp"
        assert r["sources"]["price"] == "fmp"
        assert r["price"] is not None and r["spark"]
        assert r["missing"] == []                     # nothing lost — chain worked


def test_all_candle_sources_down_price_from_finnhub_quote(wired, monkeypatch):
    """The exact production incident: Yahoo bot-walled, no FMP. Price must
    survive via the Finnhub quote and the gap must be visible, not silent."""
    yahoo_fail = lambda *a, **k: (_ for _ in ()).throw(runner.yahoo.YahooError("HTTP 999 bot wall"))
    monkeypatch.setattr(runner.yahoo, "get_ohlcv", yahoo_fail)
    monkeypatch.setattr(runner.fh, "get_quote", lambda s, **k: {"c": 123.45, "dp": -1.2})
    out = runner.run(CFG)
    for r in out["tickers"]:
        assert r["price"] == 123.45                  # quote fallback fired
        assert r["sources"]["price"] == "finnhub_quote"
        assert "ohlcv" in r["missing"]               # gap is declared…
        assert "price" not in r["missing"]           # …but price isn't lost
        assert "yahoo" in r["sources"]["ohlcv_error"]


def test_stale_ohlcv_cache_used_when_sources_down(wired, monkeypatch, tmp_path):
    # First run succeeds (warms the per-symbol OHLCV cache)…
    out = runner.run(CFG)
    assert all(r["missing"] == [] for r in out["tickers"])
    # …then every live candle source dies; the stale cache keeps charts alive.
    yahoo_fail = lambda *a, **k: (_ for _ in ()).throw(runner.yahoo.YahooError("down"))
    monkeypatch.setattr(runner.yahoo, "get_ohlcv", yahoo_fail)
    out2 = runner.run(CFG)
    for r in out2["tickers"]:
        assert r["sources"]["ohlcv"] == "cache(stale)"
        assert r["spark"] and r["price"] is not None
        assert r["missing"] == []


def test_rate_limited_ticker_retried_others_not(wired, monkeypatch):
    calls = {"AAPL": 0, "JPM": 0}
    real = runner.assemble_one

    def flaky(symbol, cfg, **kwargs):
        calls[symbol] += 1
        if symbol == "AAPL" and calls["AAPL"] == 1:
            raise runner.fh.FinnhubError("/quote -> HTTP 429 (rate limited)")
        if symbol == "JPM":
            raise runner.fh.FinnhubError("No quote data for 'JPM' (bad symbol)")
        return real(symbol, cfg, **kwargs)

    monkeypatch.setattr(runner, "assemble_one", flaky)
    out = runner.run(CFG, rate_limit_cooldown=0)
    aapl = next(r for r in out["tickers"] if r["symbol"] == "AAPL")
    jpm = next(r for r in out["tickers"] if r["symbol"] == "JPM")
    assert "error" not in aapl and calls["AAPL"] == 2   # retried once, recovered
    assert "error" in jpm and calls["JPM"] == 1          # not worth retrying
