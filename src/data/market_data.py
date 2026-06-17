"""
市场数据模块
实时行情订阅 + 历史K线获取 + 本地缓存
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable

import pandas as pd
from ib_insync import IB, Stock, BarDataList, util

logger = logging.getLogger(__name__)


class MarketData:
    """
    市场数据获取器

    使用方法:
        md = MarketData(ib)
        md.stream_quote("AAPL", callback=on_update)
        df = md.get_history("AAPL", days=60, bar_size="1 day")
    """

    def __init__(self, ib: IB, cache_dir: str = "data/history"):
        self._ib = ib
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 实时行情
    # ------------------------------------------------------------------

    def stream_quote(
        self,
        symbol: str,
        callback: Optional[Callable] = None,
    ):
        """
        订阅实时报价（流式推送）

        Args:
            symbol: 股票代码
            callback: 回调函数 callback(ticker)，每次更新时调用
                      若未提供，使用默认回调打印价格

        Returns:
            Ticker 对象
        """
        contract = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        ticker = self._ib.reqMktData(contract)

        if callback:
            ticker.updateEvent += callback

        logger.info(f"已订阅 {symbol} 实时行情")
        return ticker

    def stream_bars(
        self,
        symbol: str,
        bar_size: int = 5,
        callback: Optional[Callable] = None,
    ):
        """
        订阅实时K线（5秒K线）

        Args:
            symbol: 股票代码
            bar_size: K线周期（秒），必须是 5 的倍数
            callback: 回调函数 callback(bars, has_new_bar)

        Returns:
            RealTimeBarList 对象
        """
        contract = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        bars = self._ib.reqRealTimeBars(contract, bar_size, "TRADES", False)

        if callback:
            bars.updateEvent += callback

        logger.info(f"已订阅 {symbol} {bar_size}s K线")
        return bars

    def cancel_quote(self, ticker):
        """取消实时行情订阅"""
        self._ib.cancelMktData(ticker.contract)

    # ------------------------------------------------------------------
    # 历史数据
    # ------------------------------------------------------------------

    def get_history(
        self,
        symbol: str,
        days: int = 60,
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """
        获取历史K线数据（自动缓存）

        Args:
            symbol: 股票代码
            days: 获取天数
            bar_size: K线周期
                "1 min" | "5 mins" | "15 mins" | "30 mins" | "1 hour"
                | "1 day" | "1 week" | "1 month"
            what_to_show: 数据类型 TRADES | MIDPOINT | BID | ASK
            use_rth: 仅常规交易时段
            use_cache: 是否使用本地缓存

        Returns:
            DataFrame，列为 date/open/high/low/close/volume
        """
        cache_file = self._cache_path(symbol, bar_size)

        # 尝试从缓存加载
        cached = None
        if use_cache and cache_file.exists():
            cached = self._load_cache(cache_file)

        # 判断是否需要拉取
        if cached is not None and not cached.empty:
            last_cached = cached.index[-1]
            # 如果缓存足够新（最后一条在1天内），直接返回
            if isinstance(last_cached, pd.Timestamp):
                if (pd.Timestamp.now(tz=last_cached.tz) - last_cached).days <= 1:
                    logger.info(f"使用缓存数据: {symbol} ({len(cached)} 条)")
                    return cached

        # 从 IBKR 拉取
        contract = Stock(symbol, "SMART", "USD")
        self._ib.qualifyContracts(contract)

        duration = f"{days} D"
        logger.info(f"从 IBKR 获取 {symbol} {bar_size} 数据，{days} 天...")

        bars = self._ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
        )

        df = util.df(bars)

        if df is None or df.empty:
            logger.warning(f"{symbol} 无历史数据返回")
            return cached if cached is not None else pd.DataFrame()

        # 标准化列名
        df = df.rename(columns={"date": "date"})
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")

        # 合并缓存 + 新数据
        if cached is not None and not cached.empty:
            df = pd.concat([cached, df[~df.index.isin(cached.index)]])
            df = df.sort_index()

        # 保存缓存
        if use_cache:
            self._save_cache(cache_file, df)

        logger.info(f"✓ {symbol} 历史数据: {len(df)} 条 ({df.index[0]} ~ {df.index[-1]})")
        return df

    # ------------------------------------------------------------------
    # 缓存管理
    # ------------------------------------------------------------------

    def _cache_path(self, symbol: str, bar_size: str) -> Path:
        """生成缓存文件路径"""
        safe_name = f"{symbol}_{bar_size.replace(' ', '')}"
        return self._cache_dir / f"{safe_name}.parquet"

    def _load_cache(self, path: Path) -> Optional[pd.DataFrame]:
        """从 Parquet 加载缓存"""
        try:
            df = pd.read_parquet(path)
            if not df.empty:
                # 确保索引是 datetime
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
            return df
        except Exception as e:
            logger.warning(f"缓存读取失败 {path}: {e}")
            return None

    def _save_cache(self, path: Path, df: pd.DataFrame):
        """保存缓存到 Parquet"""
        try:
            df.to_parquet(path)
        except Exception as e:
            logger.warning(f"缓存保存失败 {path}: {e}")

    def clear_cache(self, symbol: Optional[str] = None):
        """清除缓存"""
        if symbol:
            for f in self._cache_dir.glob(f"{symbol}_*.parquet"):
                f.unlink()
                logger.info(f"已删除缓存: {f}")
        else:
            for f in self._cache_dir.glob("*.parquet"):
                f.unlink()
            logger.info("已清除所有缓存")
