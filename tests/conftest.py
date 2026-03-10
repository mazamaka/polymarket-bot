"""Fixtures для тестов paper trading бота."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from polymarket.models import AIPrediction, Position


@pytest.fixture()
def tmp_data_dir(tmp_path: Path) -> Path:
    """Временная директория для data файлов."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture()
def _patch_data_dir(tmp_data_dir: Path):
    """Патчим DATA_DIR и файлы в storage на временные."""
    with (
        patch("trader.storage.DATA_DIR", tmp_data_dir),
        patch("trader.storage.POSITIONS_FILE", tmp_data_dir / "positions.json"),
        patch("trader.storage.HISTORY_FILE", tmp_data_dir / "trade_history.json"),
        patch("trader.storage.EQUITY_FILE", tmp_data_dir / "equity_curve.json"),
    ):
        yield


@pytest.fixture()
def storage(_patch_data_dir):
    """Свежий PortfolioStorage с временными файлами."""
    from trader.storage import PortfolioStorage

    return PortfolioStorage()


@pytest.fixture()
def make_position():
    """Фабрика для создания позиций."""

    def _make(
        market_id: str = "test-market-1",
        question: str = "Test question?",
        side: str = "BUY_YES",
        entry_price: float = 0.50,
        size_usd: float = 5.0,
        current_price: float = 0.50,
        edge: float = 0.15,
        confidence: float = 0.80,
        ai_probability: float = 0.65,
    ) -> Position:
        return Position(
            market_id=market_id,
            token_id="token-123",
            question=question,
            entry_price=entry_price,
            size_usd=size_usd,
            current_price=current_price,
            side=side,
            edge=edge,
            confidence=confidence,
            ai_probability=ai_probability,
            opened_at=datetime(2026, 1, 15, 12, 0, 0),
        )

    return _make


@pytest.fixture()
def make_prediction():
    """Фабрика для AIPrediction."""

    def _make(
        market_id: str = "test-market-1",
        question: str = "Test question?",
        ai_probability: float = 0.65,
        market_probability: float = 0.50,
        confidence: float = 0.80,
        edge: float = 0.15,
        recommended_side: str = "BUY_YES",
    ) -> AIPrediction:
        return AIPrediction(
            market_id=market_id,
            question=question,
            ai_probability=ai_probability,
            market_probability=market_probability,
            confidence=confidence,
            edge=edge,
            recommended_side=recommended_side,
        )

    return _make
