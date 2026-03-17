"""Polymarket AI Trading Bot — Entry Point.

Фаза 0: Read-only анализ рынков (по умолчанию)
Фаза 1: Paper trading (--paper)
Мониторинг: --monitor
Scheduler: --schedule MINUTES
Web UI: --web
"""

import argparse
import json
import logging
import logging.handlers
import sys
import time
from datetime import datetime
from pathlib import Path

from analyzer.claude import ClaudeAnalyzer
from config import settings
from polymarket.api import PolymarketAPI
from polymarket.models import AIPrediction
from trader.monitor import update_positions
from trader.risk import RiskManager
from trader.signals_history import signals_history
from trader.storage import PortfolioStorage

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.append(
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "bot.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    )
except PermissionError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

RESULTS_DIR = Path("results")


def save_results(predictions: list[AIPrediction], filename: str | None = None) -> Path:
    """Сохранить результаты анализа в JSON."""
    RESULTS_DIR.mkdir(exist_ok=True)
    if not filename:
        filename = f"analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path = RESULTS_DIR / filename
    data = [p.model_dump(mode="json") for p in predictions]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    logger.info("Результаты сохранены: %s", path)
    return path


def run_analysis(max_markets: int = 200) -> list[AIPrediction]:
    """Фаза 0: только анализ рынков, без торговли."""
    logger.info("=== POLYMARKET AI BOT — ФАЗА 0: АНАЛИЗ ===")

    api = PolymarketAPI()
    analyzer = ClaudeAnalyzer()

    try:
        logger.info("Загружаем top %d рынков по ликвидности...", max_markets)
        markets = api.get_active_markets(max_markets=max_markets)
        tradeable = api.filter_tradeable_markets(markets)

        if not tradeable:
            logger.warning("Нет рынков с достаточной ликвидностью")
            return []

        logger.info("AI скрининг %d рынков...", len(tradeable))
        interesting = analyzer.batch_screen_markets(tradeable)
        logger.info("Потенциально mispriced: %d", len(interesting))

        if not interesting:
            logger.info("Нет рынков с достаточным edge")
            return []

        logger.info("Глубокий анализ %d рынков...", len(interesting))
        predictions = []
        for item in interesting:
            market_id = item.get("market_id", "")
            market = next((m for m in tradeable if m.id == market_id), None)
            if not market:
                continue
            prediction = analyzer.analyze_market(market)
            if prediction:
                predictions.append(prediction)

        # Результаты
        logger.info("=" * 60)
        for p in sorted(predictions, key=lambda x: abs(x.edge), reverse=True):
            label = (
                "BUY YES"
                if p.recommended_side == "BUY_YES"
                else "BUY NO"
                if p.recommended_side == "BUY_NO"
                else "SKIP"
            )
            logger.info(
                "%s | %s | AI: %.0f%% vs Mkt: %.0f%% | Edge: %+.0f%% | Conf: %.0f%%",
                label,
                p.question[:50],
                p.ai_probability * 100,
                p.market_probability * 100,
                p.edge * 100,
                p.confidence * 100,
            )

        save_results(predictions)
        return predictions

    finally:
        api.close()


def run_paper_trading(
    max_markets: int = 200,
    on_log: callable = None,
) -> None:
    """Фаза 1: paper trading с persistent storage."""
    logger.info("=== POLYMARKET AI BOT — ФАЗА 1: PAPER TRADING ===")

    def _log(msg: str) -> None:
        logger.info(msg)
        if on_log:
            on_log(msg)

    api = PolymarketAPI()
    analyzer = ClaudeAnalyzer()
    storage = PortfolioStorage()
    risk_mgr = RiskManager(positions=storage.positions)

    _log(f"Баланс: ${storage.balance:.2f} | Открытых позиций: {len(storage.positions)}")

    try:  # noqa: E501 — large try block, all resources closed in finally
        from trader.scan_log import scan_logger

        scan_logger.start_scan()

        # 1. Сначала обновляем существующие позиции
        if storage.positions:
            _log(f"Обновляем {len(storage.positions)} открытых позиций...")
            update_positions(storage)

        # 2. Получаем и фильтруем рынки
        markets = api.get_active_markets(max_markets=max_markets)
        tradeable = api.filter_tradeable_markets(markets)
        _log(f"Загружено {len(markets)} рынков → {len(tradeable)} прошли фильтр")
        scan_logger.set_filter_stats(
            loaded=len(markets),
            filtered=len(tradeable),
            skipped_time=len(markets) - len(tradeable),
            skipped_type=0,
        )

        # Snapshot рынков для backtesting
        signals_history.record_market_snapshot(
            [
                {
                    "id": m.id,
                    "question": m.question[:100],
                    "yes_price": m.outcome_prices[0] if m.outcome_prices else 0.5,
                    "volume": round(m.volume, 0),
                    "liquidity": round(m.liquidity, 0),
                    "end_date": m.end_date,
                }
                for m in tradeable
            ]
        )

        if not tradeable:
            _log("Нет торгуемых рынков")
            scan_logger.finish_scan()
            return

        # 3. Скрининг (исключаем рынки где уже есть позиция)
        open_ids = storage.get_open_market_ids()
        to_screen = [m for m in tradeable if m.id not in open_ids]
        _log(
            f"AI скрининг {len(to_screen)} рынков (исключено {len(open_ids)} с позициями)..."
        )

        interesting = analyzer.batch_screen_markets(to_screen)

        # Логируем все скринированные рынки
        interesting_ids = {x.get("market_id", "") for x in interesting}
        for m in to_screen:
            yes_price = m.outcome_prices[0] if m.outcome_prices else 0.5
            is_interesting = m.id in interesting_ids
            reason_item = next(
                (x for x in interesting if x.get("market_id") == m.id), None
            )
            reason = (
                reason_item.get("reason", "")
                if reason_item
                else "Not flagged by screener"
            )
            scan_logger.add_screened_market(
                market_id=m.id,
                question=m.question,
                yes_price=yes_price,
                volume=m.volume,
                liquidity=m.liquidity,
                interesting=is_interesting,
                reason=reason,
            )

        if not interesting:
            _log("Нет новых интересных рынков")
        else:
            # 4. Параллельный анализ интересных рынков
            markets_to_analyze = []
            for item in interesting:
                market_id = item.get("market_id", "")
                market = next((m for m in tradeable if m.id == market_id), None)
                if market:
                    markets_to_analyze.append(market)

            _log(
                f"Найдено {len(interesting)} потенциальных → глубокий анализ {len(markets_to_analyze)} рынков..."
            )
            predictions = analyzer.analyze_markets_parallel(
                markets_to_analyze, max_workers=2
            )

            # 5. Торговля по результатам
            _log(f"Анализ завершён: {len(predictions)} предсказаний, проверяем risk...")
            from polymarket.models import Position

            for prediction in predictions:
                market = next(
                    (m for m in tradeable if m.id == prediction.market_id), None
                )
                if not market:
                    continue

                # Передаём end_date в prediction для проверки в risk manager
                prediction.end_date = market.end_date

                signal = risk_mgr.evaluate_signal(
                    prediction, storage.balance, is_weather=False
                )
                skip_reason = ""
                if not signal:
                    skip_reason = (
                        f"edge {abs(prediction.edge) * 100:.0f}%<{settings.min_edge_threshold * 100:.0f}%"
                        if abs(prediction.edge) < settings.min_edge_threshold
                        else f"conf {prediction.confidence * 100:.0f}%<{settings.ai_min_confidence * 100:.0f}%"
                        if prediction.confidence < settings.ai_min_confidence
                        else f"edge {abs(prediction.edge) * 100:.0f}%>{settings.max_edge_threshold * 100:.0f}% (too high)"
                        if abs(prediction.edge) > settings.max_edge_threshold
                        else "no end_date"
                        if settings.ai_require_end_date and not market.end_date
                        else "ai_max_positions"
                        if risk_mgr._count_ai_positions() >= settings.ai_max_positions
                        else "risk limit"
                    )

                scan_logger.add_analyzed_market(
                    market_id=prediction.market_id,
                    question=prediction.question,
                    ai_prob=prediction.ai_probability,
                    market_prob=prediction.market_probability,
                    edge=prediction.edge,
                    confidence=prediction.confidence,
                    spread=0,
                    side=prediction.recommended_side,
                    skip_reason=skip_reason,
                )
                signals_history.record_ai_signal(
                    market_id=prediction.market_id,
                    question=prediction.question,
                    ai_prob=prediction.ai_probability,
                    market_prob=prediction.market_probability,
                    edge=prediction.edge,
                    confidence=prediction.confidence,
                    side=prediction.recommended_side,
                    reasoning=prediction.reasoning,
                    action="SKIP" if skip_reason else "OPEN",
                    skip_reason=skip_reason,
                    entry_price=signal.price if signal else 0,
                    size_usd=signal.size_usd if signal else 0,
                    end_date=market.end_date,
                    volume=market.volume,
                    liquidity=market.liquidity,
                )

                if not signal:
                    _log(
                        f"SKIP: {prediction.question[:50]} | "
                        f"edge: {prediction.edge * 100:+.0f}% | "
                        f"conf: {prediction.confidence * 100:.0f}%"
                    )
                    continue

                position = Position(
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    question=signal.prediction.question,
                    entry_price=signal.price,
                    size_usd=signal.size_usd,
                    current_price=signal.price,
                    side=signal.prediction.recommended_side,
                    end_date=market.end_date,
                    slug=market.slug,
                    edge=prediction.edge,
                    confidence=prediction.confidence,
                    ai_probability=prediction.ai_probability,
                    reasoning=prediction.reasoning[:300],
                    volume=market.volume,
                    liquidity=market.liquidity,
                )
                new_balance = storage.balance - signal.size_usd
                storage.add_position(position, new_balance)
                scan_logger.add_trade(
                    market_id=signal.market_id,
                    side=signal.prediction.recommended_side,
                    price=signal.price,
                    size=signal.size_usd,
                )
                _log(
                    f"OPEN: {signal.prediction.recommended_side} "
                    f"{signal.prediction.question[:50]} @ {signal.price:.4f} | "
                    f"${signal.size_usd:.2f} | edge: {prediction.edge * 100:+.0f}% | "
                    f"conf: {prediction.confidence * 100:.0f}% | "
                    f"bal: ${storage.balance:.2f}"
                )

        # 6. Корреляционный скан — поиск противоречий между связанными рынками
        try:
            from analyzer.correlations import scan_correlations, _get_yes_price

            _log("Сканируем кросс-маркет корреляции...")
            corr_signals = scan_correlations(
                min_liquidity=500, max_events=200, on_log=_log
            )
            for sig in corr_signals:
                if sig.confidence < 0.5:
                    continue
                buy_market = sig.market_buy
                if buy_market.id in storage.get_open_market_ids():
                    continue
                yes_price = _get_yes_price(buy_market)
                # Создаём prediction из корреляционного сигнала
                corr_prediction = AIPrediction(
                    market_id=buy_market.id,
                    question=buy_market.question,
                    ai_probability=min(1.0, yes_price + abs(sig.actual_spread)),
                    market_probability=yes_price,
                    confidence=sig.confidence,
                    edge=abs(sig.actual_spread),
                    reasoning=f"Correlation: {sig.signal_type} in '{sig.event_title}'. {sig.expected_relation}",
                    recommended_side="BUY_YES",
                )
                signal = risk_mgr.evaluate_signal(corr_prediction, storage.balance)
                if signal:
                    position = Position(
                        market_id=signal.market_id,
                        token_id=signal.token_id,
                        question=signal.prediction.question,
                        entry_price=signal.price,
                        size_usd=signal.size_usd,
                        current_price=signal.price,
                        side="BUY_YES",
                        end_date=buy_market.end_date,
                        slug=buy_market.slug,
                        edge=corr_prediction.edge,
                        confidence=corr_prediction.confidence,
                        ai_probability=corr_prediction.ai_probability,
                        reasoning=corr_prediction.reasoning[:300],
                        volume=buy_market.volume,
                        liquidity=buy_market.liquidity,
                    )
                    new_balance = storage.balance - signal.size_usd
                    storage.add_position(position, new_balance)
                    _log(
                        f"CORR OPEN: {buy_market.question[:50]} @ {signal.price:.4f} | "
                        f"${signal.size_usd:.2f} | spread: {sig.actual_spread:+.2f} | "
                        f"bal: ${storage.balance:.2f}"
                    )
        except Exception as e:
            logger.error("Correlation scan error: %s", e)

        # 6b. Weather scan — отдельный бюджет, мимо общего risk manager
        if settings.weather_enabled:
            try:
                from analyzer.weather import scan_weather_markets
                from polymarket.models import Position  # noqa: F811

                _log("Сканируем погодные рынки (Open-Meteo ensemble)...")
                weather_predictions = scan_weather_markets(
                    min_liquidity=settings.weather_min_liquidity,
                    max_days_ahead=settings.weather_max_days_ahead,
                    min_edge=settings.weather_min_edge,
                    on_log=_log,
                )
                # Считаем текущие weather позиции
                open_ids = storage.get_open_market_ids()
                weather_open = sum(
                    1 for p in storage.positions if "temperature" in p.question.lower()
                )
                weather_opened = 0

                for wd in weather_predictions:
                    wp = wd.prediction
                    if wp.market_id in open_ids:
                        continue

                    # Определяем skip reason для записи в историю
                    skip_reason = ""
                    direction = wd.direction
                    max_yes = settings.weather_max_yes_price.get(direction, 0.25)
                    dir_min_edge = settings.weather_direction_min_edge.get(
                        direction, settings.weather_min_edge
                    )

                    if weather_open + weather_opened >= settings.weather_max_positions:
                        skip_reason = (
                            f"max_positions ({settings.weather_max_positions})"
                        )
                    elif wp.market_probability > max_yes:
                        skip_reason = (
                            f"yes_price too high for {direction} "
                            f"({wp.market_probability:.0%} > {max_yes:.0%})"
                        )
                    elif abs(wp.edge) < dir_min_edge:
                        skip_reason = (
                            f"edge below direction threshold "
                            f"({abs(wp.edge):.0%} < {dir_min_edge:.0%} for {direction})"
                        )
                    elif abs(wp.edge) > settings.max_edge_threshold:
                        skip_reason = f"edge {abs(wp.edge):.0%} > max {settings.max_edge_threshold:.0%}"
                    elif wp.confidence < settings.min_confidence:
                        skip_reason = f"conf {wp.confidence:.0%} < min {settings.min_confidence:.0%}"
                    elif settings.weather_trade_size_usd > storage.balance:
                        skip_reason = "insufficient balance"

                    price = wp.market_probability
                    if wp.recommended_side == "BUY_NO":
                        price = 1 - wp.market_probability

                    weather_market = api.get_market_by_id(wp.market_id)

                    # Записываем ВСЕ сигналы в историю (и open, и skip)
                    signals_history.record_weather_signal(
                        market_id=wp.market_id,
                        question=wp.question,
                        city=wd.city,
                        target_date=wd.target_date,
                        temp_type=wd.temp_type,
                        direction=wd.direction,
                        threshold=wd.threshold,
                        ensemble_temps=wd.ensemble_temps,
                        model_prob=wp.ai_probability,
                        market_prob=wp.market_probability,
                        edge=wp.edge,
                        confidence=wp.confidence,
                        side=wp.recommended_side,
                        action="SKIP" if skip_reason else "OPEN",
                        skip_reason=skip_reason,
                        entry_price=price,
                        size_usd=settings.weather_trade_size_usd
                        if not skip_reason
                        else 0,
                        end_date=weather_market.end_date if weather_market else "",
                        volume=weather_market.volume if weather_market else 0,
                        liquidity=weather_market.liquidity if weather_market else 0,
                    )

                    if skip_reason:
                        if "max_positions" in skip_reason:
                            _log(
                                f"Weather: лимит позиций ({settings.weather_max_positions})"
                            )
                            break
                        if "insufficient" in skip_reason:
                            _log("Weather SKIP: недостаточно баланса")
                            break
                        _log(f"Weather SKIP: {skip_reason}")
                        continue

                    trade_size = settings.weather_trade_size_usd
                    position = Position(
                        market_id=wp.market_id,
                        token_id="",
                        question=wp.question,
                        entry_price=price,
                        size_usd=trade_size,
                        current_price=price,
                        side=wp.recommended_side,
                        end_date=weather_market.end_date if weather_market else "",
                        slug=weather_market.slug if weather_market else "",
                        edge=wp.edge,
                        confidence=wp.confidence,
                        ai_probability=wp.ai_probability,
                        reasoning=wp.reasoning[:300],
                        volume=weather_market.volume if weather_market else 0,
                        liquidity=weather_market.liquidity if weather_market else 0,
                    )
                    new_balance = storage.balance - trade_size
                    storage.add_position(position, new_balance)
                    weather_opened += 1
                    open_ids.add(wp.market_id)
                    _log(
                        f"WEATHER OPEN: {wp.recommended_side} "
                        f"{wp.question[:60]} @ {price:.4f} | "
                        f"${trade_size:.2f} | edge: {wp.edge:+.0%} | "
                        f"bal: ${storage.balance:.2f}"
                    )
            except Exception as e:
                logger.error("Weather scan error: %s", e)

        # 7. Сводка
        scan_logger.finish_scan()
        summary = storage.get_summary()
        logger.info("=" * 60)
        logger.info("ПОРТФЕЛЬ:")
        logger.info(json.dumps(summary, indent=2, ensure_ascii=False))

    finally:
        api.close()
        analyzer.close()


def run_monitor() -> None:
    """Обновить цены и P&L для открытых позиций."""
    logger.info("=== МОНИТОРИНГ ПОЗИЦИЙ ===")
    storage = PortfolioStorage()

    if not storage.positions:
        logger.info("Нет открытых позиций")
        return

    update_positions(storage)
    summary = storage.get_summary()
    logger.info("=" * 60)
    logger.info("ПОРТФЕЛЬ:")
    logger.info(json.dumps(summary, indent=2, ensure_ascii=False))


def run_live_trading(max_markets: int = 200) -> None:
    """Фаза 2: live trading с реальными ордерами через CLOB API."""
    logger.info("=== POLYMARKET AI BOT — ФАЗА 2: LIVE TRADING ===")
    logger.warning(
        "ВНИМАНИЕ: реальные деньги! Убедитесь что настроен POLYGON_WALLET_PRIVATE_KEY"
    )

    from py_clob_client.clob_types import OrderArgs

    from trader.live_executor import get_live_executor

    api = PolymarketAPI()
    analyzer = ClaudeAnalyzer()
    storage = PortfolioStorage()
    risk_mgr = RiskManager(positions=storage.positions)

    try:
        executor = get_live_executor()
        balance = executor.get_balance()
        logger.info("Wallet balance: $%.2f USDC", balance)

        if balance < 1.0:
            logger.error("Недостаточно средств на кошельке: $%.2f", balance)
            return

        # Обновляем позиции
        if storage.positions:
            update_positions(storage)

        # Рынки
        markets = api.get_active_markets(max_markets=max_markets)
        tradeable = api.filter_tradeable_markets(markets)

        if not tradeable:
            logger.warning("Нет торгуемых рынков")
            return

        # Dedup: проверяем реальные позиции из Data API, не paper storage
        # Data API возвращает conditionId (hex), Gamma API использует числовой id
        # Собираем оба + token_id для надёжного dedup
        open_condition_ids: set[str] = set()
        open_token_ids: set[str] = set()
        try:
            real_positions = executor.get_live_positions()
            open_condition_ids = {
                p["market_id"] for p in real_positions if p.get("market_id")
            }
            open_token_ids = {
                p["token_id"] for p in real_positions if p.get("token_id")
            }
            logger.info(
                "Live dedup: %d conditionIds, %d tokenIds from Data API",
                len(open_condition_ids),
                len(open_token_ids),
            )
        except Exception as e:
            logger.warning("Failed to get live positions for dedup, using paper: %s", e)
            open_condition_ids = storage.get_open_market_ids()
        # Filter by both Gamma id and conditionId
        to_screen = [
            m
            for m in tradeable
            if m.id not in open_condition_ids
            and m.condition_id not in open_condition_ids
        ]
        interesting = analyzer.batch_screen_markets(to_screen)

        if not interesting:
            logger.info("Нет новых интересных рынков")
            return

        for item in interesting:
            market_id = item.get("market_id", "")
            market = next((m for m in tradeable if m.id == market_id), None)
            if not market:
                continue

            prediction = analyzer.analyze_market(market)
            if not prediction:
                continue

            signal = risk_mgr.evaluate_signal(prediction, balance)
            if not signal:
                continue

            # Live execute
            result = executor.execute_limit_order(signal)
            if result:
                from polymarket.models import Position

                position = Position(
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    question=signal.prediction.question,
                    entry_price=signal.price,
                    size_usd=signal.size_usd,
                    current_price=signal.price,
                    side=signal.prediction.recommended_side,
                )
                new_balance = storage.balance - signal.size_usd
                storage.add_position(position, new_balance)
                balance -= signal.size_usd

                from trader.live_history import get_live_history

                get_live_history().record_open(
                    question=signal.prediction.question,
                    side=signal.prediction.recommended_side,
                    entry_price=signal.price,
                    size_usd=signal.size_usd,
                    shares=signal.size_usd / signal.price if signal.price > 0 else 0,
                    token_id=signal.token_id,
                    market_id=signal.market_id,
                    edge=signal.prediction.edge * 100,
                    confidence=signal.prediction.confidence * 100,
                    source="ai",
                )

        # Weather scan — те же погодные рынки что в paper mode
        if settings.weather_enabled:
            try:
                from analyzer.weather import scan_weather_markets

                logger.info("Сканируем погодные рынки (live)...")
                weather_predictions = scan_weather_markets(
                    min_liquidity=settings.weather_min_liquidity,
                    max_days_ahead=settings.weather_max_days_ahead,
                    min_edge=settings.weather_min_edge,
                )
                weather_opened = 0
                # Refresh dedup sets to include any just-opened AI positions
                try:
                    real_positions = executor.get_live_positions()
                    open_condition_ids = {
                        p["market_id"] for p in real_positions if p.get("market_id")
                    }
                    open_token_ids = {
                        p["token_id"] for p in real_positions if p.get("token_id")
                    }
                except Exception:
                    pass

                for wd in weather_predictions:
                    wp = wd.prediction
                    # Dedup by both Gamma id and conditionId
                    if wp.market_id in open_condition_ids:
                        continue

                    direction = wd.direction
                    max_yes = settings.weather_max_yes_price.get(direction, 0.25)
                    dir_min_edge = settings.weather_direction_min_edge.get(
                        direction, settings.weather_min_edge
                    )

                    # Проверки
                    if weather_opened >= settings.weather_max_positions:
                        break
                    if wp.market_probability > max_yes:
                        continue
                    if abs(wp.edge) < dir_min_edge:
                        continue
                    if abs(wp.edge) > settings.max_edge_threshold:
                        continue
                    if wp.confidence < settings.min_confidence:
                        continue

                    price = wp.market_probability
                    if wp.recommended_side == "BUY_NO":
                        price = 1 - wp.market_probability

                    weather_market = api.get_market_by_id(wp.market_id)
                    if not weather_market or not weather_market.clob_token_ids:
                        continue

                    # Double-check dedup by conditionId and token_id
                    if weather_market.condition_id in open_condition_ids:
                        continue

                    # Определяем token_id
                    if wp.recommended_side == "BUY_YES":
                        token_id = weather_market.clob_token_ids[0]
                    elif len(weather_market.clob_token_ids) > 1:
                        token_id = weather_market.clob_token_ids[1]
                    else:
                        token_id = weather_market.clob_token_ids[0]

                    if token_id in open_token_ids:
                        continue

                    trade_size = settings.weather_trade_size_usd
                    if trade_size > balance:
                        logger.info("Weather: недостаточно баланса ($%.2f)", balance)
                        break

                    size_shares = trade_size / price if price > 0 else 0
                    try:
                        order_args = OrderArgs(
                            token_id=token_id,
                            price=round(price, 4),
                            size=round(size_shares, 2),
                            side="BUY",
                        )
                        logger.info(
                            "WEATHER LIVE ORDER: %s %s @ %.4f | %.2f shares ($%.2f)",
                            wp.recommended_side,
                            wp.question[:50],
                            price,
                            size_shares,
                            trade_size,
                        )
                        signed = executor.client.create_order(order_args)
                        result = executor.client.post_order(signed)
                        logger.info("Weather order posted: %s", result)
                        if result and result.get("success"):
                            balance -= trade_size
                            weather_opened += 1
                            open_condition_ids.add(wp.market_id)
                            open_condition_ids.add(weather_market.condition_id)
                            open_token_ids.add(token_id)
                            # Записываем в live trade history
                            from trader.live_history import get_live_history

                            get_live_history().record_open(
                                question=wp.question,
                                side=wp.recommended_side,
                                entry_price=price,
                                size_usd=trade_size,
                                shares=size_shares,
                                token_id=token_id,
                                market_id=wp.market_id,
                                edge=wp.edge * 100,
                                confidence=wp.confidence * 100,
                                source="weather",
                            )
                            # Записываем в историю сигналов
                            signals_history.record_weather_signal(
                                market_id=wp.market_id,
                                question=wp.question,
                                city=wd.city,
                                target_date=wd.target_date,
                                temp_type=wd.temp_type,
                                direction=wd.direction,
                                threshold=wd.threshold,
                                ensemble_temps=wd.ensemble_temps,
                                model_prob=wp.ai_probability,
                                market_prob=wp.market_probability,
                                edge=wp.edge,
                                confidence=wp.confidence,
                                side=wp.recommended_side,
                                action="OPEN",
                                skip_reason="",
                                entry_price=price,
                                size_usd=trade_size,
                                end_date=weather_market.end_date,
                                volume=weather_market.volume,
                                liquidity=weather_market.liquidity,
                            )
                    except Exception as e:
                        logger.error("Weather order error: %s", e)

                logger.info("Weather scan: %d позиций открыто", weather_opened)
            except Exception as e:
                logger.error("Weather scan error: %s", e)

        logger.info("=" * 60)
        logger.info("LIVE TRADING завершён | Balance: $%.2f", balance)

    finally:
        api.close()


def run_scheduler(interval_min: int, max_markets: int) -> None:
    """Запуск по расписанию: paper trading + мониторинг каждые N минут."""
    logger.info("=== SCHEDULER: каждые %d мин ===", interval_min)

    run_count = 0
    while True:
        run_count += 1
        logger.info(
            "--- Run #%d @ %s ---", run_count, datetime.now().strftime("%H:%M:%S")
        )

        try:
            if settings.paper_trading:
                run_paper_trading(max_markets=max_markets)
            else:
                run_live_trading(max_markets=max_markets)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.exception("Ошибка в run: %s", e)

        logger.info("Следующий запуск через %d мин...", interval_min)
        try:
            time.sleep(interval_min * 60)
        except KeyboardInterrupt:
            logger.info("Scheduler остановлен")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket AI Trading Bot")
    parser.add_argument("--paper", action="store_true", help="Paper trading mode")
    parser.add_argument("--monitor", action="store_true", help="Update positions & P&L")
    parser.add_argument(
        "--schedule", type=int, metavar="MIN", help="Run every N minutes"
    )
    parser.add_argument("--web", action="store_true", help="Start web dashboard")
    parser.add_argument(
        "--live", action="store_true", help="Live trading (real money!)"
    )
    parser.add_argument(
        "--top", type=int, default=200, help="Top N markets (default: 200)"
    )
    args = parser.parse_args()

    if args.live:
        if not settings.polygon_wallet_private_key:
            logger.error("POLYGON_WALLET_PRIVATE_KEY не установлен! Добавьте в .env")
            sys.exit(1)
        run_live_trading(max_markets=args.top)
    elif args.web:
        from web.app import start_web

        start_web()
    elif args.monitor:
        run_monitor()
    elif args.schedule:
        run_scheduler(args.schedule, max_markets=args.top)
    elif args.paper:
        run_paper_trading(max_markets=args.top)
    else:
        run_analysis(max_markets=args.top)


if __name__ == "__main__":
    main()
