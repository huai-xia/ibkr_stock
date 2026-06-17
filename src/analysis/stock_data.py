"""
股票数据管理器
批量下载历史K线、本地缓存、增量更新
"""

import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class StockDataManager:
    """
    股票数据管理器

    使用方法:
        mgr = StockDataManager(ib)
        mgr.download(["AAPL", "NVDA", "SOXL"], days=365)
        df = mgr.load("SOXL")  # 从缓存加载
        mgr.update_all()       # 增量更新所有已下载股票
    """

    def __init__(self, ib=None, cache_dir: str = "data/history"):
        self._ib = ib
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def set_ib(self, ib):
        """注入 IB 连接"""
        self._ib = ib

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------

    def download(
        self,
        symbols: list[str],
        days: int = 365,
        bar_size: str = "1 day",
        use_rth: bool = True,
    ) -> dict[str, pd.DataFrame]:
        """
        批量下载历史K线

        Returns:
            {"AAPL": DataFrame, "NVDA": DataFrame, ...}
        """
        if self._ib is None:
            raise RuntimeError("需要 IB 连接，先调用 set_ib() 或传入 ib 参数")

        results = {}
        from ib_insync import Stock

        for i, sym in enumerate(symbols, 1):
            logger.info("下载 [%d/%d] %s...", i, len(symbols), sym)
            try:
                contract = Stock(sym, "SMART", "USD")
                self._ib.qualifyContracts(contract)
                bars = self._ib.reqHistoricalData(
                    contract,
                    endDateTime="",
                    durationStr=f"{days} D",
                    barSizeSetting=bar_size,
                    whatToShow="TRADES",
                    useRTH=use_rth,
                    formatDate=1,
                )
                if bars:
                    from ib_insync import util
                    df = util.df(bars)
                    if df is not None and not df.empty:
                        df = df.rename(columns={"date": "date"})
                        if "date" in df.columns:
                            df["date"] = pd.to_datetime(df["date"])
                            df = df.set_index("date")
                        self._save(sym, bar_size, df)
                        results[sym] = df
            except Exception as e:
                logger.warning("下载失败 %s: %s", sym, e)

        logger.info("下载完成: %d/%d 只股票", len(results), len(symbols))
        return results

    def download_one(self, symbol: str, days: int = 365, bar_size: str = "1 day") -> Optional[pd.DataFrame]:
        """下载单只股票"""
        results = self.download([symbol], days, bar_size)
        return results.get(symbol)

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------

    def _cache_path(self, symbol: str, bar_size: str = "1 day") -> Path:
        safe = f"{symbol}_{bar_size.replace(' ', '')}"
        return self._cache_dir / f"{safe}.parquet"

    def _save(self, symbol: str, bar_size: str, df: pd.DataFrame):
        df.to_parquet(self._cache_path(symbol, bar_size))

    def load(self, symbol: str, bar_size: str = "1 day") -> Optional[pd.DataFrame]:
        """从缓存加载"""
        path = self._cache_path(symbol, bar_size)
        if path.exists():
            try:
                df = pd.read_parquet(path)
                if not isinstance(df.index, pd.DatetimeIndex):
                    df.index = pd.to_datetime(df.index)
                return df
            except Exception as e:
                logger.warning("缓存加载失败 %s: %s", symbol, e)
        return None

    def list_cached(self) -> list[str]:
        """列出已缓存的股票"""
        symbols = set()
        for f in self._cache_dir.glob("*.parquet"):
            sym = f.stem.split("_")[0]
            symbols.add(sym)
        return sorted(symbols)

    def update_all(self, days: int = 7):
        """增量更新所有已缓存股票"""
        symbols = self.list_cached()
        return self.download(symbols, days=days)

    def get_last_date(self, symbol: str, bar_size: str = "1 day") -> Optional[str]:
        """获取缓存中最后一条数据的日期"""
        df = self.load(symbol, bar_size)
        if df is not None and not df.empty:
            return str(df.index[-1])[:10]
        return None

    # ------------------------------------------------------------------
    # 分析用数据准备
    # ------------------------------------------------------------------

    def prepare_analysis_data(self, symbols: list[str], days: int = 365) -> dict:
        """
        为退出策略准备数据：下载+加载+计算技术指标

        Returns:
            {"AAPL": DataFrame (含OHLCV+技术指标), ...}
        """
        # 先下载
        missing = [s for s in symbols if self.load(s) is None]
        if missing:
            self.download(missing, days=days)

        # 加载并计算指标
        from src.strategy.indicators import add_all

        results = {}
        for sym in symbols:
            df = self.load(sym)
            if df is not None and not df.empty:
                df = add_all(df)
                results[sym] = df

        return results
