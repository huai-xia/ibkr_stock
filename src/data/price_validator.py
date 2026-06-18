"""
实时价格校验器
多渠道交叉验证：IBKR + Yahoo Finance + Finnhub（可选）

使用方法:
    validator = PriceValidator(ib)
    result = validator.get_price("AAPL")
    print(result)  # {"price": 299.24, "sources": ["ibkr", "yahoo"], "confidence": "high"}
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PriceResult:
    """价格查询结果"""
    symbol: str = ""
    price: float = 0.0                    # 最终采用价格
    sources: list[str] = field(default_factory=list)  # 成功获取的渠道
    details: dict = field(default_factory=dict)       # 各渠道详情
    confidence: str = "low"               # high / medium / low
    timestamp: str = ""                   # 查询时间
    is_realtime: bool = False             # 是否实时数据
    warning: str = ""                     # 警告信息
    spread: float = 0.0                   # 买卖价差
    # 盘前盘后
    session: str = "closed"               # regular / pre_market / after_hours / closed
    pre_market_price: float = 0.0         # 盘前价格
    post_market_price: float = 0.0        # 盘后价格
    extended_hours_warning: str = ""      # 延长时段风险提示


class PriceValidator:
    """
    多渠道价格校验器

    优先级: IBKR > Yahoo > Finnhub
    策略: 取成功渠道的中位数，偏差 > 2% 时告警
    """

    def __init__(self, ib=None, finnhub_key: str = ""):
        self._ib = ib
        self._finnhub_key = finnhub_key

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def get_price(self, symbol: str, timeout: float = 3.0) -> PriceResult:
        """
        多渠道获取实时价格

        Returns:
            PriceResult（含最终价格、各渠道详情、可信度）
        """
        result = PriceResult(
            symbol=symbol.upper(),
            timestamp=datetime.now().isoformat(),
        )

        prices = {}
        details = {}

        # 1. IBKR（主）
        ibkr_price = self._from_ibkr(symbol, timeout)
        if ibkr_price:
            prices["ibkr"] = ibkr_price["price"]
            details["ibkr"] = ibkr_price
            result.is_realtime = True

        # 2. Yahoo Finance（备用）
        yahoo_price = self._from_yahoo(symbol)
        if yahoo_price:
            prices["yahoo"] = yahoo_price["price"]
            details["yahoo"] = yahoo_price
            if not result.is_realtime and yahoo_price.get("is_realtime"):
                result.is_realtime = True

        # 3. Finnhub（备用）
        finnhub_price = self._from_finnhub(symbol)
        if finnhub_price:
            prices["finnhub"] = finnhub_price["price"]
            details["finnhub"] = finnhub_price

        # 4. 富途 OpenD（实时 + 盘前盘后）
        futu_price = self._from_futu(symbol)
        if futu_price:
            prices["futu"] = futu_price["price"]
            details["futu"] = futu_price
            if not result.is_realtime and futu_price.get("is_realtime"):
                result.is_realtime = True

        result.sources = list(prices.keys())
        result.details = details

        if not prices:
            result.price = 0.0
            result.confidence = "low"
            result.warning = f"所有渠道均无法获取 {symbol} 的实时价格"
            return result

        # ── 取价格：非盘中用富途，盘中多源交叉验证 ──
        futu_d = details.get("futu", {})
        non_futu_prices = [v["price"] for k, v in details.items() if k != "futu"]

        if futu_d and non_futu_prices:
            avg_other = sum(non_futu_prices) / len(non_futu_prices)
            deviation = abs(futu_d["price"] - avg_other) / avg_other if avg_other > 0 else 0
            if deviation > 0.02:
                # 非盘中（Yahoo/Finnhub 仍显示昨收）：直接信任富途
                result.price = round(futu_d["price"], 2)
                result.is_realtime = True
            else:
                # 盘中：多源取中位数
                all_p = [futu_d["price"]] + non_futu_prices
                all_p.sort()
                result.price = round(all_p[len(all_p) // 2], 2) if len(all_p) % 2 == 1 else round((all_p[len(all_p)//2-1] + all_p[len(all_p)//2]) / 2, 2)
        else:
            sorted_prices = sorted(prices.values())
            n = len(sorted_prices)
            result.price = round(sorted_prices[n // 2], 2) if n % 2 == 1 else round((sorted_prices[n // 2 - 1] + sorted_prices[n // 2]) / 2, 2)

        # ── 可信度评估 ──
        all_prices = list(prices.values())
        price_range = max(all_prices) - min(all_prices) if all_prices else 0
        pct_deviation = price_range / result.price * 100 if result.price > 0 else 0

        if len(prices) >= 2 and pct_deviation < 0.5:
            result.confidence = "high"
        elif len(prices) >= 2 and pct_deviation < 2.0:
            result.confidence = "medium"
        elif len(prices) >= 1:
            result.confidence = "medium"
        else:
            result.confidence = "low"

        if pct_deviation > 2.0:
            result.warning = (
                f"⚠️ 多源价格偏差 {pct_deviation:.1f}%，"
                f"已取中位数 ${result.price:.2f}。"
                f"请人工确认。各渠道: "
                + ", ".join(f"{k}=${v:.2f}" for k, v in prices.items())
            )

        # 买卖价差（仅 IBKR 可提供）
        if "ibkr" in details:
            bid = details["ibkr"].get("bid", 0)
            ask = details["ibkr"].get("ask", 0)
            if bid > 0 and ask > 0:
                result.spread = round((ask - bid) / result.price * 100, 2)

        # ── 盘前盘后处理 ──
        # 从 Yahoo 获取延长时段价格
        if "yahoo" in details:
            yh = details["yahoo"]
            result.pre_market_price = yh.get("pre_market_price", 0)
            result.post_market_price = yh.get("post_market_price", 0)
            state = yh.get("market_state", "CLOSED")

            if state in ("PRE", "PREPRE"):
                result.session = "pre_market"
                result.extended_hours_warning = (
                    "⚠️ 当前为盘前交易时段，流动性极差，价格仅作参考。"
                    "建议等待开盘(9:30 ET)后再做决策，如需操作务必使用限价单。"
                )
            elif state in ("POST", "POSTPOST"):
                result.session = "after_hours"
                result.extended_hours_warning = (
                    "⚠️ 当前为盘后交易时段，流动性极差。"
                    "价格波动可能不代表明日开盘方向。如需操作务必使用限价单。"
                )
            elif state == "REGULAR":
                result.session = "regular"
            else:
                result.session = "closed"

        # 补充盘前盘后价格
        if result.pre_market_price > 0:
            pass  # yahoo 已提供
        if result.post_market_price > 0:
            pass  # yahoo 已提供

        return result

    # ------------------------------------------------------------------
    # 各渠道实现
    # ------------------------------------------------------------------

    def _from_ibkr(self, symbol: str, timeout: float) -> Optional[dict]:
        """IBKR 实时报价"""
        if self._ib is None or not self._ib.isConnected():
            return None

        try:
            from ib_insync import Stock
            contract = Stock(symbol, "SMART", "USD")
            self._ib.qualifyContracts(contract)
            ticker = self._ib.reqMktData(contract)
            self._ib.sleep(timeout)

            if ticker.last and ticker.last > 0:
                return {
                    "price": round(ticker.last, 2),
                    "bid": round(ticker.bid, 2) if ticker.bid > 0 else 0,
                    "ask": round(ticker.ask, 2) if ticker.ask > 0 else 0,
                    "volume": ticker.volume,
                    "is_realtime": True,
                }
            elif ticker.close and ticker.close > 0:
                return {
                    "price": round(ticker.close, 2),
                    "bid": 0, "ask": 0, "volume": 0,
                    "is_realtime": False,
                }
        except Exception as e:
            logger.debug("IBKR 报价失败 %s: %s", symbol, e)
        return None

    def _from_yahoo(self, symbol: str) -> Optional[dict]:
        """Yahoo Finance 报价（含盘前盘后）"""
        try:
            import yfinance as yf
            ticker = yf.Ticker(symbol)

            info = ticker.info or {}
            market_state = info.get("marketState", "CLOSED")

            # 判断时段
            pre_market = info.get("preMarketPrice", 0)
            post_market = info.get("postMarketPrice", 0)

            # 优先取当前活跃时段的价格
            if market_state == "PRE" and pre_market > 0:
                price = pre_market
                is_rt = True
            elif market_state == "PREPRE" and pre_market > 0:
                price = pre_market
                is_rt = True
            elif market_state == "POST" and post_market > 0:
                price = post_market
                is_rt = True
            elif market_state == "POSTPOST" and post_market > 0:
                price = post_market
                is_rt = True
            else:
                # fast_info
                try:
                    fast = ticker.fast_info
                    price = fast.last_price if fast.last_price else fast.regular_market_previous_close
                    is_rt = market_state == "REGULAR"
                except:
                    price = info.get("regularMarketPrice") or info.get("currentPrice") or info.get("previousClose", 0)
                    is_rt = market_state == "REGULAR"

            if price and price > 0:
                return {
                    "price": round(price, 2),
                    "bid": 0, "ask": 0,
                    "volume": info.get("volume", 0),
                    "is_realtime": is_rt,
                    "market_state": market_state,
                    "pre_market_price": round(pre_market, 2) if pre_market else 0,
                    "post_market_price": round(post_market, 2) if post_market else 0,
                }
        except Exception as e:
            logger.debug("Yahoo 报价失败 %s: %s", symbol, str(e)[:60])
        return None

    def _from_finnhub(self, symbol: str) -> Optional[dict]:
        """Finnhub 报价"""
        if not self._finnhub_key or "YOUR_KEY" in self._finnhub_key:
            return None
        try:
            import requests
            url = f"https://finnhub.io/api/v1/quote"
            resp = requests.get(url, params={
                "symbol": symbol,
                "token": self._finnhub_key,
            }, timeout=5)
            data = resp.json()
            price = data.get("c", 0)  # current price
            if price > 0:
                return {
                    "price": round(price, 2),
                    "bid": 0, "ask": 0, "volume": 0,
                    "is_realtime": True,
                }
        except Exception:
            pass
        return None

    def _from_futu(self, symbol: str) -> Optional[dict]:
        """富途 OpenD 实时报价（含盘前/盘后/夜盘，免费）"""
        try:
            from futu import OpenQuoteContext, RET_OK
            ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
            ret, data = ctx.get_market_snapshot([f'US.{symbol}'])
            ctx.close()

            if ret == RET_OK and len(data) > 0:
                row = data.iloc[0]
                last = row.get('last_price', 0)
                prev = row.get('prev_close_price', 0)
                bid = row.get('bid_price', 0)
                ask = row.get('ask_price', 0)

                # 延长时段数据
                overnight = row.get('overnight_price', 0)   # 夜盘 (20:00-4:00 ET)
                pre = row.get('pre_price', 0)               # 盘前 (4:00-9:30 ET)
                after = row.get('after_price', 0)           # 盘后 (16:00-20:00 ET)
                sec_status = str(row.get('sec_status', ''))

                # 智能选择当前有效时段的价格
                # 优先级: 夜盘 > 盘前 > 盘后 > 最新价
                # 注意:  bid/ask 在延长时段可能更新不及时，不作为主要价格来源
                if overnight > 0 and overnight != last:
                    price = overnight
                elif pre > 0 and pre != last:
                    price = pre
                elif after > 0 and after != last:
                    price = after
                else:
                    price = last

                if price > 0:
                    # last_price = 最近一次常规交易收盘价（作为涨跌基准）
                    # prev_close_price = 前一交易日收盘（跨日后可能滞后）
                    reference_close = last if last > 0 else prev
                    return {
                        "price": round(price, 2),
                        "bid": round(bid, 2) if bid else 0,
                        "ask": round(ask, 2) if ask else 0,
                        "volume": row.get('volume', 0),
                        "is_realtime": True,
                        "prev_close_price": round(reference_close, 2),
                        "pre_market_price": round(pre, 2) if pre else 0,
                        "post_market_price": round(after, 2) if after else 0,
                        "overnight_price": round(overnight, 2) if overnight else 0,
                        "sec_status": sec_status,
                    }
        except Exception as e:
            logger.debug("富途报价失败 %s: %s", symbol, str(e)[:60])
        return None

    # ------------------------------------------------------------------
    # 格式化
    # ------------------------------------------------------------------

    def format_result(self, result: PriceResult) -> str:
        """格式化为可读文本"""
        conf_emoji = {"high": "🟢", "medium": "🟡", "low": "🔴"}
        session_labels = {
            "regular": "📈 常规交易", "pre_market": "🌅 盘前",
            "after_hours": "🌇 盘后", "closed": "🔒 已收盘",
        }
        rt_label = "实时" if result.is_realtime else "延迟/收盘"
        session_label = session_labels.get(result.session, "")

        lines = [
            f"**{result.symbol}** ${result.price:.2f}  "
            f"{conf_emoji.get(result.confidence, '')} {result.confidence.upper()} "
            f"({rt_label}, {len(result.sources)}源) {session_label}",
        ]

        source_lines = []
        for src, detail in result.details.items():
            p = detail.get("price", 0)
            diff = ""
            if p != result.price:
                diff = f" (偏差 {(p-result.price)/result.price*100:+.1f}%)"
            source_lines.append(f"  {src}: ${p:.2f}{diff}")
        lines.extend(source_lines)

        # 盘前盘后价格
        if result.pre_market_price > 0:
            lines.append(f"  盘前: ${result.pre_market_price:.2f}")
        if result.post_market_price > 0:
            lines.append(f"  盘后: ${result.post_market_price:.2f}")

        if result.spread > 0:
            lines.append(f"  买卖价差: {result.spread:.2f}%")

        if result.warning:
            lines.append(f"  {result.warning}")
        if result.extended_hours_warning:
            lines.append(f"  {result.extended_hours_warning}")

        return "\n".join(lines)


# ── 快捷函数 ──

def quick_price(symbol: str, ib=None) -> Optional[float]:
    """快速获取价格（仅返回数字）"""
    v = PriceValidator(ib)
    r = v.get_price(symbol)
    return r.price if r.price > 0 else None
