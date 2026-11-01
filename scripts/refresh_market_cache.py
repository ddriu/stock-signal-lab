"""Scheduled transport-only job. No SMTP, signals, model fills or real orders."""
from __future__ import annotations

import argparse
import os

from src.hourly_market_refresh import refresh_market_cache
from src.storage import create_journal, load_supabase_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true")
    args = parser.parse_args(argv)
    owner = os.getenv("MARKET_CACHE_OWNER", "").strip()
    if not owner or load_supabase_config() is None:
        print("Market cache: missing explicit backend scope/configuration; no work performed.")
        return 1
    try:
        result = refresh_market_cache(create_journal(owner), bootstrap=args.bootstrap,
                                      fundamental_limit=200 if args.bootstrap else 20)
    except Exception as exc:
        # Never print owners, symbols, backend responses or credentials in a
        # public workflow. Detailed provider strings are not needed here.
        print("Market cache failed: " + type(exc).__name__)
        return 1
    print(f"Market cache: {result.symbols} instruments, {result.prices_saved} histories, "
          f"{result.fundamentals_saved} fundamentals, {result.fx_saved} FX, "
          f"{result.skipped} skipped, {len(result.errors)} incidents. No mail or orders.")
    return 1 if result.errors and result.prices_saved == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
