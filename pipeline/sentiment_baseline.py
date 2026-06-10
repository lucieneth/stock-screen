"""Per-ticker sentiment baselines from committed history snapshots.

VADER over financial newswire headlines is structurally positive (~+0.3 for
nearly every ticker), so the absolute level carries almost no information — it
just shifts every composite toward BUY. What does carry information is a
ticker's sentiment moving *relative to its own typical level*. This module
derives that baseline: the median of the ticker's raw sentiment readings over
the most recent committed snapshots.

A baseline needs MIN_OBS observations before it activates; until then the
sentiment dimension reports "building" and is excluded from coverage, which is
more honest than scoring a constant.
"""
from __future__ import annotations

import json
import glob
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HISTORY_DIR = REPO_ROOT / "docs" / "data" / "history"

MIN_OBS = 3
MAX_SNAPSHOTS = 30  # trailing window: ~6 weeks of weekday runs


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def load_baselines(history_dir: Path = HISTORY_DIR, min_obs: int = MIN_OBS,
                   max_snapshots: int = MAX_SNAPSHOTS) -> dict[str, float]:
    """Return {symbol: baseline} for tickers with enough sentiment history."""
    paths = sorted(glob.glob(str(Path(history_dir) / "*.json")))[-max_snapshots:]
    obs: dict[str, list[float]] = {}
    for path in paths:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for t in payload.get("tickers", []):
            sent = (t.get("details") or {}).get("sentiment") or {}
            if sent.get("source") not in ("vader_headlines",):
                continue
            # newer snapshots store "raw"; older ones stored "avg"
            val = sent.get("raw", sent.get("avg"))
            if val is None:
                continue
            try:
                obs.setdefault(t["symbol"], []).append(float(val))
            except (TypeError, ValueError, KeyError):
                continue
    return {sym: round(_median(vals), 4) for sym, vals in obs.items() if len(vals) >= min_obs}
