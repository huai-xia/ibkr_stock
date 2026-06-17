#!/bin/bash
# ============================================
# IBKR 收盘简报 — 定时任务脚本
# 由 launchd 每天 16:15 ET 自动执行
# ============================================

cd "$(dirname "$0")/.." || exit 1

# 记录日志
LOG="data/briefing_cron.log"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始生成收盘简报..." >> "$LOG"

# 执行简报（超时 120 秒）
timeout 120 python3 -m src.cli.main --port 4002 briefing >> "$LOG" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 完成" >> "$LOG"
