"""Получение актуальных цен: крипто (CoinGecko) и акции (Yahoo Finance)."""

import logging
import re

import httpx

logger = logging.getLogger(__name__)

# Маппинг тикеров на CoinGecko IDs
CRYPTO_MAP: dict[str, str] = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "xrp": "ripple",
    "doge": "dogecoin",
    "ada": "cardano",
    "avax": "avalanche-2",
    "matic": "matic-network",
    "dot": "polkadot",
    "link": "chainlink",
    "bnb": "binancecoin",
}

# Паттерны для извлечения тикеров из вопросов
CRYPTO_PATTERN = re.compile(
    r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|cardano|ada|"
    r"avalanche|avax|matic|polkadot|dot|chainlink|link|bnb)\b",
    re.IGNORECASE,
)
STOCK_PATTERN = re.compile(
    r"\b([A-Z]{1,5})\b.*?\b(?:stock|share|close|finish|dip|above|below)\b"
    r"|\b(?:stock|share|close|finish|dip|above|below)\b.*?\b([A-Z]{1,5})\b",
    re.IGNORECASE,
)
# Прямой маппинг известных тикеров в скобках, например "Apple (AAPL)"
TICKER_IN_PARENS = re.compile(r"\(([A-Z]{1,5})\)")


class PriceProvider:
    """Получает актуальные цены крипто и акций."""

    def __init__(self) -> None:
        self.client = httpx.Client(timeout=10.0)
        self._crypto_cache: dict[str, float] = {}
        self._stock_cache: dict[str, float] = {}

    def get_crypto_price(self, coin_id: str) -> float | None:
        """Получить цену криптовалюты через CoinGecko (бесплатный API)."""
        cg_id = CRYPTO_MAP.get(coin_id.lower(), coin_id.lower())

        if cg_id in self._crypto_cache:
            return self._crypto_cache[cg_id]

        try:
            resp = self.client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": cg_id, "vs_currencies": "usd"},
            )
            if resp.status_code == 200:
                data = resp.json()
                price = data.get(cg_id, {}).get("usd")
                if price:
                    self._crypto_cache[cg_id] = float(price)
                    logger.debug("Крипто цена %s: $%.2f", cg_id, price)
                    return float(price)
        except Exception as e:
            logger.debug("Ошибка получения цены %s: %s", cg_id, e)
        return None

    def get_stock_price(self, ticker: str) -> float | None:
        """Получить цену акции через Yahoo Finance (бесплатный endpoint)."""
        ticker = ticker.upper()

        if ticker in self._stock_cache:
            return self._stock_cache[ticker]

        try:
            resp = self.client.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                params={"interval": "1d", "range": "1d"},
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data.get("chart", {}).get("result", [{}])[0]
                meta = result.get("meta", {})
                price = meta.get("regularMarketPrice")
                if price:
                    self._stock_cache[ticker] = float(price)
                    logger.debug("Акция %s: $%.2f", ticker, price)
                    return float(price)
        except Exception as e:
            logger.debug("Ошибка получения цены %s: %s", ticker, e)
        return None

    def enrich_market_context(self, question: str) -> str:
        """Извлечь тикеры из вопроса рынка и получить актуальные цены."""
        context_parts: list[str] = []

        # Крипто
        crypto_matches = CRYPTO_PATTERN.findall(question)
        for match in crypto_matches:
            price = self.get_crypto_price(match)
            if price:
                context_parts.append(f"Current {match.upper()} price: ${price:,.2f}")

        # Акции (тикер в скобках)
        stock_matches = TICKER_IN_PARENS.findall(question)
        for ticker in stock_matches:
            if len(ticker) >= 1 and ticker.isalpha():
                price = self.get_stock_price(ticker)
                if price:
                    context_parts.append(f"Current {ticker} stock price: ${price:,.2f}")

        # S&P 500
        if "s&p 500" in question.lower() or "spx" in question.lower():
            price = self.get_stock_price("^GSPC")
            if price:
                context_parts.append(f"Current S&P 500: ${price:,.2f}")

        if context_parts:
            return "\n**Real-time price data:**\n" + "\n".join(
                f"- {p}" for p in context_parts
            )
        return ""

    def close(self) -> None:
        self.client.close()
