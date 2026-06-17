# ============================================
# IBKR 交易助手 — Makefile
# 包含本地运行 + 远程服务器同步
# ============================================

# --- 远程服务器配置（优先从 .env 读取）---
-include .env
REMOTE_HOST ?= your_remote_host
REMOTE_PORT ?= 22
REMOTE_USER ?= your_username
REMOTE_DIR  ?= /home/your_username/project

# --- 同步排除项 ---
RSYNC_EXCLUDE := --exclude '.git' \
                 --exclude '__pycache__' \
                 --exclude '*.pyc' \
                 --exclude '.DS_Store' \
                 --exclude '.venv' \
                 --exclude 'venv' \
                 --exclude '.pytest_cache' \
                 --exclude '*.egg-info' \
                 --exclude 'data/' \
                 --exclude 'output/' \
                 --exclude 'logs/' \
                 --exclude 'notebooks/' \
                 --exclude '.env' \
                 --exclude 'backtest_results/'

SSH_CMD := ssh -p $(REMOTE_PORT) $(REMOTE_USER)@$(REMOTE_HOST)

# ============================================
# 本地命令
# ============================================

# 安装依赖
install:
	pip install -r requirements.txt

# 运行测试
test:
	pytest tests/ -v

# 运行策略（示例：make run-strategy STRATEGY=momentum）
run-strategy:
	python scripts/run_strategy.py --strategy $(STRATEGY)

# 下载历史数据（示例：make download STOCK=AAPL DAYS=365）
download:
	python scripts/download_history.py --stock $(STOCK) --days $(DAYS)

# 本地回测
backtest:
	python scripts/backtest.py --strategy $(STRATEGY) --stock $(STOCK)

# ============================================
# 远程命令
# ============================================

# 同步代码到远程
sync:
	rsync -avz $(RSYNC_EXCLUDE) -e "ssh -p $(REMOTE_PORT)" \
	      ./ $(REMOTE_USER)@$(REMOTE_HOST):$(REMOTE_DIR)/

# 查看远程状态
status:
	$(SSH_CMD) "cd $(REMOTE_DIR) && ls -la && echo '---' && python -c 'import ib_insync; print(f\"ib_insync: {ib_insync.__version__}\")'"

# 远程安装依赖
remote-install:
	$(SSH_CMD) "cd $(REMOTE_DIR) && pip install -r requirements.txt"

# 远程批量回测
remote-backtest:
	$(SSH_CMD) "cd $(REMOTE_DIR) && python scripts/remote/batch_backtest.py"

# 远程参数优化
remote-optimize:
	$(SSH_CMD) "cd $(REMOTE_DIR) && python scripts/remote/param_optimize.py"

# 从远程拉取结果
fetch-results:
	rsync -avz -e "ssh -p $(REMOTE_PORT)" \
	      $(REMOTE_USER)@$(REMOTE_HOST):$(REMOTE_DIR)/backtest_results/ ./backtest_results/

# 一键同步+运行
deploy: sync
	$(SSH_CMD) "cd $(REMOTE_DIR) && python scripts/remote/batch_backtest.py"

.PHONY: install test run-strategy download backtest sync status remote-install remote-backtest remote-optimize fetch-results deploy
