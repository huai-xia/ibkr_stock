#!/usr/bin/env python3
"""
IBKR 并行拉取 1min K线 — 全自选股 × 7个月
多线程, 每线程独立 IB 连接, 共享任务队列
"""
import sys, time
from pathlib import Path
from datetime import datetime
import multiprocessing as mp

import pandas as pd
import pytz
from ib_insync import IB, Stock

DATA_DIR = Path("data/ibkr_1min")
DATA_DIR.mkdir(parents=True, exist_ok=True)
EST = pytz.timezone('US/Eastern')
WORKERS = 5
PAGE_SLEEP = 2

def load_watchlist():
    """美股自选股"""
    wl = Path("config/watchlist_full.yaml")
    if not wl.exists(): wl = Path("config/watchlist.yaml")
    syms = []
    with open(wl) as f:
        for line in f:
            line = line.strip()
            if not line or line[0] in "#-": continue
            s = line.split()[0].strip("[]").upper()
            if s.isalpha() and 1 <= len(s) <= 5:
                syms.append(s)
    return sorted(set(syms))

def fmt_ibkr(dt_str):
    """转 IBKR 时间格式"""
    dt = datetime.fromisoformat(str(dt_str)[:25])
    return dt.astimezone(EST).strftime('%Y%m%d %H:%M:%S US/Eastern')

def pull_one_stock(args):
    """子进程入口: 拉取单只股票"""
    sym, cid = args
    ib = IB()
    try:
        ib.connect('127.0.0.1', 4001, clientId=cid, timeout=15)
    except:
        return None

    contract = Stock(sym, 'SMART', 'USD')
    try:
        ib.qualifyContracts(contract)
    except:
        ib.disconnect()
        return None

    all_bars, pages, end_dt = [], 0, ''
    while pages < 50:
        pages += 1
        ib.sleep(PAGE_SLEEP)
        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime=end_dt, durationStr='1 W',
                barSizeSetting='1 min', whatToShow='TRADES',
                useRTH=True, formatDate=1
            )
        except:
            break
        if not bars or len(bars) < 10:
            break
        all_bars.extend(bars)
        dt = datetime.fromisoformat(str(bars[0].date)[:25])
        end_dt = dt.astimezone(EST).strftime('%Y%m%d %H:%M:%S US/Eastern')

    ib.disconnect()
    if not all_bars:
        return None

    rows = [{"date": b.date, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume}
            for b in all_bars]
    df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
    df.to_parquet(DATA_DIR / f"{sym}_1min.parquet", index=False)

    days = df["date"].dt.date.nunique()
    return {"symbol": sym, "bars": len(df), "days": days, "pages": pages}


def main():
    symbols = load_watchlist()
    n = len(symbols)
    print(f"📡 {n} 只美股, {WORKERS} 进程并行")
    print(f"📅 每只 ~7个月 1min K线\n")

    t0 = time.time()

    # 分配 client ID: 每个 worker 一个固定 ID
    tasks = [(s, 80 + i % WORKERS) for i, s in enumerate(symbols)]

    with mp.Pool(WORKERS) as pool:
        results = []
        for r in pool.imap_unordered(pull_one_stock, tasks):
            if r:
                results.append(r)
                done = len(results)
                pct = done / n * 100
                bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
                sys.stdout.write(f"\r  [{bar}] {pct:5.1f}% ({done}/{n}) {r['symbol']:<6} {r['bars']:>6}根 {r['days']:>3}天")
                sys.stdout.flush()

    elapsed = time.time() - t0
    results.sort(key=lambda x: x["symbol"])

    print(f"\n\n✅ 完成! {len(results)}/{n} 只, {elapsed:.0f}s\n")
    print(f"{'股票':<6} {'K线':>6} {'天数':>5} {'页数':>4}")
    print("─" * 25)
    for r in results:
        print(f"{r['symbol']:<6} {r['bars']:>6} {r['days']:>5} {r['pages']:>4}")

    total_bars = sum(r["bars"] for r in results)
    print(f"\n📊 总计: {total_bars:,} 根 K线, {len(results)} 只股票, {elapsed:.0f}s")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    main()
