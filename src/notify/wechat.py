"""
企业微信机器人通知模块
支持 Markdown 消息，三级推送（紧急/重要/日常）
"""

import json
import logging
from datetime import datetime
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class PushLevel(str, Enum):
    """推送级别"""
    URGENT = "urgent"       # 🔴 紧急：PDT预警、熔断、断线
    IMPORTANT = "important" # 🟡 重要：成交确认、大额盈亏
    DAILY = "daily"         # 🟢 日常：开盘/收盘摘要


class WeChatNotifier:
    """
    企业微信机器人通知

    使用方法:
        notifier = WeChatNotifier(webhook_url)
        notifier.send("这是一条测试消息")
        notifier.send_markdown("## 标题\n内容")
        notifier.trade_alert(symbol="AAPL", action="BUY", quantity=100, price=180.0)
        notifier.pdt_warning(count=2, max_trades=3)
    """

    def __init__(
        self,
        webhook_url: str,
        enabled: bool = True,
        push_levels: Optional[dict] = None,
    ):
        """
        Args:
            webhook_url: 企业微信机器人 Webhook URL
            enabled: 是否启用推送
            push_levels: 推送级别开关 {"urgent": True, "important": True, "daily": True}
        """
        self._webhook_url = webhook_url
        self._enabled = enabled

        self._push_levels = push_levels or {
            "urgent": True,
            "important": True,
            "daily": True,
        }

    @property
    def is_configured(self) -> bool:
        """检查 Webhook URL 是否已配置"""
        return bool(self._webhook_url) and "YOUR_KEY_HERE" not in self._webhook_url

    # ------------------------------------------------------------------
    # 基础发送
    # ------------------------------------------------------------------

    def send(self, text: str, level: PushLevel = PushLevel.DAILY) -> bool:
        """发送纯文本消息"""
        if not self._can_send(level):
            return False

        return self._post({"msgtype": "text", "text": {"content": text}})

    def send_markdown(self, content: str, level: PushLevel = PushLevel.DAILY) -> bool:
        """发送 Markdown 格式消息"""
        if not self._can_send(level):
            return False

        return self._post({"msgtype": "markdown", "markdown": {"content": content}})

    def _can_send(self, level: PushLevel) -> bool:
        """检查是否可以发送该级别的消息"""
        if not self._enabled:
            return False
        if not self.is_configured:
            logger.debug("企业微信 Webhook 未配置，跳过推送")
            return False
        if not self._push_levels.get(level.value, True):
            return False
        return True

    def _post(self, payload: dict) -> bool:
        """发送 HTTP POST 请求"""
        try:
            resp = requests.post(
                self._webhook_url,
                json=payload,
                timeout=10,
            )
            result = resp.json()
            if result.get("errcode") == 0:
                logger.debug("企业微信推送成功")
                return True
            else:
                logger.error("企业微信推送失败: %s", result)
                return False
        except Exception as e:
            logger.error("企业微信推送异常: %s", e)
            return False

    # ------------------------------------------------------------------
    # 交易通知
    # ------------------------------------------------------------------

    def trade_filled(
        self,
        symbol: str,
        action: str,
        quantity: float,
        price: float,
        pnl: Optional[float] = None,
    ) -> bool:
        """订单成交通知"""
        emoji = "🟢" if action.upper() == "BUY" else "🔴"
        sign = "+" if pnl and pnl > 0 else ""

        lines = [
            f"{emoji} **订单成交**",
            f"> 股票: {symbol}",
            f"> 方向: {action.upper()}",
            f"> 数量: {quantity:.0f} 股",
            f"> 价格: ${price:.2f}",
        ]
        if pnl is not None:
            lines.append(f"> 盈亏: {sign}${pnl:.2f}")
        lines.append(f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        return self.send_markdown("\n".join(lines), PushLevel.IMPORTANT)

    def trade_submitted(
        self,
        symbol: str,
        action: str,
        quantity: float,
        order_type: str,
        price: Optional[float] = None,
    ) -> bool:
        """订单提交通知"""
        price_str = f"${price:.2f}" if price else "市价"
        content = (
            f"📤 **订单已提交**\n"
            f"> {symbol} {action.upper()} {quantity:.0f}股 @ {price_str}\n"
            f"> 类型: {order_type}\n"
            f"> 时间: {datetime.now().strftime('%H:%M:%S')}"
        )
        return self.send_markdown(content, PushLevel.IMPORTANT)

    # ------------------------------------------------------------------
    # 风控告警
    # ------------------------------------------------------------------

    def pdt_warning(self, count: int, max_trades: int = 3) -> bool:
        """PDT 日内交易预警"""
        remaining = max_trades - count

        if count >= max_trades:
            content = (
                f"🚫 **PDT 限制已达上限！**\n"
                f"> 5日内日内交易: {count}/{max_trades}\n"
                f"> **后续日内交易已被系统阻止**\n"
                f"> 如需紧急平仓，请使用紧急通道"
            )
        else:
            content = (
                f"⚠️ **PDT 日内交易预警**\n"
                f"> 5日内日内交易: {count}/{max_trades}\n"
                f"> 剩余额度: **{remaining}** 次\n"
                f"> 请谨慎操作，避免触发限制！"
            )

        return self.send_markdown(content, PushLevel.URGENT)

    def risk_alert(self, title: str, detail: str) -> bool:
        """通用风控告警"""
        content = (
            f"🔴 **{title}**\n"
            f"> {detail}\n"
            f"> 时间: {datetime.now().strftime('%H:%M:%S')}"
        )
        return self.send_markdown(content, PushLevel.URGENT)

    def connection_lost(self) -> bool:
        """连接断开告警"""
        content = (
            f"❌ **IBKR 连接断开！**\n"
            f"> 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"> 系统将尝试自动重连..."
        )
        return self.send_markdown(content, PushLevel.URGENT)

    def connection_restored(self) -> bool:
        """连接恢复通知"""
        content = (
            f"✅ **IBKR 连接已恢复**\n"
            f"> 时间: {datetime.now().strftime('%H:%M:%S')}"
        )
        return self.send_markdown(content, PushLevel.IMPORTANT)

    # ------------------------------------------------------------------
    # 定时摘要
    # ------------------------------------------------------------------

    def daily_open_summary(
        self,
        net_liq: float,
        positions: list[dict],
        pdt_status: dict,
    ) -> bool:
        """开盘前摘要"""
        pos_lines = ""
        for p in positions:
            if p.get("position", 0) != 0:
                pos_lines += f"> {p['symbol']}: {p['position']:.0f}股 @ ${p['avg_cost']:.2f}\n"

        pdt = f"{pdt_status['count']}/{pdt_status['max']}"

        content = (
            f"📊 **每日开盘摘要** — {datetime.now().strftime('%Y-%m-%d')}\n"
            f"> 净清算值: **${net_liq:,.2f}**\n"
            f"> PDT 计数: {pdt}\n"
            f"> 当前持仓:\n"
            f"{pos_lines}"
        )
        return self.send_markdown(content, PushLevel.DAILY)

    def daily_close_summary(
        self,
        net_liq: float,
        day_pnl: float,
        pdt_status: dict,
        open_pnl: Optional[float] = None,
    ) -> bool:
        """收盘摘要"""
        pnl_emoji = "🟢" if day_pnl >= 0 else "🔴"
        pnl_sign = "+" if day_pnl >= 0 else ""

        lines = [
            f"📊 **每日收盘摘要** — {datetime.now().strftime('%Y-%m-%d')}",
            f"> 净清算值: **${net_liq:,.2f}**",
            f"> 当日盈亏: {pnl_emoji} {pnl_sign}${day_pnl:.2f}",
            f"> PDT 计数: {pdt_status['count']}/{pdt_status['max']}",
        ]

        if open_pnl is not None:
            lines.append(f"> 未实现盈亏: ${open_pnl:,.2f}")

        return self.send_markdown("\n".join(lines), PushLevel.DAILY)
