"""Watchlist editing — add/remove tickers in config.yaml safely.

Edits the `watchlist:` block as text (line-by-line) so comments and formatting
elsewhere in config.yaml are preserved — a plain yaml.dump round-trip would
strip them. Symbols are upper-cased and de-duplicated.

    python -m pipeline.watchlist list
    python -m pipeline.watchlist add NFLX
    python -m pipeline.watchlist remove TSLA
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

_ITEM_RE = re.compile(r"^(\s*)-\s*([A-Za-z0-9.\-]+)\s*$")


def _split_block(lines: list[str]) -> tuple[int, int, str]:
    """Return (start, end, indent) of the watchlist list items.

    `start` is the index of the first list item, `end` is one past the last,
    and `indent` is the leading whitespace used for items.
    """
    head = next((i for i, ln in enumerate(lines) if re.match(r"^watchlist\s*:", ln)), None)
    if head is None:
        raise ValueError("config.yaml has no top-level 'watchlist:' key.")

    start = head + 1
    end = start
    indent = "  "
    for i in range(start, len(lines)):
        m = _ITEM_RE.match(lines[i])
        if m:
            indent = m.group(1) or indent
            end = i + 1
        elif lines[i].strip() == "" or lines[i].lstrip().startswith("#"):
            # blank / comment lines inside the block are tolerated, not counted
            continue
        else:
            break  # next key -> block is over
    return start, end, indent


def read_watchlist(path: Path = CONFIG_PATH) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    start, end, _ = _split_block(lines)
    out = []
    for ln in lines[start:end]:
        m = _ITEM_RE.match(ln)
        if m:
            out.append(m.group(2).upper())
    return out


def _write(path: Path, symbol: str, *, add: bool) -> tuple[bool, list[str]]:
    symbol = symbol.strip().upper()
    if not symbol:
        raise ValueError("Empty ticker.")
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    start, end, indent = _split_block(lines)
    current = {m.group(2).upper(): i for i in range(start, end)
               for m in [_ITEM_RE.match(lines[i])] if m}

    changed = False
    if add:
        if symbol not in current:
            # insert after the last real item (or right after the key)
            insert_at = end
            lines.insert(insert_at, f"{indent}- {symbol}")
            changed = True
    else:
        if symbol in current:
            del lines[current[symbol]]
            changed = True

    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed, read_watchlist(path)


def add_ticker(symbol: str, path: Path = CONFIG_PATH) -> tuple[bool, list[str]]:
    return _write(path, symbol, add=True)


def remove_ticker(symbol: str, path: Path = CONFIG_PATH) -> tuple[bool, list[str]]:
    return _write(path, symbol, add=False)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        print("\n".join(read_watchlist()))
        return 0
    if cmd in ("add", "remove") and len(argv) > 2:
        changed, wl = (add_ticker if cmd == "add" else remove_ticker)(argv[2])
        verb = "added" if cmd == "add" else "removed"
        print(f"{argv[2].upper()} {verb}." if changed else f"No change ({argv[2].upper()}).")
        print("watchlist:", ", ".join(wl))
        return 0
    print("usage: python -m pipeline.watchlist [list | add TICKER | remove TICKER]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
