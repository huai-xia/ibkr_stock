"""
智能买入建议引擎
根据实时价格 + 技术指标 + 策略评分 → 推荐最优策略 + 生成购买建议

使用方法:
    advisor = PurchaseAdvisor(ib)
    advice = advisor.analyze("NVDA", quantity=5)           # 自动选策略
    advice = advisor.analyze("NVDA", strategy="momentum")  # 指定策略
    print(advisor.format_advice(advice))
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd
import numpy as np

from src.data.price_validator import PriceValidator, PriceResult

logger = logging.getLogger(__name__)


# ===================================================================
# 数据类
# ===================================================================


@dataclass
class PurchaseAdvice:
    """综合购买建议"""

    # 基本信息
    symbol: str = ""
    timestamp: str = ""

    # 实时价格
    current_price: float = 0.0
    is_realtime: bool = False
    price_confidence: str = "low"
    price_sources: list[str] = field(default_factory=list)

    # 订单建议
    suggested_order_type: str = "LIMIT"
    suggested_entry_price: float = 0.0
    suggested_stop_loss: float = 0.0
    suggested_take_profit: float = 0.0
    suggested_position_pct: float = 0.0

    # 风控指标
    atr: float = 0.0
    volatility_pct: float = 0.0
    risk_reward_ratio: float = 0.0
    max_loss_pct: float = 0.0

    # 信号分析
    buy_signals: list = field(default_factory=list)
    strongest_signal: str = ""
    signal_count: int = 0

    # 技术快照
    trend: str = "neutral"
    rsi: float = 0.0
    support_level: float = 0.0
    resistance_level: float = 0.0
    bb_position_pct: float = 0.0

    # 综合评分
    confidence_score: int = 0
    confidence_level: str = "low"

    # 警告
    warnings: list[str] = field(default_factory=list)

    # 市场环境
    session: str = "closed"
    session_warning: str = ""

    # 策略信息
    strategy_type: str = ""           # mean_reversion / momentum / ...
    strategy_label: str = ""          # 策略中文名
    strategy_reason: str = ""         # 为什么推荐此策略
    strategy_score: int = 0           # 策略匹配评分
    alternative_strategies: list = field(default_factory=list)  # [{type, label, score}]

    # 建议文字
    recommendation: str = ""
    summary: str = ""

    # 算法版本
    algorithm_version: str = "v1.0-rule-based"


# ===================================================================
# 策略注册表
#
# 新增策略只需:
#   1. 在此添加配置条目
#   2. 若需非规则算法，实现 PurchaseAlgorithm 子类并注册到 ALGORITHM_REGISTRY
# ===================================================================

STRATEGY_REGISTRY = {
    "mean_reversion": {
        "label": "均值回归",
        "description": "涨多卖、跌多买 — 基于布林带/RSI/Z-Score捕捉价格回归",
        "typical_hold": "1~5天",
        "risk_profile": "moderate",
        # 止损/止盈参数
        "stop_atr_mult": 1.2,         # 紧止损（回归不恋战）
        "target_rr_mult": 1.5,        # 保守止盈（回归中轨即走）
        "entry_style": "limit",
        # 置信度权重
        "weight_signal": 0.30,
        "weight_trend": 0.15,
        "weight_rr": 0.25,
        "weight_volatility": 0.30,
        # 策略偏好（自动评分用）
        "prefers_oversold": True,
        "prefers_uptrend": False,
        "prefers_low_vol": False,
        "prefers_squeeze": True,
    },
    "momentum": {
        "label": "动量交易",
        "description": "强者恒强 — 基于均线金叉/放量突破/回踩支撑追涨",
        "typical_hold": "3~10天",
        "risk_profile": "moderate",
        "stop_atr_mult": 1.5,
        "target_rr_mult": 2.0,
        "entry_style": "limit",
        "weight_signal": 0.25,
        "weight_trend": 0.35,
        "weight_rr": 0.25,
        "weight_volatility": 0.15,
        "prefers_oversold": False,
        "prefers_uptrend": True,
        "prefers_low_vol": True,
        "prefers_squeeze": False,
    },
    "trend_following": {
        "label": "趋势跟踪",
        "description": "顺势而为，截断亏损让利润奔跑 — MA交叉/ADX/通道突破",
        "typical_hold": "数周~数月",
        "risk_profile": "conservative",
        "stop_atr_mult": 2.0,
        "target_rr_mult": 3.0,
        "entry_style": "limit",
        "weight_signal": 0.15,
        "weight_trend": 0.45,
        "weight_rr": 0.20,
        "weight_volatility": 0.20,
        "prefers_oversold": False,
        "prefers_uptrend": True,
        "prefers_low_vol": True,
        "prefers_squeeze": False,
        "_status": "planned",
    },
    "breakout": {
        "label": "突破交易",
        "description": "关键位突破跟进 — 前高/开盘区间/布林带宽收窄突破",
        "typical_hold": "3~10天",
        "risk_profile": "aggressive",
        "stop_atr_mult": 1.0,
        "target_rr_mult": 2.5,
        "entry_style": "limit",
        "weight_signal": 0.30,
        "weight_trend": 0.15,
        "weight_rr": 0.30,
        "weight_volatility": 0.25,
        "prefers_oversold": False,
        "prefers_uptrend": False,
        "prefers_low_vol": False,
        "prefers_squeeze": True,
        "_status": "planned",
    },
    "event_driven": {
        "label": "事件驱动",
        "description": "财报/并购/政策事件触发 — 新闻情感+波动率异动",
        "typical_hold": "1~7天",
        "risk_profile": "aggressive",
        "stop_atr_mult": 1.5,
        "target_rr_mult": 2.0,
        "entry_style": "limit",
        "weight_signal": 0.35,
        "weight_trend": 0.10,
        "weight_rr": 0.25,
        "weight_volatility": 0.30,
        "prefers_oversold": True,
        "prefers_uptrend": False,
        "prefers_low_vol": False,
        "prefers_squeeze": False,
        "_status": "planned",
    },
    "value_invest": {
        "label": "价值投资",
        "description": "买低估等回归 — PE/PB/ROE/DCF多维估值判断",
        "typical_hold": "数月~数年",
        "risk_profile": "conservative",
        "stop_atr_mult": 2.5,
        "target_rr_mult": 3.0,
        "entry_style": "limit",
        "weight_signal": 0.10,
        "weight_trend": 0.20,
        "weight_rr": 0.20,
        "weight_volatility": 0.50,      # 低波动是长线核心
        "prefers_oversold": True,
        "prefers_uptrend": False,
        "prefers_low_vol": True,
        "prefers_squeeze": False,
        "_status": "planned",
        "_note": "需接入基本面数据(PE/PB/ROE)，当前仅有技术面框架",
    },
    "growth_invest": {
        "label": "成长投资",
        "description": "买高增长享复利 — 营收增速/毛利率/市场份额",
        "typical_hold": "数月~数年",
        "risk_profile": "moderate",
        "stop_atr_mult": 2.0,
        "target_rr_mult": 3.5,
        "entry_style": "limit",
        "weight_signal": 0.10,
        "weight_trend": 0.30,
        "weight_rr": 0.20,
        "weight_volatility": 0.40,
        "prefers_oversold": False,
        "prefers_uptrend": True,
        "prefers_low_vol": False,        # 成长股波动可接受
        "prefers_squeeze": False,
        "_status": "planned",
        "_note": "需接入营收增速/毛利率等基本面数据",
    },
    "dividend": {
        "label": "分红策略",
        "description": "稳定现金流 — 股息率/派息历史/自由现金流",
        "typical_hold": "数年",
        "risk_profile": "conservative",
        "stop_atr_mult": 3.0,            # 极宽止损（不轻易卖分红股）
        "target_rr_mult": 2.0,
        "entry_style": "limit",
        "weight_signal": 0.05,
        "weight_trend": 0.10,
        "weight_rr": 0.15,
        "weight_volatility": 0.70,       # 低波是核心
        "prefers_oversold": False,
        "prefers_uptrend": False,
        "prefers_low_vol": True,
        "prefers_squeeze": False,
        "_status": "planned",
        "_note": "需接入股息率/派息历史数据",
    },
    "stat_arbitrage": {
        "label": "统计套利",
        "description": "配对协整→价差收敛 — 协整检验/卡尔曼滤波",
        "typical_hold": "数天~数周",
        "risk_profile": "moderate",
        "stop_atr_mult": 1.5,
        "target_rr_mult": 2.0,
        "entry_style": "limit",
        "weight_signal": 0.30,
        "weight_trend": 0.10,            # 不依赖趋势
        "weight_rr": 0.30,
        "weight_volatility": 0.30,
        "prefers_oversold": True,
        "prefers_uptrend": False,
        "prefers_low_vol": False,
        "prefers_squeeze": True,         # 价差收窄=机会
        "_status": "planned",
        "_note": "需配对股票选择和协整检验模块",
    },
    "grid_trading": {
        "label": "网格交易",
        "description": "区间低买高卖 — 布林带/ATR网格自动挂单",
        "typical_hold": "数天~数周",
        "risk_profile": "moderate",
        "stop_atr_mult": 1.0,            # 网格自带止损(网格下限)
        "target_rr_mult": 1.0,           # 网格吃小波段
        "entry_style": "limit",
        "weight_signal": 0.15,
        "weight_trend": 0.10,
        "weight_rr": 0.25,
        "weight_volatility": 0.50,       # 区间震荡适合网格
        "prefers_oversold": False,
        "prefers_uptrend": False,        # 震荡市最适合
        "prefers_low_vol": True,
        "prefers_squeeze": False,
        "_status": "planned",
        "_note": "需网格执行器 + 区间自动挂单模块",
    },
}

# 已实现策略（_status != "planned" 即为 active）
ACTIVE_STRATEGIES = [
    k for k, v in STRATEGY_REGISTRY.items()
    if v.get("_status", "active") == "active"
]


# ===================================================================
# 购买算法接口
# ===================================================================


class PurchaseAlgorithm:
    """
    购买算法基类 — 新策略若需特殊逻辑，继承此类

    子类需实现:
        analyze()            核心分析（填充 advice）
        score_signal()       信号评分 (0-100)
        score_trend()        趋势评分 (0-100)
        score_rr()           盈亏比评分 (0-100)
        score_volatility()   波动率评分 (0-100)
        generate_recommendation()  建议文字
        decide_order_type()  订单类型
    """

    name: str = "base"
    version: str = "v1.0"
    description: str = ""

    def analyze(
        self, advisor: "PurchaseAdvisor", advice: PurchaseAdvice,
        df: pd.DataFrame, current_price: float, net_liq: float,
        quantity: int, risk_profile: str,
    ) -> None:
        raise NotImplementedError

    def decide_order_type(self, advice: PurchaseAdvice) -> str:
        raise NotImplementedError

    def score_signal(self, advice: PurchaseAdvice) -> float:
        raise NotImplementedError

    def score_trend(self, advice: PurchaseAdvice) -> float:
        raise NotImplementedError

    def score_rr(self, advice: PurchaseAdvice) -> float:
        raise NotImplementedError

    def score_volatility(self, advice: PurchaseAdvice) -> float:
        raise NotImplementedError

    def generate_recommendation(self, advice: PurchaseAdvice, quantity: int) -> str:
        raise NotImplementedError


# ===================================================================
# v1 参数化规则算法（一个类覆盖所有规则型策略）
# ===================================================================

class RuleBasedAlgorithm(PurchaseAlgorithm):
    """
    规则算法: 通过 STRATEGY_REGISTRY 配置参数化

    不同策略的区别在于止损乘数、盈亏比目标、评分权重 ——
    均由 strategy_config 注入，无需新建子类。
    """

    name = "rule-based"
    version = "v1.0"
    description = "参数化规则算法"

    def __init__(self, strategy_config: dict = None):
        self._config = strategy_config or {}

    # -- 主分析 --
    def analyze(
        self, advisor: "PurchaseAdvisor", advice: PurchaseAdvice,
        df: pd.DataFrame, current_price: float, net_liq: float,
        quantity: int, risk_profile: str,
    ) -> None:
        # 1. 扫描买入信号
        from src.analysis.signals import SignalDetector
        detector = SignalDetector()
        all_signals = detector.scan(advice.symbol, df)
        advice.buy_signals = [s for s in all_signals if s.signal_type == "buy"]
        advice.signal_count = len(advice.buy_signals)

        strength = {"strong": 3, "medium": 2, "weak": 1}
        best = max(advice.buy_signals, key=lambda s: strength.get(s.strength, 0), default=None)
        if best:
            advice.strongest_signal = f"{best.reason} ({best.strength.upper()})"

        # 2. 技术指标
        from src.analysis.exit_strategy import ExitStrategyEngine
        plan = ExitStrategyEngine().analyze(
            advice.symbol, df, current_price=current_price, risk_profile=risk_profile,
        )
        if plan is None:
            advice.warnings.append("无法计算止损/止盈位")
            return

        advice.atr = plan.atr
        advice.volatility_pct = plan.volatility_pct
        advice.rsi = plan.rsi
        advice.trend = plan.trend
        advice.support_level = plan.support_level
        advice.resistance_level = plan.resistance_level
        advice.suggested_position_pct = plan.suggested_position_pct

        # 3. 策略特定止损/止盈
        atr = plan.atr or current_price * 0.03
        stop_m = self._config.get("stop_atr_mult", 1.5)
        rr_m = self._config.get("target_rr_mult", 2.0)

        advice.suggested_stop_loss = max(
            round(current_price - stop_m * atr, 2),
            round(current_price * 0.85, 2),
        )
        atr_target = round(current_price + stop_m * rr_m * atr, 2)
        advice.suggested_take_profit = (
            min(atr_target, round(plan.resistance_level * 1.02, 2))
            if plan.resistance_level > current_price else atr_target
        )
        risk = current_price - advice.suggested_stop_loss
        reward = advice.suggested_take_profit - current_price
        advice.risk_reward_ratio = round(reward / risk, 1) if risk > 0 else 0

        # 4. 布林位置
        try:
            bb_l = float(df.iloc[-1].get("bb_lower", df["close"].iloc[-1] * 0.9))
            bb_u = float(df.iloc[-1].get("bb_upper", df["close"].iloc[-1] * 1.1))
            rng = bb_u - bb_l
            advice.bb_position_pct = round((current_price - bb_l) / rng * 100, 0) if rng > 0 else 50
        except Exception:
            advice.bb_position_pct = 50

        advice.suggested_entry_price = current_price
        advice.suggested_order_type = self.decide_order_type(advice)

        if quantity > 0 and net_liq > 0:
            advice.max_loss_pct = round(
                (current_price - advice.suggested_stop_loss) * quantity / net_liq * 100, 1
            )

    def decide_order_type(self, advice: PurchaseAdvice) -> str:
        return "LIMIT"

    # -- 评分 --
    def score_signal(self, a: PurchaseAdvice) -> float:
        if a.signal_count >= 2 and any(s.strength == "strong" for s in a.buy_signals):
            return 100
        if any(s.strength == "strong" for s in a.buy_signals):
            return 88
        if a.signal_count >= 2:
            return 72
        if a.signal_count == 1:
            return 56 if a.buy_signals[0].strength == "medium" else 32
        return 12

    def score_trend(self, a: PurchaseAdvice) -> float:
        if a.trend == "up" and a.rsi < 70:
            return 100
        if a.trend == "up":
            return 72
        if a.trend == "neutral":
            return 60
        if a.trend == "down" and a.rsi < 30:
            return 40
        return 12

    def score_rr(self, a: PurchaseAdvice) -> float:
        rr = a.risk_reward_ratio
        if rr >= 3.0: return 100
        if rr >= 2.0: return 80
        if rr >= 1.5: return 48
        if rr >= 1.0: return 24
        return 0

    def score_volatility(self, a: PurchaseAdvice) -> float:
        v = a.volatility_pct
        if v < 20: return 100
        if v < 30: return 92
        if v < 50: return 72
        if v < 60: return 48
        return 20

    def generate_recommendation(self, a: PurchaseAdvice, qty: int) -> str:
        parts = []
        label = self._config.get("label", "")
        if a.buy_signals:
            sn = "、".join(s.reason for s in a.buy_signals[:2])
            parts.append(f"[{label}] {a.symbol} ${a.current_price:.2f}，{a.signal_count}个信号（{sn}）。")
        else:
            parts.append(f"[{label}] {a.symbol} ${a.current_price:.2f}，无明确信号。")
        tm = {"up": "上升", "down": "下跌", "neutral": "震荡"}
        parts.append(f"趋势{tm.get(a.trend, '?')}，RSI{a.rsi:.0f}。")
        if a.suggested_stop_loss > 0:
            sp = (a.current_price - a.suggested_stop_loss) / a.current_price * 100
            tp = (a.suggested_take_profit - a.current_price) / a.current_price * 100
            parts.append(f"止损${a.suggested_stop_loss:.2f}(-{sp:.1f}%)，止盈${a.suggested_take_profit:.2f}(+{tp:.1f}%)，R/R {a.risk_reward_ratio:.1f}:1。")
        h = self._config.get("typical_hold", "")
        if h: parts.append(f"建议周期: {h}。")
        if a.confidence_score >= 70:
            parts.append(f"置信度{a.confidence_score}(高)，可执行。")
        elif a.confidence_score >= 40:
            parts.append(f"置信度{a.confidence_score}(中)，适量参与。")
        else:
            parts.append(f"置信度{a.confidence_score}(低)，建议观望。")
        return " ".join(parts)


# ===================================================================
# 算法注册表
# ===================================================================

ALGORITHM_REGISTRY: dict[str, type] = {
    "rule-based": RuleBasedAlgorithm,
}


# ===================================================================
# 购买建议引擎
# ===================================================================

class PurchaseAdvisor:
    """
    智能买入建议引擎

    流程:
        1. 获取实时价格 + 历史K线
        2. 分析股票状态指纹 (RSI/趋势/波动率/布林位置)
        3. 对每个活跃策略打分 → 推荐最优
        4. (可选) 用户 --strategy 覆盖
        5. 执行选定策略 → 生成入场/止损/止盈建议
    """

    def __init__(self, ib, finnhub_key: str = ""):
        self._ib = ib
        self._validator = PriceValidator(ib, finnhub_key=finnhub_key)
        self._data_mgr = None
        self._algorithm_override = None

    def set_algorithm(self, algo: PurchaseAlgorithm):
        """手动设置算法（覆盖策略默认绑定）"""
        self._algorithm_override = algo

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def analyze(
        self,
        symbol: str,
        quantity: int = 0,
        budget: float = 0.0,
        net_liq: float = 0.0,
        strategy: str = "",              # "" = 自动推荐；指定则覆盖
        days: int = 365,
    ) -> Optional[PurchaseAdvice]:
        """
        Args:
            symbol: 股票代码
            quantity: 期望股数
            budget: 金额预算
            net_liq: 净清算值
            strategy: 策略类型 (""=自动推荐, "mean_reversion", "momentum", ...)
            days: 历史数据天数
        """
        symbol = symbol.upper()
        advice = PurchaseAdvice(
            symbol=symbol,
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

        # ── 1. 实时价格 ──
        price_result = self._validator.get_price(symbol)
        if price_result.price <= 0:
            advice.recommendation = f"⚠️ 无法获取 {symbol} 实时价格"
            return advice
        advice.current_price = price_result.price
        advice.is_realtime = price_result.is_realtime
        advice.price_confidence = price_result.confidence
        advice.price_sources = price_result.sources
        advice.session = price_result.session
        advice.session_warning = price_result.extended_hours_warning

        # ── 2. 历史数据 ──
        self._data_mgr = self._data_mgr or self._init_data_mgr()
        data = self._data_mgr.prepare_analysis_data([symbol], days=days)
        df = data.get(symbol)
        if df is None or df.empty or len(df) < 20:
            advice.recommendation = f"⚠️ {symbol} 历史数据不足"
            return advice

        # ── 3. 策略选择 ──
        if strategy and strategy in STRATEGY_REGISTRY:
            # 用户指定策略
            strategy_config = STRATEGY_REGISTRY[strategy]
            advice.strategy_type = strategy
            advice.strategy_label = strategy_config["label"]
            advice.strategy_reason = "用户手动选择"
        else:
            # 自动推荐
            scores = self._score_strategies(df, advice.current_price)
            if not scores:
                advice.recommendation = f"⚠️ 无可用的交易策略"
                return advice
            best = scores[0]
            strategy_config = STRATEGY_REGISTRY[best["type"]]
            advice.strategy_type = best["type"]
            advice.strategy_label = best["label"]
            advice.strategy_score = best["score"]
            advice.strategy_reason = best.get("reason", "")
            # 备选策略
            advice.alternative_strategies = [
                {"type": s["type"], "label": s["label"], "score": s["score"]}
                for s in scores[1:4]  # 最多3个备选
            ]
            if strategy and strategy not in STRATEGY_REGISTRY:
                advice.warnings.append(f"未知策略 '{strategy}'，已自动推荐 {best['label']}")

        risk_profile = strategy_config["risk_profile"]

        # ── 4. 算法执行 ──
        algorithm = self._algorithm_override or self._get_algorithm(strategy_config)
        advice.algorithm_version = f"{algorithm.name}-{algorithm.version}"
        algorithm.analyze(
            advisor=self, advice=advice, df=df,
            current_price=advice.current_price,
            net_liq=net_liq, quantity=quantity,
            risk_profile=risk_profile,
        )

        # ── 5. 数量 ──
        if quantity <= 0 and budget > 0 and advice.current_price > 0:
            quantity = max(1, int(budget / advice.current_price))
        if quantity <= 0:
            quantity = 1

        # ── 6. 置信度评分（权重来自策略配置，评分函数来自算法） ──
        advice.confidence_score = self._calc_confidence(advice, strategy_config, algorithm)
        advice.confidence_level = (
            "high" if advice.confidence_score >= 70
            else "medium" if advice.confidence_score >= 40
            else "low"
        )

        # ── 7. 警告 + 建议文字 ──
        advice.warnings.extend(self._check_warnings(advice, quantity, net_liq))
        advice.recommendation = algorithm.generate_recommendation(advice, quantity)
        advice.summary = self._make_summary(advice)

        return advice

    # ------------------------------------------------------------------
    # 策略自动评分
    # ------------------------------------------------------------------

    def _score_strategies(self, df: pd.DataFrame, current_price: float) -> list[dict]:
        """
        根据股票当前状态指纹，对每个活跃策略打分

        状态指纹包含:
            - RSI 位置 (超卖/中性/超买)
            - 趋势方向 (up/down/neutral)
            - 波动率 (低/中/高)
            - 布林位置 (下轨/中轨/上轨) — 用实时价
            - 均线排列 (多头/空头/交织)
        """
        last = df.iloc[-1]
        rsi = float(last.get("rsi_14", 50))
        close = float(last["close"])
        price = current_price or close  # 优先用实时价

        # 趋势
        sma5 = float(last.get("sma_5", close))
        sma20 = float(last.get("sma_20", close))
        if sma5 > sma20 * 1.02:
            trend = "up"
        elif sma5 < sma20 * 0.98:
            trend = "down"
        else:
            trend = "neutral"

        # 波动率
        returns = df["close"].pct_change().dropna()
        vol = float(returns.std() * np.sqrt(252) * 100)

        # 布林位置（用实时价计算）
        try:
            bb_l = float(last.get("bb_lower", price * 0.9))
            bb_u = float(last.get("bb_upper", price * 1.1))
            bb_pct = (price - bb_l) / (bb_u - bb_l) * 100 if bb_u > bb_l else 50
        except Exception:
            bb_pct = 50

        scores = []
        for stype, cfg in STRATEGY_REGISTRY.items():
            if cfg.get("_status") == "planned":
                continue

            sc = 50  # 基准分

            # RSI 维度 (±20)
            if cfg.get("prefers_oversold") and rsi < 35:
                sc += 20
            elif cfg.get("prefers_oversold") and rsi > 65:
                sc -= 15
            if not cfg.get("prefers_oversold") and rsi > 55:
                sc += 10

            # 趋势维度 (±20)
            if cfg.get("prefers_uptrend") and trend == "up":
                sc += 20
            elif cfg.get("prefers_uptrend") and trend == "down":
                sc -= 15
            if not cfg.get("prefers_uptrend") and trend == "down" and rsi < 40:
                sc += 15

            # 波动率维度 (±15)
            if cfg.get("prefers_low_vol") and vol < 40:
                sc += 15
            elif cfg.get("prefers_low_vol") and vol > 70:
                sc -= 10
            if not cfg.get("prefers_low_vol") and vol > 50:
                sc += 10

            # 布林维度 (用 <35 替代 <20，更包容)
            if cfg.get("prefers_squeeze") and bb_pct < 35:
                sc += 15
            if not cfg.get("prefers_squeeze") and 35 < bb_pct < 65:
                sc += 10

            scores.append({
                "type": stype,
                "label": cfg["label"],
                "score": max(0, min(sc, 100)),
                "reason": self._strategy_reason(stype, cfg, rsi, trend, vol, bb_pct),
            })

        scores.sort(key=lambda x: x["score"], reverse=True)
        return scores

    def _strategy_reason(self, stype, cfg, rsi, trend, vol, bb_pct) -> str:
        """生成策略推荐理由（始终包含数值，而非仅阈值触发时）"""
        parts = []

        # 核心判断维度
        if cfg.get("prefers_oversold"):
            if rsi < 35:
                parts.append(f"RSI {rsi:.0f} 偏卖→适合抄底")
            else:
                parts.append(f"RSI {rsi:.0f} 未超卖，信号一般")
        else:
            if trend == "up":
                parts.append(f"趋势向上→动量有利")
            elif trend == "down":
                parts.append(f"趋势向下→动量不利")
            else:
                parts.append(f"趋势震荡→方向不明")

        if cfg.get("prefers_uptrend"):
            if trend == "up":
                parts.append("顺势做多")
            else:
                parts.append(f"非上升趋势，策略匹配度打折")

        if cfg.get("prefers_low_vol"):
            if vol < 40:
                parts.append(f"波动率 {vol:.0f}% 适中")
            else:
                parts.append(f"波动率 {vol:.0f}% 偏高")

        if cfg.get("prefers_squeeze"):
            if bb_pct < 35:
                parts.append(f"布林下轨附近({bb_pct:.0f}%)→回归机会")
            elif bb_pct > 65:
                parts.append(f"布林上轨附近({bb_pct:.0f}%)→注意风险")

        return "；".join(parts) if parts else "综合技术面匹配"

    # ------------------------------------------------------------------
    # 置信度评分
    # ------------------------------------------------------------------

    def _calc_confidence(self, advice, cfg, algo) -> int:
        w = {k: cfg.get(f"weight_{k}", 0.25) for k in ["signal", "trend", "rr", "volatility"]}
        score = round(
            algo.score_signal(advice) * w["signal"]
            + algo.score_trend(advice) * w["trend"]
            + algo.score_rr(advice) * w["rr"]
            + algo.score_volatility(advice) * w["volatility"]
        )
        return min(max(score, 0), 100)

    # ------------------------------------------------------------------
    # 警告
    # ------------------------------------------------------------------

    def _check_warnings(self, a, qty, net_liq) -> list[str]:
        w = []
        if a.session in ("pre_market", "after_hours"):
            w.append("⏰ 当前为盘前/盘后时段，流动性差，仅建议限价单")
        if a.volatility_pct > 60:
            w.append(f"⚠️ 年化波动率{a.volatility_pct:.0f}%较高，建议减仓")
        elif a.volatility_pct > 40:
            w.append(f"📊 波动率{a.volatility_pct:.0f}%中等，注意仓位")
        if a.rsi > 70:
            w.append(f"🔴 RSI{a.rsi:.0f}超买，追高风险大")
        if a.risk_reward_ratio < 1.5 and a.risk_reward_ratio > 0:
            w.append(f"⚡ 盈亏比仅{a.risk_reward_ratio:.1f}:1，低于推荐")
        if qty > 0 and net_liq > 0 and a.current_price > 0:
            pct = qty * a.current_price / net_liq * 100
            if pct > 20:
                w.append(f"🚫 单票仓位{pct:.0f}%超过20%上限")
            elif pct > 15:
                w.append(f"⚠️ 单票仓位{pct:.0f}%偏高")
        if a.price_confidence == "low":
            w.append(f"📡 价格可信度低({', '.join(a.price_sources)})")
        if a.signal_count == 0:
            w.append("🔍 当前无明确技术买入信号")
        return w

    # ------------------------------------------------------------------
    # 文字输出
    # ------------------------------------------------------------------

    def _make_summary(self, a: PurchaseAdvice) -> str:
        if a.confidence_score >= 70:
            lvl = "🟢 建议买入"
        elif a.confidence_score >= 40:
            lvl = "🟡 可适量参与"
        else:
            lvl = "🔴 建议观望"
        return (
            f"{lvl} | [{a.strategy_label}] {a.symbol} ${a.current_price:.2f} | "
            f"止损${a.suggested_stop_loss:.2f} | 止盈${a.suggested_take_profit:.2f} | "
            f"置信度{a.confidence_score}/100"
        )

    def format_advice(self, advice: PurchaseAdvice) -> str:
        if advice is None:
            return "⚠️ 无法生成购买建议"

        lines = [
            "", f"## 🧠 购买建议: {advice.symbol}",
            f"⏰ {advice.timestamp}", "",
            "### 📋 核心建议", "",
            "| 项目 | 数值 |",
            "|------|------|",
        ]

        pstatus = "实时" if advice.is_realtime else "延迟"
        pemoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(advice.price_confidence, "⚪")
        lines.append(
            f"| 当前价格 | ${advice.current_price:.2f} ({pstatus}, {len(advice.price_sources)}源, {pemoji}) |"
        )
        if advice.strategy_reason == "用户手动选择":
            lines.append(f"| 推荐策略 | **{advice.strategy_label}** (用户指定) |")
        else:
            lines.append(f"| 推荐策略 | **{advice.strategy_label}** (匹配度 {advice.strategy_score}/100) |")
        if advice.strategy_reason:
            lines.append(f"| 推荐理由 | {advice.strategy_reason} |")
        lines.append(f"| 建议订单 | ✅ 限价单 LIMIT |")
        lines.append(f"| 建议入场 | ${advice.suggested_entry_price:.2f} |")

        sd = (advice.current_price - advice.suggested_stop_loss) / advice.current_price * 100 if advice.current_price > 0 else 0
        td = (advice.suggested_take_profit - advice.current_price) / advice.current_price * 100 if advice.current_price > 0 else 0
        lines.append(f"| 建议止损 | ${advice.suggested_stop_loss:.2f} (-{sd:.1f}%) |")
        lines.append(f"| 建议止盈 | ${advice.suggested_take_profit:.2f} (+{td:.1f}%) |")
        lines.append(f"| 盈亏比 | **{advice.risk_reward_ratio:.1f}:1** |")
        lines.append(f"| 建议仓位 | {advice.suggested_position_pct:.0f}% |")
        if advice.max_loss_pct > 0:
            lines.append(f"| 最大亏损 | {advice.max_loss_pct:.1f}% 净值 |")

        clevel = f"{'🟢 HIGH' if advice.confidence_score >= 70 else ('🟡 MEDIUM' if advice.confidence_score >= 40 else '🔴 LOW')}"
        lines.append(f"| 置信度 | **{advice.confidence_score}/100 {clevel}** |")

        if advice.session != "regular":
            sl = {"pre_market": "🌅 盘前", "after_hours": "🌆 盘后", "closed": "🔒 休市"}.get(advice.session, advice.session)
            lines.append(f"| 市场时段 | {sl} |")
        lines.append("")

        # 备选策略
        if advice.alternative_strategies:
            lines.append("### 🔄 备选策略")
            lines.append("")
            for s in advice.alternative_strategies:
                lines.append(f"- {s['label']}: 匹配度 {s['score']}/100")
            lines.append("")

        # 技术指标
        lines.extend([
            "### 📊 技术指标", "",
            "| 指标 | 数值 |",
            "|------|------|",
            f"| ATR(14) | ${advice.atr:.2f} |",
            f"| 波动率 | {advice.volatility_pct:.1f}% |",
            f"| RSI(14) | {advice.rsi:.0f} |",
        ])
        te = {"up": "🟢", "down": "🔴", "neutral": "🟡"}.get(advice.trend, "⚪")
        tl = {"up": "上升", "down": "下跌", "neutral": "震荡"}.get(advice.trend, "不明")
        lines.append(f"| 趋势 | {te} {tl} |")
        lines.append(f"| 布林位置 | {advice.bb_position_pct:.0f}% |")
        lines.append(f"| 支撑位 | ${advice.support_level:.2f} |")
        lines.append(f"| 阻力位 | ${advice.resistance_level:.2f} |")
        lines.append("")

        # 信号
        if advice.buy_signals:
            lines.append("### 📈 买入信号")
            lines.append("")
            for s in advice.buy_signals:
                em = {"strong": "🟢", "medium": "🟡", "weak": "⚪"}.get(s.strength, "")
                lines.append(f"- {em} **{s.reason}** ({s.strength.upper()})")
                if s.note:
                    lines.append(f"  - {s.note}")
            lines.append("")
        else:
            lines.extend(["### 📈 买入信号", "", "⚪ 当前无明确技术买入信号", ""])

        # 警告
        if advice.warnings:
            lines.append("### ⚠️ 风险提示")
            lines.append("")
            for w in advice.warnings:
                lines.append(f"- {w}")
            lines.append("")

        # 综合建议
        lines.extend(["### 💡 综合建议", "", advice.recommendation, ""])

        # 下单命令
        if advice.confidence_score >= 40:
            lines.extend([
                "### 🚀 下单命令", "",
                "```bash",
                f"python3 -m src.cli.main --port <PORT> order buy <QTY> "
                f"{advice.symbol} --type {advice.suggested_order_type.lower()} "
                f"--price {advice.suggested_entry_price:.2f} --force",
                "```", "",
            ])

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _get_algorithm(self, cfg: dict) -> PurchaseAlgorithm:
        algo_name = cfg.get("algorithm", "rule-based")
        cls = ALGORITHM_REGISTRY.get(algo_name, RuleBasedAlgorithm)
        return cls(strategy_config=cfg)

    def _init_data_mgr(self):
        from src.analysis.stock_data import StockDataManager
        return StockDataManager(self._ib)
