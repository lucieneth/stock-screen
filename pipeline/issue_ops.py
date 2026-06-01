"""Apply a watchlist change requested via a GitHub Issue Form. (Issue+Action)

Reads the rendered issue body (env ISSUE_BODY), figures out the requested
action (add/remove) and tickers, applies them to config.yaml via
pipeline.watchlist, and writes a markdown summary for a reply comment.

UNTRUSTED INPUT: the issue body comes from whoever opened the issue. We only
ever interpret it as "add/remove these ticker symbols" — never execute it — and
reject anything that isn't a plausible ticker. The workflow additionally gates
on the issue author being the repo owner/collaborator.

    ISSUE_BODY="$BODY" python -m pipeline.issue_ops [comment_out_path]
"""
from __future__ import annotations

import os
import re
import sys

from pipeline import watchlist

TICKER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")
NO_RESPONSE = "_no response_"


def parse_issue(body: str) -> dict[str, str]:
    """Split a GitHub issue-form body (### Heading\\n\\nvalue) into a dict."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in (body or "").splitlines():
        if line.startswith("### "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[4:].strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def _value(sections: dict[str, str], *names: str) -> str:
    for n in names:
        for key, val in sections.items():
            if key.lower() == n.lower():
                return "" if val.strip().lower() == NO_RESPONSE else val.strip()
    return ""


def extract(body: str) -> tuple[str, list[str], list[str]]:
    """Return (action, valid_tickers, rejected_tokens)."""
    sections = parse_issue(body)
    action_raw = _value(sections, "Action").lower()
    action = "remove" if "remove" in action_raw else "add"
    tickers_raw = _value(sections, "Ticker(s)", "Tickers", "Ticker")
    tokens = [t for t in re.split(r"[\s,]+", tickers_raw) if t]
    valid, rejected = [], []
    for tok in tokens:
        (valid if TICKER_RE.match(tok) else rejected).append(tok.upper())
    # de-dupe, preserve order
    seen = set()
    valid = [t.upper() for t in valid if not (t.upper() in seen or seen.add(t.upper()))]
    return action, valid, rejected


def apply(action: str, tickers: list[str]) -> list[str]:
    """Apply add/remove for each ticker; return human-readable result lines."""
    fn = watchlist.add_ticker if action == "add" else watchlist.remove_ticker
    verb = "Added" if action == "add" else "Removed"
    results = []
    for t in tickers:
        changed, _ = fn(t)
        results.append(f"- {verb} **{t}**" if changed else f"- No change for **{t}** (already {'present' if action=='add' else 'absent'})")
    return results


def run(body: str) -> tuple[bool, str]:
    """Return (config_changed, comment_markdown)."""
    action, tickers, rejected = extract(body)
    if not tickers and not rejected:
        return False, "Couldn't find any ticker symbols in this request. Please use the watchlist form."

    before = set(watchlist.read_watchlist())
    lines = apply(action, tickers) if tickers else []
    after = watchlist.read_watchlist()
    changed = set(after) != before

    parts = [f"### Watchlist update — {action}"]
    if lines:
        parts.append("\n".join(lines))
    if rejected:
        parts.append("Ignored (not valid ticker symbols): " + ", ".join(f"`{r}`" for r in rejected))
    parts.append("\n**Current watchlist:** " + ", ".join(after))
    if changed:
        parts.append("\nThe screener will refresh with this change.")
    return changed, "\n\n".join(parts)


def main(argv: list[str]) -> int:
    body = os.environ.get("ISSUE_BODY", "")
    changed, comment = run(body)
    out_path = argv[1] if len(argv) > 1 else "watchlist_comment.md"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(comment)
    print(comment)
    # Signal to the workflow whether a commit is needed.
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
