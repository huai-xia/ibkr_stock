"""
下单模块
市价单 / 限价单 / 止损单 / 止损限价单

⚠️ 安全机制:
    - 默认以只读模式运行，拒绝任何写操作
    - 需显式调用 enable_trading() 才能下单
    - 所有下单前必须通过风控检查
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ib_insync import IB, Stock, LimitOrder, MarketOrder, StopOrder, StopLimitOrder, Trade

from src.trade.risk import RiskManager, RiskResult
from src.trade.recorder import TradeRecorder

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# 类型定义
# ------------------------------------------------------------------

class OrderAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


@dataclass
class OrderRequest:
    """下单请求"""
    symbol: str
    action: OrderAction
    quantity: float
    order_type: OrderType = OrderType.LIMIT
    price: Optional[float] = None      # 限价单价格 / 止损限价单的限价
    stop_price: Optional[float] = None # 止损价
    account: str = ""


@dataclass
class OrderResult:
    """下单结果"""
    success: bool
    trade: Optional[Trade] = None
    order_id: Optional[int] = None
    message: str = ""
    risk_result: Optional[RiskResult] = None


# ------------------------------------------------------------------
# 订单执行器
# ------------------------------------------------------------------

class OrderExecutor:
    """
    订单执行器（带安全锁）

    使用方法:
        executor = OrderExecutor(ib, risk_manager, recorder)
        executor.enable_trading()  # 显式开启交易

        req = OrderRequest("AAPL", OrderAction.BUY, 100,
                          OrderType.LIMIT, price=180.0)
        result = executor.place(req)
    """

    def __init__(
        self,
        ib: IB,
        risk_manager: Optional[RiskManager] = None,
        recorder: Optional[TradeRecorder] = None,
        readonly: bool = True,
    ):
        """
        Args:
            ib: IB 连接实例
            risk_manager: 风控管理器
            recorder: 交易记录器
            readonly: 只读模式（默认 True，拒绝下单）
        """
        self._ib = ib
        self._risk = risk_manager
        self._recorder = recorder
        self._readonly = readonly

    @property
    def is_readonly(self) -> bool:
        return self._readonly

    def enable_trading(self):
        """🔓 开启交易模式（慎用！）"""
        self._readonly = False
        logger.warning("⚠️ 交易模式已开启！订单将发送到 IBKR！")

    def disable_trading(self):
        """🔒 关闭交易模式，回到只读"""
        self._readonly = True
        logger.info("🔒 已回到只读模式")

    # ------------------------------------------------------------------
    # 下单
    # ------------------------------------------------------------------

    def place(self, request: OrderRequest) -> OrderResult:
        """
        下单入口

        流程: 只读检查 → 风控检查 → 创建合约 → 发送订单 → 记录
        """
        symbol = request.symbol.upper()
        action = request.action.value
        order_type = request.order_type

        # --- 1. 只读检查 ---
        if self._readonly:
            return OrderResult(
                success=False,
                message=f"[只读模式] 拒绝下单: {action} {request.quantity} {symbol} — "
                        f"请先调用 enable_trading()",
            )

        # --- 2. 风控检查 ---
        if self._risk:
            risk = self._risk.check_before_order(
                symbol=symbol,
                action=request.action,
                quantity=request.quantity,
                price=request.price,
            )
            if not risk.allowed:
                return OrderResult(
                    success=False,
                    message=f"[风控拦截] {risk.reason}",
                    risk_result=risk,
                )

        # --- 3. 创建合约 ---
        try:
            contract = Stock(symbol, "SMART", "USD")
            qualified = self._ib.qualifyContracts(contract)
            if not qualified:
                return OrderResult(success=False, message=f"无法 qualify 合约: {symbol}")
            contract = qualified[0]
        except Exception as e:
            return OrderResult(success=False, message=f"创建合约失败: {e}")

        # --- 4. 构建订单 ---
        order = self._build_order(action, request.quantity, order_type,
                                  request.price, request.stop_price, request.account)
        if not order:
            return OrderResult(success=False, message=f"无效订单类型: {order_type}")

        # --- 5. 发送 ---
        try:
            trade = self._ib.placeOrder(contract, order)
            logger.info(
                "📤 下单: %s %s %s %.0f股 @ $%s [%s]",
                symbol, action, order_type.value,
                request.quantity,
                f"{request.price:.2f}" if request.price else "市价",
                trade.orderStatus.status,
            )
        except Exception as e:
            logger.error("下单异常: %s", e)
            return OrderResult(success=False, message=f"下单失败: {e}")

        # --- 6. 记录到数据库 ---
        if self._recorder:
            self._recorder.record(
                symbol=symbol,
                action=action,
                quantity=request.quantity,
                account=request.account,
                price=request.price,
                order_type=order_type.value,
                status="PENDING",
                note=f"OrderId: {trade.order.orderId}",
            )

        return OrderResult(
            success=True,
            trade=trade,
            order_id=trade.order.orderId,
            message=f"订单已提交: {action} {request.quantity} {symbol}",
            risk_result=risk if self._risk else None,
        )

    def cancel(self, order_id: int) -> OrderResult:
        """取消订单"""
        if self._readonly:
            return OrderResult(success=False, message="[只读模式] 拒绝取消订单")
        try:
            trade = self._ib.cancelOrder(order_id)
            return OrderResult(success=True, trade=trade, message=f"订单 #{order_id} 已取消")
        except Exception as e:
            return OrderResult(success=False, message=f"取消失败: {e}")

    def _build_order(
        self,
        action: str,
        quantity: float,
        order_type: OrderType,
        price: Optional[float],
        stop_price: Optional[float],
        account: str,
    ):
        """构建 ib_insync 订单对象"""
        if order_type == OrderType.MARKET:
            order = MarketOrder(action, quantity, account=account or "")
        elif order_type == OrderType.LIMIT:
            if price is None:
                return None
            order = LimitOrder(action, quantity, price, account=account or "")
        elif order_type == OrderType.STOP:
            if stop_price is None:
                return None
            order = StopOrder(action, quantity, stop_price, account=account or "")
        elif order_type == OrderType.STOP_LIMIT:
            if stop_price is None or price is None:
                return None
            order = StopLimitOrder(action, quantity, price, stop_price, account=account or "")
        else:
            return None
        return order

    # ------------------------------------------------------------------
    # 只读查询（无需开启交易模式）
    # ------------------------------------------------------------------

    def get_open_orders(self) -> list:
        """获取所有未成交订单"""
        trades = self._ib.reqOpenOrders()
        return [
            {
                "order_id": t.order.orderId,
                "symbol": t.contract.symbol,
                "action": t.order.action,
                "quantity": t.order.totalQuantity,
                "type": t.order.orderType,
                "price": getattr(t.order, 'lmtPrice', 0),
                "status": t.orderStatus.status,
            }
            for t in trades
        ]

    def get_trade_history(self) -> list:
        """获取近期成交记录"""
        trades = self._ib.reqExecutions()
        return [
            {
                "exec_id": t.execution.execId,
                "symbol": t.contract.symbol,
                "action": t.execution.side,
                "quantity": t.execution.shares,
                "price": t.execution.price,
                "time": str(t.execution.time),
            }
            for t in trades
        ]
