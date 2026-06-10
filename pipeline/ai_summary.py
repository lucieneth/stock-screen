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
    "You explain a stock to a busy everyday investor who is NOT a finance expert. "
    "Using ONLY the facts provided, write in plain, friendly English — short sentences, "
    "no jargon. If you must mention a metric, say what it means (e.g. 'profit margins are "
    "strong' not 'net margin in top quartile'). Return STRICT JSON with exactly three keys: "
    "\"takeaway\" (one sentence, the gist a beginner needs — is this screening as a possible "
    "buy, a wait, or a pass, and the single biggest reason), \"bull\" (1-2 sentences: the "
    "main reasons it looks good), \"bear\" (1-2 sentences: the main risks/why it might not). "
    "Do not give financial advice or price targets, and do not invent facts."
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
    """Pull {"takeaway","bull","bear"} out of a model response (fence-tolerant)."""
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
        out = {"bull": bull.strip(), "bear": bear.strip()}
        tk = obj.get("takeaway")
        out["takeaway"] = tk.strip() if isinstance(tk, str) and tk.strip() else ""
        return out
    return None


# --- deterministic fallback ---------------------------------------------------

def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


# Plain-English phrasing for each metric's good/bad label.
_PLAIN = {
    ("pe", "cheap"): "the price looks cheap for its earnings",
    ("pe", "expensive"): "the price looks expensive for its earnings",
    ("pb", "cheap"): "cheap relative to assets", ("pb", "expensive"): "pricey relative to assets",
    ("ps", "cheap"): "cheap relative to sales", ("ps", "expensive"): "pricey relative to sales",
    ("gross_margin", "strong"): "strong gross margins", ("gross_margin", "weak"): "thin gross margins",
    ("net_margin", "strong"): "strong profit margins", ("net_margin", "weak"): "thin profit margins",
    ("roe", "strong"): "high returns on shareholder money", ("roe", "weak"): "low returns on shareholder money",
    ("roa", "strong"): "uses its assets efficiently", ("roa", "weak"): "uses its assets inefficiently",
    ("current_ratio", "strong"): "comfortable short-term finances", ("current_ratio", "weak"): "tight short-term finances",
    ("debt_to_equity", "lean"): "low debt", ("debt_to_equity", "heavy"): "heavy debt",
    ("rev_growth", "strong"): "fast revenue growth", ("rev_growth", "weak"): "slow revenue growth",
}


def _plain(m: dict) -> str:
    return _PLAIN.get((m.get("key"), m.get("word")), f"{m.get('label')} {m.get('word')}")


def deterministic(rec: dict) -> dict:
    """Plain-English bull/bear/takeaway from the screener's own labels — no LLM."""
    funds = rec.get("fundamentals") or []
    good = [_plain(m) for m in funds if m.get("tone") == "good"][:3]
    bad = [_plain(m) for m in funds if m.get("tone") == "bad"][:3]
    s = rec.get("scores") or {}
    verdict = rec.get("verdict")

    bull_bits = list(good)
    if (s.get("technicals") or 0) > 0.15:
        bull_bits.append("the price trend is heading up")
    if (s.get("sentiment") or 0) > 0.15:
        bull_bits.append("recent news is better than usual")

    bear_bits = list(bad)
    if (s.get("technicals") or 0) < -0.15:
        bear_bits.append("the price trend is heading down")
    if (s.get("sentiment") or 0) < -0.15:
        bear_bits.append("recent news is worse than usual")

    bull = ("On the plus side, " + _join(bull_bits) + ".") if bull_bits else \
        "Nothing clearly stands out in its favour in the screened data."
    bear = ("Watch out that " + _join(bear_bits) + ".") if bear_bits else \
        "No obvious red flags, but always check valuation and timing yourself."

    if verdict == "WATCH-BUY":
        gist = good[0] if good else "it scores well overall"
        takeaway = f"Screening as a possible BUY — mainly because {gist}."
    elif verdict == "WATCH-SELL":
        gist = bad[0] if bad else "its overall score is weak"
        takeaway = f"Screening as one to AVOID/trim — mainly because {gist}."
    else:
        takeaway = "A mixed picture — worth watching, but no strong push either way."
    return {"takeaway": takeaway, "bull": bull, "bear": bear, "source": "deterministic"}


# --- orchestration ------------------------------------------------------------

def _signature(rec: dict) -> str:
    salient = {
        "v": rec.get("verdict"),
        "s": {k: round(v, 1) for k, v in (rec.get("scores") or {}).items() if isinstance(v, (int, float))},
        "f": sorted(rec.get("flags", [])),
        "m": [(m.get("label"), m.get("word")) for m in rec.get("fundamentals", []) if m.get("word")],
    }
    return hashlib.sha256(json.dumps(salient, sort_keys=True).encode()).hexdigest()[:16]


def complete(prompt: str, session: requests.Session | None = None,
             validate=None) -> tuple[str, str] | None:
    """Raw LLM completion via Gemini -> Groq. Returns (text, source) or None.

    `validate(text)` must return truthy to accept a response; if it returns
    falsy (e.g. unparseable JSON), the next provider is tried. Shared by the
    per-stock summaries and the daily briefing.
    """
    session = session or requests.Session()
    validate = validate or (lambda t: bool(t and t.strip()))
    for name, fn in (("gemini", _gemini), ("groq", _groq)):
        if not os.environ.get(f"{name.upper()}_API_KEY"):
            continue
        try:
            text = fn(prompt, session)
        except (requests.RequestException, KeyError, IndexError, ValueError):
            continue
        if validate(text):
            return text, name
    return None


def _generate(rec: dict, session: requests.Session) -> dict:
    out = complete(_prompt(rec), session, validate=lambda t: _parse(t) is not None)
    if out:
        return {**_parse(out[0]), "source": out[1]}
    return deterministic(rec)


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def annotate(records: list[dict], pause: float = 0.0, max_new: int | None = None) -> None:
    """Attach `ai` = {bull, bear, source} to each record, using the cache.

    Cached by input signature, so an LLM is only called when a ticker's screened
    profile changed. To bound a cold run, at most `max_new` LLM generations
    happen per run (default from AI_MAX_PER_RUN, 8); once that budget is spent,
    remaining changed tickers fall back to the instant deterministic summary and
    get the richer LLM version on a later run.
    """
    if max_new is None:
        max_new = int(os.environ.get("AI_MAX_PER_RUN", "8"))
    cache = _load_cache()
    session = requests.Session()
    have_llm = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"))
    generated = 0

    for rec in records:
        if "error" in rec:
            continue
        sig = _signature(rec)
        cached = cache.get(rec["symbol"])
        if cached and cached.get("sig") == sig and "takeaway" in cached:
            rec["ai"] = {k: cached.get(k, "") for k in ("takeaway", "bull", "bear", "source")}
            continue

        # Spend the LLM budget first; afterwards (or on failure) use the
        # deterministic fallback and DON'T cache it, so a later run can still
        # upgrade that ticker via the LLM.
        if have_llm and generated < max_new:
            summary = _generate(rec, session)
            rec["ai"] = summary
            if summary["source"] != "deterministic":
                generated += 1
                cache[rec["symbol"]] = {"sig": sig, **summary}
                if pause:
                    time.sleep(pause)  # stay within free-tier RPM
        else:
            rec["ai"] = deterministic(rec)

    if generated:
        print(f"ai: generated {generated} new summary(ies)", flush=True)
    _save_cache(cache)
