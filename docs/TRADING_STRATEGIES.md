# 交易策略体系 — 子项目规划

> 三维分类：时间周期 × 策略逻辑 × 执行方式

---

## 一、策略体系总览

```
                        策略体系
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    时间周期           策略逻辑          执行方式
   (持仓多久)        (凭什么赚钱)       (怎么买卖)
          │                │                │
  短线·中线·长线   趋势·回归·动量·  一次性·定投·
                  价值·成长·套利·   分批·动态调仓
                  事件·网格·分红
```

每个策略 = 时间周期 × 策略逻辑 × 执行方式 的组合

例: "短线均值回归 + ATR止损 + 一次性建仓" = MDP当前方案

---

## 二、时间周期

| 类型 | 持仓 | K线粒度 | 交易频率 | 当前状态 |
|------|------|----------|:--:|:--:|
| 超短线 | 秒~分钟 | Tick/1min | 极高 | ⬜ |
| 日内 | 分钟~小时 | 1min/5min | 高 | ⬜ |
| **短线** | 1~5天 | 5min/30min/日 | 中高 | ✅ 已有MDP |
| **中线** | 1周~3月 | 日/周 | 中 | ⬜ |
| **长线** | 3月~3年 | 周/月 | 低 | ⬜ |
| 超长线 | 3年+ | 月/季 | 极低 | ⬜ |

---

## 三、策略逻辑

| 类型 | 核心逻辑 | 适合周期 | 代表算法 | 状态 |
|------|----------|:--:|------|:--:|
| **均值回归** | 涨多卖，跌多买 | 1~5天 | 布林带/RSI/Z-Score | ✅ 已集成 purchase_advisor |
| **动量交易** | 强者恒强 | 3~10天 | 均线金叉/ROC | ✅ 已集成 purchase_advisor |
| **趋势跟踪** | 顺势，截亏让利润跑 | 数周~数月 | MA交叉/ADX/通道突破 | 🔜 预留接口 |
| **突破交易** | 关键位突破跟进 | 3~10天 | 开盘区间/前高突破 | 🔜 预留接口 |
| **事件驱动** | 财报/并购/政策→波动 | 1~7天 | 新闻情感+波动率 | 🔜 预留接口 |
| **价值投资** | 买低估等回归 | 长线 | PE/PB/ROE/DCF | 🔜 预留接口 |
| **成长投资** | 买高增长享复利 | 长线 | 营收增速/毛利率 | 🔜 预留接口 |
| **分红策略** | 稳定现金流 | 长线 | 股息率/派息历史 | 🔜 预留接口 |
| **统计套利** | 配对协整→价差收敛 | 短/中线 | 协整检验/卡尔曼滤波 | 🔜 预留接口 |
| **网格交易** | 区间低买高卖 | 短/中线 | 布林带/ATR网格 | 🔜 预留接口 |

> **2026-06-29 更新**: 均值回归和动量交易已集成到 `src/analysis/purchase_advisor.py`。
> 系统通过 `STRATEGY_REGISTRY` 管理策略，分析股票状态指纹后自动评分推荐最优策略，
> 用户也可通过 `--strategy` 手动覆盖。趋势跟踪/突破/事件驱动已预留配置接口，
> 算法基类 `PurchaseAlgorithm` 支持后续扩展。

---

## 四、执行方式

| 方式 | 说明 | 适用周期 | 状态 |
|------|------|:--:|:--:|
| 一次性建仓 | 信号触发全部买入 | 全周期 | ✅ 已有 |
| **定投(DCA)** | 定期定额，摊平成本 | 长线 | ⬜ |
| 价值平均法 | 目标市值增长路径 | 长线 | ⬜ |
| 分批建仓 | 分2-3次入场降低择时风险 | 中/长线 | ⬜ |
| 动态调仓 | 根据波动率/趋势调整仓位 | 全周期 | ⬜ |
| 网格执行 | 区间内挂限价单自动成交 | 短/中线 | ⬜ |

---

## 五、代码结构规划

```
src/strategy/
├── base.py                    ← 策略基类（已有）
├── indicators.py              ← 技术指标库（已有）
├── signals.py                 ← 信号定义（已有）
│
├── short_term/                ← 短线策略
│   ├── __init__.py
│   ├── anomaly_detector.py   ← 迁移: 异常检测
│   ├── exit_strategy.py      ← 迁移: 退出策略
│   ├── mean_reversion.py     ← 均值回归
│   ├── momentum_scalp.py     ← 动量剥头皮
│   ├── breakout_intraday.py  ← 日内突破
│   └── vwap_trade.py         ← VWAP交易
│
├── medium_term/               ← 中线策略
│   ├── __init__.py
│   ├── trend_following.py    ← 趋势跟踪
│   ├── swing_trade.py        ← 波段交易
│   ├── event_driven.py       ← 事件驱动
│   └── sector_rotation.py    ← 板块轮动
│
├── long_term/                 ← 长线策略
│   ├── __init__.py
│   ├── value_invest.py       ← 价值投资
│   ├── growth_invest.py      ← 成长投资
│   ├── dividend.py           ← 分红策略
│   └── macro_driven.py       ← 宏观驱动
│
├── execution/                 ← 执行方式（跨周期通用）
│   ├── __init__.py
│   ├── dca.py                ← 定投
│   ├── value_avg.py          ← 价值平均法
│   ├── batch_entry.py        ← 分批建仓
│   ├── dynamic_sizer.py      ← 动态调仓
│   └── grid_executor.py      ← 网格执行
│
└── risk/                      ← 统一风控
    ├── __init__.py
    ├── position_sizer.py     ← 仓位计算(Kelly/等权)
    ├── stop_loss.py          ← 止损策略库(ATR/均线/挂单)
    └── portfolio_risk.py     ← 组合风控(相关性/VaR)
```

---

## 六、现有代码迁移计划

| 当前路径 | 目标路径 | 改动 |
|------|------|------|
| `src/analysis/anomaly.py` | `src/strategy/short_term/anomaly_detector.py` | 移动 |
| `src/analysis/exit_strategy.py` | `src/strategy/short_term/exit_strategy.py` | 移动 |
| `src/strategy/builtin/momentum.py` | `src/strategy/short_term/momentum_scalp.py` | 移动 |
| `src/strategy/builtin/mean_reversion.py` | `src/strategy/short_term/mean_reversion.py` | 移动 |
| `src/strategy/indicators.py` | 保留 | 不变 |
| `src/strategy/signals.py` | 保留 | 不变 |
| `src/strategy/base.py` | 保留 | 不变 |
| `src/data/minute_store.py` | 保留 | 不变(数据层通用) |
| `src/data/feature_cache.py` | 保留 | 不变(数据层通用) |

---

## 七、实施路线图

### ✅ 第一阶段：结构重组 + purchase_advisor 集成 (2026-06-29 完成)

- [x] 创建 `src/analysis/purchase_advisor.py` — 智能购买建议引擎
- [x] 实现 `STRATEGY_REGISTRY` — 策略配置注册表
- [x] 实现 `PurchaseAlgorithm` 基类 — 可插拔算法接口
- [x] 实现 `RuleBasedAlgorithm` — 参数化规则算法
- [x] 实现策略自动评分 — 股票状态指纹 → 策略匹配
- [x] CLI `buy-advice` 命令 — 支持自动推荐和 `--strategy` 手动覆盖
- [x] 持仓追踪存储 — `--save` 写入 `portfolio_strategy.yaml`
- [x] 均值回归 (mean_reversion) — 完整集成: 信号+止损+止盈
- [x] 动量交易 (momentum) — 完整集成: 信号+止损+止盈

### 🔜 第二阶段：预留策略实现（优先级排序）

| 策略 | 数据需求 | 工作量 | 说明 |
|------|------|:--:|------|
| 趋势跟踪 | 日线+MA+ADX | 小 | ADX 指标已部分存在，主要补趋势强度评分 |
| 突破交易 | 日线+布林带宽 | 小 | 布林带宽已有，需补突破确认逻辑 |
| 事件驱动 | 新闻API+波动率 | 中 | 已有 news/ 模块，需整合新闻打分 |

### ⬜ 第三阶段：执行方式

- 定投(DCA) + 分批建仓 + 动态调仓

### ⬜ 第四阶段：长线策略

- 价值/成长/分红 — 需接入基本面数据

---

## 八、与现有系统关系

```
Market Data Pipeline (MDP)
  ├── 1min K线 → 短线策略
  ├── 日线     → 中线策略
  └── 周/月线  → 长线策略

策略体系 (本项目)
  ├── short_term/  ← MDP异常检测+退出策略
  ├── medium_term/ ← 趋势跟踪+波段
  ├── long_term/   ← 价值+成长+分红
  ├── execution/   ← 定投+分批+调仓
  └── risk/        ← 统一仓位+止损

交易执行层 (已有)
  └── src/trade/  ← 下单+持仓+记录
```
