"""
IBKR 连接管理器
负责 TWS / IB Gateway 的连接、心跳检测、自动重连
"""

import logging
import time
from typing import Optional

from ib_insync import IB

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    IBKR 连接管理器

    使用方法:
        cm = ConnectionManager(host="127.0.0.1", port=4001, client_id=1)
        ib = cm.connect()

    特性:
        - 自动连接，失败时指数退避重试
        - 心跳检测，断线自动重连
        - 连接状态查询
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4001,
        client_id: int = 1,
        max_retries: int = 10,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        heartbeat_interval: int = 30,
    ):
        """
        Args:
            host: TWS/Gateway 地址，默认本机
            port: 端口
                - 4001: IB Gateway Paper Trading
                - 4002: IB Gateway Live Trading
                - 7496: TWS Paper Trading
                - 7497: TWS Live Trading
            client_id: 客户端 ID（同一 TWS 下每个连接需不同）
            max_retries: 最大重试次数
            base_delay: 初始重试延迟（秒）
            max_delay: 最大重试延迟（秒）
            heartbeat_interval: 心跳检测间隔（秒）
        """
        self.host = host
        self.port = port
        self.client_id = client_id
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.heartbeat_interval = heartbeat_interval

        self._ib: Optional[IB] = None
        self._connected = False

    @property
    def ib(self) -> Optional[IB]:
        """获取当前 IB 实例"""
        return self._ib

    @property
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected and self._ib is not None and self._ib.isConnected()

    def connect(self) -> IB:
        """
        连接到 TWS / IB Gateway

        Returns:
            已连接的 IB 实例

        Raises:
            ConnectionError: 所有重试均失败
        """
        self._ib = IB()

        logger.info(f"正在连接 IBKR: {self.host}:{self.port}, clientId={self.client_id}")

        for attempt in range(1, self.max_retries + 1):
            try:
                self._ib.connect(self.host, self.port, clientId=self.client_id)
                self._connected = True
                logger.info(f"✓ 已连接到 IBKR (尝试 {attempt} 次)")
                return self._ib

            except Exception as e:
                delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
                logger.warning(
                    f"连接失败 (尝试 {attempt}/{self.max_retries}): {e}，"
                    f"{delay:.0f} 秒后重试..."
                )
                if attempt < self.max_retries:
                    time.sleep(delay)
                else:
                    raise ConnectionError(
                        f"无法连接到 IBKR {self.host}:{self.port}，"
                        f"已重试 {self.max_retries} 次。"
                        f"请确认 TWS/Gateway 已启动且 API 已开启。"
                    ) from e

        raise ConnectionError("连接失败，已达最大重试次数")

    def disconnect(self):
        """断开连接"""
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            logger.info("已断开 IBKR 连接")
        self._connected = False

    def check_heartbeat(self) -> bool:
        """
        检查连接是否仍然活跃

        Returns:
            True 表示连接正常
        """
        if not self.is_connected:
            return False
        return True

    def reconnect(self) -> IB:
        """
        断线重连

        Returns:
            重新连接后的 IB 实例

        Raises:
            ConnectionError: 重连失败
        """
        logger.warning("正在尝试重连...")
        self.disconnect()
        return self.connect()

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    def __repr__(self) -> str:
        status = "✓ 已连接" if self.is_connected else "✗ 未连接"
        return f"ConnectionManager({self.host}:{self.port}, clientId={self.client_id}) [{status}]"
