"""
IBKR 活动报表 CSV 导入器
用法: python scripts/import_csv.py <csv文件路径>
"""

import sys
import csv
import hashlib
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.trade.recorder import TradeRecorder


def parse_ibkr_csv(filepath: str) -> list[dict]:
    """解析 IBKR 活动报表 CSV"""
    trades = []

    with open(filepath, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        for row in reader:
            # 兼容中英文 CSV: "Trades" 或 "交易", "Stocks" 或 "股票"
            if len(row) < 17:
                continue
            section = row[0]
            discriminator = row[2]
            asset_class = row[3]

            # 中英文兼容
            is_trade = section in ("Trades", "交易")
            is_data = discriminator in ("Data", "数据")
            is_order = discriminator == "Order"
            is_stock = asset_class in ("Stocks", "股票")

            if not (is_trade and (is_data or is_order)):
                continue
            if not is_stock:
                continue

            try:
                account = row[5].strip()
                symbol = row[6].strip()
                currency = row[4].strip() if len(row) > 4 else "USD"
                dt_str = row[7].strip().replace('"', '')
                qty_str = row[8].strip().replace('"', '').replace(',', '')
                quantity = float(qty_str)
                trade_price = float(row[9]) if row[9] else 0.0
                commission = float(row[12]) if row[12] else 0.0
                realized_pnl = float(row[14]) if row[14] else 0.0
                code = row[16].strip() if len(row) > 16 else ""

                # 解析时间
                try:
                    ts = datetime.strptime(dt_str, "%Y-%m-%d, %H:%M:%S")
                except:
                    ts = datetime.strptime(dt_str[:10], "%Y-%m-%d")

                action = "BUY" if quantity > 0 else "SELL"
                qty = abs(quantity)

                # 生成唯一键（CSV 无 execId，用字段哈希代替）
                key_str = f"{account}|{symbol}|{dt_str}|{quantity}|{trade_price}"
                exec_id = hashlib.md5(key_str.encode()).hexdigest()[:20]
                exec_id = f"csv_{exec_id}"

                trades.append({
                    "exec_id": exec_id,
                    "timestamp": ts.isoformat(),
                    "account": account,
                    "symbol": symbol,
                    "action": action,
                    "quantity": qty,
                    "price": trade_price,
                    "pnl": realized_pnl if realized_pnl != 0 else None,
                    "commission": abs(commission),
                    "source": "csv_import",
                    "note": f"CSV导入 | Code: {code}",
                })
            except (ValueError, IndexError) as e:
                print(f"  ⚠ 跳过行: {e} -> {','.join(row[:10])}...")
                continue

    return trades


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/import_csv.py <csv文件路径>")
        print("示例: python scripts/import_csv.py MULTI_20260529_20260616.csv")
        sys.exit(1)

    filepath = sys.argv[1]
    if not Path(filepath).exists():
        print(f"✗ 文件不存在: {filepath}")
        sys.exit(1)

    print(f"\n📂 解析: {filepath}")
    trades = parse_ibkr_csv(filepath)
    print(f"   发现 {len(trades)} 笔股票交易\n")

    # 导入数据库
    recorder = TradeRecorder("data/trade.db")
    new = 0
    skipped = 0
    for t in trades:
        tid = recorder.record(**t)
        if tid > 0:
            new += 1
        else:
            skipped += 1

    print(f"  ✓ 导入完成: {new} 新增, {skipped} 跳过（已存在）")

    # 显示各账户分布
    from collections import Counter
    account_dist = Counter(t["account"] for t in trades)
    symbol_dist = Counter(t["symbol"] for t in trades)
    print(f"\n  按账户:")
    for acct, cnt in account_dist.most_common():
        print(f"    {acct}: {cnt} 笔")
    print(f"\n  按股票 (Top 10):")
    for sym, cnt in symbol_dist.most_common(10):
        print(f"    {sym}: {cnt} 笔")

    # 处理 CSV：移动到 data/ 目录
    import shutil
    dest = Path("data/imported")
    dest.mkdir(parents=True, exist_ok=True)
    dest_path = dest / Path(filepath).name
    shutil.move(filepath, dest_path)
    print(f"\n  📁 CSV 已移至: {dest_path}")


if __name__ == "__main__":
    main()
