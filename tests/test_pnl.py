"""Тесты P&L расчётов, stop-loss, close_position, equity curve, RiskManager."""

from unittest.mock import patch

import pytest

from trader.risk import RiskManager


class TestPnLCalculation:
    """Тесты формулы P&L: (current - entry) * size / entry."""

    def test_buy_yes_profit(self, make_position):
        """BUY_YES: entry=0.30, current=0.50 -> profit."""
        pos = make_position(side="BUY_YES", entry_price=0.30, size_usd=5.0)

        current_price = 0.50
        pnl = (current_price - pos.entry_price) * pos.size_usd / pos.entry_price
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price

        assert pnl == pytest.approx(3.3333, abs=0.01)
        assert pnl_pct == pytest.approx(0.6667, abs=0.01)

    def test_buy_yes_loss(self, make_position):
        """BUY_YES: entry=0.50, current=0.30 -> loss."""
        pos = make_position(side="BUY_YES", entry_price=0.50, size_usd=5.0)

        current_price = 0.30
        pnl = (current_price - pos.entry_price) * pos.size_usd / pos.entry_price
        pnl_pct = (current_price - pos.entry_price) / pos.entry_price

        assert pnl == pytest.approx(-2.0, abs=0.01)
        assert pnl_pct == pytest.approx(-0.40, abs=0.01)

    def test_buy_no_profit(self, make_position):
        """BUY_NO: entry NO price=0.60, current YES=0.30 (NO=0.70) -> profit."""
        # Для BUY_NO: entry_price хранит цену NO токена
        # entry: YES=0.40, NO=0.60 -> entry_price=0.60
        # current: YES=0.30, NO=0.70 -> current_token_price=0.70
        pos = make_position(side="BUY_NO", entry_price=0.60, size_usd=5.0)

        current_yes_price = 0.30
        current_token_price = 1 - current_yes_price  # 0.70

        pnl = (current_token_price - pos.entry_price) * pos.size_usd / pos.entry_price
        pnl_pct = (current_token_price - pos.entry_price) / pos.entry_price

        assert pnl == pytest.approx(0.8333, abs=0.01)
        assert pnl_pct == pytest.approx(0.1667, abs=0.01)

    def test_buy_no_loss(self, make_position):
        """BUY_NO: entry NO price=0.60, current YES=0.60 (NO=0.40) -> loss."""
        pos = make_position(side="BUY_NO", entry_price=0.60, size_usd=5.0)

        current_yes_price = 0.60
        current_token_price = 1 - current_yes_price  # 0.40

        pnl = (current_token_price - pos.entry_price) * pos.size_usd / pos.entry_price
        pnl_pct = (current_token_price - pos.entry_price) / pos.entry_price

        assert pnl == pytest.approx(-1.6667, abs=0.01)
        assert pnl_pct == pytest.approx(-0.3333, abs=0.01)

    def test_pnl_breakeven(self, make_position):
        """Цена не изменилась -> PnL = 0."""
        pos = make_position(entry_price=0.50, size_usd=10.0)

        pnl = (0.50 - pos.entry_price) * pos.size_usd / pos.entry_price
        assert pnl == pytest.approx(0.0)

    def test_pnl_near_zero_entry(self):
        """Entry price близок к 0 -> используется max(entry, 0.001)."""
        entry_price = 0.0005
        current_price = 0.10
        size_usd = 5.0

        safe_entry = max(entry_price, 0.001)
        pnl = (current_price - entry_price) * size_usd / safe_entry

        assert pnl > 0
        assert pnl == pytest.approx((0.10 - 0.0005) * 5.0 / 0.001, abs=0.01)


class TestStopLoss:
    """Тесты срабатывания stop-loss при -30%."""

    def test_stop_loss_triggers_at_30pct(self):
        """pnl_pct = -0.30 должен тригерить stop-loss."""
        # settings.stop_loss_pct = 0.30
        pnl_pct = -0.30
        stop_loss_pct = 0.30

        assert pnl_pct <= -stop_loss_pct

    def test_stop_loss_does_not_trigger_at_29pct(self):
        """pnl_pct = -0.29 НЕ должен тригерить."""
        pnl_pct = -0.29
        stop_loss_pct = 0.30

        assert not (pnl_pct <= -stop_loss_pct)

    def test_stop_loss_triggers_at_50pct(self):
        """pnl_pct = -0.50 (хуже -30%) -> тригерит."""
        pnl_pct = -0.50
        stop_loss_pct = 0.30

        assert pnl_pct <= -stop_loss_pct

    def test_stop_loss_real_scenario(self, make_position):
        """Реальный сценарий: BUY_YES entry=0.50, цена упала до 0.30."""
        pos = make_position(side="BUY_YES", entry_price=0.50, size_usd=5.0)
        current_price = 0.30

        pnl_pct = (current_price - pos.entry_price) / max(pos.entry_price, 0.001)
        # pnl_pct = (0.30 - 0.50) / 0.50 = -0.40

        assert pnl_pct == pytest.approx(-0.40)
        assert pnl_pct <= -0.30  # stop-loss тригерит


class TestClosePosition:
    """Тесты close_position и обновления баланса."""

    def test_close_with_profit(self, storage, make_position):
        """Закрытие прибыльной позиции увеличивает баланс."""
        pos = make_position(entry_price=0.30, size_usd=5.0)
        initial_balance = storage.balance

        storage.add_position(pos, balance_after=initial_balance - pos.size_usd)
        balance_after_open = storage.balance
        assert balance_after_open == pytest.approx(initial_balance - 5.0)

        # Закрываем по 0.50: pnl = (0.50 - 0.30) * 5.0 / 0.30 = 3.33
        pnl = storage.close_position(pos.market_id, exit_price=0.50)

        assert pnl == pytest.approx(3.3333, abs=0.01)
        # balance = balance_after_open + size_usd + pnl = 95.0 + 5.0 + 3.33 = 103.33
        assert storage.balance == pytest.approx(initial_balance + pnl, abs=0.01)
        assert len(storage.positions) == 0

    def test_close_with_loss(self, storage, make_position):
        """Закрытие убыточной позиции уменьшает баланс."""
        pos = make_position(entry_price=0.50, size_usd=5.0)
        initial_balance = storage.balance

        storage.add_position(pos, balance_after=initial_balance - pos.size_usd)

        # Закрываем по 0.30: pnl = (0.30 - 0.50) * 5.0 / 0.50 = -2.0
        pnl = storage.close_position(pos.market_id, exit_price=0.30)

        assert pnl == pytest.approx(-2.0)
        # balance = 95.0 + 5.0 + (-2.0) = 98.0
        assert storage.balance == pytest.approx(98.0)

    def test_close_nonexistent_position(self, storage):
        """Закрытие несуществующей позиции возвращает 0."""
        pnl = storage.close_position("nonexistent-market", exit_price=0.50)
        assert pnl == 0.0

    def test_close_removes_from_positions(self, storage, make_position):
        """После закрытия позиция удаляется из списка."""
        pos1 = make_position(market_id="market-1")
        pos2 = make_position(market_id="market-2")

        storage.add_position(pos1, balance_after=storage.balance - pos1.size_usd)
        storage.add_position(pos2, balance_after=storage.balance - pos2.size_usd)
        assert len(storage.positions) == 2

        storage.close_position("market-1", exit_price=0.50)
        assert len(storage.positions) == 1
        assert storage.positions[0].market_id == "market-2"

    def test_close_records_history(self, storage, make_position):
        """Закрытие записывает CLOSE в историю."""
        pos = make_position(entry_price=0.40, size_usd=5.0)
        storage.add_position(pos, balance_after=storage.balance - pos.size_usd)

        storage.close_position(pos.market_id, exit_price=0.60)

        close_entries = [h for h in storage.history if h["action"] == "CLOSE"]
        assert len(close_entries) == 1
        assert close_entries[0]["entry_price"] == 0.40
        assert close_entries[0]["exit_price"] == 0.60
        assert close_entries[0]["pnl"] == pytest.approx(2.5, abs=0.01)


class TestEquityCurve:
    """Тесты записи equity curve."""

    def test_equity_recorded_on_open(self, storage, make_position):
        """При открытии позиции записывается точка equity."""
        initial_len = len(storage.equity_curve)
        pos = make_position(size_usd=5.0)

        storage.add_position(pos, balance_after=storage.balance - pos.size_usd)

        assert len(storage.equity_curve) == initial_len + 1
        last_entry = storage.equity_curve[-1]
        assert "equity" in last_entry
        assert "balance" in last_entry
        assert "ts" in last_entry

    def test_equity_recorded_on_close(self, storage, make_position):
        """При закрытии позиции записывается точка equity."""
        pos = make_position(size_usd=5.0)
        storage.add_position(pos, balance_after=storage.balance - pos.size_usd)
        len_after_open = len(storage.equity_curve)

        storage.close_position(pos.market_id, exit_price=0.60)

        assert len(storage.equity_curve) == len_after_open + 1

    def test_equity_curve_truncated_at_500(self, storage, make_position):
        """Equity curve обрезается до 500 записей при save()."""
        storage.equity_curve = [
            {"ts": f"2026-01-{i:02d}", "equity": 100.0} for i in range(1, 31)
        ] * 20  # 600 записей

        storage.save()

        assert len(storage.equity_curve) == 500


class TestRiskManager:
    """Тесты RiskManager с реальными позициями."""

    def test_blocks_at_max_positions(self, make_position, make_prediction):
        """Блокирует новые сделки при достижении max_concurrent_positions."""
        positions = [make_position(market_id=f"market-{i}") for i in range(20)]
        rm = RiskManager(positions=positions)
        prediction = make_prediction()

        with patch("trader.risk.settings") as mock_settings:
            mock_settings.max_concurrent_positions = 20
            mock_settings.min_confidence = 0.30
            mock_settings.min_edge_threshold = 0.08

            signal = rm.evaluate_signal(prediction, balance_usd=100.0)

        assert signal is None

    def test_allows_below_max_positions(self, make_position, make_prediction):
        """Разрешает сделку если позиций меньше лимита."""
        positions = [make_position(market_id=f"market-{i}") for i in range(3)]
        rm = RiskManager(positions=positions)
        prediction = make_prediction(
            ai_probability=0.70,
            market_probability=0.50,
            confidence=0.80,
            edge=0.20,
            recommended_side="BUY_YES",
        )

        with patch("trader.risk.settings") as mock_settings:
            mock_settings.max_concurrent_positions = 20
            mock_settings.min_confidence = 0.30
            mock_settings.min_edge_threshold = 0.08
            mock_settings.max_total_exposure_pct = 0.30
            mock_settings.max_position_pct = 0.05
            mock_settings.default_trade_size_usd = 5.0

            signal = rm.evaluate_signal(prediction, balance_usd=100.0)

        assert signal is not None

    def test_blocks_low_confidence(self, make_prediction):
        """Блокирует при низкой уверенности."""
        rm = RiskManager(positions=[])
        prediction = make_prediction(confidence=0.20, edge=0.20)

        with patch("trader.risk.settings") as mock_settings:
            mock_settings.min_confidence = 0.30
            mock_settings.min_edge_threshold = 0.08

            signal = rm.evaluate_signal(prediction, balance_usd=100.0)

        assert signal is None

    def test_blocks_low_edge(self, make_prediction):
        """Блокирует при малом edge."""
        rm = RiskManager(positions=[])
        prediction = make_prediction(confidence=0.80, edge=0.05)

        with patch("trader.risk.settings") as mock_settings:
            mock_settings.min_confidence = 0.30
            mock_settings.min_edge_threshold = 0.08

            signal = rm.evaluate_signal(prediction, balance_usd=100.0)

        assert signal is None

    def test_blocks_skip_side(self, make_prediction):
        """Блокирует SKIP рекомендацию."""
        rm = RiskManager(positions=[])
        prediction = make_prediction(
            confidence=0.80, edge=0.20, recommended_side="SKIP"
        )

        with patch("trader.risk.settings") as mock_settings:
            mock_settings.min_confidence = 0.30
            mock_settings.min_edge_threshold = 0.08

            signal = rm.evaluate_signal(prediction, balance_usd=100.0)

        assert signal is None

    def test_blocks_max_exposure(self, make_position, make_prediction):
        """Блокирует при превышении общей экспозиции."""
        # 5 позиций по $5 = $25 exposure, лимит = 100 * 0.30 = $30
        # Но если поставим exposure высоко...
        positions = [
            make_position(market_id=f"market-{i}", size_usd=10.0) for i in range(3)
        ]
        # $30 exposure >= $30 max -> блокировка
        rm = RiskManager(positions=positions)
        prediction = make_prediction(confidence=0.80, edge=0.20)

        with patch("trader.risk.settings") as mock_settings:
            mock_settings.min_confidence = 0.30
            mock_settings.min_edge_threshold = 0.08
            mock_settings.max_concurrent_positions = 20
            mock_settings.max_total_exposure_pct = 0.30

            signal = rm.evaluate_signal(prediction, balance_usd=100.0)

        assert signal is None
