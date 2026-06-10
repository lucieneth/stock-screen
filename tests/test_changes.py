"""changes.py: the since-last-run diff that feeds the dashboard strip."""
from pipeline import changes


def _payload(*tickers):
    return {"tickers": list(tickers)}


def _t(symbol, verdict="NEUTRAL", flags=(), change_pct=0.0):
    return {"symbol": symbol, "verdict": verdict, "flags": list(flags), "change_pct": change_pct}


def test_verdict_flip_detected_and_ordered_first():
    prev = _payload(_t("AAPL", "NEUTRAL"), _t("MSFT", "WATCH-BUY", change_pct=6.0))
    new = _payload(_t("AAPL", "WATCH-BUY"), _t("MSFT", "WATCH-BUY", change_pct=6.0))
    out = changes.diff(prev, new)
    assert out[0]["type"] == "verdict"
    assert out[0]["text"] == "AAPL NEUTRAL → WATCH-BUY"
    assert out[0]["to"] == "WATCH-BUY"


def test_new_flags_only_not_persisting_ones():
    prev = _payload(_t("AAPL", flags=["high_pe"]))
    new = _payload(_t("AAPL", flags=["high_pe", "rsi_overbought"]))
    out = changes.diff(prev, new)
    assert len(out) == 1
    assert out[0]["type"] == "flag"
    assert "rsi overbought" in out[0]["text"]
    assert "high pe" not in out[0]["text"]


def test_big_mover_threshold():
    prev = _payload(_t("AAPL"), _t("MSFT"))
    new = _payload(_t("AAPL", change_pct=-5.2), _t("MSFT", change_pct=2.0))
    out = changes.diff(prev, new, mover_pct=4.0)
    assert [c["symbol"] for c in out] == ["AAPL"]
    assert out[0]["text"] == "AAPL -5.2% today"


def test_added_and_removed_tickers():
    prev = _payload(_t("AAPL"), _t("TSLA"))
    new = _payload(_t("AAPL"), _t("NFLX"))
    out = changes.diff(prev, new)
    assert {(c["type"], c["symbol"]) for c in out} == {("added", "NFLX"), ("removed", "TSLA")}


def test_first_run_is_quiet():
    # With no previous payload, nothing should be reported as "added".
    out = changes.diff(None, _payload(_t("AAPL"), _t("MSFT")))
    assert out == []


def test_error_rows_ignored():
    prev = _payload(_t("AAPL", "WATCH-BUY"))
    new = {"tickers": [{"symbol": "AAPL", "error": "rate limited"}]}
    # An errored fetch must not read as "removed" or a verdict flip.
    assert changes.diff(prev, new) == []


def test_no_changes_yields_empty_list():
    prev = _payload(_t("AAPL", "WATCH-BUY", flags=["high_pe"]))
    assert changes.diff(prev, prev) == []
