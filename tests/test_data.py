"""
市场数据模块测试

运行方式:
    pytest tests/test_data.py -v
"""

import pandas as pd
import pytest

from src.data.market_data import MarketData


class TestMarketDataConfig:
    """MarketData 配置测试"""

    def test_cache_dir_creation(self, tmp_path):
        """测试缓存目录自动创建"""
        from unittest.mock import MagicMock
        mock_ib = MagicMock()
        md = MarketData(mock_ib, cache_dir=str(tmp_path / "cache"))
        assert md._cache_dir.exists()

    def test_cache_path_format(self):
        """测试缓存文件路径格式"""
        from unittest.mock import MagicMock
        mock_ib = MagicMock()
        md = MarketData(mock_ib)
        path = md._cache_path("AAPL", "1 day")
        assert path.name == "AAPL_1day.parquet"


class TestCacheOperations:
    """缓存读写测试"""

    def test_save_and_load_cache(self, tmp_path):
        """测试缓存保存与加载"""
        from unittest.mock import MagicMock

        mock_ib = MagicMock()
        md = MarketData(mock_ib, cache_dir=str(tmp_path))

        # 创建测试数据
        idx = pd.date_range("2026-01-01", periods=10, freq="D")
        df = pd.DataFrame({
            "open": range(100, 110),
            "high": range(101, 111),
            "low": range(99, 109),
            "close": range(100, 110),
            "volume": [1000] * 10,
        }, index=idx)

        cache_path = tmp_path / "AAPL_1day.parquet"
        md._save_cache(cache_path, df)

        # 重新加载
        loaded = md._load_cache(cache_path)
        assert loaded is not None
        assert len(loaded) == 10
        assert list(loaded.columns) == ["open", "high", "low", "close", "volume"]
