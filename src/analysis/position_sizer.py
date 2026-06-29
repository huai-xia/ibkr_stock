"""
仓位计算器 — 方向判断 + 数量推荐 + 风控约束

使用方法:
    sizer = PositionSizer()
    direction, reason = sizer.recommend_direction(signals, trend, rsi)
    qty = sizer.recommend_fixed_risk(net_liq, entry, stop, risk_pct=2.0)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ===================================================================
# 数据类
# ===================================================================

@dataclass
class PositionPlan:
    """仓位方案"""
    direction: str = "BUY"              # BUY / SELL / NEUTRAL
    direction_reason: str = ""          # 方向判断依据
    suggested_quantity: int = 0         # 建议股数
    quantity_method: str = "fixed_risk" # fixed_risk / kelly / vol_target / equal_weight / manual
    risk_per_trade_pct: float = 2.0     # 单笔风险占比 (%)
    max_loss_amount: float = 0.0        # 最大亏损金额
    position_pct: float = 0.0           # 仓位占比 (%)


# ===================================================================
# 仓位计算器
# ===================================================================

class PositionSizer:
    """
    仓位计算器

    v1: 方向判断(规则) + 固定风险数量
    预留: Kelly / 波动率目标 / 等权重
    """

    def __init__(self, default_risk_pct: float = 2.0, max_position_pct: float = 20.0):
        self._risk_pct = default_risk_pct
        self._max_pct = max_position_pct

    # ------------------------------------------------------------------
    # 方向判断
    # ------------------------------------------------------------------

    def recommend_direction(
        self,
        buy_signals: list,
        trend: str,
        rsi: float,
        vol_pct: float = 0,
    ) -> tuple[str, str]:
        """
        根据买入/卖出信号 + 趋势 + RSI 综合判断方向

        Returns:
            (direction, reason): BUY / SELL(做空) / NEUTRAL(观望)
        """
        # 统计信号
        buy_count = len([s for s in buy_signals if s.signal_type == "buy"])
        sell_count = len([s for s in buy_signals if s.signal_type in ("sell", "alert")])

        buy_strong = sum(1 for s in buy_signals
                         if s.signal_type == "buy" and s.strength == "strong")
        sell_strong = sum(1 for s in buy_signals
                          if s.signal_type in ("sell", "alert") and s.strength == "strong")

        reasons = []

        # 基础分: 买入倾向 vs 卖出倾向
        buy_score = buy_count * 1 + buy_strong * 2
        sell_score = sell_count * 1 + sell_strong * 2

        # 趋势修正
        if trend == "up":
            buy_score += 3
            reasons.append("趋势向上")
        elif trend == "down":
            sell_score += 3
            reasons.append("趋势向下")
        else:
            reasons.append("趋势震荡")

        # RSI 修正
        if rsi < 30:
            buy_score += 2
            reasons.append(f"RSI {rsi:.0f} 超卖")
        elif rsi > 70:
            sell_score += 2
            reasons.append(f"RSI {rsi:.0f} 超买")
        else:
            reasons.append(f"RSI {rsi:.0f} 中性")

        # 波动率极端时倾向观望
        if vol_pct > 80:
            reasons.append(f"波动率 {vol_pct:.0f}% 极高，建议观望")
            return ("NEUTRAL", "；".join(reasons))

        # 判决
        margin = buy_score - sell_score
        if margin >= 3:
            direction = "BUY"
            reasons.append(f"买入信号占优 (得分 {buy_score} vs {sell_score})")
        elif margin <= -3:
            direction = "SELL"
            reasons.append(f"卖出信号占优 (得分 {buy_score} vs {sell_score})")
        else:
            direction = "NEUTRAL"
            reasons.append(f"信号不明确 (买入 {buy_score} vs 卖出 {sell_score})，建议观望")

        return (direction, "；".join(reasons))

    # ------------------------------------------------------------------
    # 数量推荐 (v1: 固定风险)
    # ------------------------------------------------------------------

    def recommend_fixed_risk(
        self,
        net_liq: float,
        entry_price: float,
        stop_price: float,
        risk_pct: float = 0,
        max_quantity: int = 0,
    ) -> PositionPlan:
        """
        固定风险分数法: 每笔交易最多亏净值的 risk_pct%

        quantity = floor(net_liq × risk_pct% / |entry - stop|)
        同时约束: 仓位不超过 max_position_pct%

        Args:
            net_liq: 账户净清算值
            entry_price: 入场价
            stop_price: 止损价
            risk_pct: 单笔风险占比 (0=用默认值)
            max_quantity: 用户手动指定数量上限 (0=不限)

        Returns:
            PositionPlan
        """
        rpct = risk_pct if risk_pct > 0 else self._risk_pct
        plan = PositionPlan(
            direction="BUY",
            risk_per_trade_pct=rpct,
            quantity_method="fixed_risk",
        )

        if net_liq <= 0 or entry_price <= 0:
            plan.suggested_quantity = 0
            plan.direction_reason = "账户信息不足"
            return plan

        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            plan.suggested_quantity = 0
            plan.direction_reason = "止损价与入场价相同，无法计算风险"
            return plan

        # 基于风险的股数
        max_loss = net_liq * (rpct / 100.0)
        qty = int(max_loss / risk_per_share)

        # 仓位上限约束
        max_by_position = int(net_liq * (self._max_pct / 100.0) / entry_price)
        qty = min(qty, max_by_position)
        if qty < 1:
            qty = 1  # 至少 1 股

        # 用户上限
        if max_quantity > 0:
            qty = min(qty, max_quantity)

        plan.suggested_quantity = qty
        plan.max_loss_amount = round(risk_per_share * qty, 2)
        plan.position_pct = round(qty * entry_price / net_liq * 100, 2)

        return plan

    # ------------------------------------------------------------------
    # 预留: Kelly 公式
    # ------------------------------------------------------------------

    def recommend_kelly(
        self,
        net_liq: float,
        entry_price: float,
        stop_price: float,
        win_rate: float,
        avg_win_pct: float,
        avg_loss_pct: float,
        kelly_fraction: float = 0.5,  # 半 Kelly 更稳健
    ) -> Optional[PositionPlan]:
        """
        Kelly 准则仓位 (预留，需足够历史交易数据)

        Kelly% = (W * avg_win - (1-W) * avg_loss) / (avg_win * avg_loss)
        数量 = net_liq × Kelly% × kelly_fraction / entry_price
        """
        if win_rate <= 0 or avg_win_pct <= 0 or avg_loss_pct <= 0:
            return None

        kelly_pct = (win_rate * avg_win_pct - (1 - win_rate) * avg_loss_pct) / (avg_win_pct * avg_loss_pct)
        kelly_pct = max(0, kelly_pct) * kelly_fraction
        kelly_pct = min(kelly_pct, self._max_pct / 100.0)

        if kelly_pct <= 0:
            return None

        plan = PositionPlan(
            direction="BUY",
            quantity_method="kelly",
        )
        plan.suggested_quantity = int(net_liq * kelly_pct / entry_price)
        plan.position_pct = round(kelly_pct * 100, 2)
        if plan.suggested_quantity < 1:
            plan.suggested_quantity = 1

        return plan

    # ------------------------------------------------------------------
    # 预留: 波动率目标
    # ------------------------------------------------------------------

    def recommend_vol_target(
        self,
        net_liq: float,
        price: float,
        annual_vol_pct: float,
        target_vol_pct: float = 1.0,
    ) -> PositionPlan:
        """
        波动率目标: 让每笔交易的日波动贡献相等

        数量 = net_liq × target_vol% / (price × annual_vol%)
        """
        if price <= 0 or annual_vol_pct <= 0:
            return PositionPlan(quantity_method="vol_target", suggested_quantity=0)

        target_notional = net_liq * (target_vol_pct / 100.0) / (annual_vol_pct / 100.0)
        qty = int(target_notional / price)

        plan = PositionPlan(
            direction="BUY",
            quantity_method="vol_target",
        )
        plan.suggested_quantity = max(1, qty)
        plan.position_pct = round(plan.suggested_quantity * price / net_liq * 100, 2)

        return plan

    # ------------------------------------------------------------------
    # 预留: 等权重
    # ------------------------------------------------------------------

    def recommend_equal_weight(
        self,
        net_liq: float,
        price: float,
        n_positions: int,
    ) -> PositionPlan:
        """
        等权重: 每只持仓分配相同资金

        数量 = net_liq / n_positions / price
        """
        if n_positions <= 0 or price <= 0:
            return PositionPlan(quantity_method="equal_weight", suggested_quantity=0)

        qty = int(net_liq / n_positions / price)

        plan = PositionPlan(
            direction="BUY",
            quantity_method="equal_weight",
        )
        plan.suggested_quantity = max(1, qty)
        plan.position_pct = round(100.0 / n_positions, 2)

        return plan
