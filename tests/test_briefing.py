"""briefing.py: buy/sell selection (no neutrals), deterministic text, caching."""
from pipeline import briefing


def _t(sym, verdict, composite, takeaway=""):
    return {"symbol": sym, "company": sym + " Inc", "verdict": verdict,
            "composite": composite, "ai": {"takeaway": takeaway}, "fundamentals": []}


RECORDS = [
    _t("AAA", "WATCH-BUY", 0.6, "Possible buy — cheap vs peers."),
    _t("BBB", "WATCH-BUY", 0.3),
    _t("CCC", "NEUTRAL", 0.0),
    _t("DDD", "WATCH-SELL", -0.4, "Avoid — weak margins."),
    {"symbol": "ERR", "error": "rate limited"},
]


def test_excludes_neutrals_and_ranks(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    out = briefing.build(RECORDS, changed=[], cache_path=tmp_path / "b.json")
    assert [b["symbol"] for b in out["buy"]] == ["AAA", "BBB"]   # ranked by composite desc
    assert [s["symbol"] for s in out["sell"]] == ["DDD"]
    assert "CCC" not in [x["symbol"] for x in out["buy"] + out["sell"]]
    assert "ERR" not in [x["symbol"] for x in out["buy"] + out["sell"]]


def test_deterministic_text_mentions_changes_and_picks(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    changed = [{"type": "verdict", "text": "AAA NEUTRAL → WATCH-BUY"}]
    out = briefing.build(RECORDS, changed=changed, cache_path=tmp_path / "b.json")
    assert out["source"] == "deterministic"
    assert "AAA NEUTRAL → WATCH-BUY" in out["text"]
    assert "AAA" in out["text"] and "DDD" in out["text"]


def test_llm_used_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    calls = {"n": 0}
    def fake_complete(prompt, session=None, validate=None):
        calls["n"] += 1
        return ("Today AAA looks interesting while DDD is weak.", "groq")
    monkeypatch.setattr(briefing.ai_summary, "complete", fake_complete)
    p = tmp_path / "b.json"
    a = briefing.build(RECORDS, changed=[], cache_path=p)
    b = briefing.build(RECORDS, changed=[], cache_path=p)   # same picks -> cached
    assert a["source"] == "groq" and a["text"].startswith("Today AAA")
    assert calls["n"] == 1                                   # second call served from cache


def test_no_actionable_names(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    out = briefing.build([_t("CCC", "NEUTRAL", 0.0)], changed=[], cache_path=tmp_path / "b.json")
    assert out["buy"] == [] and out["sell"] == []
    assert "clear buy or sell" in out["text"]
