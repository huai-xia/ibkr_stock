"""
交易历史同步模块
将 IBKR 账户历史成交同步到本地 SQLite，支持多账户增量同步

去重策略:
    - 使用 execId（IBKR 每笔成交的唯一编号）作为去重键
    - 已存在的 execId 自动跳过
    - 同步来源标记为 "ibkr_sync"

多账户:
    - 统一存入同一个 SQLite 数据库
    - 通过 account 字段区分不同账户
    - 查询时加 WHERE account='xxx' 即可隔离
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from ib_insync import IB

from src.trade.recorder import TradeRecorder

logger = logging.getLogger(__name__)


class TradeSync:
    """
    交易同步器

    使用方法:
        sync = TradeSync(ib, recorder)
        result = sync.sync_all_accounts(days=90)

        # 或指定账户
        result = sync.sync_account("DU123456", days=365)
    """

    def __init__(self, ib: IB, recorder: TradeRecorder):
        self._ib = ib
        self._recorder = recorder

    def sync_account(
        self,
        account: str,
        days: int = 90,
        detect_day_trades: bool = True,
    ) -> dict:
        """
        同步单个账户的历史成交

        Args:
            account: 账户号（如 "DU123456"）
            days: 同步最近 N 天的记录
            detect_day_trades: 是否自动检测日内交易

        Returns:
            {"total": 总条数, "new": 新增, "skipped": 跳过}
        """
        cutoff = datetime.now() - timedelta(days=days)
        executions = self._ib.reqExecutions()

        new_count = 0
        skipped = 0

        # 收集该账户在时间范围内的成交
        for trade in executions:
            exec_time = trade.execution.time
            exec_account = trade.execution.acctNumber

            if exec_account != account:
                continue
            if exec_time.replace(tzinfo=None) < cutoff:
                continue

            exec_id = trade.execution.execId
            symbol = trade.contract.symbol
            side = trade.execution.side  # "BOT" or "SLD"
            shares = trade.execution.shares
            price = trade.execution.price
            order_id = trade.execution.orderId

            # 佣金和盈亏（ib_insync 返回的 commissionReport 可能未正确关联）
            commission = 0.0
            pnl = None
            cr = getattr(trade, 'commissionReport', None)
            if cr and cr.execId:  # execId 非空表示已正确关联
                commission = cr.commission
                pnl = cr.realizedPNL if cr.realizedPNL != 0.0 else None

            # 映射
            action = "BUY" if side == "BOT" else "SELL"
            ts = exec_time.isoformat()

            trade_id = self._recorder.record(
                exec_id=exec_id,
                timestamp=ts,
                account=account,
                symbol=symbol,
                action=action,
                quantity=shares,
                price=price,
                pnl=pnl,
                commission=commission,
                source="ibkr_sync",
                note=f"同步自 IBKR | OrderId: {order_id}",
            )

            if trade_id > 0:
                new_count += 1
            else:
                skipped += 1

        # 检测日内交易
        if detect_day_trades and new_count > 0:
            self._detect_day_trades(account, days)

        logger.info(
            "账户 %s 同步完成: %d 条记录, %d 新增, %d 跳过",
            account, new_count + skipped, new_count, skipped,
        )
        return {"total": new_count + skipped, "new": new_count, "skipped": skipped}

    def sync_all_accounts(self, days: int = 90) -> dict:
        """
        同步所有账户

        Returns:
            {"DU123456": {...}, "DU789012": {...}}
        """
        accounts = self._ib.managedAccounts()
        results = {}
        for acct in accounts:
            results[acct] = self.sync_account(acct, days=days)
        return results

    def quick_sync(self, account: str = "", days: int = 7) -> dict:
        """
        快速增量同步（最近 N 天，适合定期执行）
        """
        if account:
            return self.sync_account(account, days=days, detect_day_trades=False)
        else:
            accounts = self._ib.managedAccounts()
            results = {}
            for acct in accounts:
                results[acct] = self.sync_account(acct, days=days, detect_day_trades=False)
            return results

    # ------------------------------------------------------------------
    # 日内交易检测
    # ------------------------------------------------------------------

    def _detect_day_trades(self, account: str, days: int):
        """
        扫描刚同步的记录，检测同一天同一股票有买有卖的日内交易
        """
        trades = self._recorder.query(account=account, days=days)

        # 按 (日期, 股票) 分组
        from collections import defaultdict
        groups = defaultdict(list)
        for t in trades:
            date = t["timestamp"][:10]
            key = (date, t["symbol"])
            groups[key].append(t)

        for (date, symbol), group in groups.items():
            has_buy = any(t["action"] == "BUY" for t in group)
            has_sell = any(t["action"] == "SELL" for t in group)
            if has_buy and has_sell:
                # 更新所有相关记录为日内交易
                with self._recorder._connect() as conn:
                    ids = [t["id"] for t in group]
                    conn.execute(
                        f"UPDATE trades SET is_day_trade=1 WHERE id IN "
                        f"({','.join('?' for _ in ids)})",
                        ids,
                    )
                    # 记录到 day_trades 表
                    for t in group:
                        try:
                            conn.execute(
                                "INSERT OR IGNORE INTO day_trades "
                                "(trade_date, symbol, account, trade_id) VALUES (?, ?, ?, ?)",
                                (date, symbol, account, t["id"]),
                            )
                        except:
                            pass
                    conn.commit()

                logger.info("检测到日内交易: %s %s (%d笔)", date, symbol, len(group))

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def get_sync_status(self, account: str = "") -> dict:
        """获取同步状态"""
        trades = self._recorder.query(account=account, days=3650, limit=10000)  # 所有记录
        synced = [t for t in trades if t.get("source") == "ibkr_sync"]
        manual = [t for t in trades if t.get("source") == "manual"]
        auto = [t for t in trades if t.get("source") == "auto"]

        return {
            "total": len(trades),
            "from_sync": len(synced),
            "from_manual": len(manual),
            "from_auto": len(auto),
            "latest_sync": synced[0]["timestamp"] if synced else None,
            "accounts": list(set(t.get("account", "") for t in trades)),
        }
