"""
连接管理器测试

运行方式:
    pytest tests/test_connection.py -v

注意: 需要先启动 TWS 或 IB Gateway
"""

import pytest
from src.core.connection import ConnectionManager


class TestConnectionManager:
    """连接管理器单元测试（不依赖实际连接）"""

    def test_default_config(self):
        """测试默认配置"""
        cm = ConnectionManager()
        assert cm.host == "127.0.0.1"
        assert cm.port == 4001
        assert cm.client_id == 1
        assert cm.is_connected is False

    def test_custom_config(self):
        """测试自定义配置"""
        cm = ConnectionManager(
            host="192.168.1.1",
            port=7497,
            client_id=99,
            max_retries=3,
            base_delay=2.0,
            max_delay=30.0,
        )
        assert cm.host == "192.168.1.1"
        assert cm.port == 7497
        assert cm.client_id == 99
        assert cm.max_retries == 3

    def test_repr_disconnected(self):
        """测试未连接状态的字符串表示"""
        cm = ConnectionManager()
        assert "✗ 未连接" in repr(cm)

    def test_heartbeat_disconnected(self):
        """测试未连接时心跳检测返回 False"""
        cm = ConnectionManager()
        assert cm.check_heartbeat() is False


# ============================================================
# 集成测试 — 需要 TWS/Gateway 运行
# 使用 pytest --integration 标记运行:
#   pytest tests/test_connection.py -v -m integration
# ============================================================

@pytest.mark.integration
class TestConnectionIntegration:
    """需要实际 TWS/Gateway 的集成测试"""

    def test_connect_and_disconnect(self):
        """测试连接与断开"""
        cm = ConnectionManager(port=4001, client_id=999)
        try:
            ib = cm.connect()
            assert ib.isConnected()
            assert cm.is_connected
        except ConnectionError:
            pytest.skip("TWS/Gateway 未运行")
        finally:
            cm.disconnect()
            assert not cm.is_connected

    def test_context_manager(self):
        """测试上下文管理器"""
        try:
            with ConnectionManager(port=4001, client_id=998) as ib:
                assert ib.isConnected()
        except ConnectionError:
            pytest.skip("TWS/Gateway 未运行")
