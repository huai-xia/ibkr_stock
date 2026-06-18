"""
L2 增量特征缓存
从分钟线数据增量计算技术指标，缓存到 Parquet
避免每次扫描从原始数据全量重算
"""

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = Path("data/features")
BASE_DIR.mkdir(parents=True, exist_ok=True)


class FeatureCache:
    """
    单只股票的增量特征计算

    用法:
        fc = FeatureCache("AAPL")
        fc.update_from_1min(df_1min)     # 增量更新盘中特征
        fc.update_from_extended(df_ext)  # 增量更新夜盘特征
        latest = fc.latest()             # 最新一行特征
        history = fc.load()              # 全部特征历史
    """

    # 特征列定义
    COLUMNS = [
        "timestamp", "session",  # 时间戳 + 时段(regular/extended)
        "close",                 # 收盘价（计算用）
        "sma_5", "sma_20",       # 均线
        "rsi_14",                # RSI
        "zscore_20",             # 20周期 Z-Score
        "vwap_cum",              # 累计VWAP
        "vol_ratio_20",          # 20周期量比
        "roc_5", "roc_10",       # 变化率
        "volatility_20",         # 20周期波动率
    ]

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self._path = BASE_DIR / f"{self.symbol}_features.parquet"

    # ------------------------------------------------------------------
    # 增量更新
    # ------------------------------------------------------------------

    def update_from_1min(self, df_1min: pd.DataFrame):
        """从1分钟K线增量计算特征"""
        if df_1min is None or df_1min.empty:
            return

        # 加载已有特征
        existing = self.load()
        last_computed = 0
        if existing is not None and not existing.empty:
            last_computed = len(existing[existing["session"] == "regular"])

        # 只计算新增的行
        new_bars = df_1min.iloc[last_computed:]
        if new_bars.empty:
            return

        close = df_1min["close"].values.astype(float)
        volume = df_1min["volume"].values.astype(float)

        new_features = []
        for i in range(last_computed, len(df_1min)):
            row = self._compute_row(
                ts=str(df_1min.iloc[i].get("timestamp", "")),
                session="regular",
                close=close[i],
                close_series=close[:i+1],
                volume_series=volume[:i+1],
                volume=volume[i] if i < len(volume) else 0,
            )
            new_features.append(row)

        new_df = pd.DataFrame(new_features)
        self._append(new_df)

        logger.debug("%s: 增量计算 %d 行盘中特征", self.symbol, len(new_df))

    def update_from_extended(self, df_ext: pd.DataFrame):
        """从夜盘快照增量计算特征"""
        if df_ext is None or df_ext.empty:
            return

        existing = self.load()
        last_computed = 0
        if existing is not None and not existing.empty:
            last_computed = len(existing[existing["session"] == "extended"])

        new_bars = df_ext.iloc[last_computed:]
        if new_bars.empty:
            return

        prices = df_ext["price"].values.astype(float)
        volumes = df_ext["volume"].values.astype(float) if "volume" in df_ext.columns else np.zeros(len(df_ext))

        new_features = []
        for i in range(last_computed, len(df_ext)):
            row = self._compute_row(
                ts=str(df_ext.iloc[i].get("timestamp", "")),
                session="extended",
                close=prices[i],
                close_series=prices[:i+1],
                volume_series=volumes[:i+1],
                volume=volumes[i] if i < len(volumes) else 0,
            )
            new_features.append(row)

        new_df = pd.DataFrame(new_features)
        self._append(new_df)

        logger.debug("%s: 增量计算 %d 行夜盘特征", self.symbol, len(new_df))

    # ------------------------------------------------------------------
    # 核心计算
    # ------------------------------------------------------------------

    def _compute_row(
        self, ts: str, session: str,
        close: float, close_series: np.ndarray, volume_series: np.ndarray,
        volume: float,
    ) -> dict:
        """计算一根K线对应的特征行"""
        n = len(close_series)

        row = {"timestamp": ts, "session": session, "close": round(float(close), 2)}

        # SMA
        if n >= 5:
            row["sma_5"] = round(float(np.mean(close_series[-5:])), 2)
        if n >= 20:
            row["sma_20"] = round(float(np.mean(close_series[-20:])), 2)

        # RSI (14)
        if n >= 15:
            row["rsi_14"] = round(float(self._calc_rsi(close_series, 14)), 1)

        # Z-Score (20)
        if n >= 20:
            window = close_series[-20:]
            mu, std = float(np.mean(window)), float(np.std(window))
            row["zscore_20"] = round(float((close - mu) / std), 2) if std > 0 else 0.0

        # VWAP
        if n >= 1:
            row["vwap_cum"] = round(float(np.average(close_series[-n:], weights=volume_series[-n:])), 2) if volume_series[-n:].sum() > 0 else round(float(close), 2)

        # 量比
        if n >= 20:
            avg_vol = float(np.mean(volume_series[-20:-1])) if n > 1 else volume
            row["vol_ratio_20"] = round(float(volume / avg_vol), 1) if avg_vol > 0 else 1.0

        # ROC
        if n >= 6:
            row["roc_5"] = round(float((close - close_series[-6]) / close_series[-6] * 100), 1)
        if n >= 11:
            row["roc_10"] = round(float((close - close_series[-11]) / close_series[-11] * 100), 1)

        # 波动率 (20周期)
        if n >= 20:
            returns = np.diff(close_series[-20:]) / close_series[-21:-1]
            row["volatility_20"] = round(float(np.std(returns) * 100), 2)

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
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_rsi(prices: np.ndarray, period: int = 14) -> float:
        deltas = np.diff(prices[-period-1:])
        gains = np.maximum(deltas, 0)
        losses = np.abs(np.minimum(deltas, 0))
        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses) if len(losses) > 0 else 0
        if avg_loss == 0:
            return 100.0
        return 100 - 100 / (1 + avg_gain / avg_loss)
