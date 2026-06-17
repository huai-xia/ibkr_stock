"""
邮件通知模块
支持 QQ邮箱 / Gmail / 163 等 SMTP 服务
"""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

logger = logging.getLogger(__name__)


class EmailNotifier:
    """
    邮件通知

    使用方法:
        notifier = EmailNotifier(
            smtp_host="smtp.qq.com",
            smtp_port=587,
            user="your_email@qq.com",
            password="your_smtp_auth_code",  # QQ邮箱需用授权码，不是密码！
        )
        notifier.send("交易提醒", "AAPL 已成交")
    """

    def __init__(
        self,
        smtp_host: str = "smtp.qq.com",
        smtp_port: int = 587,
        user: str = "",
        password: str = "",
        to_email: str = "",
        enabled: bool = True,
    ):
        self._host = smtp_host
        self._port = smtp_port
        self._user = user
        self._password = password
        self._to = to_email or user  # 默认发给自己
        self._enabled = enabled

    @property
    def is_configured(self) -> bool:
        return bool(self._user and self._password)

    def send(self, subject: str, body: str, html: bool = False) -> bool:
        """发送邮件"""
        if not self._enabled:
            return False
        if not self.is_configured:
            logger.debug("邮件未配置，跳过发送")
            return False

        try:
            msg = MIMEMultipart()
            msg["From"] = self._user
            msg["To"] = self._to
            msg["Subject"] = f"[IBKR] {subject}"

            content_type = "html" if html else "plain"
            msg.attach(MIMEText(body, content_type, "utf-8"))

            with smtplib.SMTP(self._host, self._port, timeout=10) as server:
                server.starttls()
                server.login(self._user, self._password)
                server.sendmail(self._user, self._to, msg.as_string())

            logger.debug("邮件发送成功: %s", subject)
            return True
        except Exception as e:
            logger.error("邮件发送失败: %s", e)
            return False

    def trade_filled(self, symbol: str, action: str, quantity: float,
                     price: float, pnl: float = None) -> bool:
        """订单成交通知"""
        subject = f"{'买入' if action.upper() == 'BUY' else '卖出'} {symbol} {quantity:.0f}股 @ ${price:.2f}"
        body = f"""
        <h3>订单成交</h3>
        <table>
            <tr><td>股票</td><td>{symbol}</td></tr>
            <tr><td>方向</td><td>{action.upper()}</td></tr>
            <tr><td>数量</td><td>{quantity:.0f} 股</td></tr>
            <tr><td>价格</td><td>${price:.2f}</td></tr>
            {f'<tr><td>盈亏</td><td>${pnl:.2f}</td></tr>' if pnl else ''}
            <tr><td>时间</td><td>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</td></tr>
        </table>
        """
        return self.send(subject, body, html=True)

    def alert(self, title: str, content: str) -> bool:
        """告警通知"""
        return self.send(f"⚠️ {title}", content)


# ============================================================
# QQ邮箱 快捷配置
# ============================================================

def qq_email_notifier(user: str, auth_code: str) -> EmailNotifier:
    """
    创建 QQ邮箱通知器

    Args:
        user: QQ邮箱地址，如 "123456789@qq.com"
        auth_code: QQ邮箱 SMTP 授权码（非QQ密码！）
                   获取方式：QQ邮箱 → 设置 → 账户 → POP3/SMTP → 开启 → 生成授权码
    """
    return EmailNotifier(
        smtp_host="smtp.qq.com",
        smtp_port=587,
        user=user,
        password=auth_code,
        to_email=user,
    )
