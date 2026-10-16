#!/usr/bin/env python3
"""taz-scanner calendar feed: upcoming earnings for the scanner universe.

Fetches the Finnhub earnings calendar (/calendar/earnings) for today and
tomorrow, keeps events for tickers in tickers.json (earnings on mega/large
caps are the market-moving events that matter for this scanner), and prints
a JSON array: [{symbol, date, hour, eps_estimate, revenue_estimate}].

NOTE: Finnhub's macro /calendar/economic endpoint is NOT available on the
free tier (returns 403), so this feed covers earnings only. The general
market-news headlines from news.py cover the rest of the market-moving flow.

The Finnhub API key is injected at request time through the dynamic-credential
surrogate (connection "custom.finnhub"); the real key never touches this file.
If the credential helper or the key is unavailable, the script prints a clear
skip message and exits 0 -- it never crashes and never logs the key.

Usage:
    calendar.py [--tickers tickers.json]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))

FINNHUB_CONN = "custom.finnhub"
FINNHUB_HOSTS = ["finnhub.io"]


def load_surrogate_helpers():
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    try:
        from dynamic_credentials import (  # noqa: E402
            read_json_response,
            url_with_surrogate_query_param,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"credential helper unavailable: {exc}") from exc
    return read_json_response, url_with_surrogate_query_param


def fetch_earnings() -> list[dict]:
    read_json_response, url_with_surrogate_query_param = load_surrogate_helpers()
    today = date.today()
    params = {
        "from": today.isoformat(),
        "to": (today + timedelta(days=1)).isoformat(),
    }
    url = "https://finnhub.io/api/v1/calendar/earnings?" + urllib.parse.urlencode(params)
    # The query-param NAME (e.g. token) comes from the credential's placement
    # config; the helper appends the surrogate under the right name.
    url = url_with_surrogate_query_param(url, FINNHUB_CONN, allowed_hosts=FINNHUB_HOSTS)
    req = urllib.request.Request(url, headers={"User-Agent": "taz-scanner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = read_json_response(resp)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Finnhub earnings request failed (HTTP {exc.code}); skipping calendar"
        ) from exc
    events = data.get("earningsCalendar") if isinstance(data, dict) else None
    if not isinstance(events, list):
        raise RuntimeError("unexpected Finnhub earnings response shape; skipping")
    return events


def main() -> int:
    ap = argparse.ArgumentParser(description="taz-scanner earnings calendar feed")
    ap.add_argument("--tickers", default=os.path.join(_HERE, "tickers.json"),
                    help="ticker universe file")
    args = ap.parse_args()

    try:
        universe = {e["ticker"] for e in json.load(open(args.tickers))}
    except Exception:  # noqa: BLE001 - missing universe file: keep everything
        print(f"[warn] could not read {args.tickers}; keeping all earnings",
              file=sys.stderr)
        universe = set()

    try:
        events = fetch_earnings()
    except Exception as exc:  # noqa: BLE001 - graceful skip, never a traceback
        print(f"[skip] calendar: {exc}", file=sys.stderr)
        print(json.dumps([]))
        return 0

    kept = []
    for ev in events:
        sym = str(ev.get("symbol", "")).upper()
        if universe and sym not in universe:
            continue
        kept.append({
            "symbol": sym,
            "date": ev.get("date", ""),
            "hour": ev.get("hour", ""),          # bmo | amc | dmh | ...
            "eps_estimate": ev.get("epsEstimate"),
            "revenue_estimate": ev.get("revenueEstimate"),
            "quarter": ev.get("quarter"),
        })
    kept.sort(key=lambda x: (x["date"], x["symbol"]))
    print(json.dumps(kept, indent=1))
    print(f"[info] {len(kept)} earnings events today/tomorrow", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
