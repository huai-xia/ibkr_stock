#!/usr/bin/env python3
"""
批量拉取 yfinance 5min K线 — 62只自选股 × 60天
存储: data/minutes/{SYMBOL}_{YYYY-MM-DD}_5min.parquet
"""

import sys
from pathlib import Path
import time
from datetime import datetime

import pandas as pd
import yfinance as yf

# ── 配置 ──
DATA_DIR = Path("data/minutes")
DATA_DIR.mkdir(parents=True, exist_ok=True)
PERIOD = "60d"       # yfinance 5min 最大 60 天
INTERVAL = "5m"


def load_watchlist() -> list[str]:
    """解析 config/watchlist_full.yaml，仅返回美股"""
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
    """拉取单只股票 5min K 线"""
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=PERIOD, interval=INTERVAL)
        if df.empty:
            return df
        df = df.reset_index()
        df = df.rename(columns={
            "Datetime": "date",
            "Open": "open", "High": "high",
            "Low": "low", "Close": "close",
            "Volume": "volume",
        })
        cols = ["date", "open", "high", "low", "close", "volume"]
        df = df[[c for c in cols if c in df.columns]]
        df["date"] = pd.to_datetime(df["date"])
        return df.dropna(subset=["date"])
    except Exception as e:
        return pd.DataFrame()


def save(df: pd.DataFrame, symbol: str):
    """保存为 parquet (按天拆分)"""
    if df.empty:
        return 0
    saved = 0
    for day, grp in df.groupby(df["date"].dt.date):
        fname = DATA_DIR / f"{symbol}_{day}_5min.parquet"
        grp_out = grp.copy().reset_index(drop=True)
        grp_out.to_parquet(fname, index=False)
        saved += 1
    return saved


def main():
    symbols = load_watchlist()
    print(f"📡 自选股: {len(symbols)} 只")
    print(f"📅 参数: {PERIOD}, {INTERVAL} K线")
    print()

    total_bars = 0
    total_files = 0
    errors = 0
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
                days = df["date"].dt.date.nunique()
                saved = save(df, sym)
                total_files += saved
                print(f" → {bars}根 {days}天 {saved}文件", end="")
        except Exception as e:
            errors += 1
        finally:
            # 控制速率，避免被限
            time.sleep(0.5)

    print(f"\n\n✅ 完成!")
    elapsed = time.time() - t0
    print(f"   K线总数: {total_bars:,} 根")
    print(f"   文件数: {total_files} 个")
    print(f"   失败: {errors}")
    print(f"   耗时: {elapsed:.0f}s")


if __name__ == "__main__":
    main()
