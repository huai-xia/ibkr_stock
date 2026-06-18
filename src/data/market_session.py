"""
美股交易时段判断
自动将系统本地时间转换为美东时间，判断当前所处时段
"""

from datetime import datetime, timedelta


def get_market_session() -> dict:
    """
    判断当前美股交易时段

    Returns:
        {
            "session": "pre_market" | "regular" | "after_hours" | "overnight",
            "label": "盘前" | "盘中" | "盘后" | "夜盘",
            "price_field": "pre_price" | "last_price" | "after_price" | "overnight_price",
            "et_hour": float,  # 美东时间
            "is_extended": bool,  # 是否延长时段
        }
    """
    # 北京时间 → 美东时间 (EDT = BJT - 12h)
    bj_now = datetime.now()
    et_now = bj_now - timedelta(hours=12)
    et_hour = et_now.hour + et_now.minute / 60

    if 4 <= et_hour < 9.5:
        return {
            "session": "pre_market", "label": "盘前",
            "price_field": "pre_price", "et_hour": et_hour,
            "is_extended": True,
        }
    elif 9.5 <= et_hour < 16:
        return {
            "session": "regular", "label": "盘中",
            "price_field": "last_price", "et_hour": et_hour,
            "is_extended": False,
        }
    elif 16 <= et_hour < 20:
        return {
            "session": "after_hours", "label": "盘后",
            "price_field": "after_price", "et_hour": et_hour,
            "is_extended": True,
        }
    else:
        return {
            "session": "overnight", "label": "夜盘",
            "price_field": "overnight_price", "et_hour": et_hour,
            "is_extended": True,
        }


def et_now() -> datetime:
    """获取当前美东时间"""
    return datetime.now() - timedelta(hours=12)
