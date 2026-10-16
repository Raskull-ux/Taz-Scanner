#!/usr/bin/env python3
"""taz-scanner news feed: market-moving headlines from Finnhub.

Fetches Finnhub general market news, keeps headlines from the last 60 minutes,
and prints a JSON array: [{headline, source, url, datetime}].

The Finnhub API key is injected at request time through the dynamic-credential
surrogate (connection "custom.finnhub"); the real key never touches this file.
If the credential helper or the key is unavailable, the script prints a clear
skip message and exits 0 -- it never crashes and never logs the key.

Usage:
    news.py [--minutes 60]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

FINNHUB_CONN = "custom.finnhub"
FINNHUB_HOSTS = ["finnhub.io"]


def load_surrogate_helpers():
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    try:
        from dynamic_credentials import (  # noqa: E402
            DynamicCredentialError,
            read_json_response,
            url_with_surrogate_query_param,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"credential helper unavailable: {exc}") from exc
    return DynamicCredentialError, read_json_response, url_with_surrogate_query_param


def fetch_market_news() -> list[dict]:
    DynamicCredentialError, read_json_response, url_with_surrogate_query_param = (
        load_surrogate_helpers()
    )
    # The query-param NAME (e.g. token) comes from the credential's placement
    # config; the helper appends the surrogate under the right name.
    url = "https://finnhub.io/api/v1/news?category=general"
    url = url_with_surrogate_query_param(
        url, FINNHUB_CONN, allowed_hosts=FINNHUB_HOSTS
    )
    req = urllib.request.Request(url, headers={"User-Agent": "taz-scanner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = read_json_response(resp)
    except urllib.error.HTTPError as exc:
        # e.g. 401/403/429 -- free-tier limits; skip loudly, don't crash.
        raise RuntimeError(
            f"Finnhub news request failed (HTTP {exc.code}); skipping news feed"
        ) from exc
    if not isinstance(data, list):
        raise RuntimeError("unexpected Finnhub news response shape; skipping")
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="taz-scanner market news feed")
    ap.add_argument("--minutes", type=int, default=60,
                    help="keep headlines newer than this many minutes")
    args = ap.parse_args()

    try:
        items = fetch_market_news()
    except Exception as exc:  # noqa: BLE001 - graceful skip, never a traceback
        print(f"[skip] news: {exc}", file=sys.stderr)
        print(json.dumps([]))
        return 0

    cutoff = time.time() - args.minutes * 60
    fresh = [
        {
            "headline": it.get("headline", ""),
            "source": it.get("source", ""),
            "url": it.get("url", ""),
            "datetime": it.get("datetime"),
        }
        for it in items
        if isinstance(it.get("datetime"), (int, float)) and it["datetime"] >= cutoff
    ]
    fresh.sort(key=lambda x: x["datetime"] or 0, reverse=True)
    print(json.dumps(fresh, indent=1))
    print(f"[info] {len(fresh)} headlines in last {args.minutes} min", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
