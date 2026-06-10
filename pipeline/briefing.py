"""Daily brief: what changed, what's screening as a buy, what as a sell.

A single plain-English paragraph generated once per run (Gemini -> Groq ->
deterministic), plus structured buy/sell lists for clickable chips. Neutrals are
deliberately excluded — the brief is about what's actionable. Cached on a
signature of the buy/sell set + verdict changes so the LLM is only called when
the picture actually changes.
"""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from pipeline import ai_summary

CACHE_PATH = Path(__file__).resolve().parent.parent / "cache" / "briefing.json"
TOP_N = 6


def _line(rec: dict) -> str:
    """One short reason for a chip — prefer the AI takeaway, else a label."""
    ai = rec.get("ai") or {}
    if ai.get("takeaway"):
        return ai["takeaway"]
    for m in rec.get("fundamentals", []):
        if m.get("tone") in ("good", "bad"):
            return ai_summary._plain(m)
    return ""


def _picks(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    live = [r for r in records if "error" not in r]
    buys = sorted((r for r in live if r.get("verdict") == "WATCH-BUY"),
                  key=lambda r: r.get("composite") or 0, reverse=True)[:TOP_N]
    sells = sorted((r for r in live if r.get("verdict") == "WATCH-SELL"),
                   key=lambda r: r.get("composite") or 0)[:TOP_N]
    pack = lambda rs: [{"symbol": r["symbol"], "company": r.get("company"), "line": _line(r)} for r in rs]
    return pack(buys), pack(sells), live


def _signature(buys, sells, changed) -> str:
    salient = {"b": [b["symbol"] for b in buys], "s": [s["symbol"] for s in sells],
               "c": sorted(c.get("text", "") for c in changed)}
    return hashlib.sha256(json.dumps(salient, sort_keys=True).encode()).hexdigest()[:16]


def _prompt(buys, sells, changed) -> str:
    parts = ["Write a short daily stock-watchlist brief for a NON-expert investor in plain, "
             "friendly English (3-4 sentences, no jargon, no markdown). Cover, in order: what "
             "changed since yesterday, which names are screening as possible BUYS and the main "
             "reason, and which are screening as ones to AVOID/TRIM and why. Ignore neutral names. "
             "This is decision-support, not advice — do not tell the reader to buy or sell. "
             "Use ONLY these facts:\n"]
    if changed:
        parts.append("Changed today: " + "; ".join(c.get("text", "") for c in changed[:8]))
    parts.append("Screening as BUY: " + ("; ".join(f"{b['symbol']} ({b['line']})" for b in buys) or "none"))
    parts.append("Screening as AVOID/TRIM: " + ("; ".join(f"{s['symbol']} ({s['line']})" for s in sells) or "none"))
    return "\n".join(parts)


def _deterministic_text(buys, sells, changed) -> str:
    bits = []
    flips = [c.get("text", "") for c in changed if c.get("type") == "verdict"]
    if flips:
        bits.append("Verdict changes today: " + "; ".join(flips[:4]) + ".")
    elif changed:
        bits.append(f"{len(changed)} update(s) since the last run.")
    else:
        bits.append("No verdict changes since the last run.")
    if buys:
        bits.append("Screening as possible buys: " + ", ".join(b["symbol"] for b in buys) + ".")
    if sells:
        bits.append("Screening as ones to avoid or trim: " + ", ".join(s["symbol"] for s in sells) + ".")
    if not buys and not sells:
        bits.append("Nothing is screening as a clear buy or sell right now.")
    return " ".join(bits)


def build(records: list[dict], changed: list[dict] | None = None,
          cache_path: Path = CACHE_PATH) -> dict:
    changed = changed or []
    buys, sells, _ = _picks(records)
    sig = _signature(buys, sells, changed)

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}

    if cache.get("sig") == sig and cache.get("text"):
        text, source = cache["text"], cache.get("source", "cache")
    else:
        out = ai_summary.complete(_prompt(buys, sells, changed))
        if out and out[0].strip():
            text, source = out[0].strip(), out[1]
        else:
            text, source = _deterministic_text(buys, sells, changed), "deterministic"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"sig": sig, "text": text, "source": source}), encoding="utf-8")

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": source,
        "text": text,
        "buy": buys,
        "sell": sells,
    }
