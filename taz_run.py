#!/usr/bin/env python3
"""taz-scanner orchestrator: scan, dedupe against seen state, deliver new items.

Runs the scanner (and optionally news/calendar feeds), filters out anything
already delivered, and posts only NEW items to Discord. State lives in
.state/seen.json next to this script; entries older than 72h are pruned.

Nothing secret lives here: the Finnhub key and Discord webhook token travel
as vault surrogates inside news.py / calendar.py / discord_post.py.

Usage:
    taz_run.py --timeframe hourly [--news-minutes 70] [--dry-run]
    taz_run.py --timeframe daily --calendar [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE_DIR = os.path.join(_HERE, ".state")
_SEEN_FILE = os.path.join(_STATE_DIR, "seen.json")
_SEEN_TTL = 72 * 3600  # forget seen items after 3 days
_VENV_PY = os.path.join(_HERE, ".venv", "bin", "python")
_PY = _VENV_PY if os.path.exists(_VENV_PY) else sys.executable


def sh(args: list[str], **kw) -> str:
    p = subprocess.run(args, capture_output=True, text=True, timeout=900, **kw)
    if p.returncode != 0:
        print(f"[warn] {' '.join(args[:2])} exited {p.returncode}: "
              f"{p.stderr.strip()[:200]}", file=sys.stderr)
    return p.stdout


def load_seen() -> dict:
    try:
        with open(_SEEN_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    now = time.time()
    return {
        k: {sk: sv for sk, sv in v.items() if now - sv < _SEEN_TTL}
        for k, v in data.items() if isinstance(v, dict)
    }


def save_seen(seen: dict) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    tmp = _SEEN_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(seen, f)
    os.replace(tmp, _SEEN_FILE)


def sig_key(s: dict) -> str:
    return "|".join(str(s.get(k, "")) for k in
                    ("ticker", "timeframe", "signal", "bar_time"))


def main() -> int:
    ap = argparse.ArgumentParser(description="taz-scanner orchestrator")
    ap.add_argument("--timeframe", required=True, choices=["hourly", "4h", "daily"])
    ap.add_argument("--limit", type=int, default=0,
                    help="scan only first N tickers (testing)")
    ap.add_argument("--news-minutes", type=int, default=0,
                    help="also fetch news from last N minutes")
    ap.add_argument("--calendar", action="store_true",
                    help="also fetch earnings calendar (today + tomorrow)")
    ap.add_argument("--webhook-id", default=os.environ.get("DISCORD_WEBHOOK_ID"),
                    help="Discord webhook ID (not secret)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print payload instead of posting to Discord")
    args = ap.parse_args()

    seen = load_seen()
    now = time.time()
    new_signals, new_news, new_events = [], [], []

    # 1. scan
    scan_cmd = [_PY, os.path.join(_HERE, "scanner.py"),
                "--timeframe", args.timeframe]
    if args.limit:
        scan_cmd += ["--limit", str(args.limit)]
    out = sh(scan_cmd)
    try:
        signals = json.loads(out or "[]")
        if isinstance(signals, dict):
            signals = signals.get("signals", [])
    except ValueError:
        signals = []
    seen_sigs = seen.setdefault("signals", {})
    for s in signals:
        k = sig_key(s)
        if k not in seen_sigs:
            seen_sigs[k] = now
            new_signals.append(s)
    print(f"[info] {len(signals)} signals, {len(new_signals)} new",
          file=sys.stderr)

    # 2. news
    if args.news_minutes:
        out = sh([_PY, os.path.join(_HERE, "news.py"),
                  "--minutes", str(args.news_minutes)])
        try:
            items = json.loads(out or "[]")
        except ValueError:
            items = []
        seen_news = seen.setdefault("news", {})
        for n in items:
            k = n.get("url") or n.get("headline", "")
            if k and k not in seen_news:
                seen_news[k] = now
                new_news.append(n)
        print(f"[info] {len(items)} headlines, {len(new_news)} new",
              file=sys.stderr)

    # 3. earnings calendar
    if args.calendar:
        out = sh([_PY, os.path.join(_HERE, "calendar.py")])
        try:
            payload = json.loads(out or "{}")
            events = payload.get("events", payload) if isinstance(payload, dict) \
                else payload
        except ValueError:
            events = []
        seen_ev = seen.setdefault("events", {})
        for e in events:
            k = f"{e.get('symbol')}|{e.get('date')}"
            if k not in seen_ev:
                seen_ev[k] = now
                new_events.append(e)
        print(f"[info] {len(events)} earnings events, {len(new_events)} new",
              file=sys.stderr)

    save_seen(seen)

    combined = {"signals": new_signals, "news": new_news, "events": new_events}
    if not (new_signals or new_news or new_events):
        print("[info] nothing new — skipping Discord post", file=sys.stderr)
        return 0

    post_cmd = [_PY, os.path.join(_HERE, "discord_post.py")]
    if args.webhook_id:
        post_cmd += ["--webhook-id", args.webhook_id]
    if args.dry_run:
        post_cmd += ["--dry-run"]
    p = subprocess.run(post_cmd, input=json.dumps(combined), capture_output=True,
                       text=True, timeout=300)
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    return p.returncode


if __name__ == "__main__":
    raise SystemExit(main())
