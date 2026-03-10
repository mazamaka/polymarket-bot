"""Live trading executor через Polymarket CLOB API."""

import logging

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType

from config import settings
from polymarket.models import TradeSignal

logger = logging.getLogger(__name__)


class LiveExecutor:
    """Исполнение реальных ордеров на Polymarket."""

    def __init__(self) -> None:
        if not settings.polygon_wallet_private_key:
            raise ValueError("POLYGON_WALLET_PRIVATE_KEY не установлен в .env")

        self.client = ClobClient(
            host=settings.clob_api_url,
            chain_id=settings.polygon_chain_id,
            key=settings.polygon_wallet_private_key,
        )
        # Derive API credentials (L2 auth для постинга ордеров)
        self.creds: ApiCreds | None = None
        self._init_creds()

    def _init_creds(self) -> None:
        """Получить или создать API ключи для L2 авторизации."""
        try:
            self.creds = self.client.derive_api_key()
            logger.info("API ключи получены (derive)")
        except Exception:
            try:
                self.creds = self.client.create_api_key()
                logger.info("API ключи созданы (create)")
            except Exception as e:
                logger.error("Не удалось получить API ключи: %s", e)
                raise

        self.client.creds = self.creds
        address = self.client.get_address()
        logger.info("Wallet: %s", address)

    def get_balance(self) -> float:
        """Получить баланс USDC на Polymarket."""
        try:
            bal = self.client.get_balance_allowance()
            if isinstance(bal, dict):
                return float(bal.get("balance", 0)) / 1e6  # USDC has 6 decimals
            return 0.0
        except Exception as e:
            logger.error("Ошибка получения баланса: %s", e)
            return 0.0

    def execute_limit_order(self, signal: TradeSignal) -> dict | None:
        """Разместить лимитный ордер."""
        market = signal.prediction
        token_id = self._get_token_id(signal)
        if not token_id:
            logger.error("Нет token_id для %s", market.question[:40])
            return None

        # Размер в shares = size_usd / price
        size = signal.size_usd / signal.price if signal.price > 0 else 0
        side = "BUY"

        order_args = OrderArgs(
            token_id=token_id,
            price=round(signal.price, 4),
            size=round(size, 2),
            side=side,
        )

        try:
            logger.info(
                "LIVE ORDER: %s %s @ %.4f | size: %.2f shares ($%.2f)",
                market.recommended_side,
                market.question[:40],
                signal.price,
                size,
                signal.size_usd,
            )
            result = self.client.create_and_post_order(order_args)
            logger.info("Order posted: %s", result)
            return result
        except Exception as e:
            logger.error("Ошибка размещения ордера: %s", e)
            return None

    def execute_market_order(self, signal: TradeSignal) -> dict | None:
        """Разместить маркет-ордер (Fill-or-Kill)."""
        token_id = self._get_token_id(signal)
        if not token_id:
            return None

        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=round(signal.size_usd, 2),
            side="BUY",
            order_type=OrderType.FOK,
        )

        try:
            logger.info(
                "MARKET ORDER: %s $%.2f on %s",
                signal.prediction.recommended_side,
                signal.size_usd,
                signal.prediction.question[:40],
            )
            result = self.client.create_and_post_order(order_args)
            logger.info("Market order filled: %s", result)
            return result
        except Exception as e:
            logger.error("Ошибка маркет-ордера: %s", e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Отменить ордер."""
        try:
            self.client.cancel(order_id)
            logger.info("Ордер отменён: %s", order_id)
            return True
        except Exception as e:
            logger.error("Ошибка отмены ордера %s: %s", order_id, e)
            return False

    def get_open_orders(self) -> list[dict]:
        """Получить открытые ордера."""
        try:
            return self.client.get_orders()
        except Exception as e:
            logger.error("Ошибка получения ордеров: %s", e)
            return []

    def get_orderbook(self, token_id: str) -> dict | None:
        """Получить стакан для токена."""
        try:
            return self.client.get_order_book(token_id)
        except Exception as e:
            logger.error("Ошибка получения стакана: %s", e)
            return None

    def _get_token_id(self, signal: TradeSignal) -> str:
        """Определить token_id из сигнала."""
        if signal.token_id:
            return signal.token_id

        # Нужно получить token_ids из Gamma API
        from polymarket.api import PolymarketAPI

        api = PolymarketAPI()
        try:
            market = api.get_market_by_id(signal.market_id)
            if not market or not market.clob_token_ids:
                return ""

            # BUY_YES → первый token (YES), BUY_NO → второй token (NO)
            if signal.prediction.recommended_side == "BUY_YES":
                return market.clob_token_ids[0]
            elif (
                signal.prediction.recommended_side == "BUY_NO"
                and len(market.clob_token_ids) > 1
            ):
                return market.clob_token_ids[1]
            return market.clob_token_ids[0]
        finally:
            api.close()
