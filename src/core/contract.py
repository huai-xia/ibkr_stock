"""
合约工厂
简化 IBKR 合约（Stock / Option / Future 等）的创建
"""

from typing import Optional

from ib_insync import IB, Stock, Option, Future, Contract


class ContractFactory:
    """
    合约工厂，快速创建各类金融合约

    使用方法:
        factory = ContractFactory(ib)
        aapl = factory.stock("AAPL")
        spy_opt = factory.option("SPY", expiry="20260630", strike=450, right="C")
    """

    def __init__(self, ib: IB):
        self._ib = ib

    def stock(
        self,
        symbol: str,
        exchange: str = "SMART",
        currency: str = "USD",
        primary_exchange: Optional[str] = None,
    ) -> Stock:
        """
        创建美股合约

        Args:
            symbol: 股票代码，如 "AAPL", "TSLA"
            exchange: 交易所，默认 SMART（IB 智能路由）
            currency: 货币，默认 USD
            primary_exchange: 主交易所（如 "NASDAQ"），可选

        Returns:
            已 qualify 的 Stock 合约
        """
        contract = Stock(symbol, exchange, currency)
        if primary_exchange:
            contract.primaryExchange = primary_exchange

        return self._qualify(contract)

    def option(
        self,
        symbol: str,
        expiry: str,
        strike: float,
        right: str,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Option:
        """
        创建期权合约

        Args:
            symbol: 标的股票代码
            expiry: 到期日，格式 "YYYYMMDD"
            strike: 行权价
            right: "C"=看涨期权, "P"=看跌期权
            exchange: 交易所
            currency: 货币

        Returns:
            已 qualify 的 Option 合约
        """
        contract = Option(symbol, expiry, strike, right, exchange)
        contract.currency = currency
        return self._qualify(contract)

    def forex(self, pair: str = "EUR.USD") -> Contract:
        """
        创建外汇合约

        Args:
            pair: 货币对，如 "EUR.USD"

        Returns:
            已 qualify 的外汇合约
        """
        from ib_insync import Forex
        contract = Forex(pair)
        return self._qualify(contract)

    def _qualify(self, contract: Contract) -> Contract:
        """
        验证并填充合约详情

        ib_insync 的 qualifyContracts 会向 IBKR 请求合约细节
        （conId, 交易所列表, 乘数等），确保下单时信息完整。
        """
        qualified = self._ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"无法 qualify 合约: {contract.symbol}")
        return qualified[0]

    def details(self, contract: Contract) -> list:
        """获取合约的详细信息（交易所列表、乘数等）"""
        return self._ib.reqContractDetails(contract)


# ============================================================
# 常用美股合约快捷创建函数（无需先创建 ContractFactory）
# ============================================================

def stock(symbol: str, exchange: str = "SMART", currency: str = "USD") -> Stock:
    """
    快捷创建 Stock 合约（未 qualify，需要 IB 连接后 qualify）

    用于不需要 qualify 的场景（如 reqHistoricalData）
    """
    return Stock(symbol, exchange, currency)
