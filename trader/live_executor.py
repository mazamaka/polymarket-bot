"""Live trading executor через Polymarket CLOB API."""

import logging
import threading

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, OrderArgs

from config import settings

logger = logging.getLogger(__name__)

_instance: "LiveExecutor | None" = None
_lock = threading.Lock()


def get_live_executor() -> "LiveExecutor":
    """Thread-safe singleton."""
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is None:
            _instance = LiveExecutor()
    return _instance


class LiveExecutor:
    """Исполнение реальных ордеров на Polymarket."""

    def __init__(self) -> None:
        if not settings.polygon_wallet_private_key:
            raise ValueError("POLYGON_WALLET_PRIVATE_KEY not set")

        # Apply proxy patch if configured
        if settings.clob_proxy_url:
            from trader.proxy_patch import apply_proxy

            apply_proxy(settings.clob_proxy_url)

        funder = settings.polygon_wallet_address or None

        # Step 1: derive API credentials
        tmp = ClobClient(
            host=settings.clob_api_url,
            chain_id=settings.polygon_chain_id,
            key=settings.polygon_wallet_private_key,
            signature_type=2,
            funder=funder,
        )
        self.creds = tmp.derive_api_key()

        # Step 2: create client with creds (L2 mode)
        self.client = ClobClient(
            host=settings.clob_api_url,
            chain_id=settings.polygon_chain_id,
            key=settings.polygon_wallet_private_key,
            creds=self.creds,
            signature_type=2,
            funder=funder,
        )
        logger.info(
            "LiveExecutor ready | addr=%s | mode=%s | funder=%s",
            self.client.get_address(),
            self.client.mode,
            funder or "none",
        )

    def get_balance(self) -> float:
        """Получить баланс USDC на Polymarket."""
        try:
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=2)
            bal = self.client.get_balance_allowance(params)
            if isinstance(bal, dict):
                return float(bal.get("balance", 0)) / 1e6
            return 0.0
        except Exception as e:
            logger.error("Balance error: %s", e)
            return 0.0

    def get_allowances(self) -> dict[str, float]:
        """Получить allowances для exchange контрактов."""
        try:
            params = BalanceAllowanceParams(asset_type="COLLATERAL", signature_type=2)
            bal = self.client.get_balance_allowance(params)
            if isinstance(bal, dict):
                return {k: float(v) for k, v in bal.get("allowances", {}).items()}
            return {}
        except Exception as e:
            logger.error("Allowance error: %s", e)
            return {}

    def get_live_positions(self) -> list[dict]:
        """Получить реальные позиции из Polymarket Data API."""
        wallet = settings.polygon_wallet_address
        if not wallet:
            return []
        try:
            resp = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": wallet.lower()},
                timeout=15,
            )
            resp.raise_for_status()
            positions = []
            for p in resp.json():
                size_val = float(p.get("size", 0))
                if size_val <= 0:
                    continue
                positions.append(
                    {
                        "market_id": p.get("conditionId", ""),
                        "token_id": p.get("asset", ""),
                        "question": p.get("title", "Unknown"),
                        "side": "BUY_YES" if p.get("outcome") == "Yes" else "BUY_NO",
                        "avg_price": float(p.get("avgPrice", 0)),
                        "cur_price": float(p.get("curPrice", 0)),
                        "shares": size_val,
                        "initial_value": float(p.get("initialValue", 0)),
                        "current_value": float(p.get("currentValue", 0)),
                        "pnl": float(p.get("cashPnl", 0)),
                        "pnl_pct": float(p.get("percentPnl", 0)),
                        "outcome": p.get("outcome", "Yes"),
                        "slug": p.get("eventSlug", ""),
                        "end_date": p.get("endDate", ""),
                        "icon": p.get("icon", ""),
                        "redeemable": p.get("redeemable", False),
                    }
                )
            return positions
        except requests.RequestException as e:
            logger.error("Data API positions error: %s", e)
            return []

    def execute_limit_order(self, signal) -> dict | None:
        """Разместить лимитный ордер."""
        market = signal.prediction
        token_id = self._get_token_id(signal)
        if not token_id:
            logger.error("No token_id for %s", market.question[:40])
            return None

        size = signal.size_usd / signal.price if signal.price > 0 else 0
        order_args = OrderArgs(
            token_id=token_id,
            price=round(signal.price, 4),
            size=round(size, 2),
            side="BUY",
        )

        try:
            logger.info(
                "LIVE ORDER: %s %s @ %.4f | %.2f shares ($%.2f)",
                market.recommended_side,
                market.question[:40],
                signal.price,
                size,
                signal.size_usd,
            )
            signed = self.client.create_order(order_args)
            result = self.client.post_order(signed)
            logger.info("Order posted: %s", result)
            return result
        except Exception as e:
            logger.error("Order error: %s", e)
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Отменить ордер."""
        try:
            self.client.cancel(order_id)
            return True
        except Exception as e:
            logger.error("Cancel error %s: %s", order_id, e)
            return False

    def get_open_orders(self) -> list:
        """Получить открытые ордера."""
        try:
            return self.client.get_orders()
        except Exception as e:
            logger.error("Get orders error: %s", e)
            return []

    def execute_sell_order(self, token_id: str, price: float, size: float) -> dict:
        """Разместить ордер на продажу.

        Raises:
            ValueError: если цена вне допустимого диапазона или другая ошибка.
        """
        if price < 0.001 or price > 0.999:
            raise ValueError(f"Price {price} out of range (0.001-0.999)")
        if size <= 0:
            raise ValueError(f"Size must be positive, got {size}")

        import math

        # Floor size to avoid selling more shares than available
        # (standard round can round UP, exceeding on-chain balance)
        floored_size = math.floor(size * 100) / 100
        if floored_size < size:
            logger.info("Size floored: %.6f -> %.2f", size, floored_size)
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=floored_size,
            side="SELL",
        )
        logger.info(
            "SELL ORDER: token=%s price=%.4f size=%.2f", token_id[:16], price, size
        )
        signed = self.client.create_order(order_args)
        result = self.client.post_order(signed)
        logger.info("SELL order posted: %s", result)
        return result

    def get_best_bid(self, token_id: str) -> float:
        """Get best bid price from orderbook (highest price someone will buy at)."""
        ob = self.get_orderbook(token_id)
        if not ob:
            return 0.0
        bids = ob.get("bids", [])
        if not bids:
            return 0.0
        best = 0.0
        for b in bids:
            p = (
                float(b.get("price", 0))
                if isinstance(b, dict)
                else float(getattr(b, "price", 0))
            )
            if p > best:
                best = p
        return best

    def get_orderbook(self, token_id: str) -> dict | None:
        """Получить стакан. Возвращает dict с bids/asks."""
        try:
            ob = self.client.get_order_book(token_id)
            # OrderBookSummary — object, not dict
            bids = getattr(ob, "bids", []) or []
            asks = getattr(ob, "asks", []) or []
            return {"bids": bids, "asks": asks}
        except Exception as e:
            logger.error("Orderbook error: %s", e)
            return None

    def _get_token_id(self, signal) -> str:
        """Определить token_id из сигнала."""
        if signal.token_id:
            return signal.token_id
        from polymarket.api import PolymarketAPI

        api = PolymarketAPI()
        try:
            market = api.get_market_by_id(signal.market_id)
            if not market or not market.clob_token_ids:
                return ""
            if signal.prediction.recommended_side == "BUY_YES":
                return market.clob_token_ids[0]
            if (
                signal.prediction.recommended_side == "BUY_NO"
                and len(market.clob_token_ids) > 1
            ):
                return market.clob_token_ids[1]
            return market.clob_token_ids[0]
        finally:
            api.close()
