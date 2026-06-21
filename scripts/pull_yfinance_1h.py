#!/usr/bin/env python3
"""
批量拉取 yfinance 1h K线 → 62只自选股 × 6个月 → 筛选短线均值回归标的
"""

import sys
from pathlib import Path
import time
from datetime import datetime

import pandas as pd
import numpy as np
import yfinance as yf

# ── 配置 ──
DATA_DIR = Path("data/hourly")
DATA_DIR.mkdir(parents=True, exist_ok=True)
PERIOD = "6mo"
INTERVAL = "1h"

def load_watchlist() -> list[str]:
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

def pull_one(symbol: str) -> pd.DataFrame:
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=PERIOD, interval=INTERVAL)
        if df.empty:
            return df
        df = df.reset_index()
        df = df.rename(columns={
            "Datetime": "date", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })
        cols = ["date", "open", "high", "low", "close", "volume"]
        df = df[[c for c in cols if c in df.columns]]
        df["date"] = pd.to_datetime(df["date"])
        return df.dropna(subset=["date"])
    except:
        return pd.DataFrame()

def main():
    symbols = load_watchlist()
    print(f"📡 {len(symbols)} 只美股")
    print(f"📅 {PERIOD} {INTERVAL} K线\n")

    total_bars, total_files, errors = 0, 0, 0
    t0 = time.time()
    n = len(symbols)

    for i, sym in enumerate(symbols):
        pct = (i + 1) / n * 100
        bar_fill = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
        sys.stdout.write(f"\r  [{bar_fill}] {pct:5.1f}% ({i+1}/{n}) {sym:<6}")
        sys.stdout.flush()

        try:
            df = pull_one(sym)
            bars = len(df)
            total_bars += bars
            if not df.empty:
                fname = DATA_DIR / f"{sym}_1h.parquet"
                df.to_parquet(fname, index=False)
                total_files += 1
                days = df["date"].dt.date.nunique()
                print(f" → {bars}根 {days}天", end="")
        except:
            errors += 1
        time.sleep(0.3)

    elapsed = time.time() - t0
    print(f"\n\n✅ 完成! {total_bars:,}根K线, {total_files}只股票, {errors}失败, {elapsed:.0f}s")

if __name__ == "__main__":
    main()
