"""
买卖信号检测器
基于技术指标识别好的入场点和出场点

检测规则:
    🟢 买入信号:
        - RSI 超卖反弹 (RSI < 30)
        - 布林下轨反弹
        - 均线金叉
        - SMA50 支撑回调
        - 放量突破 20日高点
        - VWAP 下方回勾 (短时抄底)
        - Z-Score 极端偏离 (闪电崩盘抄底)
        - RSI 底背离 (价格新低但 RSI 不新低)

    🔴 卖出/警示信号:
        - RSI 超买
        - 布林上轨缩量假突破
        - 短时暴跌告警 (Z-Score < -3)
        - 跌破关键支撑
        - 成交量异常放大 (出货嫌疑)
        - 跳空缺口

    ⚡ 短时异动:
        - 放量 spike (3x 均量)
        - 价格冲击 (1分钟/5分钟 涨跌 > 3%)
        - 布林带挤压 (即将突破)
"""

import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    """交易信号"""
    symbol: str
    signal_type: str        # buy / sell / alert
    reason: str             # 触发原因
    strength: str           # strong / medium / weak
    current_price: float
    suggested_entry: float  # 建议入场价
    suggested_stop: float   # 建议止损
    suggested_target: float # 建议止盈
    risk_reward: float      # 盈亏比
    note: str = ""


class SignalDetector:
    """
    买卖信号检测器

    使用方法:
        detector = SignalDetector()
        signals = detector.scan(df)  # df 需含技术指标
    """

    def scan(self, symbol: str, df: pd.DataFrame) -> list[TradeSignal]:
        """
        扫描一只股票的所有信号

        Args:
            symbol: 股票代码
            df: 含技术指标的历史K线

        Returns:
            信号列表
        """
        if df is None or df.empty or len(df) < 50:
            return []

        signals = []
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        close = last["close"]

        # 技术指标
        rsi = last.get("rsi_14", 50)
        bb_lower = last.get("bb_lower", 0)
        bb_upper = last.get("bb_upper", 0)
        bb_middle = last.get("bb_middle", 0)
        bb_pct = last.get("bb_pct_b", 0.5)
        sma_5 = last.get("sma_5", 0)
        sma_20 = last.get("sma_20", 0)
        sma_50 = last.get("sma_50", 0)
        prev_sma_5 = prev.get("sma_5", 0)
        prev_sma_20 = prev.get("sma_20", 0)
        atr = last.get("atr_14", close * 0.03)
        vol_ratio = last.get("vol_ratio", 1.0)
        macd_hist = last.get("macd_hist", 0)

        # 20日高低
        high_20 = df["high"].iloc[-20:].max()
        low_20 = df["low"].iloc[-20:].min()

        # ══════════════════════════════════════════════
        # 🟢 买入信号
        # ══════════════════════════════════════════════

        # 1. RSI 超卖反弹
        if rsi < 30 and close > prev["close"]:
            signals.append(TradeSignal(
                symbol=symbol, signal_type="buy",
                reason=f"RSI 超卖反弹 (RSI={rsi:.0f})",
                strength="strong" if rsi < 25 else "medium",
                current_price=round(close, 2),
                suggested_entry=round(close, 2),
                suggested_stop=round(max(close - 1.5 * atr, bb_lower * 0.98), 2),
                suggested_target=round(bb_middle, 2),
                risk_reward=round((bb_middle - close) / (1.5 * atr), 1) if atr > 0 else 0,
                note="价格在布林下轨附近，RSI 显示超卖，反弹概率大"
            ))

        # 2. 布林下轨买入
        elif bb_pct < 0.15 and close > prev["close"]:
            stop = round(max(close - 2 * atr, bb_lower * 0.95), 2)
            target = round(bb_middle, 2)
            risk = close - stop
            reward = target - close
            signals.append(TradeSignal(
                symbol=symbol, signal_type="buy",
                reason=f"布林下轨反弹 (位置={bb_pct:.0%})",
                strength="strong" if bb_pct < 0.05 else "medium",
                current_price=round(close, 2),
                suggested_entry=round(close, 2),
                suggested_stop=stop,
                suggested_target=target,
                risk_reward=round(reward / risk, 1) if risk > 0 else 0,
                note="布林下轨支撑 + 价格反弹，盈亏比良好"
            ))

        # 3. 均线金叉
        if prev_sma_5 <= prev_sma_20 and sma_5 > sma_20 and sma_20 > 0:
            prev_macd = prev.get("macd_hist", 0)
            macd_turning = macd_hist > prev_macd  # MACD 改善
            signals.append(TradeSignal(
                symbol=symbol, signal_type="buy",
                reason=f"SMA 金叉 (5日↑穿越20日)" + (" + MACD改善" if macd_turning else ""),
                strength="strong" if macd_turning else "medium",
                current_price=round(close, 2),
                suggested_entry=round(close, 2),
                suggested_stop=round(sma_20 * 0.98, 2),
                suggested_target=round(close + (close - sma_20) * 2, 2),
                risk_reward=2.0,
                note="短期均线上穿中期均线，趋势转多"
            ))

        # 4. 回调至均线支撑
        elif sma_50 > 0 and abs(close - sma_50) / sma_50 < 0.02 and rsi < 50:
            signals.append(TradeSignal(
                symbol=symbol, signal_type="buy",
                reason=f"回调至 SMA50 支撑 (距均线 {(close-sma_50)/sma_50*100:+.1f}%)",
                strength="medium",
                current_price=round(close, 2),
                suggested_entry=round(sma_50, 2),
                suggested_stop=round(sma_50 * 0.96, 2),
                suggested_target=round(sma_50 * 1.08, 2),
                risk_reward=2.0,
                note="回调至长期均线支撑位，适合中线建仓"
            ))

        # ══════════════════════════════════════════════
        # 🔴 卖出/警示信号
        # ══════════════════════════════════════════════

        # 5. RSI 超买
        if rsi > 75:
            signals.append(TradeSignal(
                symbol=symbol, signal_type="alert",
                reason=f"RSI 超买 (RSI={rsi:.0f})",
                strength="strong" if rsi > 80 else "medium",
                current_price=round(close, 2),
                suggested_entry=0, suggested_stop=0, suggested_target=0,
                risk_reward=0,
                note="⚠️ RSI 进入超买区，短期回调风险大，不宜追高"
            ))

        # 6. 布林上轨 + 缩量（假突破风险）
        if bb_pct > 0.9 and vol_ratio < 0.7:
            signals.append(TradeSignal(
                symbol=symbol, signal_type="alert",
                reason=f"布林上轨缩量 (位置={bb_pct:.0%}, 量={vol_ratio:.1f}x)",
                strength="medium",
                current_price=round(close, 2),
                suggested_entry=0, suggested_stop=0, suggested_target=0,
                risk_reward=0,
                note="价格在布林上轨但缩量，突破可能为假突破，注意回落"
            ))

        # ══════════════════════════════════════════════
        # ⚡ 突破信号
        # ══════════════════════════════════════════════

        # 7. 突破 20 日高点 + 放量
        if close >= high_20 * 0.99 and vol_ratio > 1.5:
            signals.append(TradeSignal(
                symbol=symbol, signal_type="buy",
                reason=f"放量突破 20日高点 (量={vol_ratio:.1f}x)",
                strength="strong",
                current_price=round(close, 2),
                suggested_entry=round(high_20 * 1.01, 2),
                suggested_stop=round(high_20 * 0.97, 2),
                suggested_target=round(close + (high_20 - low_20), 2),
                risk_reward=round((close + (high_20 - low_20) - close) / (close - high_20 * 0.97), 1),
                note="放量突破近期高点，趋势可能加速，可追入"
            ))

        # ══════════════════════════════════════════════
        # ⚡ 短时异动检测
        # ══════════════════════════════════════════════

        # 8. Z-Score 极端偏离 (闪电崩盘抄底)
        z_score = self._calc_zscore(df, 20)
        if z_score is not None:
            if z_score < -2.5:
                # 极端超跌 → 抄底信号
                bounce_target = close * 1.03  # 反弹3%
                signals.append(TradeSignal(
                    symbol=symbol, signal_type="buy",
                    reason=f"⚡ 闪电崩盘抄底 (Z-Score={z_score:.1f}σ)",
                    strength="strong" if z_score < -3 else "medium",
                    current_price=round(close, 2),
                    suggested_entry=round(close, 2),
                    suggested_stop=round(close * 0.97, 2),
                    suggested_target=round(bounce_target, 2),
                    risk_reward=3.0,
                    note=f"价格偏离20日均线{z_score:.1f}个标准差，极端超跌，大概率均值回归"
                ))
            elif z_score < -2.0:
                signals.append(TradeSignal(
                    symbol=symbol, signal_type="alert",
                    reason=f"短时超跌 (Z-Score={z_score:.1f}σ)，关注反弹",
                    strength="medium",
                    current_price=round(close, 2),
                    suggested_entry=0, suggested_stop=0, suggested_target=0, risk_reward=0,
                    note="价格已偏离均线2σ以上，接近抄底区间但未到极端"
                ))
            elif z_score > 2.5:
                signals.append(TradeSignal(
                    symbol=symbol, signal_type="alert",
                    reason=f"⚡ 短时暴涨 (Z-Score=+{z_score:.1f}σ)，注意回落风险",
                    strength="strong" if z_score > 3 else "medium",
                    current_price=round(close, 2),
                    suggested_entry=0, suggested_stop=0, suggested_target=0, risk_reward=0,
                    note="价格偏离均线2.5σ以上，短期回落概率大，不宜追高"
                ))

        # 9. 成交量异常放大 (>3x 均量)
        if vol_ratio > 3.0:
            is_up = close > prev["close"]
            direction = "放量上涨" if is_up else "放量下跌"
            signals.append(TradeSignal(
                symbol=symbol, signal_type="alert",
                reason=f"📊 成交量异常 ({direction}, 量={vol_ratio:.1f}x均量)",
                strength="strong",
                current_price=round(close, 2),
                suggested_entry=0, suggested_stop=0, suggested_target=0, risk_reward=0,
                note="异常放量代表机构/大资金在行动" + ("，可能是吸筹" if is_up else "，可能是出货/恐慌")
            ))

        # 10. VWAP 偏离 (均值回归抄底)
        vwap = self._calc_vwap(df, 20)
        if vwap > 0:
            vwap_dev = (close - vwap) / vwap
            if vwap_dev < -0.03:  # 低于VWAP 3%以上
                signals.append(TradeSignal(
                    symbol=symbol, signal_type="buy",
                    reason=f"VWAP 下方偏离 {vwap_dev:.1%} (均值回归)",
                    strength="medium" if vwap_dev > -0.05 else "strong",
                    current_price=round(close, 2),
                    suggested_entry=round(close, 2),
                    suggested_stop=round(close * 0.98, 2),
                    suggested_target=round(vwap, 2),
                    risk_reward=round(abs(vwap_dev) / 0.02, 1),
                    note=f"价格低于成交量加权均价 {abs(vwap_dev)*100:.1f}%，大概率回归VWAP"
                ))

        # 11. RSI 底背离 (价格新低但RSI不新低 → 反转信号)
        if len(df) >= 20:
            recent_20 = df.iloc[-20:]
            price_low_idx = recent_20["close"].idxmin()
            rsi_low_idx = recent_20["rsi_14"].idxmin()
            if price_low_idx != rsi_low_idx and rsi > recent_20["rsi_14"].min() + 5:
                # 价格在创新低但 RSI 没有 → 底背离
                if close <= recent_20["close"].min() * 1.02:
                    signals.append(TradeSignal(
                        symbol=symbol, signal_type="buy",
                        reason="RSI 底背离 (价格新低但RSI未新低)",
                        strength="strong",
                        current_price=round(close, 2),
                        suggested_entry=round(close, 2),
                        suggested_stop=round(recent_20["close"].min() * 0.98, 2),
                        suggested_target=round(close * 1.05, 2),
                        risk_reward=2.5,
                        note="经典反转信号，下跌动能衰竭，反弹概率大"
                    ))

        # 12. 布林带挤压 (即将突破)
        bb_width = last.get("bb_width", 0)
        bb_width_20_ago = df["bb_width"].iloc[-21] if "bb_width" in df.columns and len(df) > 20 else 0
        if bb_width > 0 and bb_width_20_ago > 0 and bb_width < bb_width_20_ago * 0.7:
            signals.append(TradeSignal(
                symbol=symbol, signal_type="alert",
                reason=f"布林带挤压 (带宽收窄 {bb_width:.1f}%)，即将突破",
                strength="medium",
                current_price=round(close, 2),
                suggested_entry=0, suggested_stop=0, suggested_target=0, risk_reward=0,
                note="布林带宽收窄30%以上，通常预示大行情即将来临，方向待确认"
            ))

        # 13. 跳空缺口检测
        if len(df) >= 2:
            prev_close = df["close"].iloc[-2]
            today_open = df["open"].iloc[-1]
            gap_pct = (today_open - prev_close) / prev_close
            if abs(gap_pct) > 0.02:  # 跳空 > 2%
                gap_type = "向上跳空" if gap_pct > 0 else "向下跳空"
                signals.append(TradeSignal(
                    symbol=symbol, signal_type="alert",
                    reason=f"跳空缺口 ({gap_type} {gap_pct:+.1%})",
                    strength="strong" if abs(gap_pct) > 0.04 else "medium",
                    current_price=round(close, 2),
                    suggested_entry=0, suggested_stop=0, suggested_target=0, risk_reward=0,
                    note=f"{'多头强势' if gap_pct > 0 else '空头压力大'}，注意缺口回补可能"
                ))

        # 14. 短时 ROC (Rate of Change) 极端
        roc_5 = (close - df["close"].iloc[-6]) / df["close"].iloc[-6] * 100 if len(df) >= 6 else 0
        if abs(roc_5) > 8:
            direction = "暴涨" if roc_5 > 0 else "暴跌"
            signals.append(TradeSignal(
                symbol=symbol, signal_type="alert",
                reason=f"短时{direction} (5日涨跌 {roc_5:+.1f}%)",
                strength="strong",
                current_price=round(close, 2),
                suggested_entry=0, suggested_stop=0, suggested_target=0, risk_reward=0,
                note=f"5天内{direction}{abs(roc_5):.0f}%，极不寻常，注意反转或加速"
            ))

        # 15. 跌破关键均线支撑
        if sma_20 > 0 and close < sma_20 * 0.98:
            signals.append(TradeSignal(
                symbol=symbol, signal_type="alert",
                reason=f"跌破 SMA20 支撑 (距均线 {(close-sma_20)/sma_20*100:+.1f}%)",
                strength="strong" if close < sma_50 * 0.98 else "medium",
                current_price=round(close, 2),
                suggested_entry=0, suggested_stop=0, suggested_target=0, risk_reward=0,
                note="跌破20日均线是趋势转弱的信号，持有者应考虑止损"
            ))

        return signals

    # ------------------------------------------------------------------
    # 辅助计算
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_zscore(df: pd.DataFrame, period: int = 20) -> Optional[float]:
        """计算当前价格偏离均线的标准差倍数"""
        if len(df) < period:
            return None
        close = df["close"]
        mean = close.iloc[-period:].mean()
        std = close.iloc[-period:].std()
        if std == 0:
            return 0.0
        return round((close.iloc[-1] - mean) / std, 2)

    @staticmethod
    def _calc_vwap(df: pd.DataFrame, period: int = 20) -> float:
        """计算成交量加权均价 VWAP"""
        recent = df.iloc[-period:]
        if recent.empty or recent["volume"].sum() == 0:
            return 0.0
        vwap = (recent["close"] * recent["volume"]).sum() / recent["volume"].sum()
        return round(vwap, 2)
