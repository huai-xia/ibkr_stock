"""
Server酱 (ServerChan) 通知模块
推送到个人微信，微信扫码即用，无需安装额外APP

使用方法:
    1. 访问 https://sct.ftqq.com/ 微信扫码登录
    2. 获取 SendKey
    3. notifier = ServerChanNotifier(send_key="SCTxxxxx")
       notifier.send("标题", "内容")
"""

import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class ServerChanNotifier:
    """
    Server酱通知器 — 推送到个人微信

    使用方法:
        sc = ServerChanNotifier(send_key="SCT123456")
        sc.send("交易提醒", "AAPL 买入 100 股已成交")
    """

    BASE_URL = "https://sctapi.ftqq.com"

    def __init__(self, send_key: str = "", enabled: bool = True):
        self._send_key = send_key
        self._enabled = enabled

    @property
    def is_configured(self) -> bool:
        return bool(self._send_key) and "YOUR_KEY" not in self._send_key

    def send(self, title: str, content: str = "") -> bool:
        """
        发送消息到个人微信

        Args:
            title: 消息标题（显示在微信推送中）
            content: 消息正文（支持 Markdown）
        """
        if not self._enabled:
            return False
        if not self.is_configured:
            logger.debug("Server酱未配置，跳过发送")
            return False

        try:
            url = f"{self.BASE_URL}/{self._send_key}.send"
            resp = requests.post(url, data={
                "title": title,
                "desp": content,
            }, timeout=10)

            result = resp.json()
            if result.get("code") == 0:
                logger.debug("Server酱推送成功: %s", title)
                return True
            else:
                logger.error("Server酱推送失败: %s", result.get("info", "unknown"))
                return False
        except Exception as e:
            logger.error("Server酱推送异常: %s", e)
            return False

    # ------------------------------------------------------------------
    # 交易通知
    # ------------------------------------------------------------------

    def trade_filled(
        self, symbol: str, action: str, quantity: float,
        price: float, pnl: Optional[float] = None,
    ) -> bool:
        """订单成交通知"""
        emoji = "🟢" if action.upper() == "BUY" else "🔴"
        sign = "+" if pnl and pnl > 0 else ""

        lines = [
            f"**{symbol}** {action.upper()} {quantity:.0f}股",
            f"价格: ${price:.2f}",
        ]
        if pnl is not None:
            lines.append(f"盈亏: {sign}${pnl:.2f}")
        lines.append(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

        return self.send(
            f"{emoji} 订单成交: {symbol} {action.upper()}",
            "\n\n".join(lines),
        )

    def pdt_warning(self, count: int, max_trades: int = 3) -> bool:
        """PDT 预警"""
        remaining = max_trades - count
        if count >= max_trades:
            return self.send(
                "🚫 PDT 已达上限",
                f"5日内日内交易 {count}/{max_trades}\n后续日内交易已被系统阻止",
            )
        else:
            return self.send(
                f"⚠️ PDT 预警: {count}/{max_trades}",
                f"剩余 {remaining} 次额度，请谨慎操作",
            )

    def risk_alert(self, title: str, detail: str = "") -> bool:
        """风控告警"""
        return self.send(f"🔴 {title}", detail)

    def daily_summary(
        self, net_liq: float, day_pnl: float, pdt_count: int, pdt_max: int,
    ) -> bool:
        """每日摘要"""
        sign = "+" if day_pnl >= 0 else ""
        content = (
            f"净清算值: ${net_liq:,.2f}\n"
            f"当日盈亏: {sign}${day_pnl:.2f}\n"
            f"PDT 计数: {pdt_count}/{pdt_max}"
        )
        return self.send("📊 每日交易摘要", content)
