# taz-scanner

A bearish technical scanner for large/mega-cap stocks (plus a few popular
small caps), built for a trader who prefers **daily** candles. It watches for:

- **Fresh SMA 10 / 20 / 50 tests** — first touch in 10 bars after the price was
  clearly away from the SMA (bearish when the SMA acts as resistance)
- **SMA 10 / 20 / 50 crosses** — close crossing the SMA, direction tagged
- **Gravestone dojis** — long upper shadow, tiny body, close near the low
- **Bearish candle closes** — close in the lower quartile of the range, or a
  long upper-wick rejection into the close
- **Volume spikes** — bar volume ≥ 2× its 20-bar average

…plus a market-news headline feed and an earnings calendar feed, all delivered
to Discord as red embeds with TradingView links.

## Files

| File | What it does |
|---|---|
| `tickers.json` | ~200-ticker universe: S&P 500 large/mega caps (liquid-options proxy), price > $20, all 11 GICS sectors, tech-overweight (~33%), plus NIO, PLUG, MARA, RIOT |
| `scanner.py` | Scans the universe on hourly / 4h / daily candles; prints signal JSON |
| `news.py` | Finnhub general market news from the last 60 minutes; prints JSON |
| `calendar.py` | Finnhub earnings calendar for today + tomorrow, filtered to the universe; prints JSON |
| `discord_post.py` | Formats scanner/news/calendar JSON as Discord embeds and posts them via webhook |
| `requirements.txt` | Pinned Python deps |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Credentials are never stored in this repo.** Finnhub (`custom.finnhub`) and the
Discord webhook token (`custom.discord`) are injected at request time through
the dynamic-credential surrogate in
`/opt/muse/skills/custom-connector-setup/bin/dynamic_credentials.py`.
If a credential is unavailable, `news.py`, `calendar.py` and `discord_post.py`
print a `[skip]` message and exit 0 — they never crash and never log keys.

## Running

```bash
# scan daily candles, print signal JSON
.venv/bin/python scanner.py --timeframe daily

# quick test on 10 tickers
.venv/bin/python scanner.py --timeframe daily --dry-run

# hourly and 4h scans
.venv/bin/python scanner.py --timeframe hourly
.venv/bin/python scanner.py --timeframe 4h

# market news (last 60 min) and earnings calendar (today + tomorrow)
.venv/bin/python news.py
.venv/bin/python calendar.py

# post signals to Discord (dry-run prints the embed payload instead)
.venv/bin/python scanner.py --timeframe daily | .venv/bin/python discord_post.py --webhook-id $DISCORD_WEBHOOK_ID --dry-run
.venv/bin/python scanner.py --timeframe daily | .venv/bin/python discord_post.py --webhook-id $DISCORD_WEBHOOK_ID
```

## Suggested schedule

All times US/Eastern, weekdays:

| Job | Cadence | Command |
|---|---|---|
| Hourly scan | every 30 min, 9:30–16:00 | `scanner.py --timeframe hourly \| discord_post.py` |
| 4h scan | 10:00, 12:00, 14:00, 16:00 | `scanner.py --timeframe 4h \| discord_post.py` |
| Daily scan | 16:15 (after close) | `scanner.py --timeframe daily \| discord_post.py` |
| Market news | every 30 min, 8:00–17:00 | `news.py \| discord_post.py` (payload shape `{"news": [...]}`) |
| Earnings calendar | 8:00 daily | `calendar.py \| discord_post.py` (payload shape `{"events": [...]}`) |

> Note: `discord_post.py` reads a combined payload too, e.g.
> `{"signals": [...], "news": [...], "events": [...]}` — a tiny wrapper script
> can merge the three feeds into one Discord message per cycle.

## Notes & limitations

- **Finnhub free tier**: the macro economic calendar (`/calendar/economic`)
  returns 403 on the free plan, so `calendar.py` uses the **earnings**
  calendar instead — earnings on these large caps are the market-moving events
  that matter most for this scanner. Market-moving macro headlines still come
  through `news.py`.
- **yfinance** is free and keyless but unofficial: expect ~15-minute delayed
  data and occasional rate limits. The scanner batches downloads and paces
  requests; one bad ticker never kills a run.
- Signals are evaluated on the **last completed bar** of each timeframe, so
  the still-forming bar can't trigger false signals.
- "Fresh test" = price touches the SMA (within 0.25%) for the first time in
  10 bars after staying clearly away (>0.5% outside the bar range).
- Gravestone doji = body ≤ 10% of range, lower shadow ≤ 15% of range,
  upper shadow ≥ 50% of range.
