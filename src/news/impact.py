"""
事件影响评估模块
分析新闻对持仓和市场的影响，生成风险调整建议
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Optional

from src.news.fetcher import NewsFetcher
from src.news.sentiment import SentimentAnalyzer
from src.notify.serverchan import ServerChanNotifier
from src.notify.email import EmailNotifier
from src.config import get_env, ROOT_DIR

logger = logging.getLogger(__name__)


class AlertLevel(str, Enum):
    """告警等级"""
    CRITICAL = "critical"   # 🔴 重大事件，建议立即关注
    WARNING = "warning"     # 🟡 值得关注，可能影响持仓
    INFO = "info"           # 🟢 常规信息


class NewsImpactReport:
    """
    新闻影响评估报告

    包含:
        - 重要新闻列表（含情感评分）
        - 市场情绪总览
        - 风险调整建议
        - 推送就绪的摘要文本
    """

    def __init__(self):
        self.timestamp = datetime.now()
        self.articles: list[dict] = []
        self.mood: dict = {}
        self.critical: list[dict] = []
        self.warnings: list[dict] = []
        self.risk_adjustment: str = ""
        self.recommended_actions: list[str] = []


class ImpactAnalyzer:
    """
    新闻影响分析器 — 一站式：获取 → 分析 → 评估 → 推送

    使用方法:
        analyzer = ImpactAnalyzer()
        report = analyzer.assess()

        # 推送到微信
        analyzer.push_report(report, channel="serverchan")

        # 或只获取摘要文本
        print(analyzer.format_report(report))
    """

    def __init__(self):
        self.fetcher = NewsFetcher()
        self.sentiment = SentimentAnalyzer()

    def assess(self, min_score: int = 2) -> NewsImpactReport:
        """
        执行完整的新闻影响评估

        Returns:
            NewsImpactReport 包含所有分析结果
        """
        report = NewsImpactReport()

        # 1. 获取并过滤重要新闻
        articles, _ = self.fetcher.get_important_news(min_score=min_score)

        # 2. 情感分析
        articles = self.sentiment.analyze_articles(articles)

        # 3. 市场情绪
        mood = self.sentiment.market_mood(articles)

        # 4. 分类
        critical = []
        warnings = []
        for a in articles:
            s = a.get("sentiment", {})
            if s.get("compound", 0) <= -0.4:
                a["alert_level"] = AlertLevel.CRITICAL
                critical.append(a)
            elif s.get("compound", 0) <= -0.15:
                a["alert_level"] = AlertLevel.WARNING
                warnings.append(a)
            else:
                a["alert_level"] = AlertLevel.INFO

        # 5. 生成建议
        risk_adj, actions = self._generate_advice(mood, critical)

        report.articles = articles
        report.mood = mood
        report.critical = critical
        report.warnings = warnings
        report.risk_adjustment = risk_adj
        report.recommended_actions = actions

        return report

    def format_report(self, report: NewsImpactReport, max_articles: int = 8) -> str:
        """
        格式化为推送就绪的 Markdown 文本
        """
        mood = report.mood
        mood_emoji = {"RISK_ON": "🟢", "NEUTRAL": "🟡", "RISK_OFF": "🔴"}
        mood_text = {"RISK_ON": "风险偏好", "NEUTRAL": "中性", "RISK_OFF": "避险情绪"}

        lines = [
            f"📰 **市场新闻快报**",
            f"⏰ {report.timestamp.strftime('%Y-%m-%d %H:%M')}",
            "",
            f"## 市场情绪: {mood_emoji.get(mood['mood'], '🟡')} {mood_text.get(mood['mood'], '中性')}",
            f"综合情感得分: {mood['avg_compound']:.2f}  "
            f"(正面 {mood['positive_count']} | 负面 {mood['negative_count']})",
            "",
        ]

        # 风险调整建议
        if report.risk_adjustment:
            lines.append("## ⚠️ 风险提示")
            lines.append(report.risk_adjustment)
            lines.append("")

        # 紧急告警
        if report.critical:
            lines.append("## 🔴 重大事件")
            for a in report.critical[:3]:
                s = a.get("sentiment", {})
                lines.append(f"- **{a['title']}**")
                lines.append(f"  情感: {s.get('compound', 0):.2f} | {', '.join(a.get('categories', []))}")
            lines.append("")

        # 需要关注
        if report.warnings:
            lines.append("## 🟡 值得关注")
            for a in report.warnings[:5]:
                s = a.get("sentiment", {})
                lines.append(f"- {a['title']}")
                lines.append(f"  情感: {s.get('compound', 0):.2f} | {', '.join(a.get('categories', []))}")
            lines.append("")

        # 重要新闻
        lines.append("## 📋 重要新闻列表")
        for i, a in enumerate(report.articles[:max_articles], 1):
            s = a.get("sentiment", {})
            emoji = {"POSITIVE": "📈", "NEGATIVE": "📉", "NEUTRAL": "➡️"}
            e = emoji.get(s.get("label", "NEUTRAL"), "➡️")
            lines.append(f"{i}. {e} {a['title']}")
            if a.get("summary"):
                lines.append(f"   {a['summary'][:100]}...")
            lines.append(f"   🏷 {', '.join(a.get('categories', []))} | 来源: {a.get('source', '')}")
            lines.append("")

        # 建议操作
        if report.recommended_actions:
            lines.append("## 💡 建议")
            for action in report.recommended_actions:
                lines.append(f"- {action}")

        return "\n".join(lines)

    def push_report(
        self,
        report: NewsImpactReport,
        channels: list[str] = None,
    ) -> dict[str, bool]:
        """
        通过多个渠道推送报告

        Args:
            report: 影响评估报告
            channels: ["serverchan", "email"] 或 None（使用 .env 配置的渠道）

        Returns:
            {"serverchan": True/False, "email": True/False}
        """
        if channels is None:
            channels = ["serverchan", "email"]

        summary = self.format_report(report)
        results = {}

        # Server酱 → 个人微信
        if "serverchan" in channels:
            send_key = get_env("SERVERCHAN_SEND_KEY", "")
            sc = ServerChanNotifier(send_key)
            if sc.is_configured:
                # 紧急消息用醒目标题
                if report.critical:
                    title = f"🔴 市场警报: {report.critical[0]['title'][:50]}"
                elif report.mood.get("mood") == "RISK_OFF":
                    title = f"⚠️ 市场避险 — {report.mood.get('avg_compound', 0):.2f}"
                else:
                    title = f"📰 市场快报 — {report.timestamp.strftime('%H:%M')}"

                results["serverchan"] = sc.send(title, summary)
            else:
                results["serverchan"] = False

        # 邮件
        if "email" in channels:
            smtp_user = get_env("SMTP_USER", "")
            smtp_password = get_env("SMTP_PASSWORD", "")
            smtp_host = get_env("SMTP_HOST", "smtp.qq.com")
            smtp_port = int(get_env("SMTP_PORT", "587"))

            if smtp_user and smtp_password:
                notifier = EmailNotifier(smtp_host, smtp_port, smtp_user, smtp_password)
                subject = f"IBKR 市场快报 — {report.timestamp.strftime('%Y-%m-%d %H:%M')}"
                # 转 Markdown 为简单 HTML
                html_body = summary.replace("\n", "<br>")
                results["email"] = notifier.send(subject, html_body, html=True)
            else:
                results["email"] = False

        return results

    # ------------------------------------------------------------------
    # 建议生成
    # ------------------------------------------------------------------

    def _generate_advice(self, mood: dict, critical: list[dict]) -> tuple[str, list[str]]:
        """根据情绪和严重事件生成风险建议"""
        risk_adj = ""
        actions = []

        if mood["mood"] == "RISK_OFF":
            risk_adj = "⚠️ 市场处于**避险模式**，建议降低风险敞口"
            actions = [
                "减少新开仓，优先处理已有持仓",
                "检查止损单是否在合适位置",
                "关注防御性板块（公共事业、消费品）",
            ]
        elif mood["mood"] == "RISK_ON":
            risk_adj = "市场情绪偏乐观，但仍需注意仓位管理"
            actions = [
                "可适度参与，但不要追高",
                "保持止损纪律",
            ]

        # 针对具体事件
        if critical:
            for c in critical[:2]:
                cats = c.get("categories", [])
                title = c["title"]

                if "货币政策" in cats:
                    actions.append(f"关注利率敏感持仓，事件: {title[:60]}")
                if "地缘政治" in cats:
                    risk_adj = "🔴 **地缘政治风险升温**，建议减仓观望"
                    actions.append("地缘冲突期间市场不确定性极高，建议降低仓位")
                if "市场波动" in cats:
                    actions.append("波动加剧期间，注意控制单笔仓位大小")

        if not actions:
            actions = ["当前无特殊操作建议，按既定策略执行"]

        return risk_adj, actions
