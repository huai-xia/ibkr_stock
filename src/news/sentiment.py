"""
情感分析模块
使用 VADER（轻量规则引擎）进行财经新闻情感评分

VADER 优势：无需 GPU、速度快、对财经短文本效果好
备选方案：FinBERT（需 transformers + torch，后续可替换）
"""

import logging
from typing import Optional

try:
    from nltk.sentiment import SentimentIntensityAnalyzer
    import nltk

    # 确保 VADER 词典已下载
    try:
        nltk.data.find("sentiment/vader_lexicon.zip")
    except LookupError:
        nltk.download("vader_lexicon", quiet=True)

    _VADER_AVAILABLE = True
except ImportError:
    _VADER_AVAILABLE = False


logger = logging.getLogger(__name__)


class SentimentAnalyzer:
    """
    财经新闻情感分析器

    使用方法:
        analyzer = SentimentAnalyzer()
        score = analyzer.analyze("Fed raises interest rates by 25bp")
        print(score)  # {"compound": -0.34, "label": "NEGATIVE", "pos": 0.1, "neg": 0.2, "neu": 0.7}
    """

    # 金融领域专用词调整（VADER 可能对某些财经术语识别不准）
    FINANCIAL_LEXICON = {
        # 正面财经词汇
        "bullish": 2.5, "rally": 2.0, "upgrade": 2.0, "outperform": 2.0,
        "beat": 1.5, "surge": 2.0, "soar": 2.0, "boom": 2.0,
        "easing": 1.5, "stimulus": 1.5, "dovish": 1.0, "soft landing": 2.0,
        "buyback": 1.0, "dividend increase": 1.5, "guidance raise": 2.0,
        # 负面财经词汇
        "bearish": -2.5, "crash": -3.0, "plunge": -2.5, "rout": -2.5,
        "downgrade": -2.0, "underperform": -2.0, "miss": -1.5,
        "hawkish": -1.5, "tightening": -1.5, "recession": -2.5,
        "sell-off": -2.0, "selloff": -2.0, "tariff": -1.5,
        "trade war": -2.0, "sanctions": -1.5, "default": -3.0,
        "layoff": -2.0, "bankruptcy": -3.0, "probe": -1.0,
        "volatility spike": -2.0, "inflation surge": -2.0,
        "rate hike": -1.0, "debt crisis": -2.5,
    }

    def __init__(self):
        if _VADER_AVAILABLE:
            self._sia = SentimentIntensityAnalyzer()
            # 注入金融专用词汇
            self._sia.lexicon.update(self.FINANCIAL_LEXICON)
            logger.info("VADER 情感分析器已就绪（含金融词库）")
        else:
            self._sia = None
            logger.warning("VADER 不可用，情感分析将返回中性值")

    def analyze(self, text: str) -> dict:
        """
        分析文本情感

        Args:
            text: 新闻标题或正文

        Returns:
            {
                "compound": float,   # 综合得分 [-1, 1]，正=利好，负=利空
                "pos": float,        # 正面概率
                "neg": float,        # 负面概率
                "neu": float,        # 中性概率
                "label": str,        # POSITIVE / NEGATIVE / NEUTRAL
            }
        """
        if not self._sia or not text:
            return {"compound": 0.0, "pos": 0.0, "neg": 0.0, "neu": 1.0, "label": "NEUTRAL"}

        scores = self._sia.polarity_scores(text)

        # 标签
        if scores["compound"] >= 0.15:
            label = "POSITIVE"
        elif scores["compound"] <= -0.15:
            label = "NEGATIVE"
        else:
            label = "NEUTRAL"

        return {**scores, "label": label}

    def analyze_articles(self, articles: list[dict]) -> list[dict]:
        """
        批量分析新闻情感

        Args:
            articles: 新闻列表（需含 title 和 summary 字段）

        Returns:
            附带 sentiment 字段的新闻列表
        """
        for a in articles:
            text = f"{a.get('title', '')} {a.get('summary', '')}"
            a["sentiment"] = self.analyze(text)
        return articles

    def market_mood(self, articles: list[dict]) -> dict:
        """
        计算市场整体情绪

        Args:
            articles: 已附带 sentiment 的新闻列表

        Returns:
            {
                "mood": str,           # RISK_ON / NEUTRAL / RISK_OFF
                "avg_compound": float, # 平均综合得分
                "positive_count": int,
                "negative_count": int,
                "critical_issues": list[str],  # 需要关注的重大问题
            }
        """
        if not articles:
            return {"mood": "NEUTRAL", "avg_compound": 0.0,
                    "positive_count": 0, "negative_count": 0, "critical_issues": []}

        compounds = []
        pos_count = 0
        neg_count = 0
        critical = []

        for a in articles:
            s = a.get("sentiment", {})
            c = s.get("compound", 0.0)
            compounds.append(c)

            if c >= 0.15:
                pos_count += 1
            elif c <= -0.15:
                neg_count += 1

            # 极端负面 → 标记为关键问题
            if c <= -0.4:
                critical.append(a["title"])

        avg = sum(compounds) / len(compounds)

        if avg <= -0.2:
            mood = "RISK_OFF"
        elif avg >= 0.2:
            mood = "RISK_ON"
        else:
            mood = "NEUTRAL"

        return {
            "mood": mood,
            "avg_compound": round(avg, 3),
            "positive_count": pos_count,
            "negative_count": neg_count,
            "critical_issues": critical,
        }
