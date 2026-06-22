#!/usr/bin/env python3
"""
贝叶斯参数优化 — Optuna 搜索均值回归策略最优参数
Walk-forward: 40天训练 → 20天验证
"""

import sys; sys.path.insert(0, '.')
import pandas as pd; import numpy as np
from pathlib import Path
import optuna
from src.strategy.indicators import add_all

DATA_DIR = Path("data/ibkr_1min")
CAPITAL = 1000
N_TRIALS = 200  # 每只股票试200组参数


def run_backtest(df, params):
    """单次回测 — 返回 (总盈亏, 胜率, 交易数)"""
    df = df.copy()
    df = add_all(df)
    p = params

    # 自定义窗口的指标
    win = p["sma_window"]
    df["ema_custom"] = df["close"].ewm(span=win, adjust=False).mean()
    std = df["close"].rolling(win).std()
    df["sma_custom"] = df["close"].rolling(win).mean()
    df["zscore"] = (df["close"] - df["sma_custom"]) / std.replace(0, np.nan)
    typ = (df["high"] + df["low"] + df["close"]) / 3
    vp = (typ * df["volume"]).rolling(win).sum()
    vs = df["volume"].rolling(win).sum()
    df["vwap"] = vp / vs.replace(0, np.nan)

    # 布林带也要用自定义窗口
    bb_std = df["close"].rolling(win).std()
    df["bb_lower"] = df["sma_custom"] - 2 * bb_std

    trades = []
    pos = None; cool_until = None

    for i in range(win + 5, len(df)):
        row = df.iloc[i]; now = df.index[i]

        if cool_until and now < cool_until:
            continue

        # 出场
        if pos:
            exit_p = None; reason = ""
            if row["low"] <= pos["stop"]: exit_p = pos["stop"]; reason = "stop"
            elif row["high"] >= pos["target"]: exit_p = pos["target"]; reason = "target"
            elif now.hour >= 15 and now.minute >= 50: exit_p = row["close"]; reason = "close"

            if exit_p:
                pnl = (exit_p - pos["entry"]) / pos["entry"] * 100
                trades.append({"pnl": round(CAPITAL * pnl / 100, 2)})
                pos = None
                cool_until = now + pd.Timedelta(minutes=p["cool_min"])
            continue

        # 入场
        zs = row.get("zscore", 0); rsi = row.get("rsi_14", 50)
        bb = row.get("bb_lower", 0); atr = row.get("atr_14", 0); close = row["close"]

        if not (zs < p["z_thresh"] and rsi < p["rsi_thresh"] and close <= bb * (1 + p["bb_prox"])):
            continue
        if atr <= 0:
            continue

        # 趋势过滤
        if i >= 10:
            s10 = df.iloc[i-10].get("sma_custom", 0)
            s0 = row.get("sma_custom", 0)
            if s0 > 0 and s10 > 0 and (s0 - s10) / s10 <= p["trend_slope"]:
                continue

        entry = close
        stop = close - atr * p["atr_mult"]
        target = (row["ema_custom"] * p["ema_w"] +
                  row.get("vwap", close) * p["vwap_w"] +
                  row["sma_custom"] * (1 - p["ema_w"] - p["vwap_w"]))

        if target <= entry * 1.002:
            continue

        pos = {"entry": entry, "stop": round(stop, 2), "target": round(target, 2)}

    if pos:
        pnl = (df.iloc[-1]["close"] - pos["entry"]) / pos["entry"] * 100
        trades.append({"pnl": round(CAPITAL * pnl / 100, 2)})

    if not trades:
        return 0, 0, 0

    total = sum(t["pnl"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    wr = wins / len(trades)
    return total, round(wr * 100, 1), len(trades)


def objective(trial, df_train, df_val):
    """Optuna 目标函数 — 最大化 train 得分, 惩罚过拟合"""
    params = {
        "z_thresh": trial.suggest_float("z_thresh", -3.5, -1.5, step=0.1),
        "rsi_thresh": trial.suggest_int("rsi_thresh", 20, 45),
        "bb_prox": trial.suggest_float("bb_prox", 0.003, 0.03, step=0.002),
        "atr_mult": trial.suggest_float("atr_mult", 1.5, 5.0, step=0.1),
        "cool_min": trial.suggest_int("cool_min", 5, 30),
        "trend_slope": trial.suggest_float("trend_slope", -0.012, -0.001, step=0.001),
        "sma_window": trial.suggest_int("sma_window", 10, 50, step=2),
        "ema_w": trial.suggest_float("ema_w", 0.35, 0.75, step=0.05),
        "vwap_w": trial.suggest_float("vwap_w", 0.10, 0.35, step=0.05),
    }

    train_pnl, train_wr, train_n = run_backtest(df_train, params)
    val_pnl, val_wr, val_n = run_backtest(df_val, params)

    if train_n < 5 or val_n < 3:
        return -999  # 交易太少, 跳过

    # 得分 = train总盈亏 × 胜率 — 惩罚过拟合
    train_score = train_pnl * (train_wr / 100)
    val_score = val_pnl * (val_wr / 100)

    # 过拟合惩罚: 验证集比训练集差太多时扣分
    overfit_ratio = val_score / max(train_score, 0.01)
    penalty = max(0, 1 - overfit_ratio) * abs(train_score) * 0.5

    final = train_score - penalty
    trial.set_user_attr("val_pnl", val_pnl)
    trial.set_user_attr("train_pnl", train_pnl)
    trial.set_user_attr("train_n", train_n)
    trial.set_user_attr("val_n", val_n)
    return final


def optimize_stock(sym):
    """优化单只股票参数"""
    df = pd.read_parquet(DATA_DIR / f"{sym}_1min.parquet")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()

    # Walk-forward: 最近60天 → 前40天训练, 后20天验证
    all_dates = sorted(set(df.index.date))
    if len(all_dates) < 40:
        return None, None
    train_dates = set(all_dates[-40:-20])
    val_dates = set(all_dates[-20:])

    date_series = pd.Series(df.index.date, index=df.index)
    df_train = df[date_series.isin(train_dates)]
    df_val = df[date_series.isin(val_dates)]

    if len(df_train) < 500 or len(df_val) < 300:
        return None, None

    # 先用默认参数跑baseline
    default_params = {
        "z_thresh": -2.5, "rsi_thresh": 35, "bb_prox": 0.01,
        "atr_mult": 2.5, "cool_min": 10, "trend_slope": -0.003,
        "sma_window": 20, "ema_w": 0.6, "vwap_w": 0.25,
    }
    base_pnl, base_wr, base_n = run_backtest(df_val, default_params)
    base_val_score = base_pnl * (base_wr / 100)

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
        pruner=optuna.pruners.MedianPruner(n_startup_trials=20),
    )
    study.optimize(
        lambda trial: objective(trial, df_train, df_val),
        n_trials=N_TRIALS, show_progress_bar=False,
    )

    best = study.best_params
    opt_pnl, opt_wr, opt_n = run_backtest(df_val, best)

    return {
        "symbol": sym,
        "base_pnl": round(base_pnl, 1), "base_wr": base_wr, "base_n": base_n,
        "opt_pnl": round(opt_pnl, 1), "opt_wr": opt_wr, "opt_n": opt_n,
        "params": best,
        "improvement": round(opt_pnl - base_pnl, 1) if base_val_score > -999 else 0,
    }, study


if __name__ == "__main__":
    files = sorted(DATA_DIR.glob("*_1min.parquet"))
    print(f"🔬 优化 {len(files)} 只股票, 每只 {N_TRIALS} 次试验\n")

    all_results = []
    for f in files:
        sym = f.stem.replace("_1min", "")
        print(f"  {sym} ... ", end="", flush=True)
        r, study = optimize_stock(sym)
        if r:
            all_results.append(r)
            imp = r["improvement"]
            sign = "📈" if imp > 10 else ("📉" if imp < -5 else "➡️")
            print(f"{sign} 基线${r['base_pnl']:+.0f}→优化${r['opt_pnl']:+.0f} ({imp:+.0f}) "
                  f"胜率{r['base_wr']}%→{r['opt_wr']}%")
        else:
            print("❌ 数据不足")

    # 汇总
    print(f"\n{'='*80}")
    print(f"  参数优化结果汇总")
    print(f"{'='*80}")
    print(f"{'股票':<6} {'基线盈亏':>8} {'优化盈亏':>8} {'提升':>8} {'基线胜率':>8} {'优化胜率':>8} | 最优参数")
    print(f"{'─'*90}")
    for r in sorted(all_results, key=lambda x: -x["improvement"]):
        p = r["params"]
        print(f"{r['symbol']:<6} ${r['base_pnl']:>+7.1f} ${r['opt_pnl']:>+7.1f} ${r['improvement']:>+7.1f} "
              f"{r['base_wr']:>7.1f}% {r['opt_wr']:>7.1f}% | "
              f"Z{p['z_thresh']:.1f} R{p['rsi_thresh']} BB{p['bb_prox']:.3f} "
              f"ATR{p['atr_mult']:.1f} S{p['sma_window']} "
              f"E{p['ema_w']:.0%}V{p['vwap_w']:.0%}")

    total_imp = sum(r["improvement"] for r in all_results)
    total_base = sum(r["base_pnl"] for r in all_results)
    total_opt = sum(r["opt_pnl"] for r in all_results)
    print(f"{'─'*90}")
    print(f"  合计提升: ${total_imp:+.1f} ({total_base:+.1f} → {total_opt:+.1f})")
