"""
技术指标计算
基于 pandas-ta 封装，同时提供自研指标
"""

import logging
from typing import Optional

import pandas as pd
import numpy as np

try:
    import pandas_ta as ta
    _TA_AVAILABLE = True
except ImportError:
    _TA_AVAILABLE = False

logger = logging.getLogger(__name__)


class Indicators:
    """
    技术指标计算器

    使用方法:
        ind = Indicators()
        df = ind.add_all(df)
        # df 现在包含 sma_20, sma_50, rsi_14, bb_upper, bb_lower 等列
    """

    # ==================================================================
    # 均线类
    # ==================================================================

    @staticmethod
    def sma(df: pd.DataFrame, period: int = 20, column: str = "close") -> pd.Series:
        """简单移动平均"""
        return df[column].rolling(window=period).mean()

    @staticmethod
    def ema(df: pd.DataFrame, period: int = 20, column: str = "close") -> pd.Series:
        """指数移动平均"""
        return df[column].ewm(span=period, adjust=False).mean()

    @staticmethod
    def macd(
        df: pd.DataFrame,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        column: str = "close",
    ) -> pd.DataFrame:
        """
        MACD 指标

        Returns:
            DataFrame with columns: macd, macd_signal, macd_hist
        """
        ema_fast = df[column].ewm(span=fast, adjust=False).mean()
        ema_slow = df[column].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        hist = macd_line - signal_line

        return pd.DataFrame({
            "macd": macd_line,
            "macd_signal": signal_line,
            "macd_hist": hist,
        }, index=df.index)

    # ==================================================================
    # 震荡类
    # ==================================================================

    @staticmethod
    def rsi(df: pd.DataFrame, period: int = 14, column: str = "close") -> pd.Series:
        """相对强弱指标 (RSI)"""
        delta = df[column].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)

        avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()

        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def stochastic(
        df: pd.DataFrame,
        k_period: int = 14,
        d_period: int = 3,
    ) -> pd.DataFrame:
        """随机指标 (KD)"""
        low_min = df["low"].rolling(k_period).min()
        high_max = df["high"].rolling(k_period).max()

        k = 100 * (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan)
        d = k.rolling(d_period).mean()

        return pd.DataFrame({"stoch_k": k, "stoch_d": d}, index=df.index)

    # ==================================================================
    # 布林带
    # ==================================================================

    @staticmethod
    def bollinger_bands(
        df: pd.DataFrame,
        period: int = 20,
        std_dev: float = 2.0,
        column: str = "close",
    ) -> pd.DataFrame:
        """
        布林带

        Returns:
            DataFrame: bb_middle, bb_upper, bb_lower, bb_width, bb_pct_b
        """
        middle = df[column].rolling(period).mean()
        std = df[column].rolling(period).std()
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        width = (upper - lower) / middle * 100  # 带宽百分比
        pct_b = (df[column] - lower) / (upper - lower).replace(0, np.nan)  # %B

        return pd.DataFrame({
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width,
            "bb_pct_b": pct_b,
        }, index=df.index)

    # ==================================================================
    # 波动率
    # ==================================================================

    @staticmethod
    def atr(
        df: pd.DataFrame,
        period: int = 14,
    ) -> pd.Series:
        """平均真实波幅 (ATR)"""
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)

        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        return tr.ewm(alpha=1/period, adjust=False).mean()

    @staticmethod
    def historical_volatility(
        df: pd.DataFrame,
        period: int = 20,
        column: str = "close",
    ) -> pd.Series:
        """历史波动率（年化）"""
        log_returns = np.log(df[column] / df[column].shift(1))
        return log_returns.rolling(period).std() * np.sqrt(252)

    # ==================================================================
    # 成交量
    # ==================================================================

    @staticmethod
    def volume_sma(df: pd.DataFrame, period: int = 20) -> pd.Series:
        """成交量均线"""
        return df["volume"].rolling(period).mean()

    @staticmethod
    def volume_ratio(df: pd.DataFrame, period: int = 20) -> pd.Series:
        """量比 = 当日成交量 / N日均量"""
        avg_vol = df["volume"].rolling(period).mean()
        return df["volume"] / avg_vol.replace(0, np.nan)

    # ==================================================================
    # 趋势
    # ==================================================================

    @staticmethod
    def adx(
        df: pd.DataFrame,
        period: int = 14,
    ) -> pd.Series:
        """平均趋向指数 (ADX) — 判断趋势强度"""
        high, low, close = df["high"], df["low"], df["close"]

        up_move = high.diff()
        down_move = (-low).diff()
        plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/period, adjust=False).mean()

        plus_di = 100 * plus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
        minus_di = 100 * minus_dm.ewm(alpha=1/period, adjust=False).mean() / atr.replace(0, np.nan)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)

        return dx.ewm(alpha=1/period, adjust=False).mean()

    # ==================================================================
    # 批量添加
    # ==================================================================

    def add_all(
        self,
        df: pd.DataFrame,
        sma_periods: list[int] = None,
    ) -> pd.DataFrame:
        """
        批量添加常用指标到 DataFrame

        Args:
            df: 必须含 open/high/low/close/volume 列
            sma_periods: 需要计算的 SMA 周期列表，默认 [5, 10, 20, 50]

        Returns:
            添加了指标列的 DataFrame
        """
        df = df.copy()
        sma_periods = sma_periods or [5, 10, 20, 50]

        # 均线
        for p in sma_periods:
            df[f"sma_{p}"] = self.sma(df, p)

        # RSI
        df["rsi_14"] = self.rsi(df, 14)

        # 布林带
        bb = self.bollinger_bands(df, 20, 2)
        for col in bb.columns:
            df[col] = bb[col]

        # MACD
        macd_df = self.macd(df)
        for col in macd_df.columns:
            df[col] = macd_df[col]

        # ATR
        df["atr_14"] = self.atr(df, 14)

        # 量比
        df["vol_ratio"] = self.volume_ratio(df, 20)

        # 波动率
        df["hv_20"] = self.historical_volatility(df, 20)

        logger.debug("已添加技术指标: %d x %d", len(df), len(df.columns))
        return df


# 模块级快捷函数
_calc = Indicators()
add_all = _calc.add_all
sma = Indicators.sma
ema = Indicators.ema
rsi = Indicators.rsi
macd = Indicators.macd
bollinger_bands = Indicators.bollinger_bands
atr = Indicators.atr
