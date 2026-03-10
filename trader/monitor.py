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

            # Обновляем текущую цену
            pos.current_price = yes_price

            # P&L расчёт
            if pos.side == "BUY_YES":
                pos.pnl = (
                    (yes_price - pos.entry_price)
                    * pos.size_usd
                    / max(pos.entry_price, 0.001)
                )
                pos.pnl_pct = (yes_price - pos.entry_price) / max(
                    pos.entry_price, 0.001
                )
            else:  # BUY_NO
                no_entry = 1 - pos.entry_price
                no_current = 1 - yes_price
                pos.pnl = (no_current - no_entry) * pos.size_usd / max(no_entry, 0.001)
                pos.pnl_pct = (no_current - no_entry) / max(no_entry, 0.001)

            logger.info(
                "  %s | %s | Entry: %.2f → Now: %.2f | PnL: $%.2f (%+.1f%%)",
                pos.side,
                pos.question[:40],
                pos.entry_price,
                yes_price,
                pos.pnl,
                pos.pnl_pct * 100,
            )

            # Stop-loss check
            if pos.pnl_pct <= -settings.stop_loss_pct:
                logger.warning(
                    "STOP-LOSS: %s | PnL: %+.1f%%", pos.question[:40], pos.pnl_pct * 100
                )
                storage.close_position(pos.market_id, yes_price)
                closed_count += 1
                continue

            # Рынок закрылся
            if market.closed:
                logger.info(
                    "RESOLVED: %s закрылся по цене %.2f", pos.question[:40], yes_price
                )
                storage.close_position(pos.market_id, yes_price)
                closed_count += 1

        storage.save()

        if closed_count:
            logger.info("Закрыто %d позиций", closed_count)
    finally:
        api.close()
