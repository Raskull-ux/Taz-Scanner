#!/usr/bin/env python3
"""taz-scanner Discord delivery: format scanner output as embeds and post.

Reads a JSON payload from stdin or --file. Accepted shapes:
  * {"signals": [...]}            - scanner.py output
  * {"news": [...]}               - news.py output
  * {"events": [...]}             - calendar.py output
  * [...]                        - a bare list is treated as signals
  * {"signals": [...], "news": [...], "events": [...]} - combined

Embeds use a bearish-red color; each signal links to its TradingView page.
With --dry-run the final Discord JSON payload is printed instead of posted.

The webhook token is injected at request time through the dynamic-credential
surrogate (connection "custom.discord"). The webhook ID is NOT secret: pass
it with --webhook-id or the DISCORD_WEBHOOK_ID env var. The real token never
touches this file, logs, or the shell history.

Usage:
    scanner.py --timeframe daily | discord_post.py --webhook-id 123456
    discord_post.py --file out.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DISCORD_CONN = "custom.discord"
DISCORD_HOSTS = ["discord.com"]
BEARISH_RED = 0xE02B2B
MAX_EMBEDS_PER_MESSAGE = 10  # Discord limit


def load_surrogate_helpers():
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    try:
        from dynamic_credentials import (  # noqa: E402
            url_with_surrogate_path_segment,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"credential helper unavailable: {exc}") from exc
    return url_with_surrogate_path_segment


def tv_link(ticker: str) -> str:
    return f"https://www.tradingview.com/symbols/{ticker}/"


def signal_embed(s: dict) -> dict:
    ticker = str(s.get("ticker", "?")).upper()
    sig = str(s.get("signal", "signal")).replace("_", " ").upper()
    direction = str(s.get("direction", "neutral"))
    emoji = {"bearish": "\U0001f53b", "bullish": "\U0001f7e2"}.get(direction, "\u26aa")
    return {
        "title": f"{emoji} {ticker} — {sig}",
        "url": tv_link(ticker),
        "color": BEARISH_RED,
        "fields": [
            {"name": "Price", "value": f"${s.get('price', '?')}", "inline": True},
            {"name": "Volume", "value": f"{s.get('volume_multiple', '?')}x avg",
             "inline": True},
            {"name": "Timeframe", "value": str(s.get("timeframe", "?")), "inline": True},
            {"name": "Detail", "value": str(s.get("detail", ""))[:256], "inline": False},
        ],
        "footer": {"text": f"taz-scanner • {s.get('bar_time', '')}"},
    }


def news_embed(n: dict) -> dict:
    return {
        "title": "\U0001f4f0 " + str(n.get("headline", ""))[:256],
        "url": n.get("url") or None,
        "color": BEARISH_RED,
        "fields": [{"name": "Source", "value": str(n.get("source", "?")), "inline": True}],
    }


def event_embed(e: dict) -> dict:
    hour = {"bmo": "before open", "amc": "after close", "dmh": "during hours"}.get(
        str(e.get("hour", "")).lower(), str(e.get("hour", ""))
    )
    ticker = str(e.get("symbol", "?")).upper()
    return {
        "title": f"\U0001f4c5 {ticker} earnings — {e.get('date', '')} ({hour})",
        "url": tv_link(ticker),
        "color": BEARISH_RED,
        "fields": [
            {"name": "EPS est.", "value": str(e.get("eps_estimate", "?")), "inline": True},
            {"name": "Revenue est.", "value": str(e.get("revenue_estimate", "?")),
             "inline": True},
        ],
    }


def build_messages(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        payload = {"signals": payload}
    embeds: list[dict] = []
    for s in payload.get("signals", []) or []:
        embeds.append(signal_embed(s))
    for n in payload.get("news", []) or []:
        embeds.append(news_embed(n))
    for e in payload.get("events", []) or []:
        embeds.append(event_embed(e))
    if not embeds:
        embeds.append({
            "title": "\u2705 taz-scanner: no signals this run",
            "color": BEARISH_RED,
        })
    return [
        {"embeds": embeds[i:i + MAX_EMBEDS_PER_MESSAGE]}
        for i in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE)
    ]


def post_messages(messages: list[dict], webhook_id: str) -> None:
    url_with_surrogate_path_segment = load_surrogate_helpers()
    template = f"https://discord.com/api/webhooks/{webhook_id}/{{}}"
    # The "{}" path segment is replaced by the surrogate; Sentinel swaps it for
    # the real webhook token on approved egress.
    url = url_with_surrogate_path_segment(
        template, DISCORD_CONN, allowed_hosts=DISCORD_HOSTS
    )
    for msg in messages:
        body = json.dumps(msg).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "taz-scanner/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status not in (200, 204):
                    raise RuntimeError(f"Discord returned HTTP {resp.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Discord post failed (HTTP {exc.code})") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description="taz-scanner Discord delivery")
    ap.add_argument("--file", help="JSON file to post (default: stdin)")
    ap.add_argument("--webhook-id", default=os.environ.get("DISCORD_WEBHOOK_ID"),
                    help="Discord webhook ID (not secret; token comes from vault)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the Discord payload instead of posting")
    args = ap.parse_args()

    raw = open(args.file).read() if args.file else sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[error] invalid JSON input: {exc}", file=sys.stderr)
        return 1

    messages = build_messages(payload)
    if args.dry_run:
        print(json.dumps(messages, indent=1))
        print(f"[info] dry-run: {len(messages)} message(s), "
              f"{sum(len(m['embeds']) for m in messages)} embed(s)",
              file=sys.stderr)
        return 0

    if not args.webhook_id:
        print("[error] set --webhook-id or DISCORD_WEBHOOK_ID", file=sys.stderr)
        return 1
    try:
        post_messages(messages, args.webhook_id)
    except Exception as exc:  # noqa: BLE001 - report, never leak the token
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    print(f"[info] posted {len(messages)} message(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
