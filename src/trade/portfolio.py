"""
持仓与账户查询模块
"""

import logging
from typing import Optional

from ib_insync import IB, util

logger = logging.getLogger(__name__)


class Portfolio:
    """
    持仓与账户查询

    使用方法:
        pf = Portfolio(ib)
        positions = pf.get_positions()
        account = pf.get_account_summary()
    """

    def __init__(self, ib: IB):
        self._ib = ib

    def get_positions(self) -> list[dict]:
        """
        获取当前持仓列表

        Returns:
            [{
                "symbol": str,
                "conid": int,
                "position": float,      # 持仓数量
                "avg_cost": float,      # 平均成本
                "market_price": float,  # 当前市价（慢，需要额外请求）
                "market_value": float,  # 市值
                "unrealized_pnl": float,# 未实现盈亏
                "unrealized_pnl_pct": float,  # 未实现盈亏百分比
                "account": str,
                "currency": str,
            }]
        """
        positions = self._ib.positions()
        result = []

        for p in positions:
            pos = {
                "symbol": p.contract.symbol,
                "conid": p.contract.conId,
                "position": p.position,
                "avg_cost": p.avgCost,
                "market_price": 0.0,
                "market_value": 0.0,
                "unrealized_pnl": 0.0,
                "unrealized_pnl_pct": 0.0,
                "account": p.account,
                "currency": p.contract.currency,
            }

            # 计算市值和盈亏
            if p.position != 0 and p.avgCost and p.avgCost > 0:
                if p.position > 0:
                    pos["market_value"] = p.position * p.avgCost  # 近似值
                    # 实际需用市价，这里占位
                    pos["unrealized_pnl"] = getattr(p, 'unrealizedPNL', 0.0) if hasattr(p, 'unrealizedPNL') else 0.0
                else:
                    pos["market_value"] = abs(p.position) * p.avgCost

            result.append(pos)

        return result

    def get_account_summary(self, account: str = "") -> dict:
        """
        获取账户摘要

        Args:
            account: 账户号（空=所有账户）

        Returns:
            {
                "net_liquidation": float,  # 净清算值
                "available_funds": float,  # 可用资金
                "gross_position": float,   # 持仓市值
                "cash": float,             # 现金
                "buying_power": float,     # 购买力
                "accounts": list[str],     # 账户列表
            }
        """
        accounts = self._ib.managedAccounts()
        result = {
            "accounts": accounts,
            "net_liquidation": 0.0,
            "available_funds": 0.0,
            "gross_position": 0.0,
            "cash": 0.0,
            "buying_power": 0.0,
        }

        try:
            summary = self._ib.accountSummary(account=account or "All")

            for s in summary:
                if s.tag == "NetLiquidation" and s.currency == "USD":
                    result["net_liquidation"] += float(s.value)
                elif s.tag == "AvailableFunds" and s.currency == "USD":
                    result["available_funds"] += float(s.value)
                elif s.tag == "TotalCashValue" and s.currency == "USD":
                    result["cash"] += float(s.value)
                elif s.tag == "GrossPositionValue" and s.currency == "USD":
                    result["gross_position"] += float(s.value)
                elif s.tag == "BuyingPower" and s.currency == "USD":
                    result["buying_power"] += float(s.value)
        except Exception as e:
            logger.warning("获取账户摘要失败: %s", e)

        return result

    def get_account_values(self, account: str) -> dict:
        """
        获取单个账户的详细数值

        Returns:
            dict: key="tag_currency", value=float
        """
        try:
            values = self._ib.accountValues(account)
            return {
                f"{v.tag}_{v.currency}": float(v.value)
                for v in values
                if v.value and v.currency == "USD"
            }
        except Exception as e:
            logger.warning("获取账户数值失败: %s", e)
            return {}

    def format_positions(self) -> str:
        """格式化持仓列表为可打印字符串"""
        positions = self.get_positions()

        if not positions:
            return "当前无持仓"

        lines = ["┌──────────────────────────────────────────────────────────────────┐"]
        lines.append("│  Symbol    | 持仓量    | 成本价     | 市值        | 盈亏(%)      │")
        lines.append("├──────────────────────────────────────────────────────────────────┤")

        for p in positions:
            if p["position"] == 0:
                continue
            pnl_str = ""
            if p["unrealized_pnl_pct"] != 0:
                sign = "+" if p["unrealized_pnl_pct"] > 0 else ""
                pnl_str = f"{sign}{p['unrealized_pnl_pct']:.2f}%"

            lines.append(
                f"│ {p['symbol']:<10s} | {p['position']:>8.0f} | "
                f"${p['avg_cost']:>8.2f} | ${p['market_value']:>10,.2f} | "
                f"{pnl_str:>12s} │"
            )

        lines.append("└──────────────────────────────────────────────────────────────────┘")
        return "\n".join(lines)

    def format_account(self, account: str = "") -> str:
        """格式化账户摘要为可打印字符串"""
        s = self.get_account_summary(account)

        lines = [
            "╔══════════════════════════════════╗",
            "║         账户摘要                  ║",
            "╠══════════════════════════════════╣",
            f"║ 净清算值:   ${s['net_liquidation']:>12,.2f}    ║",
            f"║ 可用资金:   ${s['available_funds']:>12,.2f}    ║",
            f"║ 持仓市值:   ${s['gross_position']:>12,.2f}    ║",
            f"║ 现金余额:   ${s['cash']:>12,.2f}    ║",
            "╚══════════════════════════════════╝",
        ]
        return "\n".join(lines)
