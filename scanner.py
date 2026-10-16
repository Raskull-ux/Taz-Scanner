#!/usr/bin/env python3
"""taz-scanner: bearish technical scanner for large/mega-cap stocks.

Scans a ticker universe (tickers.json) on hourly, 4h, or daily candles and
emits a JSON list of bearish-leaning signals:

  * fresh SMA 10 / 20 / 50 tests   - price touches the SMA for the first time
    in 10 bars after being clearly away from it (bearish when the SMA is
    acting as resistance, i.e. close < SMA)
  * SMA 10 / 20 / 50 crosses       - close crosses the SMA (direction tagged)
  * gravestone doji                - long upper shadow, tiny body, close near low
  * bearish candle closes          - close in the lower quartile of the range,
    or a long upper-wick rejection
  * volume spikes                  - bar volume >= 2x its 20-bar average

Signals are evaluated on the last *completed* bar of each timeframe, so the
forming bar never triggers a false signal.

Usage:
    scanner.py --timeframe daily [--tickers tickers.json] [--limit 10] [--dry-run]

--dry-run limits the scan to the first 10 tickers (quick sanity test).
Output is always a JSON array on stdout; pipe it into discord_post.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_HERE = os.path.dirname(os.path.abspath(__file__))
# This repo ships a calendar.py which shadows the stdlib `calendar` module for
# third-party imports (pandas/dateutil do `import calendar` internally).
# Keep this directory off the import path; local data files are found via _HERE.
sys.path = [p for p in sys.path if os.path.abspath(p or ".") != _HERE]

import pandas as pd
import yfinance as yf

ET = ZoneInfo("America/New_York")
NY = ET  # alias for readability

TIMEFRAMES = {
    "hourly": {"interval": "60m", "period": "1mo", "minutes": 60},
    "4h": {"interval": "60m", "period": "3mo", "minutes": 240, "resample": "4h"},
    "daily": {"interval": "1d", "period": "1y", "minutes": 60 * 24},
}

SMAS = (10, 20, 50)
FRESH_LOOKBACK = 10        # bars to check for a "fresh" first touch
TOUCH_TOL = 0.0025         # 0.25% of price counts as touching the SMA
AWAY_TOL = 0.005           # SMA must be >0.5% outside the bar range to be "away"
VOL_LOOKBACK = 20          # bars for average volume
VOL_MULTIPLE = 2.0         # volume >= 2x average = spike


# --------------------------------------------------------------------------- #
# data fetching
# --------------------------------------------------------------------------- #
def fetch_batch(symbols: list[str], interval: str, period: str) -> dict[str, pd.DataFrame]:
    """Download OHLCV for a batch of tickers; returns {symbol: df}."""
    try:
        raw = yf.download(
            symbols, period=period, interval=interval,
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
    except Exception as exc:  # noqa: BLE001 - batch failure is non-fatal
        print(f"[warn] download failed for batch of {len(symbols)}: {exc}", file=sys.stderr)
        return {}
    out: dict[str, pd.DataFrame] = {}
    single = len(symbols) == 1
    for sym in symbols:
        try:
            df = raw if single else raw[sym]
            df = df.dropna(subset=["Open", "High", "Low", "Close"])
            if not df.empty:
                out[sym] = df[["Open", "High", "Low", "Close", "Volume"]]
        except Exception:  # noqa: BLE001 - per-ticker failure must not kill the run
            print(f"[warn] skipping {sym}: no usable data", file=sys.stderr)
    return out


def to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """Resample 60m bars to 4h bars anchored at midnight ET."""
    if df.index.tz is None:
        df = df.tz_localize("UTC").tz_convert(ET)
    else:
        df = df.tz_convert(ET)
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    return df.resample("4h", origin="start_day").agg(agg).dropna()


def drop_forming_bar(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Remove the still-forming bar so signals only fire on completed candles."""
    if df.empty:
        return df
    now = datetime.now(ET)
    last = df.index[-1]
    if timeframe == "daily":
        last_day = last.tz_localize("UTC").tz_convert(ET).date() if last.tzinfo is None \
            else last.tz_convert(ET).date()
        if last_day > now.date():
            return df.iloc[:-1]
        if last_day == now.date():
            # Today's bar is only complete after the closing auction settles.
            # Before ~16:05 ET it is still forming (or pre-market) -> drop it.
            if now < now.replace(hour=16, minute=5, second=0, microsecond=0):
                return df.iloc[:-1]
    else:
        minutes = TIMEFRAMES[timeframe]["minutes"]
        ts = last if last.tzinfo is not None else last.tz_localize("UTC").tz_convert(ET)
        ts = ts.tz_convert(ET)
        if ts > now - timedelta(minutes=minutes):
            return df.iloc[:-1]
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for n in SMAS:
        df[f"sma{n}"] = df["Close"].rolling(n).mean()
    df["avgvol20"] = df["Volume"].rolling(VOL_LOOKBACK).mean()
    return df


# --------------------------------------------------------------------------- #
# signal detection (evaluated on the last row of a fully-computed dataframe)
# --------------------------------------------------------------------------- #
def _touches_sma(row: pd.Series, sma: float) -> bool:
    tol = row["Close"] * TOUCH_TOL
    return (row["Low"] - tol) <= sma <= (row["High"] + tol)


def _away_from_sma(row: pd.Series, sma: float) -> bool:
    tol = row["Close"] * AWAY_TOL
    return sma > row["High"] + tol or sma < row["Low"] - tol


def detect(df: pd.DataFrame, symbol: str, timeframe: str) -> list[dict]:
    signals: list[dict] = []
    if len(df) < 61:  # need 50-bar SMA + 10-bar fresh lookback + margin
        return signals
    row = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(row["Close"])
    bar_time = row.name
    if hasattr(bar_time, "isoformat"):
        bar_time = bar_time.isoformat()

    vol = float(row["Volume"] or 0)
    avgvol = float(row["avgvol20"] or 0)
    vol_mult = round(vol / avgvol, 2) if avgvol > 0 else 0.0

    def base(sig: str, direction: str, detail: str) -> dict:
        return {
            "ticker": symbol,
            "timeframe": timeframe,
            "signal": sig,
            "direction": direction,          # bearish | bullish | neutral
            "price": round(price, 2),
            "volume": int(vol),
            "volume_avg20": int(avgvol),
            "volume_multiple": vol_mult,
            "bar_time": bar_time,
            "detail": detail,
        }

    o, h, lo, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
    rng = h - lo
    if rng <= 0:
        return signals
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - lo

    # --- SMA tests & crosses -------------------------------------------------
    for n in SMAS:
        col = f"sma{n}"
        sma = float(row[col])
        sma_prev = float(prev[col])
        if pd.isna(sma) or pd.isna(sma_prev):
            continue
        # fresh test: touches now, untouched and clearly away for prior 10 bars
        if _touches_sma(row, sma):
            hist = df.iloc[-1 - FRESH_LOOKBACK:-1]
            fresh = all(
                _away_from_sma(r, float(r[col])) and not _touches_sma(r, float(r[col]))
                for _, r in hist.iterrows()
            )
            if fresh:
                # bearish setup: price under a declining/flat SMA = resistance test
                bearish = c < sma
                signals.append(base(
                    f"fresh_test_sma{n}",
                    "bearish" if bearish else "bullish",
                    f"first touch of SMA{n} ({sma:.2f}) in {FRESH_LOOKBACK} bars; "
                    f"{'testing as resistance' if bearish else 'testing as support'}",
                ))
        # cross: sign change of (close - sma)
        d_now, d_prev = c - sma, float(prev["Close"]) - sma_prev
        if d_prev >= 0 and d_now < 0:
            signals.append(base(f"cross_sma{n}_bearish", "bearish",
                                f"closed below SMA{n} ({sma:.2f})"))
        elif d_prev <= 0 and d_now > 0:
            signals.append(base(f"cross_sma{n}_bullish", "bullish",
                                f"closed above SMA{n} ({sma:.2f})"))

    # --- gravestone doji ------------------------------------------------------
    if body <= 0.10 * rng and lower <= 0.15 * rng and upper >= 0.50 * rng:
        signals.append(base("gravestone_doji", "bearish",
                            f"long upper shadow ({upper:.2f}) with tiny body; "
                            f"rejection at {h:.2f}"))

    # --- bearish candle closes ------------------------------------------------
    if (c - lo) <= 0.25 * rng:
        signals.append(base("bearish_close", "bearish",
                            f"closed in lower quartile of {lo:.2f}-{h:.2f} range"))
    elif upper >= 2 * max(body, 1e-9) and c <= (h + lo) / 2:
        signals.append(base("upper_wick_rejection", "bearish",
                            f"upper wick {upper:.2f} vs body {body:.2f}; "
                            f"rally faded into the close"))

    # --- volume spike ----------------------------------------------------------
    if avgvol > 0 and vol >= VOL_MULTIPLE * avgvol:
        signals.append(base("volume_spike", "neutral",
                            f"volume {vol_mult}x the 20-bar average"))
    return signals


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="taz-scanner bearish signal scan")
    ap.add_argument("--timeframe", choices=sorted(TIMEFRAMES), required=True)
    ap.add_argument("--tickers", default=os.path.join(_HERE, "tickers.json"),
                    help="ticker universe file")
    ap.add_argument("--limit", type=int, default=0, help="scan only first N tickers")
    ap.add_argument("--dry-run", action="store_true",
                    help="quick test: scan first 10 tickers only")
    args = ap.parse_args()

    with open(args.tickers) as f:
        universe = json.load(f)
    symbols = [e["ticker"] for e in universe]
    if args.dry_run:
        symbols = symbols[:10]
    elif args.limit:
        symbols = symbols[:args.limit]

    cfg = TIMEFRAMES[args.timeframe]
    all_signals: list[dict] = []
    for i in range(0, len(symbols), 60):
        batch = symbols[i:i + 60]
        frames = fetch_batch(batch, cfg["interval"], cfg["period"])
        for sym, df in frames.items():
            try:
                if cfg.get("resample"):
                    df = to_4h(df)
                df = drop_forming_bar(df, args.timeframe)
                df = add_indicators(df)
                all_signals.extend(detect(df, sym, args.timeframe))
            except Exception as exc:  # noqa: BLE001 - never die on one ticker
                print(f"[warn] {sym}: {exc}", file=sys.stderr)
        time.sleep(1)  # polite pacing between batches

    print(json.dumps(all_signals, indent=1))
    print(f"[info] {len(all_signals)} signals from {len(symbols)} tickers "
          f"({args.timeframe})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
