"""
L2 增量特征缓存
从分钟线数据滑动窗口增量计算技术指标，缓存到 Parquet
"""

import logging
from collections import deque
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = Path("data/features")
BASE_DIR.mkdir(parents=True, exist_ok=True)

# 滑动窗口大小
WINDOWS = {"sma_5": 5, "sma_20": 20, "rsi": 14, "zscore": 20,
           "vol_ratio": 20, "roc_5": 5, "roc_10": 10, "volatility": 20}


class FeatureCache:
    """
    单只股票的滑动窗口增量特征计算

    原理: 每个特征维护一个固定大小的 deque，
          新 bar 到来时 O(1) 更新，不重算历史数据。

    用法:
        fc = FeatureCache("AAPL")
        fc.update_from_1min(df_1min)
        latest = fc.latest()
    """

    COLUMNS = [
        "timestamp", "session", "close",
        "sma_5", "sma_20", "rsi_14",
        "zscore_20", "vwap_cum", "vol_ratio_20",
        "roc_5", "roc_10", "volatility_20",
    ]

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self._path = BASE_DIR / f"{self.symbol}_features.parquet"
        # 滑动窗口缓存
        self._dq_close: deque = deque(maxlen=22)    # 最大窗口+1
        self._dq_volume: deque = deque(maxlen=22)
        self._dq_close20: deque = deque(maxlen=21)   # Z-Score/波动率窗口
        self._ema_20: float = 0.0  # EMA(20) 状态
        self._ema_alpha: float = 2.0 / 21.0  # α = 2/(N+1)
        # VWAP 累加
        self._vwap_pv_sum: float = 0.0
        self._vwap_v_sum: float = 0.0

    # ------------------------------------------------------------------
    # 增量更新
    # ------------------------------------------------------------------

    def update_from_1min(self, df_1min: pd.DataFrame):
        """从1分钟K线滑动窗口增量计算特征"""
        if df_1min is None or df_1min.empty:
            return

        existing = self.load()
        last_computed = 0
        if existing is not None and not existing.empty:
            last_computed = len(existing[existing["session"] == "regular"])

        new_bars = df_1min.iloc[last_computed:]
        if new_bars.empty:
            return

        new_features = []
        for i in range(last_computed, len(df_1min)):
            row = self._compute_row_sliding(
                ts=str(df_1min.iloc[i].get("timestamp", "")),
                session="regular",
                close=float(df_1min.iloc[i]["close"]),
                volume=float(df_1min.iloc[i].get("volume", 0)),
            )
            new_features.append(row)

        new_df = pd.DataFrame(new_features)
        self._append(new_df)

    def update_from_extended(self, df_ext: pd.DataFrame):
        """从夜盘快照滑动窗口增量计算特征"""
        if df_ext is None or df_ext.empty:
            return

        existing = self.load()
        last_computed = 0
        if existing is not None and not existing.empty:
            last_computed = len(existing[existing["session"] == "extended"])

        new_bars = df_ext.iloc[last_computed:]
        if new_bars.empty:
            return

        new_features = []
        for i in range(last_computed, len(df_ext)):
            row = self._compute_row_sliding(
                ts=str(df_ext.iloc[i].get("timestamp", "")),
                session="extended",
                close=float(df_ext.iloc[i]["price"]),
                volume=float(df_ext.iloc[i].get("volume", 0)),
            )
            new_features.append(row)

        new_df = pd.DataFrame(new_features)
        self._append(new_df)

    # ------------------------------------------------------------------
    # 滑动窗口增量计算
    # ------------------------------------------------------------------

    def _compute_row_sliding(
        self, ts: str, session: str, close: float, volume: float,
    ) -> dict:
        """
        O(1) 滑动窗口增量计算

        每个 deque 维护固定大小的窗口，新值入队时最旧值自动出队。
        只对当前 bar 做 O(1) 运算，不重算历史。
        """
        self._dq_close.append(close)
        self._dq_volume.append(volume)
        self._dq_close20.append(close)

        n = len(self._dq_close)
        row = {"timestamp": ts, "session": session, "close": round(close, 2)}

        # ── SMA & EMA (滑动窗口均线) ──
        c_list = list(self._dq_close)
        if n >= 5:
            row["sma_5"] = round(sum(c_list[-5:]) / 5, 2)
        if n >= 20:
            row["sma_20"] = round(sum(c_list[-20:]) / 20, 2)
            # EMA(20): 第一天用SMA初始化，后续指数加权
            if self._ema_20 == 0.0:
                self._ema_20 = row["sma_20"]
            else:
                self._ema_20 = self._ema_alpha * close + (1 - self._ema_alpha) * self._ema_20
            row["ema_20"] = round(self._ema_20, 2)

        # ── RSI (简单均值, 14周期) ──
        if n >= 15:
            c = list(self._dq_close)
            deltas = [c[i+1] - c[i] for i in range(-15, -1)]
            gains = [max(d, 0) for d in deltas]
            losses = [abs(min(d, 0)) for d in deltas]
            avg_gain = sum(gains) / 14
            avg_loss = sum(losses) / 14
            rs = avg_gain / avg_loss if avg_loss > 0 else 100
            row["rsi_14"] = round(100.0 - 100.0 / (1.0 + rs), 1)

        # ── Z-Score (滑动窗口) ──
        if len(self._dq_close20) >= 20:
            w20 = list(self._dq_close20)[-20:]
            mu = sum(w20) / 20
            var = sum((x - mu) ** 2 for x in w20) / 20
            std = var ** 0.5
            row["zscore_20"] = round((close - mu) / std, 2) if std > 0 else 0.0

        # ── VWAP (增量累加) ──
        self._vwap_pv_sum += close * max(volume, 1)
        self._vwap_v_sum += max(volume, 1)
        row["vwap_cum"] = round(self._vwap_pv_sum / self._vwap_v_sum, 2) if self._vwap_v_sum > 0 else round(close, 2)

        # ── 综合目标 (多因素加权) ──
        if n >= 20 and hasattr(self, '_ema_20') and self._ema_20 > 0:
            ema = self._ema_20
            sma = row.get("sma_20", close)
            # 滚动20期VWAP（而非全天累加VWAP）
            c = list(self._dq_close)
            v = list(self._dq_volume)
            vwap20 = float(np.average(c[-20:], weights=v[-20:])) if sum(v[-20:]) > 0 else sma
            # EMA60% + 滚动VWAP25% + SMA15%
            row["comp_target"] = round(ema * 0.60 + vwap20 * 0.25 + sma * 0.15, 2)

        # ── 量比 ──
        if n >= 20:
            vol_list = list(self._dq_volume)
            avg_vol = sum(vol_list[-20:-1]) / 19 if n > 1 else volume
            row["vol_ratio_20"] = round(volume / avg_vol, 1) if avg_vol > 0 else 1.0

        # ── ROC ──
        close_list = list(self._dq_close)
        if n >= 6:
            row["roc_5"] = round((close - close_list[-6]) / close_list[-6] * 100, 1)
        if n >= 11:
            row["roc_10"] = round((close - close_list[-11]) / close_list[-11] * 100, 1)

        # ── 波动率 ──
        if n >= 22:
            w21 = close_list[-21:]
            rets = [(w21[i+1] - w21[i]) / w21[i] for i in range(20)]
            mu_r = sum(rets) / 20
            var_r = sum((r - mu_r) ** 2 for r in rets) / 20
            row["volatility_20"] = round(var_r ** 0.5 * 100, 2)

        return row

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------

    def load(self) -> Optional[pd.DataFrame]:
        if self._path.exists():
            return pd.read_parquet(self._path)
        return None

    def latest(self) -> Optional[dict]:
        df = self.load()
        if df is not None and not df.empty:
            return df.iloc[-1].to_dict()
        return None

    def _append(self, new_df: pd.DataFrame):
        existing = self.load()
        if existing is not None:
            # 只保留同 session 的 + 追加新的
            other_session = existing[existing["session"] != new_df["session"].iloc[0]]
            same_session = existing[existing["session"] == new_df["session"].iloc[0]]
            merged = pd.concat([same_session, new_df], ignore_index=True).drop_duplicates(subset=["timestamp", "session"])
            result = pd.concat([other_session, merged], ignore_index=True)
        else:
            result = new_df

        result.to_parquet(self._path, index=False)

    # ------------------------------------------------------------------
    # 复制滑动窗口状态（跨 session 保持连续性）
    # ------------------------------------------------------------------

    def _copy_state(self, other: "FeatureCache"):
        """从另一个 FeatureCache 复制滑动窗口状态"""
        self._dq_close = deque(other._dq_close, maxlen=22)
        self._dq_volume = deque(other._dq_volume, maxlen=22)
        self._dq_close20 = deque(other._dq_close20, maxlen=21)
        self._vwap_pv_sum = other._vwap_pv_sum
        self._vwap_v_sum = other._vwap_v_sum
