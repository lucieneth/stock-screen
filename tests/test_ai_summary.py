"""ai_summary.py: parsing, provider chain, deterministic fallback, caching."""
import json

import pytest

from pipeline import ai_summary as ai


def _rec(**over):
    base = {
        "symbol": "AAPL", "company": "Apple", "sector": "Technology",
        "verdict": "WATCH-BUY", "composite": 0.45,
        "scores": {"fundamentals": 0.4, "technicals": 0.5, "sentiment": 0.2},
        "reasons": ["[technicals] 12-1 momentum +44% (positive)"],
        "flags": ["high_pe"],
        "fundamentals": [
            {"label": "ROE", "word": "strong", "tone": "good"},
            {"label": "P/E", "word": "expensive", "tone": "bad"},
        ],
    }
    base.update(over)
    return base


class TestParse:
    def test_plain_json(self):
        assert ai._parse('{"bull": "Good ROE.", "bear": "Pricey."}') == {"bull": "Good ROE.", "bear": "Pricey."}

    def test_code_fenced_json(self):
        out = ai._parse('```json\n{"bull":"a","bear":"b"}\n```')
        assert out == {"bull": "a", "bear": "b"}

    def test_surrounding_prose_tolerated(self):
        assert ai._parse('Here you go: {"bull":"x","bear":"y"} thanks')["bull"] == "x"

    def test_missing_keys_rejected(self):
        assert ai._parse('{"bull": "only one"}') is None

    def test_garbage_rejected(self):
        assert ai._parse("not json at all") is None


class TestDeterministic:
    def test_cites_good_and_bad_labels_keeping_case(self):
        out = ai.deterministic(_rec())
        assert out["source"] == "deterministic"
        assert "ROE strong" in out["bull"]      # casing preserved, not "roe"
        assert "P/E expensive" in out["bear"]

    def test_flags_surface_in_bear(self):
        out = ai.deterministic(_rec())
        assert "high pe" in out["bear"].lower()

    def test_handles_no_signal_gracefully(self):
        out = ai.deterministic({"symbol": "X", "scores": {}, "fundamentals": [], "flags": []})
        assert out["bull"] and out["bear"]


class TestProviderChain:
    def test_no_keys_uses_deterministic(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "ai.json")
        recs = [_rec()]
        ai.annotate(recs)
        assert recs[0]["ai"]["source"] == "deterministic"

    def test_groq_used_when_gemini_absent(self, monkeypatch, tmp_path):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.setenv("GROQ_API_KEY", "x")
        monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "ai.json")
        monkeypatch.setattr(ai, "_groq", lambda p, s: '{"bull":"g-bull","bear":"g-bear"}')
        recs = [_rec()]
        ai.annotate(recs)
        assert recs[0]["ai"] == {"bull": "g-bull", "bear": "g-bear", "source": "groq"}

    def test_gemini_preferred_then_falls_back_to_groq_on_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setenv("GROQ_API_KEY", "y")
        monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "ai.json")
        monkeypatch.setattr(ai, "_gemini", lambda p, s: "not valid json")  # parse fails
        monkeypatch.setattr(ai, "_groq", lambda p, s: '{"bull":"b","bear":"r"}')
        recs = [_rec()]
        ai.annotate(recs)
        assert recs[0]["ai"]["source"] == "groq"

    def test_all_llms_fail_falls_to_deterministic(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "ai.json")
        def boom(p, s): raise ai.requests.RequestException("down")
        monkeypatch.setattr(ai, "_gemini", boom)
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        recs = [_rec()]
        ai.annotate(recs)
        assert recs[0]["ai"]["source"] == "deterministic"


class TestCache:
    def test_cache_hit_skips_regeneration(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "ai.json")
        calls = {"n": 0}
        def gen(p, s):
            calls["n"] += 1
            return '{"bull":"b","bear":"r"}'
        monkeypatch.setattr(ai, "_gemini", gen)

        recs = [_rec()]
        ai.annotate(recs)               # cold: generates
        ai.annotate([_rec()])           # same signature: cached
        assert calls["n"] == 1

    def test_signature_changes_on_verdict_flip(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GEMINI_API_KEY", "x")
        monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "ai.json")
        calls = {"n": 0}
        monkeypatch.setattr(ai, "_gemini", lambda p, s: calls.__setitem__("n", calls["n"] + 1) or '{"bull":"b","bear":"r"}')
        ai.annotate([_rec()])
        ai.annotate([_rec(verdict="WATCH-SELL")])   # material change -> regenerate
        assert calls["n"] == 2

    def test_errored_records_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ai, "CACHE_PATH", tmp_path / "ai.json")
        recs = [{"symbol": "ZZZ", "error": "rate limited"}]
        ai.annotate(recs)
        assert "ai" not in recs[0]
