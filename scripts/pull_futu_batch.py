#!/usr/bin/env python3
"""
批量拉取富途 1min K线 — 62只自选股 × 3个月
分页策略: 每2天一个请求（避开 1000 根限制）
存储: data/minutes/{SYMBOL}_{YYYY-MM-DD}_1min.parquet
"""

import sys
from pathlib import Path
import time
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

from futu import OpenQuoteContext, RET_OK, KLType, SubType

# ── 配置 ──
DATA_DIR = Path("data/minutes")
DATA_DIR.mkdir(parents=True, exist_ok=True)

BACKTRACK_DAYS = 92  # ~3 个月
WINDOW_DAYS = 2      # 每次请求 2 天（2×390=780 < 1000 限制）

# ── 加载自选股 ──
def load_watchlist() -> list[str]:
    """解析 config/watchlist_full.yaml，仅返回美股 (纯字母 ticker)"""
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
            # 美股: 纯字母 (1-5位), 排除含数字的(韩股/日股)和含.的(HK/Crypto)
            if sym and sym.isalpha() and 1 <= len(sym) <= 5:
                symbols.append(sym)
    return sorted(set(symbols))


def generate_windows(end_date: datetime, backtrack_days: int, window_days: int) -> list[tuple[str, str]]:
    """生成分页时间窗口"""
    windows = []
    current = end_date
    start_bound = end_date - timedelta(days=backtrack_days)
    while current > start_bound:
        seg_end = current
        seg_start = current - timedelta(days=window_days)
        if seg_start < start_bound:
            seg_start = start_bound
        windows.append((seg_start.strftime("%Y-%m-%d"), seg_end.strftime("%Y-%m-%d")))
        current = seg_start
    return windows


def pull_one_window(ctx, symbol: str, start: str, end: str) -> pd.DataFrame:
    """拉取一个时间窗口的 1min K 线"""
    futu_sym = f"US.{symbol}"
    ret, df, _ = ctx.request_history_kline(
        futu_sym, start=start, end=end,
        ktype=KLType.K_1M, max_count=1000
    )
    if ret != RET_OK or df is None or len(df) == 0:
        return pd.DataFrame()
    df = df.rename(columns={
        "time_key": "date",
        "open": "open", "close": "close",
        "high": "high", "low": "low", "volume": "volume",
    })
    cols = ["date", "open", "high", "low", "close", "volume"]
    df = df[[c for c in cols if c in df.columns]]
    df["date"] = pd.to_datetime(df["date"])
    df = df.dropna(subset=["date"])
    return df


def save_by_day(df: pd.DataFrame, symbol: str):
    """按天拆分保存 parquet"""
    if df.empty:
        return 0
    saved = 0
    for day, grp in df.groupby(df["date"].dt.date):
        fname = DATA_DIR / f"{symbol}_{day}_1min.parquet"
        # 跳过已存在的
        # if fname.exists():
        #     continue
        grp_out = grp.reset_index(drop=True)
        grp_out.to_parquet(fname, index=False)
        saved += 1
    return saved


def show_progress(done: int, total: int, symbol: str, bars: int, elapsed: float):
    """简单进度条"""
    pct = done / total * 100 if total > 0 else 0
    bar_len = 30
    filled = int(bar_len * done / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    eta = ""
    if done > 0 and elapsed > 0:
        eta_sec = elapsed / done * (total - done)
        if eta_sec < 60:
            eta = f"剩余 {eta_sec:.0f}s"
        else:
            eta = f"剩余 {eta_sec/60:.1f}min"
    sys.stdout.write(
        f"\r  [{bar}] {pct:5.1f}% ({done}/{total}) | {symbol:<6} {bars:>4}根 | {eta:<15}"
    )
    sys.stdout.flush()


# ── 主流程 ──
def main():
    symbols = load_watchlist()
    end_date = datetime.now()
    windows = generate_windows(end_date, BACKTRACK_DAYS, WINDOW_DAYS)

    print(f"📡 自选股: {len(symbols)} 只")
    print(f"📅 时间范围: {windows[-1][0]} ~ {windows[0][1]} ({len(windows)} 个窗口)")
    n_requests = len(symbols) * len(windows)
    print(f"📦 预计请求: {n_requests} 次")
    print(f"💾 存储目录: {DATA_DIR}")
    print()

    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    total_windows = len(symbols) * len(windows)
    done = 0
    total_bars = 0
    total_files = 0
    t0 = time.time()
    errors = 0

    try:
        for sym in symbols:
            for (start, end) in windows:
                try:
                    df = pull_one_window(ctx, sym, start, end)
                    bars = len(df)
                    total_bars += bars
                    if not df.empty:
                        saved = save_by_day(df, sym)
                        total_files += saved
                except Exception as e:
                    errors += 1
                    if errors <= 3:
                        pass  # 静默跳过偶发错误
                    # 重连
                    try:
                        ctx.close()
                        time.sleep(2)
                        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
                    except:
                        pass

                done += 1
                if done % 50 == 0 or done == total_windows:
                    show_progress(done, total_windows, sym, bars, time.time() - t0)
            # 每只股票完成后刷新进度
            show_progress(done, total_windows, sym, bars, time.time() - t0)

        print("\n")
        elapsed = time.time() - t0
        print(f"✅ 拉取完成!")
        print(f"   请求: {done} 次, 错误: {errors}")
        print(f"   K线总数: {total_bars:,} 根")
        print(f"   保存文件: {total_files} 个")
        print(f"   耗时: {elapsed/60:.1f} 分钟")
        print(f"   速率: {total_bars/elapsed:.0f} 根/秒")

    except KeyboardInterrupt:
        print("\n⚠️ 用户中断")
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
