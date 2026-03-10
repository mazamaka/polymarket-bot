"""Исполнение сделок — paper trading и real trading."""

import logging
from datetime import datetime

from polymarket.models import Position, TradeSignal

logger = logging.getLogger(__name__)


class PaperExecutor:
    """Paper trading — симуляция сделок без реальных денег."""

    def __init__(self, initial_balance: float = 100.0) -> None:
        self.balance = initial_balance
        self.positions: list[Position] = []
        self.trade_history: list[dict] = []
        self.total_pnl = 0.0

    def execute(self, signal: TradeSignal) -> Position | None:
        """Исполнить сигнал в режиме paper trading."""
        if signal.size_usd > self.balance:
            logger.warning(
                "Paper: недостаточно средств. Нужно $%.2f, есть $%.2f",
                signal.size_usd,
                self.balance,
            )
            return None

        self.balance -= signal.size_usd

        position = Position(
            market_id=signal.market_id,
            token_id=signal.token_id,
            question=signal.prediction.question,
            entry_price=signal.price,
            size_usd=signal.size_usd,
            current_price=signal.price,
            side=signal.prediction.recommended_side,
        )
        self.positions.append(position)

        self.trade_history.append(
            {
                "action": "OPEN",
                "market_id": signal.market_id,
                "question": signal.prediction.question[:60],
                "side": signal.prediction.recommended_side,
                "price": signal.price,
                "size_usd": signal.size_usd,
                "edge": signal.prediction.edge,
                "confidence": signal.prediction.confidence,
                "timestamp": datetime.now().isoformat(),
            }
        )

        logger.info(
            "PAPER TRADE: %s %s @ %.2f | $%.2f | balance: $%.2f",
            signal.prediction.recommended_side,
            signal.prediction.question[:40],
            signal.price,
            signal.size_usd,
            self.balance,
        )

        return position

    def get_portfolio_summary(self) -> dict:
        """Сводка по портфелю."""
        return {
            "balance_usd": round(self.balance, 2),
            "open_positions": len(self.positions),
            "total_trades": len(self.trade_history),
            "total_pnl": round(self.total_pnl, 2),
            "positions": [
                {
                    "question": p.question[:60],
                    "side": p.side,
                    "entry": p.entry_price,
                    "current": p.current_price,
                    "size": p.size_usd,
                    "pnl": round(p.pnl, 2),
                }
                for p in self.positions
            ],
        }
