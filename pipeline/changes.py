"""What changed between the previous run and this one.

The dashboard's most useful answer for a daily user isn't "what is" but "what's
different since yesterday": verdict flips, newly raised flags, big movers, and
watchlist edits. This module diffs the previous latest.json payload against the
new one and emits a compact, ordered list the frontend renders as a strip.

Each entry: {"symbol", "type", "text"} where type is one of
verdict | flag | mover | added | removed.
"""
from __future__ import annotations

MOVER_PCT = 4.0  # |day move| at or above this is worth surfacing

# Severity order for the strip: verdict flips first, then new flags, movers, edits.
_ORDER = {"verdict": 0, "flag": 1, "mover": 2, "added": 3, "removed": 4}


def _by_symbol(payload: dict) -> dict[str, dict]:
    return {t["symbol"]: t for t in (payload or {}).get("tickers", [])
            if isinstance(t, dict) and "symbol" in t and "error" not in t}


def diff(prev_payload: dict | None, new_payload: dict, mover_pct: float = MOVER_PCT) -> list[dict]:
    prev = _by_symbol(prev_payload or {})
    new = _by_symbol(new_payload)
    # Symbols whose fetch errored this run: unknown state, not "removed".
    errored = {t["symbol"] for t in (new_payload or {}).get("tickers", [])
               if isinstance(t, dict) and "symbol" in t and "error" in t}
    out: list[dict] = []

    for sym, rec in new.items():
        old = prev.get(sym)

        if old is None:
            if prev:  # don't call everything "added" on the very first run
                out.append({"symbol": sym, "type": "added", "text": f"{sym} added to watchlist"})
            continue

        if old.get("verdict") and rec.get("verdict") and old["verdict"] != rec["verdict"]:
            out.append({"symbol": sym, "type": "verdict",
                        "text": f"{sym} {old['verdict']} → {rec['verdict']}",
                        "to": rec["verdict"]})

        new_flags = sorted(set(rec.get("flags", [])) - set(old.get("flags", [])))
        if new_flags:
            pretty = ", ".join(f.replace("_", " ") for f in new_flags)
            out.append({"symbol": sym, "type": "flag", "text": f"{sym} new: {pretty}"})

        dp = rec.get("change_pct")
        if isinstance(dp, (int, float)) and abs(dp) >= mover_pct:
            out.append({"symbol": sym, "type": "mover", "text": f"{sym} {dp:+.1f}% today"})

    for sym in prev:
        if sym not in new and sym not in errored:
            out.append({"symbol": sym, "type": "removed", "text": f"{sym} removed from watchlist"})

    out.sort(key=lambda e: (_ORDER.get(e["type"], 9), e["symbol"]))
    return out
