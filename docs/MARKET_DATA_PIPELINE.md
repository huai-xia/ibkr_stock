# Market Data Pipeline（市场数据管线）v2

> 多时间框架数据采集 · 增量特征计算 · 短时异常检测
>
> 状态: 计划中 | 关联: [项目计划书](../PROJECT_PLAN.md)

---

## 一、问题

日线无法检测盘中异动：

```
AAPL 今天:
  开盘 $300 → 10分钟跌到 $270 (-10%) → 30分钟反弹到 $295 (-1.7%)

日线看: 开盘 $300，当前 $295，跌了 1.7%
真实情况: 盘中暴跌 10%，但因为没有分钟数据，完全看不见
```

---

## 二、数据分层架构

### 四层数据

```
┌───────────────────────────────────────────────────────────┐
│ L0: 原始分钟线 & 快照                                       │
│   盘中:   SYM_YYYY-MM-DD_1min.parquet    (OHLCV K线)      │
│   夜盘:   SYM_YYYY-MM-DD_extended.parquet (价格快照)       │
│   用途:   短时异常检测 · 特征计算源                          │
│   保留:   30天                                              │
├───────────────────────────────────────────────────────────┤
│ L1: 聚合多时间框架 (从 L0 自动生成)                          │
│   SYM_5min.parquet  → 短期趋势 + VWAP                     │
│   SYM_15min.parquet → 支撑阻力 + 开盘区间                   │
│   用途:   多尺度信号 · 回测                                  │
│   保留:   90天                                              │
├───────────────────────────────────────────────────────────┤
│ L2: 特征缓存 (从 L0/L1 增量计算)                            │
│   SYM_features.parquet                                     │
│   列: sma_20 | rsi_14 | zscore_20 | vwap_cum | vol_ratio │
│       bb_pct | macd_hist | atr_14 | roc_5 | roc_15       │
│   用途:   避免每次扫描重复计算                                │
│   保留:   30天                                              │
├───────────────────────────────────────────────────────────┤
│ L3: 日聚合 (永久)                                           │
│   SYM_1day.parquet                                         │
│   用途:   趋势分析 · 支撑阻力 · 退出策略 · 回测               │
│   保留:   永久                                              │
│                                                           │
│ L4: 实时快照 (内存, 60s TTL)                                │
│   富途 overnight_price / bid / ask / volume                 │
│   用途:   当前价格 · 盘口价差                                │
└───────────────────────────────────────────────────────────┘
```

### 数据流

```
盘中 9:30-16:00:
  IBKR reqHistoricalData(bar_size="1 min") → L0 分钟线
  每新增 1 根分钟 → 触发增量计算:
    → 追加 L0 文件
    → 聚合 L1 (5min/15min, 到整点触发)
    → 计算 L2 特征行
    → 跑 L0 异常检测 (9种算法)

夜盘 20:00-4:00 + 盘前 4:00-9:30:
  富途 get_market_snapshot → L0 快照
  每新增 1 次快照 → 同上增量计算

                                             │
                                      异常触发 → 📧 邮件告警

收盘后 (16:00 ET):
  L0 分钟线 → 聚合 OHLCV → 追加 L3 日线
```

### 增量计算 vs 全量计算

```
之前: 每次扫描 → add_all(df) 全量重算 → 390行 × 15个指标
之后: 新增1根分钟 → 只算这一根的增量 → 追加1行到L2 → ~40行内完成

从 390次计算 → 1次
```

---

## 三、存储估算

```
L0 分钟:   36KB/天/股  ×65股×30天 =  70MB
L1 聚合:   12KB/天/股  ×65股×90天 =  70MB
L2 特征:   25KB/天/股  ×65股×30天 =  49MB
L3 日线:    0.5KB/天/股 ×65股×永久  ≈  忽略

合计约 200MB ← 完全可本地存储
```

---

## 四、短时异常检测算法

### 盘中 (L0 1分钟K线)

| 检测项 | 算法 | 数据需求 | 触发 |
|--------|------|:--:|------|
| 闪电崩盘 | 20分钟 Z-Score | 20根 | Z < -3σ |
| VWAP 偏离 | 当日累计 VWAP | 全部 | 偏离 > 3% |
| 量异常 | 量 vs 20日均量 | 5-20根 | > 5x |
| 反转 | 15分钟 ROC + 回弹 | 15根 | 跌>5%后升>2% |
| 突破 | 开盘区间 + 量 | 30根 | 突破 + 量>2x |
| 跳空 | 开盘 vs 昨收 vs 15分钟走势 | 15根 | 回补>50% |

### 夜盘/盘前 (L0 快照)

| 检测项 | 算法 | 数据需求 | 触发 |
|--------|------|:--:|------|
| 夜盘异动 | 30分钟变化率 | 30快照 | 涨跌 > 3% |
| 盘前跳变 | 最近价 vs 昨收 | 1快照 | 偏差 > 5% |
| 盘前趋势 | 线性回归斜率 | 60快照 | > 0.05%/分钟 |

### L1 多时间框架信号 (第二期)

| 检测项 | 框架 | 算法 | 触发 |
|--------|:--:|------|------|
| 短期趋势确认 | 5min | 均线排列 + MACD | 多头/空头排列 |
| 支撑阻力突破 | 15min | 开盘区间 + 前日高低 | 突破 + 量确认 |
| 多框架共振 | 1+5+15min | 三框架同向 + 量放大 | 强信号 |

---

## 五、分两期实施

### 第一期（核心能力）

**新增文件 3 个：**

| 文件 | 作用 |
|------|------|
| `src/data/minute_store.py` | 分钟内存储 (MinuteStore) — L0/L1 读写 |
| `src/data/feature_cache.py` | 特征缓存 (FeatureCache) — L2 增量计算 |
| `src/analysis/anomaly.py` | 短时异常检测器 (AnomalyDetector) — 9种算法 |

**修改文件 2 个：**

| 文件 | 改动 |
|------|------|
| `scripts/monitor_daemon.py` | 追加分钟线 → 增量计算特征 → 异常检测 → 邮件告警 |
| `src/cli/main.py` | `analyze` 显示当日分钟走势概要 |

**不修改：**
- `price_validator.py` — 快照逻辑不变
- `stock_data.py` — 日线不变
- `exit_strategy.py` — 基于日线不变

### 第二期（精进）

- Volume Profile (高成交量节点 = 支撑阻力)
- L1 多时间框架信号
- 数据质量管线 (复权/缺失填充/冻结检测)
- 分钟级回测 + 滑点模型

---

## 六、文件结构

```
data/
├── history/               ← 已有 (日线 Parquet)
│   └── AAPL_1day.parquet
├── minutes/               ← 新建
│   ├── AAPL_2026-06-18_1min.parquet     (L0)
│   ├── AAPL_2026-06-18_extended.parquet  (L0 夜盘)
│   ├── AAPL_5min.parquet                 (L1 第二期)
│   └── AAPL_15min.parquet                (L1 第二期)
├── features/              ← 新建
│   └── AAPL_features.parquet             (L2)
├── trade.db               ← 已有
├── portfolio_strategy.yaml ← 已有
├── alerts_today.md         ← 已有
└── monitor_status.txt      ← 已有
```

---

## 七、验证

```bash
# L0 分钟线存储
python3 -c "
from src.data.minute_store import MinuteStore
ms = MinuteStore('AAPL')
ms.append('09:30', 300.00, 298.00, 300.50, 299.80, 1.2e6)
print(ms.get_series(5))
"

# L2 特征缓存
python3 -c "
from src.data.feature_cache import FeatureCache
fc = FeatureCache('AAPL')
fc.update_from_minute(ms)
print(fc.latest())
"

# 守护进程 (含异常检测)
python3 scripts/monitor_daemon.py
```
