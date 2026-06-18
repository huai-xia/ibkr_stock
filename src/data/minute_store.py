"""
L0 分钟数据存储器
盘中: 1分钟 OHLCV K线 → SYM_YYYY-MM-DD_1min.parquet
夜盘: 1分钟价格快照 → SYM_YYYY-MM-DD_extended.parquet
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = Path("data/minutes")
BASE_DIR.mkdir(parents=True, exist_ok=True)


class MinuteStore:
    """
    单只股票的分钟数据管理

    用法:
        ms = MinuteStore("AAPL")
        ms.append_1min("09:30:00", 300, 298, 301, 299.5, 1.2e6)
        series = ms.recent(20)  # 最近20分钟
        ms.close_day()          # 收盘聚合
    """

    def __init__(self, symbol: str):
        self.symbol = symbol.upper()
        self._today = datetime.now().strftime("%Y-%m-%d")
        self._1min_path = BASE_DIR / f"{self.symbol}_{self._today}_1min.parquet"
        self._ext_path = BASE_DIR / f"{self.symbol}_{self._today}_extended.parquet"

    # ------------------------------------------------------------------
    # 盘中 1分钟K线 (OHLCV)
    # ------------------------------------------------------------------

    def append_1min(
        self,
        timestamp: str,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ):
        """追加一根1分钟K线"""
        row = pd.DataFrame([{
            "timestamp": timestamp,
            "open": round(open_price, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "close": round(close, 2),
            "volume": volume,
        }])

        if self._1min_path.exists():
            df = pd.read_parquet(self._1min_path)
            df = pd.concat([df, row], ignore_index=True)
        else:
            df = row

        df.to_parquet(self._1min_path, index=False)

    def load_1min(self) -> Optional[pd.DataFrame]:
        """加载当日的1分钟K线"""
        if self._1min_path.exists():
            return pd.read_parquet(self._1min_path)
        return None

    def recent_1min(self, n: int = 20) -> Optional[pd.DataFrame]:
        """获取最近N分钟数据"""
        df = self.load_1min()
        if df is not None and len(df) >= n:
            return df.iloc[-n:]
        return df

    # ------------------------------------------------------------------
    # 夜盘/盘前快照 (价格 + 量)
    # ------------------------------------------------------------------

    def append_extended(
        self,
        timestamp: str,
        price: float,
        bid: float = 0,
        ask: float = 0,
        volume: float = 0,
    ):
        """追加一次延长时段快照"""
        row = pd.DataFrame([{
            "timestamp": timestamp,
            "price": round(price, 2),
            "bid": round(bid, 2) if bid else 0,
            "ask": round(ask, 2) if ask else 0,
            "volume": volume,
        }])

        if self._ext_path.exists():
            df = pd.read_parquet(self._ext_path)
            df = pd.concat([df, row], ignore_index=True)
        else:
            df = row

        df.to_parquet(self._ext_path, index=False)

    def load_extended(self) -> Optional[pd.DataFrame]:
        """加载当日延长时段快照"""
        if self._ext_path.exists():
            return pd.read_parquet(self._ext_path)
        return None

    def recent_extended(self, n: int = 20) -> Optional[pd.DataFrame]:
        df = self.load_extended()
        if df is not None and len(df) >= n:
            return df.iloc[-n:]
        return df

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def price_series(self, session: str = "all", n: int = 20) -> Optional[list[float]]:
        """获取最近 N 个价格点"""
        if session == "extended":
            df = self.recent_extended(n)
            if df is not None:
                return df["price"].tolist()
        elif session == "regular":
            df = self.recent_1min(n)
            if df is not None:
                return df["close"].tolist()
        else:
            # 合并两个时段
            prices = []
            ext = self.load_extended()
            if ext is not None:
                prices.extend(ext["price"].tolist()[-n:])
            reg = self.load_1min()
            if reg is not None:
                prices.extend(reg["close"].tolist()[-(n-len(prices)):])
            return prices[-n:] if prices else None
        return None

    # ------------------------------------------------------------------
    # 收盘聚合
    # ------------------------------------------------------------------

    def close_day(self) -> Optional[dict]:
        """收盘时将分钟线聚合为日线摘要"""
        df = self.load_1min()
        if df is None or df.empty:
            return None

        return {
            "date": self._today,
            "open": round(df["open"].iloc[0], 2),
            "high": round(df["high"].max(), 2),
            "low": round(df["low"].min(), 2),
            "close": round(df["close"].iloc[-1], 2),
            "volume": int(df["volume"].sum()),
            "bars": len(df),
        }

    def count(self) -> int:
        """当日已累计的分钟线数量"""
        df = self.load_1min()
        return len(df) if df is not None else 0


# ── 批量操作 ──

def collect_extended_snapshots(symbols: list[str]) -> dict[str, dict]:
    """
    一次性从富途批量拉取延长时段快照并存入分钟线
    自动根据当前时段选择正确价格字段
    返回 {symbol: price_data}
    """
    try:
        from futu import OpenQuoteContext, RET_OK
        from src.data.market_session import get_market_session

        session = get_market_session()
        price_field = session["price_field"]

        ctx = OpenQuoteContext(host='127.0.0.1', port=11111)

        codes = [f"US.{s}" for s in symbols]
        ret, data = ctx.get_market_snapshot(codes)
        ctx.close()

        if ret != RET_OK or data.empty:
            return {}

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results = {}

        for _, row in data.iterrows():
            sym = row.get("code", "").replace("US.", "")
            if not sym:
                continue

            overnight = row.get("overnight_price", 0)
            pre = row.get("pre_price", 0)
            after = row.get("after_price", 0)
            last = row.get("last_price", 0)
            bid = row.get("bid_price", 0)
            ask = row.get("ask_price", 0)
            vol = row.get("volume", 0)

            # 根据时段选正确价格字段
            if price_field == "pre_price":
                price = pre or last
            elif price_field == "after_price":
                price = after or last
            elif price_field == "overnight_price":
                price = overnight or last
            else:
                price = last

            if price <= 0:
                continue

            # 保存
            ms = MinuteStore(sym)
            ms.append_extended(now, price, bid, ask, vol)

            results[sym] = {
                "price": round(price, 2),
                "bid": round(bid, 2) if bid else 0,
                "ask": round(ask, 2) if ask else 0,
                "volume": vol,
                "prev_close": round(last, 2),
                "session": session["session"],
            }

        return results

    except Exception as e:
        logger.warning("Futu 批量快照失败: %s", e)
        return {}
