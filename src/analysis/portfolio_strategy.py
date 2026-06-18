"""
持仓策略管理
本地缓存持仓的退出策略、加减仓规则，避免每次都调 API 重算

文件: data/portfolio_strategy.yaml (不入 git)
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

STRATEGY_FILE = Path("data/portfolio_strategy.yaml")


class PortfolioStrategyManager:
    """
    持仓策略管理器

    流程:
        1. 从 IBKR 拉持仓 → 2. 用退出策略引擎计算 → 3. 缓存到本地 YAML
        4. 用户可编辑自定义值 → 5. 下次加载时保留用户自定义

    使用方法:
        mgr = PortfolioStrategyManager(ib)
        mgr.refresh()          # 刷新所有持仓策略
        strategy = mgr.load()  # 加载本地策略
        mgr.show()             # 格式化展示
    """

    def __init__(self, ib=None):
        self._ib = ib

    # ------------------------------------------------------------------
    # 刷新：从 IBKR 拉取 + 计算 + 保存
    # ------------------------------------------------------------------

    def refresh(self, risk_profile: str = "moderate") -> dict:
        """
        从 IBKR 获取当前持仓，计算退出策略，保存到本地

        Returns:
            策略字典
        """
        if self._ib is None:
            logger.warning("无 IB 连接，仅读取本地缓存")
            return self.load()

        from src.trade.portfolio import Portfolio
        from src.analysis.exit_strategy import ExitStrategyEngine
        from src.analysis.stock_data import StockDataManager
        from src.data.price_validator import PriceValidator
        from src.config import FINNHUB_API_KEY

        pf = Portfolio(self._ib)
        engine = ExitStrategyEngine()
        mgr = StockDataManager(self._ib)
        validator = PriceValidator(self._ib, finnhub_key=FINNHUB_API_KEY)

        # 加载已有策略（保留用户自定义字段）
        existing = self.load()
        existing_holdings = existing.get("holdings", {})

        positions = pf.get_positions()
        holdings = {}

        for pos in positions:
            if pos.get("position", 0) == 0:
                continue

            sym = pos["symbol"]
            entry = pos["avg_cost"]
            qty = abs(pos["position"])

            # 获取实时价
            price_result = validator.get_price(sym)
            current = price_result.price

            # 计算退出策略
            stop = 0.0
            target = 0.0
            risk_reward = 0.0

            try:
                df = mgr.load(sym)
                if df is not None and not df.empty:
                    plan = engine.analyze(sym, df, entry_price=entry, current_price=current, risk_profile=risk_profile)
                    if plan:
                        stop = plan.normal_stop
                        target = plan.target_2
                        risk_reward = plan.risk_reward_ratio
            except:
                pass

            # 构建策略条目（确保所有数值是普通 Python float，不是 numpy）
            item = {
                "symbol": str(sym),
                "entry_price": round(float(entry), 2) if entry else 0.0,
                "quantity": float(qty),
                "current_price": round(float(current), 2) if current else 0.0,
                "stop_loss": round(float(stop), 2) if stop else (existing_holdings.get(sym, {}).get("stop_loss", 0) or 0.0),
                "take_profit": round(float(target), 2) if target else (existing_holdings.get(sym, {}).get("take_profit", 0) or 0.0),
                "risk_reward": round(float(risk_reward), 1) if risk_reward else 0.0,
                "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }

            # 保留用户自定义字段
            prev = existing_holdings.get(sym, {})
            for key in ["add_on_dip", "reduce_on_rip", "max_position", "notes", "type"]:
                if key in prev and prev[key]:
                    item[key] = prev[key]
                elif key in item:
                    pass
                elif key == "type":
                    item["type"] = "stock"
                elif key == "max_position":
                    item["max_position"] = qty
                else:
                    item[key] = None

            holdings[sym] = item

        strategy = {
            "updated": datetime.now().isoformat(),
            "account": self._ib.managedAccounts()[0] if self._ib.managedAccounts() else "",
            "holdings": holdings,
        }

        self._save(strategy)
        logger.info("持仓策略已刷新: %d 只股票", len(holdings))
        return strategy

    # ------------------------------------------------------------------
    # 读写本地文件
    # ------------------------------------------------------------------

    def load(self) -> dict:
        """加载本地策略文件"""
        if not STRATEGY_FILE.exists():
            return {"holdings": {}}

        try:
            with open(STRATEGY_FILE) as f:
                data = yaml.safe_load(f) or {}
            return data
        except Exception as e:
            logger.warning("策略文件读取失败: %s", e)
            return {"holdings": {}}

    def _save(self, data: dict):
        """保存到本地"""
        STRATEGY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(STRATEGY_FILE, "w") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def update_holding(self, symbol: str, **kwargs):
        """
        更新单只股票的策略（用户自定义）

        Args:
            symbol: 股票代码
            **kwargs: 要更新的字段 (stop_loss, take_profit, add_on_dip, reduce_on_rip, max_position, notes)
        """
        data = self.load()
        holdings = data.get("holdings", {})

        sym_upper = symbol.upper()
        if sym_upper not in holdings:
            holdings[sym_upper] = {"symbol": sym_upper}

        for key, val in kwargs.items():
            if val is not None:
                holdings[sym_upper][key] = val

        holdings[sym_upper]["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")

        data["holdings"] = holdings
        self._save(data)
        logger.info("持仓策略已更新: %s %s", symbol, kwargs)

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------

    def show(self, data: dict = None) -> str:
        """格式化为可读文本"""
        if data is None:
            data = self.load()

        holdings = data.get("holdings", {})
        if not holdings:
            return "📭 暂无持仓策略数据，请先运行 refresh"

        updated = data.get("updated", "未知")[:16]
        lines = [
            "## 📊 持仓策略",
            f"⏰ 更新: {data.get('updated', 'N/A')[:16]}",
            f"🏦 账户: {data.get('account', 'N/A')}",
            "",
            f"| 股票 | 持仓 | 成本 | 现价 | 🛑止损 | 🎯止盈 | 盈亏比 | 加仓 | 减仓 | 备注 |",
            f"|------|------|------|------|--------|--------|--------|------|------|------|",
        ]

        for sym, h in holdings.items():
            if h.get("quantity", 0) == 0:
                continue

            stop_str = f"${h.get('stop_loss', 0):.2f}" if h.get("stop_loss") else "—"
            target_str = f"${h.get('take_profit', 0):.2f}" if h.get("take_profit") else "—"
            add_str = f"${h.get('add_on_dip'):.2f}" if h.get("add_on_dip") else "—"
            reduce_str = f"${h.get('reduce_on_rip'):.2f}" if h.get("reduce_on_rip") else "—"
            rr_str = f"{h.get('risk_reward', 0):.1f}" if h.get("risk_reward") else "—"
            notes = str(h.get("notes", ""))[:20] if h.get("notes") else "—"

            # 盈亏
            entry = h.get("entry_price", 0)
            current = h.get("current_price", 0)
            if entry and current:
                pnl = (current - entry) / entry * 100
                price_str = f"${current:.2f} ({pnl:+.1f}%)"
            else:
                price_str = f"${current:.2f}" if current else "—"

            lines.append(
                f"| {sym} | {h.get('quantity', 0):.0f} | "
                f"${entry:.2f} | {price_str} | "
                f"{stop_str} | {target_str} | {rr_str} | "
                f"{add_str} | {reduce_str} | {notes} |"
            )

        lines.append("")
        lines.append("💡 编辑策略: 直接修改 `data/portfolio_strategy.yaml`")
        lines.append("🔄 重新计算: `ibkr-stock --port 4002 portfolio-strategy --refresh`")

        return "\n".join(lines)
