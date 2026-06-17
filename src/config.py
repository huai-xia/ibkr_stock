"""
配置加载器
从 .env + YAML 文件加载配置
"""

import os
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

# 项目根目录
ROOT_DIR = Path(__file__).parent.parent

# 加载 .env
load_dotenv(ROOT_DIR / ".env")


def _load_yaml(filename: str) -> dict:
    """加载 YAML 配置文件"""
    path = ROOT_DIR / "config" / filename
    if path.exists():
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    return {}


# 全局配置
settings = _load_yaml("settings.yaml")

# 风控规则
risk_rules = _load_yaml("risk_rules.yaml") if (ROOT_DIR / "config" / "risk_rules.yaml").exists() else {}


def get_env(key: str, default: Any = None) -> Optional[str]:
    """从 .env 获取环境变量"""
    return os.getenv(key, default)


# 常用配置快捷访问
IBKR_HOST = get_env("IBKR_HOST", settings.get("connection", {}).get("host", "127.0.0.1"))
IBKR_PORT = int(get_env("IBKR_PORT", settings.get("connection", {}).get("port", 4001)))
IBKR_CLIENT_ID = int(get_env("IBKR_CLIENT_ID", settings.get("connection", {}).get("client_id", 1)))

WECHAT_WEBHOOK_URL = get_env("WECHAT_WEBHOOK_URL", "")
FINNHUB_API_KEY = get_env("FINNHUB_API_KEY", "")

# 远程服务器
REMOTE_HOST = get_env("REMOTE_HOST", "your_remote_host")
REMOTE_PORT = int(get_env("REMOTE_PORT", 22))
REMOTE_USER = get_env("REMOTE_USER", "your_username")
REMOTE_DIR = get_env("REMOTE_DIR", "/home/your_username/project")


def get_risk_config() -> dict:
    """获取风控配置（settings.yaml > risk_rules.yaml > 默认值）"""
    return {
        "pdt_max_day_trades": risk_rules.get("pdt", {}).get("max_day_trades", 3),
        "pdt_warning_threshold": risk_rules.get("pdt", {}).get("warning_threshold", 2),
        "max_single_stock_pct": risk_rules.get("position", {}).get("max_single_stock_pct", 0.20),
        "max_total_position_pct": risk_rules.get("position", {}).get("max_total_position_pct", 0.80),
        "daily_loss_pct": risk_rules.get("circuit_breaker", {}).get("daily_loss_pct", 0.05),
        "consecutive_losses": risk_rules.get("circuit_breaker", {}).get("consecutive_losses", 3),
    }
