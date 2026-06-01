"""Orchestrator: load config -> fetch -> check -> score -> write docs/data/*.json.

Wired up across Phases 1-2 (data + scoring) and Phase 4 (Actions cron).
This skeleton just loads config.yaml and confirms the wiring point.
"""
from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Load the watchlist + thresholds + weights from config.yaml."""
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def main() -> None:
    config = load_config()
    watchlist = config.get("watchlist", [])
    print(f"Loaded config: {len(watchlist)} tickers in watchlist.")
    print("Pipeline stages land in Phases 1-2. Skeleton OK.")


if __name__ == "__main__":
    main()
