"""Data clients: Yahoo payload parsing (offline) and the peer-benchmark cache."""
import pytest

from pipeline.data import yahoo_client
from pipeline import peers


def _payload(ts, closes, error=None):
    return {"chart": {"error": error, "result": [{
        "timestamp": ts,
        "indicators": {"quote": [{"close": closes,
                                  "open": closes, "high": closes, "low": closes,
                                  "volume": [1] * len(closes)}]},
    }] if error is None else []}}


class TestYahooParse:
    def test_basic_parse_oldest_to_newest(self):
        day = 86400
        out = yahoo_client._parse(_payload([day, 2 * day, 3 * day], [1.0, 2.0, 3.0]), "X", days=400)
        assert out["s"] == "ok"
        assert out["c"] == [1.0, 2.0, 3.0]
        assert out["t"][0] < out["t"][-1]

    def test_null_closes_skipped(self):
        day = 86400
        out = yahoo_client._parse(_payload([day, 2 * day, 3 * day], [1.0, None, 3.0]), "X", days=400)
        assert out["c"] == [1.0, 3.0]

    def test_days_trims_from_the_end(self):
        day = 86400
        out = yahoo_client._parse(_payload([i * day for i in range(1, 6)], [1, 2, 3, 4, 5]), "X", days=2)
        assert out["c"] == [4.0, 5.0]

    def test_chart_error_raises(self):
        with pytest.raises(yahoo_client.YahooError):
            yahoo_client._parse(_payload([], [], error={"code": "Not Found"}), "X", days=10)

    def test_empty_result_raises(self):
        with pytest.raises(yahoo_client.YahooError):
            yahoo_client._parse({"chart": {"result": []}}, "X", days=10)

    def test_get_many_parallel_collects_all(self, monkeypatch):
        monkeypatch.setattr(yahoo_client, "get_ohlcv",
                            lambda s, **k: {"c": [1.0, 2.0], "s": "ok"} if s != "BAD"
                            else (_ for _ in ()).throw(yahoo_client.YahooError("no data")))
        out = yahoo_client.get_many(["AAA", "BBB", "BAD"], second_chance_wait=0)
        assert out["AAA"]["c"] == [1.0, 2.0]
        assert "error" in out["BAD"]          # failures are per-ticker, not fatal
        assert set(out) == {"AAA", "BBB", "BAD"}

    def test_get_many_second_chance_recovers(self, monkeypatch):
        # First pass 429s (burst), the slow serial retry succeeds.
        calls = {"BAD": 0}

        def fake_get(s, **k):
            if s == "BAD":
                calls["BAD"] += 1
                if calls["BAD"] == 1:
                    raise yahoo_client.YahooError("HTTP 429")
            return {"c": [1.0], "s": "ok"}

        slept = []
        monkeypatch.setattr(yahoo_client, "get_ohlcv", fake_get)
        monkeypatch.setattr(yahoo_client.time, "sleep", lambda s: slept.append(s))
        out = yahoo_client.get_many(["AAA", "BAD"], second_chance_wait=45.0)
        assert out["BAD"]["c"] == [1.0]       # recovered, no error key
        assert 45.0 in slept                  # cooled down before retrying

    def test_get_many_empty(self):
        assert yahoo_client.get_many([]) == {}

    def test_retry_wait_honors_retry_after_header(self):
        mk = lambda h: type("R", (), {"headers": h})()
        assert yahoo_client._retry_wait(mk({"Retry-After": "7"}), 1.0) == 7.0
        assert yahoo_client._retry_wait(mk({}), 2.0) == 2.0            # falls back to backoff
        assert yahoo_client._retry_wait(mk({"Retry-After": "999"}), 1.0) == 60.0  # capped


class TestPeerBenchmarks:
    @pytest.fixture
    def wired(self, monkeypatch, tmp_path):
        self.peer_calls = {"n": 0}

        def get_peers(s, **k):
            self.peer_calls["n"] += 1
            return ["P1", "P2"]

        monkeypatch.setattr(peers.fh, "get_peers", get_peers)
        monkeypatch.setattr(peers.fh, "get_basic_financials",
                            lambda s, **k: {"peTTM": {"P1": 10, "P2": 20}[s]})
        monkeypatch.setattr(peers, "CACHE_PATH", tmp_path / "bm.json")
        monkeypatch.setattr("time.sleep", lambda *a: None)

    def test_median_of_peers(self, wired):
        bm = peers.build_benchmarks(["AAPL"])
        assert bm["AAPL"]["values"]["pe"] == 15.0   # median of 10, 20
        assert bm["AAPL"]["peers"] == 2

    def test_cache_prevents_refetch(self, wired):
        peers.build_benchmarks(["AAPL"])
        first = self.peer_calls["n"]
        bm = peers.build_benchmarks(["AAPL"])     # second run: served from cache
        assert self.peer_calls["n"] == first
        assert bm["AAPL"]["values"]["pe"] == 15.0

    def test_refresh_cap_bounds_cold_start(self, wired):
        # Cold cache, 5 symbols, cap of 2 -> only 2 get_peers calls this run.
        bm = peers.build_benchmarks(["A", "B", "C", "D", "E"], max_refresh=2)
        assert self.peer_calls["n"] == 2
        # The 2 refreshed get values; the rest are skipped (no stale cache yet).
        refreshed = [s for s in ("A", "B", "C", "D", "E") if bm.get(s, {}).get("values")]
        assert len(refreshed) == 2


class TestFMPQuotaBreaker:
    def _wired(self, monkeypatch):
        from pipeline.data import fmp_client as fmp
        monkeypatch.setattr(fmp, "_quota_strikes", 0)
        monkeypatch.setenv("FMP_API_KEY", "test-key")
        slept = []
        monkeypatch.setattr(fmp.time, "sleep", lambda s: slept.append(s))
        resp = type("R", (), {"status_code": 429, "ok": False, "headers": {}, "text": ""})()
        session = type("S", (), {"get": lambda self, *a, **k: resp})()
        return fmp, session, slept

    def test_429_waits_out_the_minute_window(self, monkeypatch):
        fmp, session, slept = self._wired(monkeypatch)
        with pytest.raises(fmp.FMPError):
            fmp._request(session, "http://x", {})
        assert slept and all(s >= 10 for s in slept)

    def test_fails_fast_once_quota_is_clearly_gone(self, monkeypatch):
        fmp, session, slept = self._wired(monkeypatch)
        for _ in range(fmp._QUOTA_STRIKE_LIMIT):
            with pytest.raises(fmp.FMPError):
                fmp._request(session, "http://x", {})
        before = len(slept)
        with pytest.raises(fmp.FMPError):
            fmp._request(session, "http://x", {})   # strike limit hit
        assert len(slept) == before                 # no waiting: fail straight to next source


class TestThrottle:
    def test_paces_calls_to_min_interval(self, monkeypatch):
        from pipeline.data import finnhub_client as fh
        slept = []
        monkeypatch.setattr(fh, "_MIN_INTERVAL", 1.05)
        monkeypatch.setattr(fh, "_last_call", 0.0)
        clock = {"t": 1000.0}
        monkeypatch.setattr(fh.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(fh.time, "sleep", lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
        fh._throttle()                 # first call: clock far from 0 -> no wait
        clock["t"] += 0.2              # only 0.2s elapses before next call
        fh._throttle()                 # must wait ~0.85s to honour 1.05s spacing
        assert slept and abs(slept[-1] - 0.85) < 0.01
