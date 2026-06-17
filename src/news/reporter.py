"""
新闻报告生成器（中文版）
集成：翻译 + 影响因子计算 + 利好利空分类 + 持仓感知 + 多渠道推送

核心输出: 中文 Markdown 市场快报
"""

import logging
import re
from datetime import datetime
from typing import Optional

from src.news.fetcher import NewsFetcher, KEYWORD_CATEGORIES
from src.news.sentiment import SentimentAnalyzer
from src.news.translator import (
    NewsTranslator, REPORT_HEADER, SECTION_MARKET, SECTION_BEARISH,
    SECTION_BULLISH, SECTION_HOLDINGS, SECTION_ADVICE, SECTION_FOOTER,
    MOOD_LABELS, CATEGORY_LABELS_ZH, impact_label,
)
from src.notify.serverchan import ServerChanNotifier
from src.notify.email import EmailNotifier
from src.config import get_env, ROOT_DIR

logger = logging.getLogger(__name__)


# ============================================================
# 影响因子计算
# ============================================================

# 各类别的市场影响权重
CATEGORY_WEIGHTS = {
    "货币政策": 1.3,
    "地缘政治": 1.2,
    "市场波动": 1.1,
    "宏观经济": 1.0,
    "科技监管": 0.9,
}


def calculate_impact(article: dict) -> float:
    """
    计算新闻对股市的影响因子

    公式: 情感强度 × 相关性得分 × 类别权重 × 标题加成

    Returns:
        影响因子 [-10, 10]，正=利好，负=利空
    """
    sentiment = article.get("sentiment", {})
    compound = sentiment.get("compound", 0.0)  # [-1, 1]

    # 相关性得分: 关键词命中数归一化
    relevance = article.get("relevance_score", 0)  # 原始命中数
    relevance_norm = min(relevance / 8.0, 1.0)  # 归一化到 [0, 1]

    # 类别权重: 取最高权重
    categories = article.get("categories", [])
    max_cat_weight = 1.0
    for cat in categories:
        if cat in CATEGORY_WEIGHTS:
            max_cat_weight = max(max_cat_weight, CATEGORY_WEIGHTS[cat])

    # 标题加成: 标题命中关键词 +20%
    title_bonus = 1.0
    title_text = article.get("title", "").lower()
    for category, keywords in KEYWORD_CATEGORIES.items():
        for kw in keywords:
            if kw.lower() in title_text:
                title_bonus = 1.2
                break

    # 最终影响因子
    impact = compound * 10.0 * (0.3 + 0.7 * relevance_norm) * max_cat_weight * title_bonus

    # 钳制到 [-10, 10]
    return round(max(-10.0, min(10.0, impact)), 1)


def classify_articles(articles: list[dict]) -> dict:
    """
    按利好/利空分类，并按影响因子绝对值排序

    Returns:
        {
            "bullish": [...],   # 利好，impact > 0，降序
            "bearish": [...],   # 利空，impact < 0，升序（最负面在前）
            "neutral": [...],   # 中性，impact ≈ 0
        }
    """
    bullish = []
    bearish = []
    neutral = []

    for a in articles:
        impact = a.get("impact_factor", 0.0)
        if impact >= 1.0:
            bullish.append(a)
        elif impact <= -1.0:
            bearish.append(a)
        else:
            neutral.append(a)

    # 利好按影响因子降序（最强利好在前）
    bullish.sort(key=lambda x: x.get("impact_factor", 0), reverse=True)
    # 利空按影响因子升序（最强利空在前）
    bearish.sort(key=lambda x: x.get("impact_factor", 0))
    # 中性按绝对值降序
    neutral.sort(key=lambda x: abs(x.get("impact_factor", 0)), reverse=True)

    return {"bullish": bullish, "bearish": bearish, "neutral": neutral}


def market_impact_summary(articles: list[dict]) -> dict:
    """
    市场影响总览

    Returns:
        {
            "total_impact": float,       # 综合影响因子
            "bullish_count": int,
            "bearish_count": int,
            "dominant_direction": str,   # "偏多" / "偏空" / "中性"
            "top_events": list[dict],    # 影响最大的 3 个事件
        }
    """
    if not articles:
        return {"total_impact": 0.0, "bullish_count": 0, "bearish_count": 0,
                "dominant_direction": "中性", "top_events": []}

    impacts = [a.get("impact_factor", 0.0) for a in articles]
    total = round(sum(impacts), 2)

    bullish_count = sum(1 for i in impacts if i >= 1.0)
    bearish_count = sum(1 for i in impacts if i <= -1.0)

    if total >= 3.0:
        direction = "偏多 ↑"
    elif total <= -3.0:
        direction = "偏空 ↓"
    else:
        direction = "中性 →"

    # Top 3 按影响绝对值排序
    sorted_by_abs = sorted(articles, key=lambda a: abs(a.get("impact_factor", 0)), reverse=True)

    return {
        "total_impact": total,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "dominant_direction": direction,
        "top_events": sorted_by_abs[:3],
    }


# ============================================================
# 报告生成器
# ============================================================

class MarketReporter:
    """
    市场报告生成器（中文版）

    使用方法:
        reporter = MarketReporter()
        report = reporter.generate(holdings=["AAPL", "KO", "AMD", "INTC"])
        reporter.push_email(report)
    """

    def __init__(self):
        self.fetcher = NewsFetcher()
        self.sentiment = SentimentAnalyzer()
        self.translator = NewsTranslator()

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def generate(
        self,
        holdings: list[str] = None,
        holdings_data: list[dict] = None,
        account_summary: dict = None,
        min_score: int = 2,
    ) -> str:
        """
        生成完整中文市场快报

        Args:
            holdings: 持仓股票代码列表，如 ["AAPL", "KO"]
            holdings_data: 持仓详情 [{"symbol": "AAPL", "position": 10, "avg_cost": 180}, ...]
            account_summary: 账户摘要 {"net_liq": 8827, "available_funds": 8250, ...}
            min_score: 最低相关性分数

        Returns:
            中文 Markdown 格式报告
        """
        holdings = holdings or []
        holdings_data = holdings_data or []
        account_summary = account_summary or {}

        # 1. 获取并分析通用新闻
        all_news, _ = self.fetcher.get_important_news(min_score=min_score)
        all_news = self.sentiment.analyze_articles(all_news)

        # 计算影响因子
        for a in all_news:
            a["impact_factor"] = calculate_impact(a)

        # 2. 分类
        classified = classify_articles(all_news)
        summary = market_impact_summary(all_news)

        # 3. 持仓公司新闻
        holdings_news = {}
        if holdings:
            holdings_news = self._fetch_holdings_news(holdings)

        # 4. 组装报告
        report = self._build_report(
            summary=summary,
            classified=classified,
            holdings_news=holdings_news,
            holdings_data=holdings_data,
            account_summary=account_summary,
        )

        return report

    # ------------------------------------------------------------------
    # 持仓公司新闻
    # ------------------------------------------------------------------

    def _fetch_holdings_news(self, symbols: list[str]) -> dict[str, list[dict]]:
        """
        为每只持仓股票搜索相关新闻

        Returns:
            {"AAPL": [...], "KO": [...]}
        """
        result = {}
        for sym in symbols:
            sym_upper = sym.upper()
            company_articles = []

            # 从通用新闻中筛选包含该股票名的
            all_news = self.fetcher.fetch_all(max_per_source=5)
            for a in all_news:
                text = f"{a['title']} {a['summary']}".lower()
                if sym_upper.lower() in text:
                    a = self.sentiment.analyze_articles([a])[0]
                    a["impact_factor"] = calculate_impact(a)
                    company_articles.append(a)

            result[sym_upper] = sorted(
                company_articles,
                key=lambda x: abs(x.get("impact_factor", 0)),
                reverse=True,
            )[:5]  # 每只股票最多 5 条

        return result

    # ------------------------------------------------------------------
    # 报告组装
    # ------------------------------------------------------------------

    def _build_report(
        self,
        summary: dict,
        classified: dict,
        holdings_news: dict[str, list],
        holdings_data: list[dict],
        account_summary: dict,
    ) -> str:
        """组装完整中文报告"""
        now = datetime.now()
        lines = []

        # ===== 头部 =====
        lines.append(REPORT_HEADER)
        lines.append(f"⏰ {now.strftime('%Y年%m月%d日 %H:%M')} (美东)")
        lines.append("")

        # ===== 账户概览（如果有）=====
        if account_summary:
            net = account_summary.get("net_liquidation", 0)
            lines.append(f"💼 账户净值: **${net:,.2f}**")
            if holdings_data:
                pos_str = " · ".join(
                    f"{h.get('symbol', '')} ({h.get('position', 0):.0f}股)"
                    for h in holdings_data if h.get("position", 0) != 0
                )
                if pos_str:
                    lines.append(f"📦 持仓: {pos_str}")
            lines.append("")

        lines.append("━" * 35)

        # ===== 市场总览 =====
        lines.append(f"## {SECTION_MARKET}")
        lines.append("")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 市场情绪 | {MOOD_LABELS.get(summary.get('dominant_direction', ''), summary.get('dominant_direction', ''))} |")
        lines.append(f"| 综合影响因子 | **{summary['total_impact']:+.1f}** |")
        lines.append(f"| 利好事件 | {summary['bullish_count']} 条 |")
        lines.append(f"| 利空事件 | {summary['bearish_count']} 条 |")
        lines.append("")

        # 影响最大的事件
        if summary["top_events"]:
            lines.append("**影响最大的事件：**")
            for e in summary["top_events"]:
                impact = e.get("impact_factor", 0)
                sign = "+" if impact > 0 else ""
                title_cn = self.translator.translate_title(e["title"])
                lines.append(f"- {impact_label(impact)} [{sign}{impact:.1f}] {title_cn}")
            lines.append("")

        lines.append("━" * 35)

        # ===== 重大利空 =====
        bearish = classified.get("bearish", [])
        if bearish:
            lines.append(f"## {SECTION_BEARISH}")
            lines.append("")
            for i, a in enumerate(bearish[:8], 1):
                lines.extend(self._format_article(i, a))
            lines.append("")
        else:
            lines.append(f"## {SECTION_BEARISH}")
            lines.append("")
            lines.append("> ✅ 当前无重大利空事件")
            lines.append("")

        lines.append("━" * 35)

        # ===== 重大利好 =====
        bullish = classified.get("bullish", [])
        if bullish:
            lines.append(f"## {SECTION_BULLISH}")
            lines.append("")
            for i, a in enumerate(bullish[:8], 1):
                lines.extend(self._format_article(i, a))
            lines.append("")
        else:
            lines.append(f"## {SECTION_BULLISH}")
            lines.append("")
            lines.append("> ℹ️ 当前无重大利好事件")
            lines.append("")

        # ===== 持仓动态 =====
        if holdings_news:
            lines.append("━" * 35)
            lines.append(f"## {SECTION_HOLDINGS}")
            lines.append("")

            has_any_news = False
            for sym, articles in holdings_news.items():
                important = [a for a in articles if abs(a.get("impact_factor", 0)) >= 2.0]
                if important:
                    has_any_news = True
                    lines.append(f"### 🔹 {sym}")
                    for a in important[:3]:
                        impact = a.get("impact_factor", 0)
                        sign = "+" if impact > 0 else ""
                        cat = "利好" if impact > 0 else "利空"
                        title_cn = self.translator.translate_title(a["title"])
                        lines.append(f"- {cat} [{sign}{impact:.1f}] {title_cn}")
                        if a.get("link"):
                            lines.append(f"  🔗 {a['link']}")
                    lines.append("")

                elif articles:
                    lines.append(f"### 🔹 {sym}")
                    lines.append("> 暂无重大新闻，常规动态正常")
                    lines.append("")

            if not has_any_news:
                lines.append("> ✅ 持仓公司暂无重大新闻，基本面平稳")
                lines.append("")
        else:
            lines.append("━" * 35)
            lines.append(f"## {SECTION_HOLDINGS}")
            lines.append("")
            lines.append("> ℹ️ 无持仓数据，跳过持仓分析")
            lines.append("")

        # ===== 操作建议 =====
        lines.append("━" * 35)
        lines.append(f"## {SECTION_ADVICE}")
        lines.append("")
        advice = self._generate_advice(summary, classified, holdings_news)
        for item in advice:
            lines.append(f"- {item}")
        lines.append("")

        # ===== 免责声明 =====
        lines.append("━" * 35)
        lines.append(f"*{SECTION_FOOTER}：以上分析由 AI 自动生成，仅供参考，不构成投资建议。*")
        lines.append(f"*投资决策请结合自身风险承受能力独立判断。*")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 格式化单条新闻
    # ------------------------------------------------------------------

    def _format_article(self, index: int, article: dict) -> list[str]:
        """格式化单条新闻为中文 Markdown"""
        impact = article.get("impact_factor", 0)
        sign = "+" if impact > 0 else ""
        title_cn = self.translator.translate_title(article["title"])
        summary_cn = self.translator.translate_summary(
            article.get("summary", "")[:150]
        )

        categories = article.get("categories", [])
        cat_tags = " · ".join(
            CATEGORY_LABELS_ZH.get(c, c) for c in categories[:3]
        )

        lines = [
            f"**{index}. [{impact_label(impact)}·影响 {sign}{impact:.1f}] {title_cn}**",
        ]
        if summary_cn and summary_cn != article.get("summary", ""):
            lines.append(f"   📝 {summary_cn}")
        lines.append(f"   {cat_tags} | 来源: {article.get('source', '')}")
        if article.get("link"):
            lines.append(f"   🔗 {article['link']}")
        lines.append("")
        return lines

    # ------------------------------------------------------------------
    # 操作建议
    # ------------------------------------------------------------------

    def _generate_advice(
        self,
        summary: dict,
        classified: dict,
        holdings_news: dict[str, list],
    ) -> list[str]:
        """生成中文操作建议"""
        advice = []

        total_impact = summary.get("total_impact", 0)
        bearish = classified.get("bearish", [])

        # 整体仓位建议
        if total_impact <= -5.0:
            advice.append("⚠️ 综合影响因子偏空，建议**降低总仓位至 50% 以下**，等待不确定性消退")
        elif total_impact <= -2.0:
            advice.append("📌 市场存在一定下行风险，建议**控制新开仓节奏**，收紧止损线")
        elif total_impact >= 5.0:
            advice.append("📈 市场情绪积极，可适度参与，注意**勿追高**，严格止损")
        elif total_impact >= 2.0:
            advice.append("✅ 市场情绪偏积极，按既定策略执行即可")
        else:
            advice.append("➡️ 市场方向不明朗，建议**观望为主**，减少交易频率")

        # 针对利空事件
        if bearish:
            has_geo = any("地缘政治" in a.get("categories", []) for a in bearish)
            has_policy = any("货币政策" in a.get("categories", []) for a in bearish)
            has_tech = any("科技监管" in a.get("categories", []) for a in bearish)

            if has_geo:
                advice.append("🌍 地缘政治风险升温，对**出口导向型、半导体**等板块影响较大，注意相关持仓")
            if has_policy:
                advice.append("🏦 货币政策存在不确定性，**利率敏感型资产**（债券、REITs、成长股）需密切关注")
            if has_tech:
                advice.append("🔧 科技监管动态值得关注，**AI/芯片**相关持仓波动可能加剧")

        # 持仓建议
        if holdings_news:
            for sym, articles in holdings_news.items():
                bearish_articles = [a for a in articles if a.get("impact_factor", 0) <= -2.0]
                bullish_articles = [a for a in articles if a.get("impact_factor", 0) >= 2.0]

                if bearish_articles:
                    advice.append(f"🔴 **{sym}** 出现利空消息，建议检查止损位，或考虑减仓")
                if bullish_articles:
                    advice.append(f"🟢 **{sym}** 有利好消息，可关注但不宜追高")

        # 止损提醒
        if total_impact <= -3.0:
            advice.append("🛡️ 当前环境建议**收紧止损位至 -3%**，保护本金安全")

        return advice

    # ------------------------------------------------------------------
    # 推送
    # ------------------------------------------------------------------

    def push_email(self, report: str, subject: str = "") -> bool:
        """通过 QQ邮箱推送报告"""
        smtp_user = get_env("SMTP_USER", "")
        smtp_password = get_env("SMTP_PASSWORD", "")
        smtp_host = get_env("SMTP_HOST", "smtp.qq.com")
        smtp_port = int(get_env("SMTP_PORT", "587"))

        if not smtp_user or not smtp_password:
            logger.warning("邮箱未配置，无法推送")
            return False

        notifier = EmailNotifier(smtp_host, smtp_port, smtp_user, smtp_password)

        if not subject:
            now = datetime.now()
            # 根据影响因子生成标题
            subject = f"IBKR 市场快报 — {now.strftime('%m/%d %H:%M')}"

        html = report.replace("\n", "<br>")
        return notifier.send(subject, html, html=True)

    def push_serverchan(self, report: str) -> bool:
        """通过 Server酱推送到微信"""
        send_key = get_env("SERVERCHAN_SEND_KEY", "")
        sc = ServerChanNotifier(send_key)

        if not sc.is_configured:
            return False

        # 提取标题（第一行）
        first_line = report.split("\n")[0] if report else "市场快报"
        return sc.send(first_line, report)
