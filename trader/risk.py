"""Risk management — контроль размеров позиций и экспозиции."""

import logging

from config import settings
from polymarket.models import AIPrediction, Position, TradeSignal

logger = logging.getLogger(__name__)


class RiskManager:
    """Контролирует риски: размер позиций, общая экспозиция, stop-loss."""

    def __init__(self, positions: list[Position] | None = None) -> None:
        self.positions: list[Position] = positions or []

    def evaluate_signal(
        self, prediction: AIPrediction, balance_usd: float
    ) -> TradeSignal | None:
        """Оценить prediction и создать trade signal с учётом рисков."""
        # Проверка уверенности Claude
        if prediction.confidence < settings.min_confidence:
            logger.info(
                "SKIP: низкая уверенность %.0f%% < %.0f%% для %s",
                prediction.confidence * 100,
                settings.min_confidence * 100,
                prediction.question[:50],
            )
            return None

        # Проверка edge (минимальный)
        if abs(prediction.edge) < settings.min_edge_threshold:
            logger.info(
                "SKIP: малый edge %.0f%% < %.0f%% для %s",
                abs(prediction.edge) * 100,
                settings.min_edge_threshold * 100,
                prediction.question[:50],
            )
            return None

        # Проверка edge (максимальный — слишком большой edge = AI скорее ошибается)
        if abs(prediction.edge) > settings.max_edge_threshold:
            logger.info(
                "SKIP: edge %.0f%% > %.0f%% (вероятно AI ошибается) для %s",
                abs(prediction.edge) * 100,
                settings.max_edge_threshold * 100,
                prediction.question[:50],
            )
            return None

        if prediction.recommended_side == "SKIP":
            return None

        # Проверка max concurrent positions
        if len(self.positions) >= settings.max_concurrent_positions:
            logger.warning("SKIP: достигнут лимит позиций %d", len(self.positions))
            return None

        # Проверка max total exposure
        total_exposure = sum(p.size_usd for p in self.positions)
        max_exposure = balance_usd * settings.max_total_exposure_pct
        if total_exposure >= max_exposure:
            logger.warning(
                "SKIP: общая экспозиция $%.0f >= лимит $%.0f",
                total_exposure,
                max_exposure,
            )
            return None

        # Размер позиции
        max_position = balance_usd * settings.max_position_pct
        trade_size = min(settings.default_trade_size_usd, max_position)

        # Не торговать если баланс слишком мал
        if trade_size < 1.0:
            logger.warning("SKIP: размер сделки $%.2f слишком мал", trade_size)
            return None

        # Определяем token_id и цену
        # BUY_YES → покупаем YES токен по рыночной цене
        # BUY_NO → покупаем NO токен (= продаём YES)
        price = prediction.market_probability
        if prediction.recommended_side == "BUY_NO":
            price = 1 - prediction.market_probability

        signal = TradeSignal(
            market_id=prediction.market_id,
            token_id="",  # будет заполнен при исполнении
            side="BUY",
            price=price,
            size_usd=trade_size,
            prediction=prediction,
        )

        logger.info(
            "SIGNAL: %s %s @ $%.2f | size: $%.0f | edge: %+.0f%% | conf: %.0f%%",
            prediction.recommended_side,
            prediction.question[:40],
            price,
            trade_size,
            prediction.edge * 100,
            prediction.confidence * 100,
        )

        return signal
