"""Мониторинг открытых позиций — обновление цен, P&L, stop-loss."""

import logging

from config import settings
from polymarket.api import PolymarketAPI
from trader.storage import PortfolioStorage

logger = logging.getLogger(__name__)


def update_positions(storage: PortfolioStorage) -> None:
    """Обновить текущие цены и P&L для всех открытых позиций."""
    if not storage.positions:
        logger.info("Нет открытых позиций")
        return

    api = PolymarketAPI()
    closed_count = 0

    try:
        for pos in list(storage.positions):
            market = api.get_market_by_id(pos.market_id)
            if not market:
                logger.warning("Рынок %s не найден", pos.market_id)
                continue

            yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5

            # Обновляем метаданные
            if market.end_date and not pos.end_date:
                pos.end_date = market.end_date
            if market.slug and not pos.slug:
                pos.slug = market.slug
            if market.volume and not pos.volume:
                pos.volume = market.volume
            if market.liquidity:
                pos.liquidity = market.liquidity

            # Текущая цена нашего токена
            # entry_price всегда хранит цену купленного токена (YES или NO)
            if pos.side == "BUY_YES":
                current_token_price = yes_price
            else:  # BUY_NO
                current_token_price = 1 - yes_price

            pos.current_price = current_token_price

            # P&L: единая формула для обоих сторон
            pos.pnl = (
                (current_token_price - pos.entry_price)
                * pos.size_usd
                / max(pos.entry_price, 0.001)
            )
            pos.pnl_pct = (current_token_price - pos.entry_price) / max(
                pos.entry_price, 0.001
            )

            logger.info(
                "  %s | %s | Entry: %.2f → Now: %.2f | PnL: $%.2f (%+.1f%%)",
                pos.side,
                pos.question[:40],
                pos.entry_price,
                current_token_price,
                pos.pnl,
                pos.pnl_pct * 100,
            )

            # Stop-loss check
            if pos.pnl_pct <= -settings.stop_loss_pct:
                logger.warning(
                    "STOP-LOSS: %s | PnL: %+.1f%%", pos.question[:40], pos.pnl_pct * 100
                )
                storage.close_position(pos.market_id, current_token_price)
                closed_count += 1
                continue

            # Рынок закрылся
            if market.closed:
                logger.info(
                    "RESOLVED: %s закрылся по цене %.2f",
                    pos.question[:40],
                    current_token_price,
                )
                storage.close_position(pos.market_id, current_token_price)
                closed_count += 1

        storage.save()

        if closed_count:
            logger.info("Закрыто %d позиций", closed_count)
    finally:
        api.close()
