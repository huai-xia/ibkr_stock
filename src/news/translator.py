"""
新闻翻译模块
将英文财经新闻翻译为中文

使用 deep-translator (Google Translate 免费接口)
带节流和失败重试，不可用时保留原文
"""

import logging
import time
from functools import lru_cache
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from deep_translator import GoogleTranslator
    _TRANSLATOR_AVAILABLE = True
except ImportError:
    _TRANSLATOR_AVAILABLE = False


class NewsTranslator:
    """
    新闻翻译器（英文 → 中文）

    使用方法:
        translator = NewsTranslator()
        zh = translator.translate("Fed raises interest rates")
        # → "美联储加息"
    """

    def __init__(self, rate_limit: float = 1.0):
        self._available = _TRANSLATOR_AVAILABLE
        self._rate_limit = rate_limit
        self._last_call = 0.0
        self._cache = {}

    @property
    def available(self) -> bool:
        return self._available

    def translate(self, text: str, max_length: int = 500) -> str:
        """
        翻译文本为中文

        Args:
            text: 英文文本
            max_length: 最大字符数（超长截断）

        Returns:
            中文翻译，失败时返回原文
        """
        if not text or not self._available:
            return text

        # 截断过长的文本
        if len(text) > max_length:
            text = text[:max_length]

        # 缓存检查
        cache_key = text[:100]
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 节流
        elapsed = time.time() - self._last_call
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)

        try:
            result = GoogleTranslator(source="en", target="zh-CN").translate(text)
            self._last_call = time.time()
            self._cache[cache_key] = result
            return result
        except Exception as e:
            logger.debug("翻译失败: %s", str(e)[:80])
            return text  # 静默降级，返回原文

    def translate_title(self, title: str) -> str:
        """翻译新闻标题（保留较短结果）"""
        if not self._available:
            return title
        zh = self.translate(title, max_length=300)
        # 标题通常较短
        if len(zh) > len(title) * 3:
            return title  # 翻译结果异常长，保留原文
        return zh

    def translate_summary(self, summary: str) -> str:
        """翻译新闻摘要"""
        if not self._available:
            return summary
        return self.translate(summary, max_length=200)

    def batch_translate(self, texts: list[str]) -> list[str]:
        """批量翻译"""
        return [self.translate(t) for t in texts]


# ============================================================
# 不需翻译的模板文本（直接中文输出）
# ============================================================

# 报告模板常量（中文）
REPORT_HEADER = "📰 IBKR 智能市场快报"
SECTION_MARKET = "📊 市场总览"
SECTION_BEARISH = "🔴 重大利空"
SECTION_BULLISH = "🟢 重大利好"
SECTION_HOLDINGS = "📦 持仓动态"
SECTION_ADVICE = "💡 操作建议"
SECTION_FOOTER = "📌 免责声明"

MOOD_LABELS = {
    "RISK_OFF": "🔴 避险情绪",
    "NEUTRAL": "🟡 中性偏谨慎",
    "RISK_ON": "🟢 风险偏好",
}

CATEGORY_LABELS_ZH = {
    "货币政策": "🏦 货币政策",
    "地缘政治": "🌍 地缘政治",
    "宏观经济": "📈 宏观经济",
    "科技监管": "🔧 科技监管",
    "市场波动": "📉 市场波动",
}

# 影响因子等级
def impact_label(score: float) -> str:
    """影响因子 → 标签"""
    if abs(score) >= 7:
        return "⚡ 极强"
    elif abs(score) >= 5:
        return "🔥 强"
    elif abs(score) >= 3:
        return "📌 中等"
    else:
        return "💭 一般"
