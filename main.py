"""Polymarket AI Trading Bot — Entry Point.

Фаза 0: Read-only анализ рынков (по умолчанию)
Фаза 1: Paper trading (--paper)
"""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from analyzer.claude import ClaudeAnalyzer
from config import settings
from polymarket.api import PolymarketAPI
from polymarket.models import AIPrediction
from trader.executor import PaperExecutor
from trader.risk import RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
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


def run_analysis(max_markets: int = 200, use_thinking: bool = True) -> None:
    """Фаза 0: только анализ рынков, без торговли."""
    logger.info("=== POLYMARKET AI BOT — ФАЗА 0: АНАЛИЗ ===")
    logger.info("С реальными ценами крипто/акций через CoinGecko + Yahoo Finance")

    api = PolymarketAPI()
    analyzer = ClaudeAnalyzer(use_thinking=use_thinking)

    try:
        # 1. Получаем рынки (только top N по ликвидности)
        logger.info("Загружаем top %d рынков по ликвидности...", max_markets)
        markets = api.get_active_markets(max_markets=max_markets)
        logger.info("Всего рынков: %d", len(markets))

        # 2. Фильтруем по ликвидности
        tradeable = api.filter_tradeable_markets(markets)
        logger.info(
            "Торгуемых (ликвидность >= $%.0f): %d",
            settings.min_liquidity_usd,
            len(tradeable),
        )

        if not tradeable:
            logger.warning("Нет рынков с достаточной ликвидностью")
            return

        # 3. Батч-скрининг через Claude
        logger.info("Запускаем AI скрининг %d рынков...", len(tradeable))
        interesting = analyzer.batch_screen_markets(tradeable)
        logger.info("Потенциально mispriced рынков: %d", len(interesting))

        if not interesting:
            logger.info("Нет рынков с достаточным edge. Рынок эффективен!")
            return

        # 4. Глубокий анализ интересных рынков
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

        # 5. Результаты
        logger.info("=" * 60)
        logger.info("РЕЗУЛЬТАТЫ АНАЛИЗА:")
        logger.info("=" * 60)

        actionable = []
        for p in sorted(predictions, key=lambda x: abs(x.edge), reverse=True):
            label = (
                "BUY YES"
                if p.recommended_side == "BUY_YES"
                else "BUY NO"
                if p.recommended_side == "BUY_NO"
                else "SKIP"
            )
            logger.info(
                "%s | %s | AI: %.0f%% vs Market: %.0f%% | Edge: %+.0f%% | Conf: %.0f%%",
                label,
                p.question[:50],
                p.ai_probability * 100,
                p.market_probability * 100,
                p.edge * 100,
                p.confidence * 100,
            )
            logger.info("  Reasoning: %s", p.reasoning[:120])
            if p.recommended_side != "SKIP" and p.confidence >= settings.min_confidence:
                actionable.append(p)

        logger.info("=" * 60)
        logger.info(
            "Actionable сигналов (edge >= %.0f%%, conf >= %.0f%%): %d",
            settings.min_edge_threshold * 100,
            settings.min_confidence * 100,
            len(actionable),
        )

        # 6. Сохраняем
        save_results(predictions)

    finally:
        api.close()


def run_paper_trading(max_markets: int = 200, use_thinking: bool = True) -> None:
    """Фаза 1: paper trading — симуляция сделок."""
    logger.info("=== POLYMARKET AI BOT — ФАЗА 1: PAPER TRADING ===")
    logger.info("С реальными ценами крипто/акций через CoinGecko + Yahoo Finance")

    api = PolymarketAPI()
    analyzer = ClaudeAnalyzer(use_thinking=use_thinking)
    risk_mgr = RiskManager()
    executor = PaperExecutor(initial_balance=100.0)

    try:
        # 1. Получаем и фильтруем рынки
        markets = api.get_active_markets(max_markets=max_markets)
        tradeable = api.filter_tradeable_markets(markets)

        if not tradeable:
            logger.warning("Нет торгуемых рынков")
            return

        # 2. Скрининг
        interesting = analyzer.batch_screen_markets(tradeable)
        if not interesting:
            logger.info("Нет интересных рынков")
            return

        # 3. Анализ и торговля
        for item in interesting:
            market_id = item.get("market_id", "")
            market = next((m for m in tradeable if m.id == market_id), None)
            if not market:
                continue

            prediction = analyzer.analyze_market(market)
            if not prediction:
                continue

            # 4. Risk check
            signal = risk_mgr.evaluate_signal(prediction, executor.balance)
            if not signal:
                continue

            # 5. Paper execute
            executor.execute(signal)

        # 6. Сводка
        summary = executor.get_portfolio_summary()
        logger.info("=" * 60)
        logger.info("ПОРТФЕЛЬ (Paper Trading):")
        logger.info(json.dumps(summary, indent=2, ensure_ascii=False))

        # 7. Сохраняем историю
        RESULTS_DIR.mkdir(exist_ok=True)
        history_path = (
            RESULTS_DIR / f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        history_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
        logger.info("История сохранена: %s", history_path)

    finally:
        api.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket AI Trading Bot")
    parser.add_argument("--paper", action="store_true", help="Paper trading mode")
    parser.add_argument(
        "--live", action="store_true", help="Live trading (NOT IMPLEMENTED)"
    )
    parser.add_argument(
        "--top", type=int, default=200, help="Top N markets by liquidity (default: 200)"
    )
    parser.add_argument(
        "--no-thinking",
        action="store_true",
        help="Disable extended thinking (faster, cheaper)",
    )
    args = parser.parse_args()

    if not settings.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY не установлен! Добавьте в .env")
        sys.exit(1)

    use_thinking = not args.no_thinking

    if args.live:
        logger.error("Live trading ещё не реализован. Используйте --paper")
        sys.exit(1)
    elif args.paper:
        run_paper_trading(max_markets=args.top, use_thinking=use_thinking)
    else:
        run_analysis(max_markets=args.top, use_thinking=use_thinking)


if __name__ == "__main__":
    main()
