"""
交易画像生成器
基于历史交易数据，分析用户交易风格、强项、弱项、行为模式
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from src.trade.recorder import TradeRecorder

logger = logging.getLogger(__name__)


class TradingProfile:
    """
    交易画像

    使用方法:
        profile = TradingProfile("data/trade.db")
        report = profile.analyze(account="DU123456")
        print(profile.format(report))
    """

    def __init__(self, db_path: str = "data/trade.db"):
        self._recorder = TradeRecorder(db_path)

    # ------------------------------------------------------------------
    # 主分析入口
    # ------------------------------------------------------------------

    def analyze(self, account: str = "", days: int = 3650) -> dict:
        """
        生成完整交易画像

        Returns:
            {
                "summary": {...},             # 总览
                "monthly": [...],             # 月度趋势
                "by_symbol": {...},           # 按股票
                "by_day_of_week": {...},      # 按星期几
                "by_hour": {...},             # 按时段
                "holding": {...},             # 持仓周期分析
                "exit_behavior": {...},       # 退出行为分析
                "risk_metrics": {...},        # 风险指标
                "patterns": {...},            # 行为模式
                "strengths": [...],           # 优势
                "weaknesses": [...],          # 弱点
                "suggestions": [...],         # 改进建议
            }
        """
        trades = self._recorder.query(account=account, days=days, limit=10000)

        profile = {
            "summary": self._summary(trades),
            "monthly": self._monthly_trend(trades),
            "by_symbol": self._by_symbol(trades),
            "by_day_of_week": self._by_day_of_week(trades),
            "by_hour": self._by_hour(trades),
            "holding": self._holding_analysis(trades),
            "exit_behavior": self._exit_behavior(trades),
            "risk_metrics": self._risk_metrics(trades),
            "patterns": self._behavior_patterns(trades),
        }

        # 生成文字描述
        profile["strengths"] = self._identify_strengths(profile)
        profile["weaknesses"] = self._identify_weaknesses(profile)
        profile["suggestions"] = self._generate_suggestions(profile)

        return profile

    # ------------------------------------------------------------------
    # 各项分析
    # ------------------------------------------------------------------

    def _summary(self, trades: list[dict]) -> dict:
        """总览统计"""
        if not trades:
            return {"total_trades": 0}

        dates = sorted(set(t["timestamp"][:10] for t in trades))
        pnl_trades = [t for t in trades if t.get("pnl") is not None and t["pnl"] != 0]
        total_pnl = sum(t["pnl"] for t in pnl_trades)
        wins = [t for t in pnl_trades if t["pnl"] > 0]
        losses = [t for t in pnl_trades if t["pnl"] < 0]

        symbols = set(t["symbol"] for t in trades)
        accounts = set(t["account"] for t in trades)

        # 买入均价 vs 卖出均价
        buys = [t for t in trades if t["action"] == "BUY" and t.get("price")]
        sells = [t for t in trades if t["action"] == "SELL" and t.get("price")]

        return {
            "total_trades": len(trades),
            "unique_symbols": len(symbols),
            "accounts": sorted(accounts),
            "date_range": f"{dates[0]} ~ {dates[-1]}" if dates else "N/A",
            "trading_days": len(dates),
            "total_pnl": round(total_pnl, 2),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / (len(wins) + len(losses)) * 100, 1) if (wins or losses) else 0,
            "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss": round(abs(sum(t["pnl"] for t in losses)) / len(losses), 2) if losses else 0,
            "profit_factor": round(
                sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 2
            ) if losses and wins else 0,
            "avg_buy_price": round(sum(t["price"] for t in buys) / len(buys), 2) if buys else 0,
            "avg_sell_price": round(sum(t["price"] for t in sells) / len(sells), 2) if sells else 0,
            "largest_win": max(t["pnl"] for t in wins) if wins else 0,
            "largest_loss": min(t["pnl"] for t in losses) if losses else 0,
        }

    def _monthly_trend(self, trades: list[dict]) -> list[dict]:
        """月度趋势"""
        by_month = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0, "losses": 0, "symbols": set()})
        for t in trades:
            month = t["timestamp"][:7]
            by_month[month]["trades"] += 1
            by_month[month]["symbols"].add(t["symbol"])
            pnl = t.get("pnl") or 0
            if pnl > 0:
                by_month[month]["wins"] += 1
                by_month[month]["pnl"] += pnl
            elif pnl < 0:
                by_month[month]["losses"] += 1
                by_month[month]["pnl"] += pnl

        return [
            {
                "month": m,
                "trades": d["trades"],
                "pnl": round(d["pnl"], 2),
                "wins": d["wins"],
                "losses": d["losses"],
                "win_rate": round(d["wins"] / (d["wins"] + d["losses"]) * 100, 1) if (d["wins"] + d["losses"]) else 0,
                "symbols": len(d["symbols"]),
            }
            for m, d in sorted(by_month.items())
        ]

    def _by_symbol(self, trades: list[dict]) -> list[dict]:
        """按股票汇总"""
        by_sym = defaultdict(lambda: {"trades": 0, "buys": 0, "sells": 0, "pnl": 0, "wins": 0, "losses": 0,
                                       "day_trades": 0, "total_qty": 0})
        # 日内交易检测
        day_sym = defaultdict(set)
        for t in trades:
            sym = t["symbol"]
            by_sym[sym]["trades"] += 1
            by_sym[sym]["total_qty"] += t["quantity"]
            if t["action"] == "BUY":
                by_sym[sym]["buys"] += 1
            else:
                by_sym[sym]["sells"] += 1
            pnl = t.get("pnl") or 0
            if pnl > 0:
                by_sym[sym]["wins"] += 1
                by_sym[sym]["pnl"] += pnl
            elif pnl < 0:
                by_sym[sym]["losses"] += 1
                by_sym[sym]["pnl"] += pnl
            # 日内交易检测
            key = (t["timestamp"][:10], sym)
            day_sym[key].add(t["action"])
        for (day, sym), actions in day_sym.items():
            if "BUY" in actions and "SELL" in actions:
                by_sym[sym]["day_trades"] += 1

        return sorted(
            [{"symbol": s, **d, "pnl": round(d["pnl"], 2)} for s, d in by_sym.items()],
            key=lambda x: abs(x["pnl"]), reverse=True,
        )

    def _by_day_of_week(self, trades: list[dict]) -> dict:
        """按星期几分析"""
        days = ["周一", "周二", "周三", "周四", "周五"]
        by_day = defaultdict(lambda: {"trades": 0, "pnl": 0, "wins": 0})
        for t in trades:
            try:
                dt = datetime.fromisoformat(t["timestamp"])
                day_name = days[dt.weekday()]
                by_day[day_name]["trades"] += 1
                pnl = t.get("pnl") or 0
                by_day[day_name]["pnl"] += pnl
                if pnl > 0:
                    by_day[day_name]["wins"] += 1
            except:
                pass
        return {d: dict(by_day.get(d, {})) for d in days}

    def _by_hour(self, trades: list[dict]) -> dict:
        """按时段分析"""
        hours = defaultdict(lambda: {"trades": 0, "pnl": 0})
        for t in trades:
            try:
                h = int(t["timestamp"][11:13])
                label = "盘前" if h < 9 else ("开盘" if h < 11 else ("午间" if h < 14 else ("收盘" if h < 16 else "盘后")))
                hours[label]["trades"] += 1
                hours[label]["pnl"] += (t.get("pnl") or 0)
            except:
                pass
        return dict(hours)

    def _holding_analysis(self, trades: list[dict]) -> dict:
        """持仓周期分析（按配对买卖估计）"""
        # 按 (symbol, account) 配对买入和卖出
        by_key = defaultdict(list)
        for t in trades:
            key = (t["symbol"], t["account"])
            by_key[key].append(t)

        holding_periods = []
        for key, ts in sorted(by_key.items(), key=lambda x: x[1][0]["timestamp"]):
            buys = [t for t in ts if t["action"] == "BUY"]
            sells = [t for t in ts if t["action"] == "SELL"]
            for i, sell in enumerate(sells):
                if i < len(buys):
                    try:
                        buy_time = datetime.fromisoformat(buys[i]["timestamp"])
                        sell_time = datetime.fromisoformat(sell["timestamp"])
                        days_held = (sell_time - buy_time).total_seconds() / 86400
                        holding_periods.append({
                            "symbol": key[0],
                            "days": round(days_held, 1),
                            "pnl": sell.get("pnl") or 0,
                        })
                    except:
                        pass

        if not holding_periods:
            return {"avg_days": 0, "categories": {}}

        avg_days = sum(h["days"] for h in holding_periods) / len(holding_periods)

        # 按持仓周期分组
        categories = {"日内(0-1天)": [], "短线(2-7天)": [], "中线(8-30天)": [], "长线(30+天)": []}
        for h in holding_periods:
            if h["days"] <= 1:
                categories["日内(0-1天)"].append(h)
            elif h["days"] <= 7:
                categories["短线(2-7天)"].append(h)
            elif h["days"] <= 30:
                categories["中线(8-30天)"].append(h)
            else:
                categories["长线(30+天)"].append(h)

        cat_stats = {}
        for cat, items in categories.items():
            if items:
                cat_pnl = sum(x["pnl"] for x in items)
                cat_stats[cat] = {
                    "count": len(items),
                    "total_pnl": round(cat_pnl, 2),
                    "avg_pnl": round(cat_pnl / len(items), 2),
                }

        return {"avg_days": round(avg_days, 1), "categories": cat_stats}

    def _exit_behavior(self, trades: list[dict]) -> dict:
        """卖出行为分析——你是如何退出的"""
        sells = [t for t in trades if t["action"] == "SELL"]
        if not sells:
            return {}

        # 盈亏分布
        pnl_list = [t.get("pnl") for t in sells if t.get("pnl") is not None]
        if not pnl_list:
            return {}

        # 按盈亏百分比分桶
        buckets = {"大亏(<-10%)": 0, "亏损(-10~-3%)": 0, "小亏(-3~0%)": 0,
                    "小盈(0~3%)": 0, "盈利(3~10%)": 0, "大盈(>10%)": 0}
        for p in pnl_list:
            if p < -10:
                buckets["大亏(<-10%)"] += 1
            elif p < -3:
                buckets["亏损(-10~-3%)"] += 1
            elif p < 0:
                buckets["小亏(-3~0%)"] += 1
            elif p < 3:
                buckets["小盈(0~3%)"] += 1
            elif p < 10:
                buckets["盈利(3~10%)"] += 1
            else:
                buckets["大盈(>10%)"] += 1

        # 止损/止盈判断（简化：亏损 > -3% 视为止损，盈利 > 10% 视为止盈到达）
        stop_loss_like = sum(1 for p in pnl_list if -5 < p < -1)

        return {
            "pnl_distribution": buckets,
            "stop_loss_discipline": "有止损习惯" if stop_loss_like > len(pnl_list) * 0.15 else "较少止损",
        }

    def _risk_metrics(self, trades: list[dict]) -> dict:
        """风险指标"""
        pnl_list = [t.get("pnl") for t in trades if t.get("pnl") is not None and t["pnl"] != 0]
        if not pnl_list:
            return {}

        # 计算连续亏损
        max_consecutive = 0
        current = 0
        for p in pnl_list:
            if p < 0:
                current += 1
                max_consecutive = max(max_consecutive, current)
            else:
                current = 0

        # 最大单笔风险
        values = [t["quantity"] * t.get("price", 0) for t in trades if t.get("price") and t["quantity"]]
        avg_value = sum(values) / len(values) if values else 0

        return {
            "max_consecutive_losses": max_consecutive,
            "largest_drawdown_trade": min(pnl_list) if pnl_list else 0,
            "avg_trade_value": round(avg_value, 2),
            "pnl_volatility": round(
                (sum((p - sum(pnl_list)/len(pnl_list))**2 for p in pnl_list) / len(pnl_list))**0.5, 2
            ) if len(pnl_list) > 1 else 0,
        }

    def _behavior_patterns(self, trades: list[dict]) -> dict:
        """行为模式识别"""
        # 检测加仓/减仓模式
        by_symbol = defaultdict(list)
        for t in trades:
            by_symbol[t["symbol"]].append(t)

        pyramiding = 0  # 分批加仓
        scaling_out = 0  # 分批卖出
        for sym, ts in by_symbol.items():
            buys = [t for t in ts if t["action"] == "BUY"]
            sells = [t for t in ts if t["action"] == "SELL"]
            if len(buys) >= 3:
                pyramiding += 1
            if len(sells) >= 3:
                scaling_out += 1

        # 同一天多笔交易（高频特征）
        day_counts = defaultdict(int)
        for t in trades:
            day_counts[t["timestamp"][:10]] += 1
        high_freq_days = sum(1 for c in day_counts.values() if c >= 5)

        return {
            "pyramiding_stocks": pyramiding,      # 分批建仓的股票数
            "scaling_out_stocks": scaling_out,     # 分批卖出的股票数
            "high_activity_days": high_freq_days,  # 高频交易日
            "avg_daily_trades": round(len(trades) / len(day_counts), 1) if day_counts else 0,
        }

    # ------------------------------------------------------------------
    # 文字评价
    # ------------------------------------------------------------------

    def _identify_strengths(self, profile: dict) -> list[str]:
        strengths = []
        s = profile["summary"]

        if s.get("profit_factor", 0) > 1.5:
            strengths.append(f"盈亏比优秀({s['profit_factor']:.1f})，赚时赚得多、亏时亏得少")
        if s.get("avg_win", 0) > s.get("avg_loss", 0) * 1.5:
            strengths.append(f"平均盈利(${s['avg_win']:.0f})显著大于平均亏损(${s['avg_loss']:.0f})")

        # 月度趋势
        monthly = profile["monthly"]
        if len(monthly) >= 3:
            recent = monthly[-3:]
            if all(m["pnl"] > 0 for m in recent):
                strengths.append("近3个月连续盈利，交易状态良好")

        # 持仓周期
        holding = profile["holding"]
        if holding.get("categories"):
            mid = holding["categories"].get("中线(8-30天)", {})
            if mid.get("avg_pnl", 0) > 0:
                strengths.append(f"中线持仓(8-30天)表现最好，适合你的节奏")

        return strengths or ["交易数据积累中，暂无明显优势模式"]

    def _identify_weaknesses(self, profile: dict) -> list[str]:
        weaknesses = []
        s = profile["summary"]

        if s.get("win_rate", 0) < 40:
            weaknesses.append(f"胜率偏低({s['win_rate']}%)，需要提高入场信号质量")
        if s.get("largest_loss", 0) < -100:
            weaknesses.append(f"存在大额单笔亏损(${abs(s['largest_loss']):.0f})，需要更严格的止损")

        patterns = profile["patterns"]
        if patterns.get("high_activity_days", 0) > 5:
            weaknesses.append(f"高频交易日过多({patterns['high_activity_days']}天)，容易情绪化交易")

        # 时段分析
        hours = profile["by_hour"]
        if hours.get("盘后", {}).get("pnl", 0) < -50:
            weaknesses.append("盘后交易整体亏损，建议只在常规时段交易")

        exit_b = profile["exit_behavior"]
        if exit_b.get("pnl_distribution", {}).get("大亏(<-10%)", 0) > 3:
            weaknesses.append("存在深度亏损(>10%)的退出，止损线设得太宽或没有执行")

        return weaknesses or ["暂未发现明显弱点"]

    def _generate_suggestions(self, profile: dict) -> list[str]:
        suggestions = []
        s = profile["summary"]
        weaknesses = profile["weaknesses"]
        strengths = profile["strengths"]

        if s.get("win_rate", 0) < 50:
            suggestions.append("提高入场标准：只交易有明确技术信号+时政支持的标的")

        patterns = profile["patterns"]
        if patterns.get("high_activity_days", 0) > 5:
            suggestions.append("设定每日交易上限(≤3笔)，避免过度交易")

        holding = profile["holding"]
        if holding.get("avg_days", 0) < 3:
            suggestions.append("考虑延长持仓周期至3-7天，减少佣金成本和日内交易风险")

        exit_b = profile["exit_behavior"]
        if exit_b.get("pnl_distribution", {}).get("大亏(<-10%)", 0) > 3:
            suggestions.append("建议使用硬止损：每笔交易入场时同时设置-5%止损单")

        # 基于优势
        if holding.get("categories", {}).get("中线(8-30天)", {}).get("avg_pnl", 0) > 0:
            suggestions.append("多使用中线策略(8-30天持仓)，这是你的盈利舒适区")

        return suggestions

    # ------------------------------------------------------------------
    # 格式化输出
    # ------------------------------------------------------------------

    def format(self, profile: dict) -> str:
        """格式化为可读 Markdown"""
        s = profile["summary"]
        lines = [
            "## 📊 交易画像",
            "",
            "### 📋 基本数据",
            f"| 指标 | 数值 |",
            f"|------|------|",
            f"| 总交易数 | {s['total_trades']} 笔 |",
            f"| 涉及股票 | {s['unique_symbols']} 只 |",
            f"| 交易天数 | {s['trading_days']} 天 |",
            f"| 时间范围 | {s['date_range']} |",
            f"| 累计盈亏 | **${s['total_pnl']:,.2f}** |",
            f"| 胜率 | {s['win_rate']}% |",
            f"| 盈亏比 | {s['profit_factor']:.2f} |",
            f"| 平均盈利 | ${s['avg_win']:,.2f} |",
            f"| 平均亏损 | ${s['avg_loss']:,.2f} |",
            f"| 最大单笔盈利 | ${s['largest_win']:,.2f} |",
            f"| 最大单笔亏损 | ${s['largest_loss']:,.2f} |",
            "",
            "### 📈 月度趋势",
            f"| 月份 | 笔数 | 盈亏 | 胜率 | 股票数 |",
            f"|------|------|------|------|--------|",
        ]
        for m in profile["monthly"]:
            lines.append(f"| {m['month']} | {m['trades']} | ${m['pnl']:+,.0f} | {m['win_rate']}% | {m['symbols']} |")

        lines.extend([
            "",
            "### ⏱️ 持仓周期分析",
            f"平均持仓: {profile['holding']['avg_days']} 天",
            f"| 类型 | 笔数 | 总盈亏 | 平均盈亏 |",
            f"|------|------|--------|----------|",
        ])
        for cat, stats in profile["holding"].get("categories", {}).items():
            lines.append(f"| {cat} | {stats['count']} | ${stats['total_pnl']:+,.0f} | ${stats['avg_pnl']:+,.0f} |")

        lines.extend([
            "",
            "### 🔍 退出行为分析",
            f"| 盈亏区间 | 笔数 |",
            f"|----------|------|",
        ])
        for bucket, count in profile["exit_behavior"].get("pnl_distribution", {}).items():
            bar = "█" * count
            lines.append(f"| {bucket} | {count} {bar} |")

        lines.extend([
            "",
            "### 📅 交易时段偏好",
            f"| 时段 | 笔数 | 盈亏 |",
            f"|------|------|------|",
        ])
        for hour, stats in profile["by_hour"].items():
            lines.append(f"| {hour} | {stats['trades']} | ${stats['pnl']:+,.0f} |")

        lines.extend([
            "",
            "### ✅ 优势",
        ])
        for st in profile["strengths"]:
            lines.append(f"- {st}")

        lines.extend([
            "",
            "### ⚠️ 待改进",
        ])
        for wk in profile["weaknesses"]:
            lines.append(f"- {wk}")

        lines.extend([
            "",
            "### 💡 建议",
        ])
        for sg in profile["suggestions"]:
            lines.append(f"- {sg}")

        return "\n".join(lines)
