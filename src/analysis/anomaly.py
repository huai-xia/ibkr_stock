"""
短时异常检测器
基于分钟数据和特征缓存，实时检测9种盘中/夜盘异常
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class AnomalyAlert:
    """异常告警"""
    symbol: str
    alert_type: str        # flash_crash / volume_spike / vwap_deviation 等
    session: str           # regular / extended
    level: str             # critical / warning / info
    reason: str            # 人类可读原因
    current_price: float
    trigger_value: float   # 触发阈值
    metric_value: float    # 实际指标值
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))


class AnomalyDetector:
    """
    短时异常检测器

    用法:
        detector = AnomalyDetector()
        alerts = detector.detect_regular("AAPL", df_1min, features)
        alerts += detector.detect_extended("AAPL", df_ext, features)
    """

    # 阈值配置
    THRESHOLDS = {
        "zscore_crash": -3.0,
        "zscore_surge": 3.0,
        "roc_drop_10": -2.0,
        "roc_rise_10": 2.0,
        "vwap_deviation": 3.0,
        "vol_spike": 5.0,
        "volatility_spike": 3.0,
        "overnight_change": 3.0,
        "pre_jump": 3.0,
        "trend_slope": 0.05,
    }

    # ── 盘中检测 (6种) ──

    def detect_regular(
        self, symbol: str, df_1min, features=None,
    ) -> list[AnomalyAlert]:
        """基于1分钟K线检测盘中异常"""
        alerts = []
        if df_1min is None or df_1min.empty:
            return alerts

        close = df_1min["close"].values.astype(float)
        volume = df_1min["volume"].values.astype(float) if "volume" in df_1min.columns else None
        price = float(close[-1])
        n = len(close)

        # 1. 闪电崩盘 (20分钟 Z-Score)
        if n >= 20:
            z = self._zscore(close, 20)
            if z < self.THRESHOLDS["zscore_crash"]:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="flash_crash", session="regular",
                    level="critical" if z < -4 else "warning",
                    reason=f"⚡ 闪电崩盘 Z={z:.1f}σ (20分钟)",
                    current_price=price, trigger_value=self.THRESHOLDS["zscore_crash"],
                    metric_value=z,
                ))
            elif z > self.THRESHOLDS["zscore_surge"]:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="price_surge", session="regular",
                    level="critical" if z > 4 else "warning",
                    reason=f"⚡ 瞬间暴涨 Z=+{z:.1f}σ (20分钟)",
                    current_price=price, trigger_value=self.THRESHOLDS["zscore_surge"],
                    metric_value=z,
                ))

        # 2. VWAP 偏离
        if n >= 5 and volume is not None:
            vwap = float(np.average(close[-n:], weights=volume[-n:])) if volume[-n:].sum() > 0 else price
            vwap_dev = (price - vwap) / vwap * 100 if vwap > 0 else 0
            if abs(vwap_dev) > self.THRESHOLDS["vwap_deviation"]:
                direction = "高于" if vwap_dev > 0 else "低于"
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="vwap_deviation", session="regular",
                    level="warning",
                    reason=f"VWAP 偏离 {vwap_dev:+.1f}% (价格{direction}均价)",
                    current_price=price, trigger_value=self.THRESHOLDS["vwap_deviation"],
                    metric_value=round(vwap_dev, 1),
                ))

        # 3. 量异常 (10分钟)
        if n >= 10 and volume is not None:
            recent_vol = float(np.mean(volume[-5:]))
            base_vol = float(np.mean(volume[-10:-5])) if n >= 11 else recent_vol
            vol_ratio = recent_vol / base_vol if base_vol > 0 else 1.0
            if vol_ratio > self.THRESHOLDS["vol_spike"]:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="volume_spike", session="regular",
                    level="warning",
                    reason=f"📊 成交量异常 (量比 {vol_ratio:.1f}x, 5分钟)",
                    current_price=price, trigger_value=self.THRESHOLDS["vol_spike"],
                    metric_value=round(vol_ratio, 1),
                ))

        # 4. ROC 反转 (15分钟)
        if n >= 16:
            roc_15 = (close[-1] - close[-16]) / close[-16] * 100
            if roc_15 < -5 and close[-1] > close[-6]:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="reversal", session="regular",
                    level="info",
                    reason=f"📈 跌后反弹 (15分钟跌 {roc_15:.1f}%, 5分钟回升)",
                    current_price=price, trigger_value=-5,
                    metric_value=round(roc_15, 1),
                ))

        # 5. 突破确认
        if n >= 30:
            high_30 = float(np.max(close[-30:-1]))
            if price > high_30 * 1.01 and volume is not None and float(np.mean(volume[-5:])) > float(np.mean(volume[-30:-5])) * 2:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="breakout", session="regular",
                    level="info",
                    reason=f"📈 放量突破 30分钟高点",
                    current_price=price, trigger_value=round(high_30, 2),
                    metric_value=round(price, 2),
                ))

        # 6. 跳空回补
        if n >= 15 and hasattr(df_1min, 'iloc'):
            open_price = float(df_1min.iloc[0]["open"]) if "open" in df_1min.columns else 0
            prev_close = 0  # 需要外部传入
            # (简化，实际需要昨收)

        return alerts

    # ── 夜盘/盘前检测 (9种) ──

    def detect_extended(
        self, symbol: str, df_ext, prev_close: float = 0,
    ) -> list[AnomalyAlert]:
        """基于快照检测夜盘/盘前异常"""
        alerts = []
        if df_ext is None or df_ext.empty:
            return alerts

        prices = df_ext["price"].values.astype(float)
        volumes = df_ext["volume"].values.astype(float) if "volume" in df_ext.columns else None
        price = float(prices[-1])
        n = len(prices)

        # 1-2. 瞬间暴跌/暴涨 (5分钟 Z-Score)
        if n >= 5:
            z5 = self._zscore(prices, 5)
            if z5 < -3.0:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="flash_crash_ext", session="extended",
                    level="critical",
                    reason=f"⚡ 夜盘瞬间暴跌 Z={z5:.1f}σ (5分钟)",
                    current_price=price, trigger_value=-3.0, metric_value=z5,
                ))
            elif z5 > 3.0:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="price_surge_ext", session="extended",
                    level="critical",
                    reason=f"⚡ 夜盘瞬间暴涨 Z=+{z5:.1f}σ (5分钟)",
                    current_price=price, trigger_value=3.0, metric_value=z5,
                ))

        # 3-4. 快速下跌/上涨 (10分钟 ROC)
        t = self.THRESHOLDS
        if n >= 10:
            roc10 = (prices[-1] - prices[-11]) / prices[-11] * 100
            if roc10 < -t["roc_drop_10"]:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="quick_drop", session="extended",
                    level="critical" if roc10 < -5 else "warning",
                    reason=f"📉 夜盘快速下跌 {roc10:.1f}% (10分钟)",
                    current_price=price, trigger_value=-t["roc_drop_10"], metric_value=round(roc10, 1),
                ))
            elif roc10 > t["roc_rise_10"]:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="quick_rise", session="extended",
                    level="critical" if roc10 > 5 else "warning",
                    reason=f"📈 夜盘快速上涨 +{roc10:.1f}% (10分钟)",
                    current_price=price, trigger_value=t["roc_rise_10"], metric_value=round(roc10, 1),
                ))

        # 5. 波动率异常 (20分钟)
        if n >= 20:
            window = prices[-20:]
            sigma = float(np.std(np.diff(window) / window[:-1]) * 100)
            if sigma > t.get("volatility_spike", 3.0):
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="volatility_spike", session="extended",
                    level="warning",
                    reason=f"📊 夜盘波动率异常 σ={sigma:.2f}% (正常 <{t.get('volatility_spike',3.0)}%)",
                    current_price=price, trigger_value=t.get("volatility_spike", 3.0),
                    metric_value=round(sigma, 2),
                ))

        # 6. 异动 (20分钟变化率)
        if n >= 20:
            change20 = (prices[-1] - prices[-21]) / prices[-21] * 100
            if abs(change20) > t.get("overnight_change", 3.0):
                direction = "上涨" if change20 > 0 else "下跌"
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="overnight_move", session="extended",
                    level="warning",
                    reason=f"夜盘异动: {direction} {abs(change20):.1f}% (20分钟)",
                    current_price=price, trigger_value=t.get("overnight_change", 3.0),
                    metric_value=round(change20, 1),
                ))

        # 7. 跳变 vs 昨收
        if prev_close > 0:
            jump = (price - prev_close) / prev_close * 100
            if abs(jump) > t.get("pre_jump", 3.0):
                direction = "高开" if jump > 0 else "低开"
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="gap", session="extended",
                    level="critical" if abs(jump) > 5 else "warning",
                    reason=f"盘前跳变: {direction} {abs(jump):.1f}%",
                    current_price=price, trigger_value=t.get("pre_jump", 3.0),
                    metric_value=round(jump, 1),
                ))

        # 8. 趋势 (60分钟线性回归)
        if n >= 60:
            x = np.arange(60)
            y = prices[-60:]
            slope = np.polyfit(x, y, 1)[0] / prices[-60] * 100  # 每分钟涨跌%
            if abs(slope) > t.get("trend_slope", 0.05):
                direction = "上升" if slope > 0 else "下降"
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="trend", session="extended",
                    level="info",
                    reason=f"盘前趋势: 持续{direction} {abs(slope):.2f}%/分钟",
                    current_price=price, trigger_value=t.get("trend_slope", 0.05),
                    metric_value=round(slope, 3),
                ))

        # 9. 放量异常
        if n >= 20 and volumes is not None:
            recent_vol = float(np.mean(volumes[-5:]))
            base_vol = float(np.mean(volumes[:15])) if n >= 25 else (float(np.mean(volumes[:5])) if n >= 10 else recent_vol)
            ratio = recent_vol / base_vol if base_vol > 0 else 1.0
            if ratio > 5:
                alerts.append(AnomalyAlert(
                    symbol=symbol, alert_type="vol_spike_ext", session="extended",
                    level="warning",
                    reason=f"夜盘放量 {ratio:.1f}x (5分钟 vs 基准)",
                    current_price=price, trigger_value=5, metric_value=round(ratio, 1),
                ))

        return alerts

    # ── 多时间框架共振 (L1) ──

    def detect_resonance(self, symbol: str, aggregated: dict[int, 'pd.DataFrame']) -> list[AnomalyAlert]:
        """
        多时间框架共振检测
        在3/5/15/30分钟框架上分别检测，多框架同向 → 强信号

        Args:
            symbol: 股票代码
            aggregated: {3: df_3min, 5: df_5min, 15: df_15min, 30: df_30min}

        Returns:
            共振告警列表
        """
        alerts = []
        if not aggregated:
            return alerts

        # 对每个框架分别检测
        frame_signals = {}  # {period: [signal_directions]}

        for period, df in aggregated.items():
            if df is None or df.empty:
                continue

            close = df["close"].values.astype(float)
            volume = df["volume"].values.astype(float) if "volume" in df.columns else None
            n = len(close)

            signals = []

            # Z-Score (长样本) 或 ROC (短样本)
            if n >= 10:
                z = self._zscore(close, min(10, n))
                if z < -1.5:
                    signals.append("down")
                elif z > 1.5:
                    signals.append("up")
            elif n >= 4:
                # 短样本用首尾ROC代替Z
                roc_n = min(5, n - 1)
                roc = (close[-1] - close[-roc_n]) / close[-roc_n] * 100
                if roc < -1.5:
                    signals.append("down")
                elif roc > 1.5:
                    signals.append("up")

            # 量确认
            if n >= 5 and volume is not None:
                recent_vol = float(np.mean(volume[-3:])) if len(volume) >= 3 else volume[-1]
                base_vol = float(np.mean(volume[:-3])) if len(volume) > 3 else recent_vol
                ratio = recent_vol / base_vol if base_vol > 0 else 1
                if ratio > 2:
                    signals.append("volume")

            frame_signals[period] = signals

        # 统计共振
        up_frames = [p for p, sigs in frame_signals.items() if "up" in sigs]
        down_frames = [p for p, sigs in frame_signals.items() if "down" in sigs]
        vol_frames = [p for p, sigs in frame_signals.items() if "volume" in sigs]

        price = 0
        for df in aggregated.values():
            if df is not None and not df.empty:
                price = float(df["close"].iloc[-1])
                break

        # 共振：2+框架同向即可，量确认加分
        total_frames = len(frame_signals)
        min_frames = 2 if total_frames <= 3 else 3  # 数据少时放宽要求

        if len(up_frames) >= min_frames:
            frames_str = "+".join([f"{p}min" for p in sorted(up_frames)])
            strength = "strong" if len(vol_frames) >= 1 else "medium"
            alerts.append(AnomalyAlert(
                symbol=symbol, alert_type="resonance_up", session="regular",
                level="info",
                reason=f"📈 多框架共振看涨 ({frames_str}){'·放量' if vol_frames else ''}",
                current_price=price, trigger_value=min_frames, metric_value=len(up_frames),
            ))

        if len(down_frames) >= min_frames:
            frames_str = "+".join([f"{p}min" for p in sorted(down_frames)])
            strength = "strong" if len(vol_frames) >= 1 else "medium"
            alerts.append(AnomalyAlert(
                symbol=symbol, alert_type="resonance_down", session="regular",
                level="warning",
                reason=f"📉 多框架共振看跌 ({frames_str}){'·放量' if vol_frames else ''}",
                current_price=price, trigger_value=min_frames, metric_value=len(down_frames),
            ))

        return alerts

    # ── 辅助 ──

    @staticmethod
    def _zscore(series: np.ndarray, window: int) -> float:
        if len(series) < window:
            return 0.0
        w = series[-window:]
        mu, std = float(np.mean(w)), float(np.std(w))
        return float((series[-1] - mu) / std) if std > 0 else 0.0
