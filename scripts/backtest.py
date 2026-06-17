"""
回测脚本
用法:
    python scripts/backtest.py --strategy momentum --stock AAPL --days 365
    python scripts/backtest.py --strategy mean_reversion --stock TSLA --days 180
"""

import sys
import argparse
from pathlib import Path

# 项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.connection import ConnectionManager
from src.data.market_data import MarketData
from src.strategy.builtin.momentum import MomentumStrategy
from src.strategy.builtin.mean_reversion import MeanReversionStrategy
from src.backtest.engine import BacktestEngine
from src.backtest.performance import PerformanceAnalyzer


STRATEGIES = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
}


def main():
    parser = argparse.ArgumentParser(description="IBKR 策略回测")
    parser.add_argument("--strategy", default="momentum", choices=list(STRATEGIES.keys()),
                        help="策略名称")
    parser.add_argument("--stock", default="AAPL", help="股票代码")
    parser.add_argument("--days", type=int, default=365, help="历史数据天数")
    parser.add_argument("--bar-size", default="1 day", help="K线周期")
    parser.add_argument("--capital", type=float, default=10000, help="初始资金")
    parser.add_argument("--port", type=int, default=4001, help="IBKR 端口")
    args = parser.parse_args()

    print(f"\n  📈 策略回测: {args.strategy} on {args.stock}")
    print(f"  天数: {args.days} | K线: {args.bar_size} | 初始资金: ${args.capital:,.0f}\n")

    # 1. 获取历史数据
    print("  正在获取历史数据...")
    cm = ConnectionManager(port=args.port)
    try:
        ib = cm.connect()
        md = MarketData(ib)
        df = md.get_history(args.stock, days=args.days, bar_size=args.bar_size)
        ib.disconnect()
    except ConnectionError:
        print("  ⚠ 无法连接 IBKR，尝试使用 yfinance 兜底...")
        try:
            import yfinance as yf
            ticker = yf.Ticker(args.stock)
            df = ticker.history(period=f"{args.days}d")
            if df.empty:
                print(f"  ✗ 无法获取 {args.stock} 的历史数据")
                return
            # 统一列名
            df = df.rename(columns={
                "Open": "open", "High": "high", "Low": "low",
                "Close": "close", "Volume": "volume",
            })
            df.index.name = "date"
            print(f"  ✓ 已通过 yfinance 获取 {len(df)} 条 {args.stock} 数据")
        except ImportError:
            print("  ✗ 请安装 yfinance: pip install yfinance")
            return

    if df.empty:
        print(f"  ✗ 无数据")
        return

    # 2. 创建策略
    strategy_cls = STRATEGIES[args.strategy]
    strategy = strategy_cls()

    # 3. 运行回测
    engine = BacktestEngine(strategy, initial_capital=args.capital)
    result = engine.run(df)

    # 4. 绩效分析
    result = PerformanceAnalyzer.analyze(result)

    # 5. 输出
    print(PerformanceAnalyzer.summary(result))

    # 6. 交易明细
    if result.trades:
        print("\n  📋 交易明细:")
        print(f"  {'序号':<5} {'入场':<12} {'出场':<12} {'方向':<6} {'盈亏($)':<12} {'盈亏(%)':<10} {'原因'}")
        print("  " + "-" * 80)
        for i, t in enumerate(result.trades[:20], 1):
            direction = "做多"
            print(f"  {i:<5} {t.entry_time[:12]:<12} {t.exit_time[:12]:<12} "
                  f"{direction:<6} ${t.pnl:>8.2f}   {t.pnl_pct*100:>6.2f}%    {t.exit_reason}")


if __name__ == "__main__":
    main()
