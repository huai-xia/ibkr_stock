"""
新闻获取模块
多源聚合：RSS（免费）+ Finnhub API（可选）

设计思路：
  - 优先使用免费 RSS 源，无需 API Key 即可使用
  - Finnhub 作为补充（需要 API Key）
  - 关键词过滤 + 去重排序
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import feedparser
import requests

from src.config import FINNHUB_API_KEY

logger = logging.getLogger(__name__)


# ============================================================
# 新闻源配置
# ============================================================

# 财经 RSS 源（免费，无需 API Key）
RSS_FEEDS = {
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "CNBC Top News": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    "MarketWatch": "https://feeds.marketwatch.com/marketwatch/topstories",
    "Reuters Business": "https://news.google.com/rss/search?q=site:reuters.com+business&hl=en-US&gl=US&ceid=US:en",
    "WSJ Markets": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "Bloomberg": "https://news.google.com/rss/search?q=site:bloomberg.com+markets&hl=en-US&gl=US&ceid=US:en",
}

# 关键词分类
KEYWORD_CATEGORIES = {
    "货币政策": [
        "fed ", "federal reserve", "fomc", "powell", "rate hike", "rate cut",
        "interest rate", "monetary policy", "inflation", "cpi", "ppi",
        "central bank", "boj", "boe", "ecb", "bank of japan",
        "hawkish", "dovish", "quantitative", "taper", "yield curve",
    ],
    "地缘政治": [
        "tariff", "trade war", "sanctions", "geopolitical", "china", "russia",
        "ukraine", "taiwan", "nato", "middle east", "iran", "north korea",
        "export control", "chip ban", "decoupling",
    ],
    "宏观经济": [
        "gdp", "recession", "employment", "jobless", "unemployment",
        "manufacturing", "pmi", "consumer confidence", "retail sales",
        "housing", "debt ceiling", "government shutdown",
    ],
    "科技监管": [
        "ai regulation", "antitrust", "doj", "ftc", "eu regulation",
        "section 230", "chip ", "semiconductor", "nvidia", "apple",
        "microsoft", "google", "amazon", "meta", "tesla",
    ],
    "市场波动": [
        "crash", "plunge", "surge", "rout", "sell-off", "selloff",
        "bear market", "bull market", "correction", "volatility", "vix",
        "circuit breaker", "trading halt",
    ],
}


class NewsFetcher:
    """
    多源新闻获取器

    使用方法:
        fetcher = NewsFetcher()
        articles = fetcher.fetch_all(max_per_source=10)
        important = fetcher.filter_important(articles)
        summary = fetcher.summarize(important)
    """

    def __init__(self, finnhub_key: str = ""):
        self._finnhub_key = finnhub_key or FINNHUB_API_KEY

    # ------------------------------------------------------------------
    # RSS 获取
    # ------------------------------------------------------------------

    def fetch_rss(self, source_name: str, feed_url: str, max_items: int = 10) -> list[dict]:
        """从单个 RSS 源获取新闻"""
        articles = []
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:max_items]:
                article = {
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "summary": self._clean_summary(entry.get("summary", "")),
                    "published": entry.get("published", ""),
                    "source": source_name,
                    "type": "rss",
                }
                articles.append(article)
            logger.debug("RSS [%s]: %d 条", source_name, len(articles))
        except Exception as e:
            logger.warning("RSS 获取失败 [%s]: %s", source_name, e)

        return articles

    def fetch_all_rss(self, max_per_source: int = 10) -> list[dict]:
        """获取所有 RSS 源的新闻"""
        all_articles = []
        for name, url in RSS_FEEDS.items():
            articles = self.fetch_rss(name, url, max_per_source)
            all_articles.extend(articles)
            # 避免请求过快
            time.sleep(0.5)

        # 去重（按标题相似度）
        return self._deduplicate(all_articles)

    # ------------------------------------------------------------------
    # Finnhub API 获取（可选）
    # ------------------------------------------------------------------

    def fetch_finnhub(self, category: str = "general") -> list[dict]:
        """从 Finnhub 获取新闻"""
        if not self._finnhub_key or "YOUR_KEY" in self._finnhub_key:
            logger.debug("Finnhub API Key 未配置，跳过")
            return []

        articles = []
        try:
            url = "https://finnhub.io/api/v1/news"
            params = {
                "category": category,
                "token": self._finnhub_key,
            }
            resp = requests.get(url, params=params, timeout=15)
            data = resp.json()

            for item in data[:20]:
                article = {
                    "title": item.get("headline", ""),
                    "link": item.get("url", ""),
                    "summary": item.get("summary", ""),
                    "published": datetime.fromtimestamp(
                        item.get("datetime", 0)
                    ).isoformat() if item.get("datetime") else "",
                    "source": item.get("source", "Finnhub"),
                    "type": "api",
                }
                articles.append(article)
            logger.debug("Finnhub: %d 条", len(articles))
        except Exception as e:
            logger.warning("Finnhub 获取失败: %s", e)

        return articles

    # ------------------------------------------------------------------
    # 获取与过滤
    # ------------------------------------------------------------------

    def fetch_all(self, max_per_source: int = 10) -> list[dict]:
        """获取所有来源的新闻"""
        rss = self.fetch_all_rss(max_per_source)
        finnhub = self.fetch_finnhub()
        return self._deduplicate(rss + finnhub)

    def filter_important(self, articles: list[dict]) -> list[dict]:
        """
        按关键词过滤重要新闻，并标注类别和重要性

        Returns:
            排序后的重要新闻列表（含 relevance_score 和 categories）
        """
        scored = []
        for a in articles:
            text = f"{a['title']} {a['summary']}".lower()
            score = 0
            matched_categories = []

            for category, keywords in KEYWORD_CATEGORIES.items():
                cat_score = 0
                for kw in keywords:
                    if kw.lower() in text:
                        cat_score += 1
                if cat_score > 0:
                    matched_categories.append(category)
                    score += cat_score

            # 标题命中额外加分
            title_lower = a["title"].lower()
            for category, keywords in KEYWORD_CATEGORIES.items():
                for kw in keywords:
                    if kw.lower() in title_lower:
                        score += 2  # 标题命中权重更高

            if score > 0:
                a["relevance_score"] = score
                a["categories"] = matched_categories
                scored.append(a)

        # 按相关性排序
        scored.sort(key=lambda x: x["relevance_score"], reverse=True)
        return scored

    def summarize(self, articles: list[dict], max_items: int = 10) -> str:
        """
        将重要新闻汇总为可推送的文本

        Args:
            articles: 已过滤的重要新闻
            max_items: 最多取几条

        Returns:
            格式化的 Markdown 摘要
        """
        if not articles:
            return "📰 当前无重要新闻"

        lines = [f"📰 **市场重要新闻速览** — {datetime.now().strftime('%m-%d %H:%M')}", ""]

        for i, a in enumerate(articles[:max_items], 1):
            cats = " · ".join(a.get("categories", []))
            lines.append(f"**{i}.** {a['title']}")
            summary = a.get("summary", "")[:120]
            if summary:
                lines.append(f"   {summary}...")
            if cats:
                lines.append(f"   🏷 {cats}")
            lines.append(f"   🔗 {a.get('link', '')}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 核心推送方法：获取 + 过滤 + 总结一条龙
    # ------------------------------------------------------------------

    def get_important_news(
        self, min_score: int = 2, max_items: int = 10,
    ) -> tuple[list[dict], str]:
        """
        一站式：获取所有新闻 → 过滤重要新闻 → 生成摘要

        Args:
            min_score: 最低相关性分数
            max_items: 最多返回条数

        Returns:
            (articles_list, markdown_summary)
        """
        all_news = self.fetch_all(max_per_source=10)
        important = self.filter_important(all_news)
        important = [a for a in important if a.get("relevance_score", 0) >= min_score]
        summary = self.summarize(important, max_items)
        return important, summary

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_summary(text: str) -> str:
        """清理 HTML 标签"""
        import re
        clean = re.sub(r"<[^>]+>", "", text)
        clean = re.sub(r"\s+", " ", clean)
        return clean.strip()[:300]

    @staticmethod
    def _deduplicate(articles: list[dict]) -> list[dict]:
        """简单去重（按标题前30字符相似度）"""
        seen = set()
        unique = []
        for a in articles:
            key = a["title"][:30].lower().strip()
            if key not in seen:
                seen.add(key)
                unique.append(a)
        return unique
