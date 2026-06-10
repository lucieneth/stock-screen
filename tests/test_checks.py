"""Checks: technicals, sentiment, and the absolute-threshold fundamentals fallback."""
import math

from pipeline.checks import technicals, sentiment, fundamentals

TECH_CFG = {"sma_fast": 50, "sma_slow": 200, "rsi_oversold": 30, "rsi_overbought": 70}


def _closes(n=260, slope=0.4, base=80.0):
    return [base + i * slope + 1.5 * math.sin(i / 9) for i in range(n)]


class TestTechnicals:
    def test_uptrend_scores_positive(self):
        res = technicals.check({"c": _closes(slope=0.4), "s": "ok"}, TECH_CFG)
        assert res["metrics"]["sma_fast"] > res["metrics"]["sma_slow"]
        assert any("uptrend" in r for r in res["reasons"])
        assert -1.0 <= res["score"] <= 1.0

    def test_downtrend_scores_negative(self):
        res = technicals.check({"c": _closes(slope=-0.4, base=300.0), "s": "ok"}, TECH_CFG)
        assert any("downtrend" in r for r in res["reasons"])
        assert res["score"] < 0

    def test_short_history_is_neutral_not_crash(self):
        res = technicals.check({"c": _closes(50), "s": "ok"}, TECH_CFG)
        assert res["score"] == 0.0
        assert "candles" in res["reasons"][0]

    def test_error_input_is_neutral(self):
        res = technicals.check({"error": "HTTP 403"}, TECH_CFG)
        assert res["score"] == 0.0
        assert "unavailable" in res["reasons"][0]

    def test_rsi_within_bounds(self):
        res = technicals.check({"c": _closes(), "s": "ok"}, TECH_CFG)
        assert 0 <= res["metrics"]["rsi"] <= 100

    def test_momentum_positive_in_uptrend(self):
        res = technicals.check({"c": _closes(300, slope=0.4), "s": "ok"}, TECH_CFG)
        assert res["metrics"]["momentum_12_1"] > 0
        assert any("12-1 momentum +" in r for r in res["reasons"])
        assert res["score"] > 0

    def test_momentum_negative_in_downtrend(self):
        res = technicals.check({"c": _closes(300, slope=-0.4, base=300.0), "s": "ok"}, TECH_CFG)
        assert res["metrics"]["momentum_12_1"] < 0
        assert res["score"] < 0

    def test_momentum_omitted_gracefully_below_lookback(self):
        # 260 candles >= SMA200 but < the 273 needed for 12-1 momentum.
        res = technicals.check({"c": _closes(260), "s": "ok"}, TECH_CFG)
        assert "momentum_12_1" not in res["metrics"]
        assert any("momentum unavailable" in r for r in res["reasons"])

    def test_momentum_outweighs_rsi(self):
        # A strong 12-1 uptrend must not be flipped negative by overbought RSI.
        res = technicals.check({"c": _closes(300, slope=0.6), "s": "ok"}, TECH_CFG)
        if "rsi_overbought" in res["flags"]:
            assert res["score"] > 0


class TestSentiment:
    CFG = {"negative_spike": -0.2, "deviation_gain": 2.5}
    POS = [{"headline": "Company beats earnings and raises guidance"},
           {"headline": "Analysts upgrade after record quarter"}]
    NEG = [{"headline": "Company files for bankruptcy after fraud probe"},
           {"headline": "Shares plunge as lawsuit and recall widen losses"}]

    def test_finnhub_premium_score_preferred(self):
        res = sentiment.check({"companyNewsScore": 0.8}, [{"headline": "irrelevant"}], self.CFG)
        assert res["metrics"]["source"] == "finnhub_news_sentiment"
        assert res["score"] == 0.6  # (0.8 - 0.5) * 2

    def test_no_baseline_reports_building_not_a_score(self):
        # Absolute VADER level is structurally positive; without a baseline the
        # dimension must be excluded from coverage, not scored.
        res = sentiment.check({"error": "403"}, self.POS, self.CFG, baseline=None)
        assert res["score"] == 0.0
        assert "unavailable" in res["reasons"][0]
        assert res["metrics"]["raw"] > 0  # raw still recorded for future baselines

    def test_deviation_above_baseline_scores_positive(self):
        res = sentiment.check({"error": "403"}, self.POS, self.CFG, baseline=0.1)
        assert res["metrics"]["source"] == "vader_headlines"
        assert res["score"] > 0
        assert res["metrics"]["deviation"] > 0

    def test_same_as_baseline_is_neutral(self):
        raw = sentiment.check({"error": "403"}, self.POS, self.CFG, baseline=0.0)["metrics"]["raw"]
        res = sentiment.check({"error": "403"}, self.POS, self.CFG, baseline=raw)
        # typical positivity with no deviation -> no signal (tolerance: raw is
        # rounded to 3 decimals in metrics)
        assert abs(res["score"]) < 0.01

    def test_negative_deviation_spike_flag(self):
        res = sentiment.check({"error": "403"}, self.NEG, self.CFG, baseline=0.3)
        assert res["score"] < 0
        assert "negative_sentiment_spike" in res["flags"]

    def test_no_headlines_is_neutral(self):
        res = sentiment.check({"error": "403"}, [], self.CFG, baseline=0.3)
        assert res["score"] == 0.0
        assert res["metrics"]["source"] == "none"


class TestFundamentalsAbsoluteFallback:
    CFG = {"pe_good": 25, "pe_flag": 40, "rev_growth_yoy": 0.05,
           "debt_to_equity_max": 1.5, "require_positive_fcf": True}

    def test_all_good_clamps_to_one(self):
        fin = {"peTTM": 18, "revenueGrowthTTMYoy": 12.0,
               "totalDebt/totalEquityQuarterly": 0.4, "freeCashFlowTTM": 5000}
        res = fundamentals.check(fin, self.CFG)
        assert res["score"] == 1.0

    def test_rich_pe_flags(self):
        res = fundamentals.check({"peTTM": 60}, self.CFG)
        assert "high_pe" in res["flags"]
        assert res["score"] < 0

    def test_percent_vs_fraction_growth_normalized(self):
        # 12.0 (percent form) and 0.12 (fraction form) must score identically.
        a = fundamentals.check({"revenueGrowthTTMYoy": 12.0}, self.CFG)
        b = fundamentals.check({"revenueGrowthTTMYoy": 0.12}, self.CFG)
        assert a["score"] == b["score"] > 0

    def test_no_data_is_neutral(self):
        res = fundamentals.check({"error": "403"}, self.CFG)
        assert res["score"] == 0.0
