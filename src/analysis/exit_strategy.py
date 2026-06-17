"""
退出策略引擎
基于历史波动率 + 技术位 + 你的交易习惯，为每只持仓股票定制止损/止盈方案

核心思路:
    - 止损: 基于 ATR(波动率) + 关键技术支撑位，不设太近(避免被噪音震出)
    - 止盈: 基于布林带上轨 + 阻力位 + 盈亏比要求(≥2:1)
    - 时间止损: 避免持仓过久
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ExitPlan:
    """单只股票的退出计划"""
    symbol: str
    current_price: float = 0.0
    entry_price: float = 0.0

    # 止损方案（多级）
    tight_stop: float = 0.0     # 紧止损（短线）
    normal_stop: float = 0.0    # 标准止损（推荐）
    wide_stop: float = 0.0      # 宽止损（长线）

    # 止盈方案
    target_1: float = 0.0       # 第一目标（保守）
    target_2: float = 0.0       # 第二目标（标准）
    target_3: float = 0.0       # 第三目标（乐观）

    # 风控数据
    atr: float = 0.0            # 当前ATR
    volatility_pct: float = 0.0 # 年化波动率
    support_level: float = 0.0  # 最近支撑位
    resistance_level: float = 0.0  # 最近阻力位
    rsi: float = 0.0            # 当前RSI
    trend: str = "neutral"      # up / down / neutral

    # 建议
    recommendation: str = ""
    risk_reward_ratio: float = 0.0
    suggested_position_pct: float = 0.0  # 建议仓位占比

    # 计算参数
    _stop_multiplier: float = 1.5
    _profit_multiplier: float = 2.0


class ExitStrategyEngine:
    """
    退出策略引擎

    使用方法:
        engine = ExitStrategyEngine()
        plan = engine.analyze("NVDA", df)   # df是包含技术指标的历史K线
        print(plan)
    """

    def __init__(self):
        pass

    def analyze(
        self,
        symbol: str,
        df: pd.DataFrame,
        entry_price: float = 0.0,
        current_price: float = 0.0,
        risk_profile: str = "moderate",  # conservative / moderate / aggressive
    ) -> Optional[ExitPlan]:
        """
        分析单只股票，生成退出计划

        Args:
            symbol: 股票代码
            df: 包含技术指标的历史K线 DataFrame
            entry_price: 入场价格（0=用当前价）
            current_price: 实时价格覆盖（0=用历史收盘价），适合盘前/盘中查询
            risk_profile: 风险偏好

        Returns:
            ExitPlan 或 None
        """
        if df is None or df.empty or len(df) < 20:
            logger.warning("%s 数据不足", symbol)
            return None

        current = df.iloc[-1]
        live_price = current_price or entry_price or current["close"]

        plan = ExitPlan(symbol=symbol)
        plan.current_price = round(live_price, 2)
        plan.entry_price = round(entry_price, 2) if entry_price else plan.current_price

        # 1. ATR（平均真实波幅）→ 止损基础
        atr_col = "atr_14"
        plan.atr = round(float(current.get(atr_col, df["close"].diff().abs().rolling(14).mean().iloc[-1])), 2)
        if plan.atr <= 0:
            plan.atr = round(current_price * 0.03, 2)

        # 2. 波动率
        returns = df["close"].pct_change().dropna()
        plan.volatility_pct = round(float(returns.std() * np.sqrt(252) * 100), 1)

        # 3. 技术位
        plan.support_level, plan.resistance_level = self._find_levels(df)

        # 4. RSI
        plan.rsi = round(float(current.get("rsi_14", 50)), 1)

        # 5. 趋势判断
        plan.trend = self._judge_trend(df)

        # 6. 止损计算
        plan = self._calc_stops(plan, risk_profile)

        # 7. 止盈计算
        plan = self._calc_targets(plan, risk_profile)

        # 8. 风险收益比
        if plan.normal_stop > 0 and plan.target_2 > 0 and plan.current_price > 0:
            risk = plan.current_price - plan.normal_stop
            reward = plan.target_2 - plan.current_price
            plan.risk_reward_ratio = round(reward / risk, 1) if risk > 0 else 0

        # 9. 仓位建议
        plan.suggested_position_pct = self._suggest_position(plan.volatility_pct, risk_profile)

        # 10. 文字建议
        plan.recommendation = self._make_recommendation(plan, risk_profile)

        return plan

    # ------------------------------------------------------------------
    # 技术位分析
    # ------------------------------------------------------------------

    def _find_levels(self, df: pd.DataFrame) -> tuple:
        """寻找最近的支撑位和阻力位"""
        close = df["close"]
        current = close.iloc[-1]

        # 方法：找 SMA_20 / 布林下轨 作为支撑
        bb_lower = df["bb_lower"].iloc[-1] if "bb_lower" in df.columns else current * 0.95
        bb_upper = df["bb_upper"].iloc[-1] if "bb_upper" in df.columns else current * 1.05
        sma_20 = df["sma_20"].iloc[-1] if "sma_20" in df.columns else current

        # 找最近的局部低点（过去20天）
        recent_low = close.iloc[-20:].min() if len(close) >= 20 else close.min()
        recent_high = close.iloc[-20:].max() if len(close) >= 20 else close.max()

        # 综合支撑: max(布林下轨, 20日低点, SMA20向下偏移)
        support_candidates = [bb_lower, recent_low, sma_20 * 0.97]
        support = max(s for s in support_candidates if s < current and not np.isnan(s))

        # 综合阻力: min(布林上轨, 20日高点)
        resist_candidates = [bb_upper, recent_high]
        resistance = min(r for r in resist_candidates if r > current and not np.isnan(r))

        return round(support, 2), round(resistance, 2)

    def _judge_trend(self, df: pd.DataFrame) -> str:
        """判断短期趋势"""
        close = df["close"]
        sma_5 = df["sma_5"].iloc[-1] if "sma_5" in df.columns else close.iloc[-5:].mean()
        sma_20 = df["sma_20"].iloc[-1] if "sma_20" in df.columns else close.iloc[-20:].mean()

        if close.iloc[-1] > sma_5 > sma_20:
            return "up"
        elif close.iloc[-1] < sma_5 < sma_20:
            return "down"
        return "neutral"

    # ------------------------------------------------------------------
    # 止损/止盈计算
    # ------------------------------------------------------------------

    def _calc_stops(self, plan: ExitPlan, risk_profile: str) -> ExitPlan:
        """
        三级止损:
            - 紧止损: 1.0x ATR below entry (短线/激进)
            - 标准止损: 1.5x ATR below key support (推荐)
            - 宽止损: 2.0x ATR below major support (长线)
        """
        price = plan.current_price or plan.entry_price
        atr = plan.atr

        # 风险系数
        multipliers = {
            "conservative": (0.8, 1.2, 1.8),
            "moderate": (1.0, 1.5, 2.0),
            "aggressive": (1.2, 1.8, 2.5),
        }
        tight_m, normal_m, wide_m = multipliers.get(risk_profile, (1.0, 1.5, 2.0))

        # 紧止损: 当前价 - N倍ATR，但不低于支撑位
        plan.tight_stop = round(max(price - tight_m * atr, plan.support_level * 0.98), 2)

        # 标准止损: 取支撑位和ATR止损中较高者
        atr_stop = price - normal_m * atr
        plan.normal_stop = round(max(atr_stop, plan.support_level), 2)

        # 宽止损: 更宽松的ATR止损
        plan.wide_stop = round(price - wide_m * atr, 2)

        return plan

    def _calc_targets(self, plan: ExitPlan, risk_profile: str) -> ExitPlan:
        """
        三级止盈（基于盈亏比 ≥ 2:1）:
            - 第一目标: 2:1 盈亏比（保守）
            - 第二目标: 3:1 盈亏比（标准），不超过布林上轨
            - 第三目标: 阻力位或 4:1（乐观）
        """
        price = plan.current_price
        risk = price - plan.normal_stop if plan.normal_stop > 0 else plan.atr * 1.5

        plan.target_1 = round(price + risk * 2, 2)  # 2:1

        # 第二目标不超过阻力位太多
        raw_target_2 = price + risk * 3
        plan.target_2 = round(min(raw_target_2, plan.resistance_level * 1.02) if plan.resistance_level > price else raw_target_2, 2)

        plan.target_3 = round(plan.resistance_level, 2) if plan.resistance_level > price else round(price + risk * 4, 2)

        return plan

    def _suggest_position(self, volatility_pct: float, risk_profile: str) -> float:
        """
        根据波动率建议仓位占比
            低波动(<30%) → 可重仓 20-25%
            中波动(30-60%) → 标准 10-15%
            高波动(>60%) → 轻仓 5-8%
        """
        base = {"conservative": 0.8, "moderate": 1.0, "aggressive": 1.3}
        multiplier = base.get(risk_profile, 1.0)

        if volatility_pct < 30:
            pct = 0.20
        elif volatility_pct < 60:
            pct = 0.12
        else:
            pct = 0.06

        return round(pct * multiplier * 100, 1)

    def _make_recommendation(self, plan: ExitPlan, risk_profile: str) -> str:
        """生成文字建议"""
        parts = []

        # 趋势+RSI 综合
        if plan.trend == "up" and plan.rsi < 70:
            parts.append("趋势向上，RSI健康，可持仓")
        elif plan.trend == "down":
            parts.append("趋势偏弱，建议谨慎，设紧止损")
        elif plan.rsi > 70:
            parts.append("⚠️ RSI超买，短期有回调风险，建议部分止盈")
        elif plan.rsi < 30:
            parts.append("RSI超卖，可能反弹，但不宜加仓")

        # 波动率
        if plan.volatility_pct > 60:
            parts.append(f"高波动({plan.volatility_pct:.0f}%)，仓位控制在{plan.suggested_position_pct:.0f}%以内")

        # 风险收益比
        if plan.risk_reward_ratio >= 2.0:
            parts.append(f"✅ 盈亏比{plan.risk_reward_ratio:.1f}:1，值得交易")
        elif plan.risk_reward_ratio < 1.5:
            parts.append(f"⚠️ 盈亏比偏低({plan.risk_reward_ratio:.1f}:1)，建议等待更好入场点")

        return "；".join(parts) if parts else "按标准策略执行"

    # ------------------------------------------------------------------
    # 格式化输出
    # ------------------------------------------------------------------

    def format_plan(self, plan: ExitPlan) -> str:
        """格式化退出计划为可读文本"""
        if plan is None:
            return "数据不足，无法生成退出计划"

        trend_emoji = {"up": "🟢 上升", "down": "🔴 下降", "neutral": "🟡 震荡"}

        lines = [
            f"## 🎯 {plan.symbol} 退出策略",
            "",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 当前价 | ${plan.current_price:.2f} |",
            f"| ATR(波动) | ${plan.atr:.2f} |",
            f"| 年化波动率 | {plan.volatility_pct:.1f}% |",
            f"| RSI(14) | {plan.rsi:.1f} |",
            f"| 趋势 | {trend_emoji.get(plan.trend, plan.trend)} |",
            f"| 支撑位 | ${plan.support_level:.2f} |",
            f"| 阻力位 | ${plan.resistance_level:.2f} |",
            "",
            "### 🛑 止损方案",
            f"| 类型 | 价格 | 距离 | 跌幅 |",
            f"|------|------|------|------|",
        ]

        for name, price in [("紧止损", plan.tight_stop), ("标准止损 ★", plan.normal_stop), ("宽止损", plan.wide_stop)]:
            dist = plan.current_price - price
            pct = dist / plan.current_price * 100 if plan.current_price > 0 else 0
            star = " ← 推荐" if "★" in name else ""
            lines.append(f"| {name.replace(' ★', '')} | ${price:.2f} | ${dist:.2f} | {pct:.1f}%{star} |")

        lines.extend([
            "",
            "### 🎯 止盈方案",
            f"| 目标 | 价格 | 距离 | 涨幅 | 盈亏比 |",
            f"|------|------|------|------|--------|",
        ])

        for name, price, rr_mult in [("第一目标", plan.target_1, 2), ("第二目标 ★", plan.target_2, 3), ("第三目标", plan.target_3, 4)]:
            dist = price - plan.current_price
            pct = dist / plan.current_price * 100 if plan.current_price > 0 else 0
            risk = plan.current_price - plan.normal_stop
            rr = dist / risk if risk > 0 else 0
            star = " ← 推荐" if "★" in name else ""
            lines.append(f"| {name.replace(' ★', '')} | ${price:.2f} | ${dist:.2f} | {pct:.1f}% | {rr:.1f}:1{star} |")

        lines.extend([
            "",
            "### 📊 风险评估",
            f"- 建议仓位: **{plan.suggested_position_pct:.0f}%** 总资金",
            f"- 盈亏比: **{plan.risk_reward_ratio:.1f}:1**",
            f"- {plan.recommendation}",
        ])

        return "\n".join(lines)

    def format_batch(self, plans: dict[str, ExitPlan]) -> str:
        """批量格式化"""
        sections = []
        for sym, plan in plans.items():
            if plan:
                sections.append(self.format_plan(plan))
                sections.append("\n---\n")
        return "\n".join(sections)
