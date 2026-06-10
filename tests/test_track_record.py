"""track_record.py: forward returns, directional hit rates, baseline, horizon guard."""
import json
from datetime import date, timedelta

import pytest

from pipeline import track_record as tr


@pytest.fixture
def history(tmp_path, monkeypatch):
    hist = tmp_path / "history"
    hist.mkdir()
    monkeypatch.setattr(tr, "HISTORY_DIR", hist)
    monkeypatch.setattr(tr, "OUT_PATH", tmp_path / "track_record.json")

    def snap(d, rows):
        (hist / f"{d.isoformat()}.json").write_text(json.dumps({"tickers": rows}))
    return snap


def _index(entry: date, slope: float, days: int = 200):
    return [(entry + timedelta(days=i), 100.0 * (1 + slope * i)) for i in range(days)]


def test_hit_rates_and_baseline(history):
    d = date.today() - timedelta(days=120)
    history(d, [{"symbol": "WIN", "verdict": "WATCH-BUY"},
                {"symbol": "LOSE", "verdict": "WATCH-BUY"},
                {"symbol": "DROP", "verdict": "WATCH-SELL"}])
    slopes = {"WIN": 0.004, "LOSE": -0.003, "DROP": -0.005}
    res = tr.evaluate(price_fetch=lambda s: _index(d, slopes[s]))

    h30 = res["by_horizon"]["30"]
    assert h30["verdicts"]["WATCH-BUY"]["n"] == 2
    assert h30["verdicts"]["WATCH-BUY"]["hit_rate"] == 0.5      # WIN up, LOSE down
    assert h30["verdicts"]["WATCH-SELL"]["hit_rate"] == 1.0     # DROP fell -> sell was right
    assert h30["baseline_n"] == 3
    assert h30["verdicts"]["WATCH-BUY"]["confident"] is False   # n < 10


def test_unelapsed_horizon_not_graded(history):
    history(date.today() - timedelta(days=5), [{"symbol": "NEW", "verdict": "WATCH-BUY"}])
    res = tr.evaluate(price_fetch=lambda s: _index(date.today() - timedelta(days=5), 0.001, days=6))
    assert res["by_horizon"]["30"]["verdicts"] == {}            # only 5 days have passed


def test_missing_prices_skipped(history):
    history(date.today() - timedelta(days=120), [{"symbol": "GONE", "verdict": "WATCH-BUY"}])
    res = tr.evaluate(price_fetch=lambda s: [])
    assert res["by_horizon"]["30"]["verdicts"] == {}


def test_sell_avg_return_sign(history):
    d = date.today() - timedelta(days=120)
    history(d, [{"symbol": "DROP", "verdict": "WATCH-SELL"}])
    res = tr.evaluate(price_fetch=lambda s: _index(d, -0.005))
    assert res["by_horizon"]["90"]["verdicts"]["WATCH-SELL"]["avg_return"] < 0


def test_reuses_run_ohlcv_cache_no_network(history, tmp_path, monkeypatch):
    """The main run's cached OHLCV is reused instead of re-fetching prices."""
    import json
    d = date.today() - timedelta(days=120)
    history(d, [{"symbol": "WIN", "verdict": "WATCH-BUY"}])
    # Simulate cache/fetches.json with WIN's dates+closes from the main run.
    dates = [(d + timedelta(days=i)).isoformat() for i in range(150)]
    closes = [100.0 * (1 + 0.003 * i) for i in range(150)]
    cache_file = tmp_path / "fetches.json"
    cache_file.write_text(json.dumps({"ohlcv:WIN": {"ts": "x", "v": {"t": dates, "c": closes}}}))
    monkeypatch.setattr(tr, "FETCH_CACHE_PATH", cache_file)
    # If anything tries the network, fail loudly.
    monkeypatch.setattr(tr.yahoo, "get_ohlcv", lambda *a, **k: (_ for _ in ()).throw(AssertionError("hit network")))

    res = tr.evaluate()   # price_fetch=None -> builds from cache
    assert res["by_horizon"]["30"]["verdicts"]["WATCH-BUY"]["hit_rate"] == 1.0
