#!/usr/bin/env python3
"""yfinance 15min K线 60天 — 62只自选股 → 筛选均值回归标的"""
import sys, time
from pathlib import Path
import pandas as pd
import yfinance as yf

DATA_DIR = Path("data/minute_15")
DATA_DIR.mkdir(parents=True, exist_ok=True)

def load_watchlist():
    wl_path = Path("config/watchlist_full.yaml")
    if not wl_path.exists():
        wl_path = Path("config/watchlist.yaml")
    symbols = []
    with open(wl_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            sym = line.split()[0] if " " in line else line
            sym = sym.strip("[]").strip().upper()
            if sym.isalpha() and 1 <= len(sym) <= 5:
                symbols.append(sym)
    return sorted(set(symbols))

def pull(symbol):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="60d", interval="15m")
        if df.empty:
            return None
        df = df.reset_index()
        df = df.rename(columns={"Datetime": "date", "Open": "open", "High": "high",
                                "Low": "low", "Close": "close", "Volume": "volume"})
        return df[["date", "open", "high", "low", "close", "volume"]]
    except:
        return None

symbols = load_watchlist()
n = len(symbols)
t0 = time.time()
total = 0

for i, sym in enumerate(symbols):
    pct = (i + 1) / n * 100
    bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
    sys.stdout.write(f"\r  [{bar}] {pct:5.1f}% {sym:<6}")
    sys.stdout.flush()

    df = pull(sym)
    if df is not None and len(df) > 0:
        df.to_parquet(DATA_DIR / f"{sym}_15m.parquet", index=False)
        total += 1
        days = df["date"].dt.date.nunique()
        print(f" → {len(df)}根 {days}天", end="")
    time.sleep(0.4)

print(f"\n\n✅ {total}/{n}只, {time.time()-t0:.0f}s")
