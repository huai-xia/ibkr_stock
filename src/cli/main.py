"""
CLI 入口
ibkr-stock 命令行工具
"""

import sys
import argparse
import logging
from typing import Optional

from src.core.connection import ConnectionManager
from src.core.contract import ContractFactory
from src.data.market_data import MarketData
from src.trade.portfolio import Portfolio
from src.trade.orders import OrderExecutor
from src.trade.risk import RiskManager
from src.trade.recorder import TradeRecorder
from src.notify.wechat import WeChatNotifier, PushLevel
from src.notify.serverchan import ServerChanNotifier
from src.notify.email import EmailNotifier
from src.news.fetcher import NewsFetcher
from src.news.sentiment import SentimentAnalyzer
from src.news.impact import ImpactAnalyzer, AlertLevel
from src.news.reporter import MarketReporter
from src.strategy.builtin.momentum import MomentumStrategy
from src.strategy.builtin.mean_reversion import MeanReversionStrategy
from src.backtest.engine import BacktestEngine
from src.backtest.performance import PerformanceAnalyzer
from src.trade.sync import TradeSync
from src.analysis.stock_data import StockDataManager
from src.analysis.exit_strategy import ExitStrategyEngine
from src.data.price_validator import PriceValidator
from src.analysis.monitor import PositionMonitor
from src.analysis.briefing import DailyBriefing
from src.analysis.portfolio_strategy import PortfolioStrategyManager
from src.config import get_env, WECHAT_WEBHOOK_URL, ROOT_DIR

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO"):
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _connect(host: str, port: int, client_id: int):
    """建立连接并返回 (ib, factory, market_data, portfolio)"""
    cm = ConnectionManager(host=host, port=port, client_id=client_id)
    ib = cm.connect()
    factory = ContractFactory(ib)
    md = MarketData(ib)
    pf = Portfolio(ib)
    return ib, factory, md, pf


# ------------------------------------------------------------------
# 子命令处理
# ------------------------------------------------------------------

def cmd_status(args):
    """查看连接状态和账户摘要"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)

    print("\n" + "=" * 50)
    print("  IBKR 交易助手 - 连接状态")
    print("=" * 50)
    print(f"  服务器: {args.host}:{args.port}")
    print(f"  连接状态: {'✓ 已连接' if ib.isConnected() else '✗ 未连接'}")

    try:
        accounts = ib.managedAccounts()
        print(f"  账户: {accounts}")

        if accounts:
            summary = ib.accountSummary()
            for s in summary:
                if s.tag == "NetLiquidation" and s.currency == "USD":
                    print(f"  净清算值: ${float(s.value):,.2f}")
                if s.tag == "AvailableFunds" and s.currency == "USD":
                    print(f"  可用资金: ${float(s.value):,.2f}")
                if s.tag == "GrossPositionValue" and s.currency == "USD":
                    print(f"  持仓市值: ${float(s.value):,.2f}")
    except Exception as e:
        print(f"  [账户信息获取失败: {e}]")

    print("=" * 50 + "\n")
    ib.disconnect()


def cmd_quote(args):
    """查看实时报价"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    symbol = args.symbol.upper()

    print(f"\n  正在获取 {symbol} 实时报价...\n")

    try:
        ticker = md.stream_quote(symbol)
        ib.sleep(2)

        if ticker.last and ticker.last > 0:
            change = ""
            if ticker.close and ticker.close > 0:
                pct = (ticker.last - ticker.close) / ticker.close * 100
                sign = "+" if pct >= 0 else ""
                change = f"({sign}{pct:.2f}%)"

            print("  " + "─" * 40)
            print(f"  {symbol:8s}  ${ticker.last:,.2f}  {change}")
            print(f"  买价: ${ticker.bid:,.2f}  |  卖价: ${ticker.ask:,.2f}")
            if ticker.volume:
                print(f"  成交量: {ticker.volume:,.0f}")
            print("  " + "─" * 40 + "\n")
        else:
            print(f"  ⚠ 未获取到 {symbol} 实时数据（可能非交易时段）\n")
    except Exception as e:
        print(f"  ✗ 获取报价失败: {e}\n")

    ib.disconnect()


def cmd_history(args):
    """获取历史K线"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    symbol = args.symbol.upper()

    print(f"\n  正在获取 {symbol} {args.days} 天 {args.bar_size} K线...\n")

    try:
        df = md.get_history(
            symbol, days=args.days, bar_size=args.bar_size,
            use_cache=not args.no_cache,
        )
        if df.empty:
            print(f"  ⚠ 未获取到数据\n")
        else:
            print(f"  {symbol} 历史数据 ({len(df)} 条):")
            print(f"  时间范围: {df.index[0]} ~ {df.index[-1]}")
            print(f"\n{df.tail(10)}\n")
    except Exception as e:
        print(f"  ✗ 获取历史数据失败: {e}\n")

    ib.disconnect()


def cmd_portfolio(args):
    """查看持仓和账户"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)

    # 账户摘要
    accounts = ib.managedAccounts()
    primary_account = getattr(args, 'account', '') or (accounts[0] if accounts else "")

    print(f"\n{pf.format_account(primary_account)}")
    print(f"\n{pf.format_positions()}\n")

    ib.disconnect()


def cmd_pdt(args):
    """查看 PDT 日内交易计数"""
    # 连接 IBKR 获取账户净清算值
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    accounts = ib.managedAccounts()
    account = getattr(args, 'account', '') or (accounts[0] if accounts else "")
    net_liq = 0.0
    try:
        summary = ib.accountSummary()
        for s in summary:
            if s.tag == "NetLiquidation" and s.currency == "USD":
                net_liq = float(s.value)
                break
    except Exception:
        pass
    ib.disconnect()

    # PDT 规则仅适用于净清算值 < $25,000 的账户
    PDT_THRESHOLD = 25000.0
    if net_liq >= PDT_THRESHOLD:
        print("\n" + "=" * 40)
        print("  PDT 日内交易状态")
        print("=" * 40)
        print(f"  净清算值: ${net_liq:,.2f} (≥ $25,000)")
        print(f"  ✅ PDT 规则不适用 — 账户净值超过 $25,000，无日内交易限制")
        print("=" * 40 + "\n")
        return

    recorder = TradeRecorder("data/trade.db")
    rm = RiskManager(recorder, account=account, net_liq=net_liq)

    status = rm.get_pdt_status()

    print("\n" + "=" * 40)
    print("  PDT 日内交易状态")
    print("=" * 40)
    print(f"  净清算值: ${net_liq:,.2f} (低于 $25,000，PDT 适用)")
    print(f"  过去 {status['window_days']} 个交易日")
    print(f"  日内交易次数: {status['count']} / {status['max']}")
    print(f"  剩余额度: {status['remaining']} 次")
    print(f"  风险等级: {status['level']}")

    if status["details"]:
        print("\n  明细:")
        for d in status["details"]:
            print(f"    {d['trade_date']}: {d['symbol']} ({d['cnt']}次)")

    print("=" * 40 + "\n")


def cmd_order(args):
    """下单（⚠️ 当前只读模式）"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)

    recorder = TradeRecorder("data/trade.db")
    risk = RiskManager(recorder)

    # 默认只读模式
    readonly = not args.force
    executor = OrderExecutor(ib, risk, recorder, readonly=readonly)

    from src.trade.orders import OrderRequest, OrderAction, OrderType

    # 解析订单类型
    type_map = {
        "market": OrderType.MARKET,
        "limit": OrderType.LIMIT,
        "stop": OrderType.STOP,
        "stop_limit": OrderType.STOP_LIMIT,
    }
    order_type = type_map.get(args.type, OrderType.LIMIT)

    req = OrderRequest(
        symbol=args.symbol.upper(),
        action=OrderAction(args.action.upper()),
        quantity=args.quantity,
        order_type=order_type,
        price=args.price,
        stop_price=args.stop_price,
    )

    result = executor.place(req)

    print(f"\n{'✓' if result.success else '✗'} {result.message}")
    if result.risk_result and result.risk_result.warnings:
        for w in result.risk_result.warnings:
            print(f"  {w}")
    print()

    ib.disconnect()


def cmd_trade_summary(args):
    """查看交易记录统计（仅当前登录账户）"""
    # 连接 IBKR 获取当前账户 ID
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    accounts = ib.managedAccounts()
    account = getattr(args, 'account', '') or (accounts[0] if accounts else "")
    ib.disconnect()

    recorder = TradeRecorder("data/trade.db")

    # 盈亏总览（仅当前账户）
    pnl = recorder.pnl_summary(account=account)
    print("\n" + "=" * 40)
    print("  交易盈亏总览")
    print("=" * 40)
    print(f"  账户: {account}")
    print(f"  总交易数: {pnl['total_trades']}")
    print(f"  盈利: {pnl['wins']} | 亏损: {pnl['losses']}")
    print(f"  胜率: {pnl['win_rate']:.1%}")
    print(f"  累计盈亏: ${pnl['total_pnl']:.2f}")
    print("=" * 40)

    # 按股票汇总（仅当前账户，近30天）
    print("\n  按股票汇总（近30天）:")
    symbol_summary = recorder.symbol_summary(days=30, account=account)
    if symbol_summary:
        for s in symbol_summary:
            pnl_val = s["total_pnl"] or 0.0
            sign = "+" if pnl_val > 0 else ""
            print(f"    {s['symbol']}: {s['trades']}笔, "
                  f"盈亏 {sign}${pnl_val:.2f}, "
                  f"日内 {s['day_trades']}次")
    else:
        print("    (无交易记录)")

    # 每日汇总（仅当前账户，近7天）
    print("\n  每日汇总（近7天）:")
    daily = recorder.daily_summary(days=7, account=account)
    if daily:
        for d in daily:
            pnl_val = d["total_pnl"] or 0.0
            sign = "+" if pnl_val > 0 else ""
            print(f"    {d['date']}: {d['trade_count']}笔, "
                  f"盈亏 {sign}${pnl_val:.2f}, "
                  f"日内 {d['day_trades']}次")
    else:
        print("    (无交易记录)")

    print()


def cmd_notify_test(args):
    """测试通知推送（微信/Server酱/邮件）"""
    channel = args.channel

    # 企业微信
    if channel == "wechat":
        webhook_url = args.webhook or get_env("WECHAT_WEBHOOK_URL", "")
        notifier = WeChatNotifier(webhook_url)
        if not notifier.is_configured:
            print("\n  ✗ 企业微信 Webhook 未配置！")
            print("  请在 .env 中设置 WECHAT_WEBHOOK_URL\n")
            return
        print("\n  📱 测试企业微信通知...")
        ok = notifier.send(
            "🧪 IBKR 交易助手 — 企业微信通知测试\n\n"
            "如果你收到这条消息，说明配置成功！"
        )
        print(f"  {'✓ 发送成功！请检查企业微信' if ok else '✗ 发送失败'}\n")

    # Server酱（推送到个人微信）
    elif channel == "serverchan":
        send_key = args.key or get_env("SERVERCHAN_SEND_KEY", "")
        notifier = ServerChanNotifier(send_key)
        if not notifier.is_configured:
            print("\n  ✗ Server酱 SendKey 未配置！")
            print("  获取方式：https://sct.ftqq.com/ 微信扫码 → 获取 SendKey")
            print("  然后在 .env 中设置 SERVERCHAN_SEND_KEY=你的key\n")
            return
        print("\n  📱 测试 Server酱 通知（推送到个人微信）...")
        ok = notifier.send(
            "🧪 IBKR 交易助手 — 通知测试",
            "如果你收到这条消息，说明 Server酱 配置成功！\n\n"
            "后续交易信号、PDT 预警、时政分析都会通过此渠道推送到你的微信。"
        )
        print(f"  {'✓ 发送成功！请检查微信' if ok else '✗ 发送失败'}\n")

    # 邮件
    elif channel == "email":
        smtp_host = args.smtp_host or get_env("SMTP_HOST", "smtp.qq.com")
        smtp_port = int(args.smtp_port or get_env("SMTP_PORT", "587"))
        user = args.email or get_env("SMTP_USER", "")
        password = args.password or get_env("SMTP_PASSWORD", "")

        notifier = EmailNotifier(smtp_host, smtp_port, user, password)
        if not notifier.is_configured:
            print("\n  ✗ 邮件未配置！")
            print("  请在 .env 中设置 SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD\n")
            return
        print(f"\n  📧 测试邮件通知 ({user})...")
        ok = notifier.send(
            "IBKR 交易助手 — 通知测试",
            "<p>如果你收到这封邮件，说明邮件通知配置成功！</p>"
            "<p>后续交易通知将通过邮件发送。</p>",
            html=True,
        )
        print(f"  {'✓ 发送成功！请检查收件箱' if ok else '✗ 发送失败'}\n")

    else:
        print(f"\n  未知通知渠道: {channel}\n")


# ------------------------------------------------------------------
# 新闻分析
# ------------------------------------------------------------------

def cmd_news(args):
    """获取重要新闻 → 中文分析 → 推送"""

    # 解析持仓
    holdings = []
    if args.holdings:
        holdings = [s.strip().upper() for s in args.holdings.split(",") if s.strip()]

    # 如果有 IB 连接，自动从账户获取持仓
    if args.auto_holdings:
        try:
            ib, _, _, pf = _connect(args.host, args.port, args.client_id)
            positions = pf.get_positions()
            holdings = [p["symbol"] for p in positions if p.get("position", 0) != 0]
            print(f"\n  📦 自动获取持仓: {holdings}")
            ib.disconnect()
        except Exception as e:
            print(f"  ⚠ 无法连接 IBKR 获取持仓: {e}")

    print(f"\n  📰 正在获取市场新闻（含持仓分析: {holdings if holdings else '无'}）...\n")

    reporter = MarketReporter()
    report = reporter.generate(
        holdings=holdings,
        min_score=args.min_score,
    )

    # 终端输出
    print(report)

    # 推送
    if args.push:
        if args.email_only:
            ok = reporter.push_email(report)
            print(f"\n  {'✓' if ok else '✗'} 邮件推送{'成功' if ok else '失败，请检查邮箱配置'}")
        else:
            # 仅用邮箱
            ok_email = reporter.push_email(report)
            print(f"  {'✓' if ok_email else '✗'} 邮件推送{'成功' if ok_email else '失败'}")
    print()


# ------------------------------------------------------------------
# 回测
# ------------------------------------------------------------------

STRATEGIES = {
    "momentum": MomentumStrategy,
    "mean_reversion": MeanReversionStrategy,
}


def cmd_backtest(args):
    """运行策略回测"""
    symbol = args.symbol.upper()

    print(f"\n  📈 策略回测: {args.strategy} on {symbol}\n  获取历史数据...")

    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    df = md.get_history(symbol, days=args.days, bar_size=args.bar_size)
    ib.disconnect()

    if df.empty:
        print(f"  ✗ 无数据\n")
        return

    strategy_cls = STRATEGIES[args.strategy]
    strategy = strategy_cls()

    engine = BacktestEngine(strategy, initial_capital=args.capital)
    result = engine.run(df)
    result = PerformanceAnalyzer.analyze(result)

    print(f"\n{PerformanceAnalyzer.summary(result)}")

    if result.trades:
        print(f"\n  📋 交易明细 (最近 15 笔):")
        for i, t in enumerate(result.trades[-15:], 1):
            sign = "+" if t.pnl > 0 else ""
            print(f"  {i:>2}. {t.entry_reason} → {t.exit_reason}")
            print(f"      入场 ${t.entry_price:.2f} → 出场 ${t.exit_price:.2f} | "
                  f"盈亏 {sign}${t.pnl:.2f} ({t.pnl_pct*100:+.2f}%)")
    print()


# ------------------------------------------------------------------
# 交易同步
# ------------------------------------------------------------------

def cmd_sync(args):
    """从 IBKR 同步历史交易到本地数据库"""
    days = getattr(args, 'days', 7)  # overview 调用时默认7天增量
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    recorder = TradeRecorder("data/trade.db")
    sync = TradeSync(ib, recorder)

    # 同步所有账户
    results = sync.sync_all_accounts(days=days)
    total_new = sum(r["new"] for r in results.values())
    total_skip = sum(r["skipped"] for r in results.values())
    print(f"\n  ✓ 同步完成: {total_new} 新增, {total_skip} 跳过")
    for acct, r in results.items():
        if r["new"] > 0 or r["skipped"] > 0:
            print(f"    {acct}: {r['new']} 新增, {r['skipped']} 跳过")

    # 同步状态
    status = sync.get_sync_status()
    print(f"\n  📋 本地数据库: {status['total']} 条记录")
    print(f"     同步来源: {status['from_sync']} | 手动: {status['from_manual']} | 自动: {status['from_auto']}")
    print(f"     账户: {', '.join(status['accounts'])}")

    ib.disconnect()
    print()


# ------------------------------------------------------------------
# 持仓监控
# ------------------------------------------------------------------

def cmd_monitor(args):
    """持仓监控：扫描持仓 + 止损止盈告警"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    monitor = PositionMonitor(ib)

    # 先下载缺失的缓存数据
    positions = pf.get_positions()
    active = [p["symbol"] for p in positions if p.get("position", 0) != 0]
    if active:
        print(f"\n  📥 准备持仓数据: {', '.join(active)}")
        mgr = StockDataManager(ib)
        mgr.prepare_analysis_data(active, days=365)

    # 循环监控模式
    if args.loop:
        print(f"\n  🔄 循环监控模式 (间隔 {args.interval}秒)")
        ib.disconnect()  # 先断开，循环内部会重新连接
        monitor = None  # 循环内重建
        import time as t

        iteration = 0
        try:
            while True:
                iteration += 1
                print(f"\n{'─'*40}")
                print(f"  🔍 检查 #{iteration} — {t.strftime('%H:%M:%S')}")

                # 每次循环重新连接（避免连接超时）
                ib2, _, _, pf2 = _connect(args.host, args.port, args.client_id)
                mon = PositionMonitor(ib2)

                alerts_list = mon.check()
                snaps = mon.snapshot()
                print(f"\n{mon.format_snapshot(snaps)}")

                if alerts_list:
                    print(f"\n{mon.format_alerts(alerts_list)}")
                    if not args.no_push:
                        mon.push_alerts(alerts_list)
                else:
                    print("  ✅ 无告警")

                ib2.disconnect()

                if args.max_iter > 0 and iteration >= args.max_iter:
                    break

                print(f"\n  ⏰ 下次检查: {args.interval}秒后...")
                t.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  👋 监控已停止")
        return

    # 一次性检查模式
    print(f"\n  🔍 扫描持仓...")
    alerts = monitor.check()

    # 展示快照
    snapshots = monitor.snapshot()
    print(f"\n{monitor.format_snapshot(snapshots)}")

    # 告警
    if alerts:
        print(f"\n{monitor.format_alerts(alerts)}")
        if not args.no_push:
            ok = monitor.push_alerts(alerts)
            print(f"  {'✓' if ok else '✗'} 告警推送{'成功' if ok else '失败'}")

    ib.disconnect()
    print()


# ------------------------------------------------------------------
# 每日简报
# ------------------------------------------------------------------

def cmd_briefing(args):
    """生成收盘简报并推送"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)

    print(f"\n  📰 生成收盘简报...")
    briefer = DailyBriefing(ib)
    report = briefer.generate()

    if not args.no_push:
        ok = briefer.push(report)
        print(f"  {'✓' if ok else '✗'} 简报推主账号{'成功' if ok else '失败'}")

    # 好友推送（精简版，无持仓/风控）
    if args.friend:
        print(f"  📡 推送好友精简版...")
        ok_f = briefer.push_friend()
        print(f"  {'✓' if ok_f else '✗'} 好友推送{'成功' if ok_f else '失败'}")

    # 预览关键数据
    for line in report.split("\n"):
        if any(kw in line for kw in ["净清算值", "总浮动盈亏", "大盘表现", "PDT", "持仓日终", "强烈买入",
                                       "明日财经", "财报发布", "经济数据", "风控状态"]):
            clean = line.replace("#", "").replace("*", "").strip()
            if clean:
                print(f"  {clean}")

    ib.disconnect()
    print()


# ------------------------------------------------------------------
# 持仓策略
# ------------------------------------------------------------------

def cmd_portfolio_strategy(args):
    """查看/刷新持仓策略"""
    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    mgr = PortfolioStrategyManager(ib)

    if args.refresh:
        print("\n  🔄 刷新持仓策略...")
        data = mgr.refresh(risk_profile=args.risk)
    else:
        data = mgr.load()

    print(f"\n{mgr.show(data)}")

    if args.set_symbol:
        kwargs = {}
        if args.set_stop:
            kwargs["stop_loss"] = args.set_stop
        if args.set_target:
            kwargs["take_profit"] = args.set_target
        if args.set_add:
            kwargs["add_on_dip"] = args.set_add
        if args.set_reduce:
            kwargs["reduce_on_rip"] = args.set_reduce
        if args.set_notes:
            kwargs["notes"] = args.set_notes

        mgr.update_holding(args.set_symbol, **kwargs)
        print(f"\n  ✅ {args.set_symbol} 策略已更新")
        print(f"{mgr.show(mgr.load())}")

    ib.disconnect()
    print()


# ------------------------------------------------------------------
# 一键概览
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# 股票深度分析
# ------------------------------------------------------------------

def cmd_analyze(args):
    """实时股价 + 历史K线 → 退出策略 + 技术面分析"""
    symbol = args.symbol.upper()

    ib, factory, md, pf = _connect(args.host, args.port, args.client_id)
    mgr = StockDataManager(ib)

    # 1. 多渠道获取实时价格（IBKR + Yahoo + Finnhub）
    from src.config import FINNHUB_API_KEY
    validator = PriceValidator(ib, finnhub_key=FINNHUB_API_KEY)
    price_result = validator.get_price(symbol)
    live_price = price_result.price if price_result.price > 0 else 0.0

    # 价格来源标注
    if price_result.confidence == "high":
        price_note = f"💰 {price_result.symbol} ${live_price:.2f} (实时·{len(price_result.sources)}源一致·高可信)"
    elif price_result.is_realtime:
        price_note = f"💰 {price_result.symbol} ${live_price:.2f} (实时·{len(price_result.sources)}源)"
    else:
        price_note = f"💰 {price_result.symbol} ${live_price:.2f} (收盘价·{len(price_result.sources)}源)"

    if price_result.warning:
        price_note += f"\n  {price_result.warning}"

    # 2. 拉历史数据
    print(f"\n  📊 分析 {symbol}")
    print(f"  {price_note}")
    # 显示各渠道详情
    if len(price_result.sources) > 1:
        src_info = " | ".join(
            f"{s}: ${price_result.details[s]['price']:.2f}"
            for s in price_result.sources
        )
        print(f"  来源: {src_info}")
    if price_result.spread > 0:
        print(f"  价差: {price_result.spread:.2f}%")
    print(f"  📥 下载历史数据...")

    data = mgr.prepare_analysis_data([symbol], days=args.days)
    df = data.get(symbol)

    ib.disconnect()

    if df is None or df.empty:
        print(f"  ✗ 数据不足\n")
        return

    # 3. 退出策略
    engine = ExitStrategyEngine()
    plan = engine.analyze(symbol, df, current_price=live_price, risk_profile=args.risk)
    if plan:
        output = engine.format_plan(plan)
        if price_result.is_realtime:
            output = output.replace("| 当前价", "| 当前价 (实时)", 1)
        print(f"\n{output}")

    # 4. 简化技术面
    print(f"\n  {'─' * 50}")
    print(f"  📈 技术面速览")
    print(f"  {'─' * 50}")

    last = df.iloc[-1]
    bb_pct = last.get("bb_pct_b", 0) * 100
    sma20 = last.get("sma_20", 0)
    rsi = last.get("rsi_14", 50)
    pct_5d = (df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100 if len(df) >= 6 else 0

    print(f"  布林位置: {bb_pct:.0f}%  {'← 超卖' if bb_pct < 10 else ('← 超买' if bb_pct > 90 else '')}")
    print(f"  RSI(14): {rsi:.1f}  {'← 超卖' if rsi < 30 else ('← 超买' if rsi > 70 else '')}")
    print(f"  SMA20: ${sma20:.2f}")
    print(f"  5日涨跌: {pct_5d:+.1f}%")
    print()


def cmd_overview(args):
    """一键执行多个只读查询：status + portfolio + pdt + trade-summary"""
    # 如果指定了 --sync，先同步历史交易
    if args.sync:
        print("  🔄 正在同步最新交易...")
        cmd_sync(args)

        # cmd_sync 内部会断开连接，需要重新连接
        print()

    # Status
    cmd_status(args)

    # Portfolio
    cmd_portfolio(args)

    # PDT
    cmd_pdt(args)

    # Trade Summary
    cmd_trade_summary(args)


# ------------------------------------------------------------------
# 主入口
# ------------------------------------------------------------------

def main():
    examples = """\
使用案例:
  # 连接真实账户（必须加 --port 4002）
  ibkr-stock --port 4002 overview

  # 同步历史交易
  ibkr-stock --port 4002 sync-history                   同步最近90天
  ibkr-stock --port 4002 sync-history --days 365        同步最近一年

  # 查询类（无需参数）
  ibkr-stock --port 4002 status                   查看账户状态
  ibkr-stock --port 4002 portfolio                查看持仓
  ibkr-stock --port 4002 pdt                      PDT 日内交易计数
  ibkr-stock --port 4002 trade-summary            交易记录统计

  # 行情类
  ibkr-stock --port 4002 quote AAPL               AAPL 实时报价
  ibkr-stock --port 4002 history AAPL --days 60   AAPL 60天日线
  ibkr-stock --port 4002 history TSLA --days 30 --bar-size "1 hour"  小时线

  # 下单类（默认只读，删 --force 可预览风控结果）
  ibkr-stock --port 4002 order buy 100 AAPL --type limit --price 180.00
  ibkr-stock --port 4002 order sell 50 TSLA --type stop --stop-price 170.00

  # 新闻类
  ibkr-stock news --holdings "AAPL,KO,AMD,INTC" --push     中文快报+邮件
  ibkr-stock --port 4002 news --auto-holdings --push       自动获取持仓

  # 回测类
  ibkr-stock --port 4002 backtest momentum SOXL --days 365
  ibkr-stock --port 4002 backtest mean_reversion TSLA --days 180 --capital 5000

  # 通知测试
  ibkr-stock notify-test serverchan       测试 Server酱→微信
  ibkr-stock notify-test email            测试 QQ邮箱

  # 查看具体命令参数
  ibkr-stock order --help
  ibkr-stock backtest --help"""

    parser = argparse.ArgumentParser(
        prog="ibkr-stock",
        description="IBKR 智能股票交易助手",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="TWS/Gateway 地址")
    parser.add_argument("--port", type=int, default=4001, help="端口 (4001=Paper, 4002=Live)")
    parser.add_argument("--client-id", type=int, default=1, help="客户端 ID")
    parser.add_argument("--log-level", default="INFO", help="日志级别")

    subparsers = parser.add_subparsers(dest="command", help="可用命令（ibkr-stock <命令> --help 查看详细用法）")

    # status
    subparsers.add_parser("status", help="[查询] 查看连接状态和账户摘要")

    # quote: 需要 symbol
    p_quote = subparsers.add_parser("quote", help="[行情] 查看实时报价 | 用法: quote <股票代码>")
    p_quote.add_argument("symbol", help="股票代码，如 AAPL, TSLA, NVDA")

    # history: 需要 symbol
    p_hist = subparsers.add_parser("history", help="[行情] 获取历史K线 | 用法: history <代码> --days 天数")
    p_hist.add_argument("symbol", help="股票代码")
    p_hist.add_argument("--days", type=int, default=60, help="天数 (默认: 60)")
    p_hist.add_argument("--bar-size", default="1 day", help="K线周期: 1 min, 1 hour, 1 day 等")
    p_hist.add_argument("--no-cache", action="store_true", help="跳过本地缓存")

    # portfolio
    p_port = subparsers.add_parser("portfolio", help="[查询] 查看持仓和账户 | 用法: portfolio [--account 账户号]")
    p_port.add_argument("--account", default="", help="指定账户（不填则用默认）")

    # pdt
    subparsers.add_parser("pdt", help="[查询] PDT 日内交易计数（5日滚动窗口）")

    # order: 需要 action quantity symbol
    p_order = subparsers.add_parser("order", help="[交易] 下单 | 用法: order <buy/sell> <数量> <代码> --type limit --price 价格")
    p_order.add_argument("action", choices=["buy", "sell"], help="buy=买入, sell=卖出")
    p_order.add_argument("quantity", type=float, help="股数")
    p_order.add_argument("symbol", help="股票代码")
    p_order.add_argument("--type", default="limit", choices=["market", "limit", "stop", "stop_limit"],
                         help="订单类型: limit=限价, market=市价, stop=止损")
    p_order.add_argument("--price", type=float, help="限价（limit/stop_limit 必填）")
    p_order.add_argument("--stop-price", type=float, help="止损触发价（stop/stop_limit 必填）")
    p_order.add_argument("--force", action="store_true", help="⚠️ 关闭只读保护，真实下单")

    # trade-summary
    subparsers.add_parser("trade-summary", help="[查询] 交易记录统计（胜率/盈亏/每日汇总）")

    # notify-test
    p_notify = subparsers.add_parser("notify-test", help="[通知] 测试消息推送 | 用法: notify-test <渠道>")
    p_notify.add_argument("channel", choices=["serverchan", "wechat", "email"],
                          help="serverchan=微信 | wechat=企业微信 | email=QQ邮箱")
    p_notify.add_argument("--key", default="", help="Server酱 SendKey（可选，默认读 .env）")
    p_notify.add_argument("--webhook", default="", help="企业微信 Webhook（可选）")
    p_notify.add_argument("--email", default="", help="邮箱地址（可选）")

    # news
    p_news = subparsers.add_parser("news", help="[资讯] 中文市场快报 | 用法: news --holdings AAPL,TSLA --push")
    p_news.add_argument("--min-score", type=int, default=2, help="最低相关性分数 (默认: 2)")
    p_news.add_argument("--holdings", default="", help="持仓股票，逗号分隔: AAPL,KO,AMD,INTC")
    p_news.add_argument("--auto-holdings", action="store_true", help="从 IBKR 自动获取持仓（需连接）")
    p_news.add_argument("--push", action="store_true", help="推送到 QQ邮箱")
    p_news.add_argument("--email-only", action="store_true", help="仅推送，不显示终端")

    # backtest: 需要 strategy symbol
    p_bt = subparsers.add_parser("backtest", help="[回测] 策略回测 | 用法: backtest <策略> <代码> --days 天数")
    p_bt.add_argument("strategy", choices=["momentum", "mean_reversion"],
                      help="momentum=双均线动量 | mean_reversion=布林带回归")
    p_bt.add_argument("symbol", help="股票代码，如 SOXL, TSLA, NVDA")
    p_bt.add_argument("--days", type=int, default=365, help="回测天数 (默认: 365)")
    p_bt.add_argument("--bar-size", default="1 day", help="K线周期 (默认: 1 day)")
    p_bt.add_argument("--capital", type=float, default=10000, help="初始资金 (默认: 10000)")

    # sync-history
    p_sync = subparsers.add_parser("sync-history", help="[同步] 从 IBKR 同步历史交易 | 用法: sync-history [--days 天数]")
    p_sync.add_argument("--days", type=int, default=90, help="同步天数 (默认: 90)")
    p_sync.add_argument("--all", action="store_true", help="同步所有账户（默认行为）")

    # analyze
    p_an = subparsers.add_parser("analyze", help="[分析] 股票深度分析 | 用法: analyze <代码>")
    p_an.add_argument("symbol", help="股票代码")
    p_an.add_argument("--days", type=int, default=365, help="历史数据天数 (默认: 365)")
    p_an.add_argument("--risk", default="moderate", choices=["conservative", "moderate", "aggressive"],
                      help="风险偏好 (默认: moderate)")

    # monitor
    p_mon = subparsers.add_parser("monitor", help="[监控] 持仓止损止盈检查 | 用法: monitor [--loop] [--no-push]")
    p_mon.add_argument("--no-push", action="store_true", help="仅显示，不推送告警")
    p_mon.add_argument("--loop", action="store_true", help="循环监控模式")
    p_mon.add_argument("--interval", type=int, default=300, help="循环间隔秒数 (默认: 300=5分钟)")
    p_mon.add_argument("--max-iter", type=int, default=0, help="最大检查次数 (0=无限)")

    # briefing
    p_brief = subparsers.add_parser("briefing", help="[简报] 每日交易简报 | 用法: briefing [--no-push] [--friend]")
    p_brief.add_argument("--no-push", action="store_true", help="仅显示，不推送邮件")
    p_brief.add_argument("--friend", action="store_true", help="同时推送精简版给好友（无持仓/风控信息）")
    p_brief.add_argument("--email-only", action="store_true", help="仅推送邮件，不终端显示")

    # portfolio-strategy
    p_ps = subparsers.add_parser("portfolio-strategy",
        help="[持仓] 查看/设置持仓策略 | 用法: portfolio-strategy [--refresh] [--set SYM --stop 价 --target 价]")
    p_ps.add_argument("--refresh", action="store_true", help="从IBKR刷新并重算策略")
    p_ps.add_argument("--risk", default="moderate", choices=["conservative","moderate","aggressive"])
    p_ps.add_argument("--set-symbol", default="", help="要自定义的股票代码")
    p_ps.add_argument("--set-stop", type=float, help="自定义止损价")
    p_ps.add_argument("--set-target", type=float, help="自定义止盈价")
    p_ps.add_argument("--set-add", type=float, help="加仓触发价")
    p_ps.add_argument("--set-reduce", type=float, help="减仓触发价")
    p_ps.add_argument("--set-notes", default="", help="备注")

    # overview
    p_ov = subparsers.add_parser("overview", help="[查询] 一键概览: status + portfolio + pdt + trade-summary")
    p_ov.add_argument("--sync", action="store_true", help="概览前先同步 IBKR 最新交易")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    setup_logging(args.log_level)

    commands = {
        "status": cmd_status,
        "quote": cmd_quote,
        "history": cmd_history,
        "portfolio": cmd_portfolio,
        "pdt": cmd_pdt,
        "order": cmd_order,
        "trade-summary": cmd_trade_summary,
        "notify-test": cmd_notify_test,
        "news": cmd_news,
        "backtest": cmd_backtest,
        "overview": cmd_overview,
        "portfolio-strategy": cmd_portfolio_strategy,
        "analyze": cmd_analyze,
        "monitor": cmd_monitor,
        "briefing": cmd_briefing,
        "sync-history": cmd_sync,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
    else:
        print(f"未知命令: {args.command}")


if __name__ == "__main__":
    main()
