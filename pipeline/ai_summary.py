"""Pre-generated "why buy / why not" summaries per ticker.

The site is static (no server, no client-side keys), so summaries are generated
in the pipeline and written into latest.json; the dashboard's "Ask AI" button
just reveals the pre-written answer. Provider chain, each best-effort:

    Gemini 2.0 Flash  (GEMINI_API_KEY)  ->  Groq Llama-3.3  (GROQ_API_KEY)
                                         ->  deterministic synthesis (no key)

Grounded by construction: the model is given ONLY the screener's own computed
facts and asked to return strict JSON {"bull", "bear"}. Results are cached on a
signature of the salient inputs, so the LLM is called only when a ticker's
verdict / scores / flags / metric labels actually change — keeping us inside the
free tiers.
"""
from __future__ import annotations

import os
import json
import time
import hashlib
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = REPO_ROOT / "cache" / "ai_summaries.json"

GEMINI_MODEL = "gemini-2.0-flash"
GROQ_MODEL = "llama-3.3-70b-versatile"
TIMEOUT = 30

_SYSTEM = (
    "You are a neutral equity analyst. Using ONLY the facts provided, give a brief, "
    "balanced view. Return STRICT JSON with exactly two keys, \"bull\" and \"bear\", "
    "each 1-2 short plain-language sentences citing the given factors. Do not give "
    "investment advice or price targets, and do not invent facts."
)


# --- prompt -------------------------------------------------------------------

def _facts(rec: dict) -> str:
    lines = [f"Ticker: {rec.get('symbol')} ({rec.get('company') or rec.get('symbol')}) — {rec.get('sector')}",
             f"Screener verdict: {rec.get('verdict')} (composite {rec.get('composite')})"]
    s = rec.get("scores") or {}
    lines.append(f"Dimension scores -1..+1: fundamentals {s.get('fundamentals')}, "
                 f"technicals {s.get('technicals')}, sentiment {s.get('sentiment')}")
    reasons = rec.get("reasons") or []
    if reasons:
        lines.append("Factors:")
        lines += [f"- {r}" for r in reasons[:12]]
    if rec.get("flags"):
        lines.append("Flags: " + ", ".join(rec["flags"]))
    return "\n".join(lines)


def _prompt(rec: dict) -> str:
    return f"{_SYSTEM}\n\n{_facts(rec)}"


# --- providers ----------------------------------------------------------------

def _gemini(prompt: str, session: requests.Session) -> str:
    key = os.environ["GEMINI_API_KEY"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300,
                                 "responseMimeType": "application/json"}}
    r = session.post(url, params={"key": key}, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _groq(prompt: str, session: requests.Session) -> str:
    key = os.environ["GROQ_API_KEY"]
    body = {"model": GROQ_MODEL, "temperature": 0.4, "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}]}
    r = session.post("https://api.groq.com/openai/v1/chat/completions",
                     headers={"Authorization": f"Bearer {key}"}, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse(text: str) -> dict | None:
    """Pull {"bull","bear"} out of a model response, tolerating code fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    try:
        obj = json.loads(t[t.find("{"): t.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    bull, bear = obj.get("bull"), obj.get("bear")
    if isinstance(bull, str) and isinstance(bear, str) and bull and bear:
        return {"bull": bull.strip(), "bear": bear.strip()}
    return None


# --- deterministic fallback ---------------------------------------------------

def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def deterministic(rec: dict) -> dict:
    """Synthesize bull/bear from the screener's own labels — no LLM needed.

    Kept short (a few strongest factors) so it reads like a quick take, and
    metric names keep their casing (P/E, ROE) — no lower-casing.
    """
    funds = rec.get("fundamentals") or []
    good = [f"{m['label']} {m['word']}" for m in funds if m.get("tone") == "good"][:4]
    bad = [f"{m['label']} {m['word']}" for m in funds if m.get("tone") == "bad"][:4]
    s = rec.get("scores") or {}

    bull_bits = []
    if good:
        bull_bits.append(f"versus peers, {_join(good)}")
    if (s.get("technicals") or 0) > 0.15:
        bull_bits.append("price trend/momentum is positive")
    if (s.get("sentiment") or 0) > 0.15:
        bull_bits.append("recent news runs better than usual")

    bear_bits = []
    if bad:
        bear_bits.append(f"versus peers, {_join(bad)}")
    if (s.get("technicals") or 0) < -0.15:
        bear_bits.append("price trend/momentum is negative")
    if (s.get("sentiment") or 0) < -0.15:
        bear_bits.append("recent news runs worse than usual")
    for f in rec.get("flags", []):
        if f in ("high_pe", "high_leverage", "negative_fcf", "negative_earnings"):
            bear_bits.append(f.replace("_", " "))

    bull = f"Bull case: {_join(bull_bits)}." if bull_bits else \
        "Bull case: no clearly favourable factors stand out in the screened data."
    bear = f"Bear case: {_join(bear_bits)}." if bear_bits else \
        "Bear case: no clear red flags in the screened data, but valuation/timing still warrant a look."
    return {"bull": bull, "bear": bear, "source": "deterministic"}


# --- orchestration ------------------------------------------------------------

def _signature(rec: dict) -> str:
    salient = {
        "v": rec.get("verdict"),
        "s": {k: round(v, 1) for k, v in (rec.get("scores") or {}).items() if isinstance(v, (int, float))},
        "f": sorted(rec.get("flags", [])),
        "m": [(m.get("label"), m.get("word")) for m in rec.get("fundamentals", []) if m.get("word")],
    }
    return hashlib.sha256(json.dumps(salient, sort_keys=True).encode()).hexdigest()[:16]


def _generate(rec: dict, session: requests.Session) -> dict:
    prompt = _prompt(rec)
    if os.environ.get("GEMINI_API_KEY"):
        try:
            parsed = _parse(_gemini(prompt, session))
            if parsed:
                return {**parsed, "source": "gemini"}
        except (requests.RequestException, KeyError, IndexError, ValueError):
            pass
    if os.environ.get("GROQ_API_KEY"):
        try:
            parsed = _parse(_groq(prompt, session))
            if parsed:
                return {**parsed, "source": "groq"}
        except (requests.RequestException, KeyError, IndexError, ValueError):
            pass
    return deterministic(rec)


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def annotate(records: list[dict], pause: float = 0.0) -> None:
    """Attach `ai` = {bull, bear, source} to each record, using the cache.

    Cached by input signature, so an LLM is only called when a ticker's screened
    profile changed. Deterministic fallback never touches the network.
    """
    cache = _load_cache()
    session = requests.Session()
    have_llm = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"))

    for rec in records:
        if "error" in rec:
            continue
        sig = _signature(rec)
        cached = cache.get(rec["symbol"])
        if cached and cached.get("sig") == sig:
            rec["ai"] = {k: cached[k] for k in ("bull", "bear", "source")}
            continue
        summary = _generate(rec, session)
        rec["ai"] = summary
        cache[rec["symbol"]] = {"sig": sig, **summary}
        if have_llm and summary["source"] != "deterministic" and pause:
            time.sleep(pause)  # stay within free-tier RPM on cold cache

    _save_cache(cache)
