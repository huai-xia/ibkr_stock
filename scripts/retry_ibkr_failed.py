#!/usr/bin/env python3
"""补拉失败的 IBKR 1min 数据 — 逐个重试，间隔冷却"""
import sys, time
from pathlib import Path
from datetime import datetime
import pytz
import pandas as pd
from ib_insync import IB, Stock

DATA_DIR = Path("data/ibkr_1min")
EST = pytz.timezone('US/Eastern')

# 检查缺失
CANDIDATES = ['PANW','WULF','SOXL','EWY','INTC','ORCL','SNDK','MU','COHR','CBRS','DELL']
missing = [s for s in CANDIDATES if not (DATA_DIR / f"{s}_1min.parquet").exists()]
print(f"❌ 缺失: {len(missing)} 只\n")

for i, sym in enumerate(missing):
    print(f"[{i+1}/{len(missing)}] {sym} ... ", end="", flush=True)

    # 每次重连前等待冷却
    if i > 0:
        time.sleep(5)

    try:
        ib = IB()
        ib.connect('127.0.0.1', 4001, clientId=80, timeout=20)
        contract = Stock(sym, 'SMART', 'USD')
        ib.qualifyContracts(contract)

        all_bars, pages, end_dt = [], 0, ''
        while pages < 50:
            pages += 1
            ib.sleep(2)
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

        if all_bars:
            rows = [{"date": b.date, "open": b.open, "high": b.high,
                     "low": b.low, "close": b.close, "volume": b.volume}
                    for b in all_bars]
            df = pd.DataFrame(rows).drop_duplicates("date").sort_values("date")
            df.to_parquet(DATA_DIR / f"{sym}_1min.parquet", index=False)
            print(f"✅ {len(df)}根 {df['date'].dt.date.nunique()}天 {pages}页")
        else:
            print("❌ 无数据")
    except Exception as e:
        print(f"❌ {str(e)[:50]}")

# 最终统计
files = list(DATA_DIR.glob("*_1min.parquet"))
total = sum(len(pd.read_parquet(f)) for f in files)
print(f"\n📊 总计: {len(files)}/14 只, {total:,} 根 K线")
