"""
交易记录存储模块
使用 SQLite 存储所有交易记录，支持多维度查询与统计分析
"""

import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TradeRecorder:
    """
    交易记录数据库

    表结构:
        trades:
            id, timestamp, account, symbol, action,
            quantity, price, order_type, status,
            is_day_trade, strategy, pnl, commission, note

    使用方法:
        recorder = TradeRecorder("data/trade.db")
        recorder.record(trade_dict)
        df = recorder.query(symbol="AAPL", days=30)
        summary = recorder.daily_summary()
    """

    def __init__(self, db_path: str = "data/trade.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # 数据库初始化
    # ------------------------------------------------------------------

    def _init_db(self):
        """创建表结构（如果不存在）"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    exec_id TEXT,                        -- IBKR 成交编号（去重键）
                    timestamp TEXT NOT NULL,             -- ISO 时间戳
                    account TEXT NOT NULL,               -- 账户号
                    symbol TEXT NOT NULL,                -- 股票代码
                    action TEXT NOT NULL,                -- BUY / SELL
                    quantity REAL NOT NULL,              -- 数量
                    price REAL,                          -- 成交价
                    order_type TEXT DEFAULT 'MARKET',    -- MARKET / LIMIT / STOP
                    status TEXT DEFAULT 'FILLED',        -- FILLED / CANCELLED / PENDING
                    is_day_trade INTEGER DEFAULT 0,      -- 是否日内交易
                    strategy TEXT,                       -- 策略名称
                    pnl REAL,                            -- 盈亏
                    commission REAL DEFAULT 0,           -- 佣金
                    source TEXT DEFAULT 'manual',        -- 来源: manual / ibkr_sync / auto
                    note TEXT,                           -- 备注
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                )
            """)

            # 索引
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_timestamp
                ON trades(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_symbol
                ON trades(symbol)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_account
                ON trades(account)
            """)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_exec_id
                ON trades(exec_id) WHERE exec_id IS NOT NULL AND exec_id != ''
            """)

            # PDT 日内交易计数表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS day_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    account TEXT NOT NULL,
                    trade_id INTEGER NOT NULL,
                    UNIQUE(trade_id)
                )
            """)

            # 迁移：给旧表加 exec_id 和 source 列（如果从旧版本升级）
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN exec_id TEXT")
            except:
                pass
            try:
                conn.execute("ALTER TABLE trades ADD COLUMN source TEXT DEFAULT 'manual'")
            except:
                pass

            conn.commit()
        logger.debug("交易数据库已就绪: %s", self._db_path)

    def _connect(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # 记录写入
    # ------------------------------------------------------------------

    def record(
        self,
        symbol: str,
        action: str,
        quantity: float,
        account: str = "",
        price: Optional[float] = None,
        order_type: str = "MARKET",
        status: str = "FILLED",
        is_day_trade: bool = False,
        strategy: str = "",
        pnl: Optional[float] = None,
        commission: float = 0.0,
        exec_id: str = "",
        source: str = "manual",
        timestamp: str = "",
        note: str = "",
    ) -> int:
        """
        记录一笔交易

        Args:
            exec_id: IBKR 成交编号（用于去重，从 IBKR 同步时必填）
            source: 来源 manual=手动 / ibkr_sync=同步 / auto=策略自动
            timestamp: ISO 时间戳（从 IBKR 同步时用实际成交时间）

        Returns:
            插入记录的 ID，如果 exec_id 已存在则返回 0
        """
        ts = timestamp or datetime.now().isoformat()

        with self._connect() as conn:
            # exec_id 去重检查
            if exec_id:
                existing = conn.execute(
                    "SELECT id FROM trades WHERE exec_id = ?", (exec_id,)
                ).fetchone()
                if existing:
                    logger.debug("交易已存在，跳过: %s", exec_id)
                    return 0

            cursor = conn.execute(
                """INSERT INTO trades
                   (exec_id, timestamp, account, symbol, action, quantity, price,
                    order_type, status, is_day_trade, strategy, pnl, commission, source, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    exec_id, ts, account, symbol.upper(), action.upper(),
                    quantity, price, order_type.upper(), status.upper(),
                    1 if is_day_trade else 0, strategy, pnl, commission, source, note,
                ),
            )
            trade_id = cursor.lastrowid

            # 如果是日内交易，额外记录
            if is_day_trade:
                trade_date = datetime.now().strftime("%Y-%m-%d")
                conn.execute(
                    """INSERT OR IGNORE INTO day_trades
                       (trade_date, symbol, account, trade_id)
                       VALUES (?, ?, ?, ?)""",
                    (trade_date, symbol.upper(), account, trade_id),
                )

            conn.commit()

        logger.info(
            "交易记录 #%d: %s %s %.0f股 @ $%.2f [%s] %s",
            trade_id, action, symbol, quantity, price or 0,
            "日内" if is_day_trade else "持仓",
            f"PNL=${pnl:.2f}" if pnl else "",
        )
        return trade_id

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def query(
        self,
        symbol: Optional[str] = None,
        account: Optional[str] = None,
        days: Optional[int] = None,
        status: str = "FILLED",
        limit: int = 100,
    ) -> list[dict]:
        """
        查询交易记录

        Args:
            symbol: 按股票过滤
            account: 按账户过滤
            days: 最近 N 天
            status: 按状态过滤
            limit: 最大返回数
        """
        conditions = ["status = ?"]
        params = [status]

        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol.upper())
        if account:
            conditions.append("account = ?")
            params.append(account)
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            conditions.append("timestamp >= ?")
            params.append(cutoff)

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM trades WHERE {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def daily_summary(self, days: int = 7) -> list[dict]:
        """每日交易汇总"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DATE(timestamp) as date,
                          COUNT(*) as trade_count,
                          SUM(CASE WHEN is_day_trade=1 THEN 1 ELSE 0 END) as day_trades,
                          SUM(pnl) as total_pnl,
                          SUM(commission) as total_commission
                   FROM trades
                   WHERE status='FILLED' AND DATE(timestamp) >= ?
                   GROUP BY DATE(timestamp)
                   ORDER BY date DESC""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def symbol_summary(self, days: int = 30) -> list[dict]:
        """按股票汇总交易"""
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT symbol,
                          COUNT(*) as trades,
                          SUM(pnl) as total_pnl,
                          AVG(pnl) as avg_pnl,
                          SUM(CASE WHEN is_day_trade=1 THEN 1 ELSE 0 END) as day_trades
                   FROM trades
                   WHERE status='FILLED' AND DATE(timestamp) >= ?
                   GROUP BY symbol
                   ORDER BY total_pnl DESC""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def pnl_summary(self) -> dict:
        """盈亏总览"""
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) as n, SUM(pnl) as total_pnl FROM trades WHERE status='FILLED'"
            ).fetchone()
            wins = conn.execute(
                "SELECT COUNT(*) as n FROM trades WHERE status='FILLED' AND pnl > 0"
            ).fetchone()
            losses = conn.execute(
                "SELECT COUNT(*) as n FROM trades WHERE status='FILLED' AND pnl < 0"
            ).fetchone()

        total_n = total["n"] or 0
        win_n = wins["n"] or 0
        loss_n = losses["n"] or 0

        return {
            "total_trades": total_n,
            "wins": win_n,
            "losses": loss_n,
            "win_rate": win_n / total_n if total_n > 0 else 0,
            "total_pnl": total["total_pnl"] or 0,
        }

    # ------------------------------------------------------------------
    # PDT 日内交易计数
    # ------------------------------------------------------------------

    def count_day_trades(self, account: str = "", window_days: int = 5) -> int:
        """
        统计过去 N 个交易日内的日内交易次数

        Args:
            account: 账户过滤（空=不限制）
            window_days: 滚动窗口天数
        """
        cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            if account:
                row = conn.execute(
                    """SELECT COUNT(DISTINCT trade_date) as days,
                              COUNT(*) as total
                       FROM day_trades
                       WHERE trade_date >= ? AND account = ?""",
                    (cutoff, account),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COUNT(DISTINCT trade_date) as days,
                              COUNT(*) as total
                       FROM day_trades
                       WHERE trade_date >= ?""",
                    (cutoff,),
                ).fetchone()
        return row["total"] if row else 0

    def get_day_trade_details(self, account: str = "", window_days: int = 5) -> list[dict]:
        """获取日内交易明细"""
        cutoff = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
        with self._connect() as conn:
            if account:
                rows = conn.execute(
                    """SELECT trade_date, symbol, COUNT(*) as cnt
                       FROM day_trades
                       WHERE trade_date >= ? AND account = ?
                       GROUP BY trade_date, symbol
                       ORDER BY trade_date DESC""",
                    (cutoff, account),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT trade_date, symbol, COUNT(*) as cnt
                       FROM day_trades
                       WHERE trade_date >= ?
                       GROUP BY trade_date, symbol
                       ORDER BY trade_date DESC""",
                    (cutoff,),
                ).fetchall()
        return [dict(r) for r in rows]
