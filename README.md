# IBKR 智能股票交易助手

基于 Interactive Brokers API 的模块化 Python 股票交易助手。

## 功能概览

- 🔌 **连接管理**：自动连接 IB Gateway/TWS，心跳检测，断线重连
- 📊 **市场数据**：实时行情订阅 + 历史K线（自动缓存）
- 📝 **交易执行**：市价单/限价单/止损单，持仓与账户查询
- 🛡️ **智能风控**：PDT 日内交易限制、仓位管理、熔断机制
- 📰 **时政分析**：财经新闻获取 → FinBERT 情感评分 → 风险等级调整
- 📱 **微信通知**：交易信号、PDT 预警、时政告警实时推送
- 📈 **策略引擎**：统一接口，内置动量/回归策略，方便扩展
- ⏮️ **回测系统**：本地小规模 + 远程 113 服务器大规模批量回测
- 📚 **学习文档**：零基础到进阶的金融知识体系

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 IBKR 账户信息和微信 Webhook URL
```

### 3. 启动 IB Gateway

在客户端启动 IB Gateway（Paper Trading 推荐），确保 API 端口开放（默认 4001）。

### 4. 测试连接

```bash
python -m src.cli.main status
```

### 5. 获取行情

```bash
python -m src.cli.main quote AAPL
python -m src.cli.main history AAPL --days 30
```

## 项目结构

```
src/
├── core/          连接管理、合约工厂
├── data/          行情数据、历史K线、缓存
├── trade/         下单、持仓、风控、交易记录
├── strategy/      策略基类、技术指标、内置策略
├── backtest/      回测引擎、绩效分析
├── news/          新闻获取、情感分析
├── notify/        企业微信推送、邮件通知
└── cli/           命令行入口
config/            策略参数与风控规则 YAML 配置
docs/              金融知识学习文档
scripts/           批量脚本 + 远程 113 执行脚本
```

## 远程计算

大规模参数优化和批量回测可以同步到 113 服务器执行：

```bash
make sync              # 同步代码到 113
make remote-backtest   # 远程批量回测
make fetch-results     # 拉取结果到本地
```

## 文档

- [📋 项目计划书](PROJECT_PLAN.md)
- [📚 学习文档](docs/README.md)
- [⚠️ PDT 规则](docs/03-regulations/pdt-rule.md)
