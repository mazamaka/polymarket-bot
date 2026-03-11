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
from trader.storage import PortfolioStorage

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            LOG_DIR / "bot.log",
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,
            encoding="utf-8",
        ),
    ],
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
        # 1. Сначала обновляем существующие позиции
        if storage.positions:
            _log(f"Обновляем {len(storage.positions)} открытых позиций...")
            update_positions(storage)

        # 2. Получаем и фильтруем рынки
        markets = api.get_active_markets(max_markets=max_markets)
        tradeable = api.filter_tradeable_markets(markets)
        _log(f"Загружено {len(markets)} рынков → {len(tradeable)} прошли фильтр")

        if not tradeable:
            _log("Нет торгуемых рынков")
            return

        # 3. Скрининг (исключаем рынки где уже есть позиция)
        open_ids = storage.get_open_market_ids()
        to_screen = [m for m in tradeable if m.id not in open_ids]
        _log(
            f"AI скрининг {len(to_screen)} рынков (исключено {len(open_ids)} с позициями)..."
        )

        interesting = analyzer.batch_screen_markets(to_screen)
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
                markets_to_analyze, max_workers=3
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

                signal = risk_mgr.evaluate_signal(prediction, storage.balance)
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

        # 7. Сводка
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

    from trader.live_executor import LiveExecutor

    api = PolymarketAPI()
    analyzer = ClaudeAnalyzer()
    storage = PortfolioStorage()
    risk_mgr = RiskManager(positions=storage.positions)

    try:
        executor = LiveExecutor()
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

        open_ids = storage.get_open_market_ids()
        to_screen = [m for m in tradeable if m.id not in open_ids]
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

        summary = storage.get_summary()
        logger.info("=" * 60)
        logger.info("ПОРТФЕЛЬ (Live):")
        logger.info(json.dumps(summary, indent=2, ensure_ascii=False))

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
            run_paper_trading(max_markets=max_markets)
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
