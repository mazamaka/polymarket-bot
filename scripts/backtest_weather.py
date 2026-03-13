"""Backtest weather стратегии на исторических данных Polymarket.

Скачивает закрытые weather рынки с Gamma API, прогоняет парсер,
анализирует результаты стратегий (always NO, ensemble-based).
"""

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer.weather import CITY_COORDS, parse_weather_question  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

GAMMA_API_URL = "https://gamma-api.polymarket.com"
CLOB_API_URL = "https://clob.polymarket.com"
DATA_DIR = PROJECT_ROOT / "data"
RATE_LIMIT_SEC = 0.3
PAGE_SIZE = 500


@dataclass
class BacktestTrade:
    """Результат одной виртуальной сделки."""

    market_id: str
    question: str
    direction: str
    threshold: float
    threshold_high: float | None
    city: str
    temp_type: str
    date_str: str
    unit: str
    resolved_yes: bool
    yes_price_at_open: float | None
    side: str
    entry_price: float
    pnl: float


@dataclass
class BacktestResults:
    """Агрегированные результаты backtest."""

    total_weather_markets: int = 0
    parsed_markets: int = 0
    unparsed_questions: list[str] = field(default_factory=list)
    known_city_markets: int = 0
    unknown_cities: list[str] = field(default_factory=list)

    always_no_trades: int = 0
    always_no_wins: int = 0
    always_no_pnl: float = 0.0

    exact_no_trades: int = 0
    exact_no_wins: int = 0
    exact_no_pnl: float = 0.0

    direction_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)


async def fetch_closed_weather_markets(
    client: httpx.AsyncClient,
) -> list[dict[str, Any]]:
    """Скачать все закрытые рынки с Gamma API и отфильтровать weather.

    Использует инкрементальный кэш — сохраняет каждые 50 страниц,
    чтобы при прерывании не терять прогресс.
    """
    cache_path = DATA_DIR / "historical_weather_markets.json"
    progress_path = DATA_DIR / "weather_download_progress.json"

    if cache_path.exists():
        logger.info("Загружаем кэш из %s", cache_path)
        with open(cache_path) as f:
            markets = json.load(f)
        logger.info("Загружено %d weather рынков из кэша", len(markets))
        return markets

    # Проверяем незавершённую загрузку
    weather_markets: list[dict[str, Any]] = []
    start_offset = 0

    if progress_path.exists():
        with open(progress_path) as f:
            progress = json.load(f)
        weather_markets = progress.get("markets", [])
        start_offset = progress.get("next_offset", 0)
        logger.info(
            "Возобновляем загрузку: %d weather рынков, offset=%d",
            len(weather_markets),
            start_offset,
        )

    logger.info("Скачиваем закрытые рынки с Gamma API (page_size=%d)...", PAGE_SIZE)
    offset = start_offset
    pages_since_save = 0

    while True:
        try:
            resp = await client.get(
                f"{GAMMA_API_URL}/markets",
                params={
                    "closed": "true",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
                timeout=30,
            )
            resp.raise_for_status()
            page = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning("Gamma API error at offset %d: %s", offset, e)
            break
        except httpx.RequestError as e:
            logger.warning("Request error at offset %d: %s — retrying in 5s", offset, e)
            await asyncio.sleep(5)
            continue

        if not page:
            break

        for market in page:
            question = market.get("question", "").lower()
            if "temperature" in question:
                weather_markets.append(market)

        pages_since_save += 1
        if pages_since_save % 20 == 0:
            # Инкрементальное сохранение каждые 20 страниц (10000 рынков)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(progress_path, "w") as f:
                json.dump(
                    {"markets": weather_markets, "next_offset": offset + PAGE_SIZE},
                    f,
                    default=str,
                )
            logger.info(
                "Progress saved: %d weather markets at offset %d",
                len(weather_markets),
                offset,
            )

        logger.info(
            "offset=%d, page=%d, weather=%d, total_scanned=%d",
            offset,
            len(page),
            len(weather_markets),
            offset + len(page),
        )

        if len(page) < PAGE_SIZE:
            break

        offset += PAGE_SIZE
        await asyncio.sleep(RATE_LIMIT_SEC)

    logger.info("Всего найдено %d weather рынков", len(weather_markets))

    # Финальное сохранение
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "w") as f:
        json.dump(weather_markets, f, indent=2, default=str)
    logger.info("Сохранено в %s", cache_path)

    # Удаляем файл прогресса
    if progress_path.exists():
        progress_path.unlink()

    return weather_markets


def determine_resolution(market: dict[str, Any]) -> bool | None:
    """Определить resolved YES или NO."""
    outcome_prices_str = market.get("outcomePrices", "")
    if outcome_prices_str:
        try:
            if isinstance(outcome_prices_str, str):
                prices = json.loads(outcome_prices_str)
            else:
                prices = outcome_prices_str
            if len(prices) >= 2:
                yes_price = float(prices[0])
                if yes_price > 0.9:
                    return True
                if yes_price < 0.1:
                    return False
        except (json.JSONDecodeError, ValueError, IndexError):
            pass

    resolved_to = market.get("resolvedTo", market.get("resolved_to", ""))
    if resolved_to:
        resolved_str = str(resolved_to).lower()
        if resolved_str in ("yes", "1", "true"):
            return True
        if resolved_str in ("no", "0", "false"):
            return False

    return None


def get_best_price(market: dict[str, Any]) -> float | None:
    """Получить цену YES из bestAsk или outcomePrices."""
    # bestAsk — цена на момент закрытия/последняя
    best_ask = market.get("bestAsk")
    if best_ask:
        try:
            return float(best_ask)
        except (ValueError, TypeError):
            pass

    outcome_prices_str = market.get("outcomePrices", "")
    if outcome_prices_str:
        try:
            if isinstance(outcome_prices_str, str):
                prices = json.loads(outcome_prices_str)
            else:
                prices = outcome_prices_str
            if prices:
                p = float(prices[0])
                # Если не 0 и не 1 (иначе это post-resolution цена)
                if 0.01 < p < 0.99:
                    return p
        except (json.JSONDecodeError, ValueError, IndexError):
            pass
    return None


async def run_backtest() -> BacktestResults:
    """Главная функция backtest."""
    results = BacktestResults()

    async with httpx.AsyncClient() as client:
        markets = await fetch_closed_weather_markets(client)
        results.total_weather_markets = len(markets)

        if not markets:
            logger.warning("Не найдено weather рынков")
            return results

        parsed_markets: list[tuple[dict[str, Any], dict[str, Any]]] = []

        for market in markets:
            question = market.get("question", "")
            parsed = parse_weather_question(question)

            if parsed is None:
                results.unparsed_questions.append(question)
                continue

            results.parsed_markets += 1

            city_lower = parsed["city"].lower()
            has_coords = city_lower in CITY_COORDS
            if not has_coords:
                for name in CITY_COORDS:
                    if name in city_lower or city_lower in name:
                        has_coords = True
                        break

            if has_coords:
                results.known_city_markets += 1
            else:
                if parsed["city"] not in results.unknown_cities:
                    results.unknown_cities.append(parsed["city"])

            parsed_markets.append((market, parsed))

        logger.info(
            "Parsed: %d / %d | Known cities: %d | Unknown: %d",
            results.parsed_markets,
            results.total_weather_markets,
            results.known_city_markets,
            len(results.unknown_cities),
        )

        # Анализируем каждый рынок
        for market, parsed in parsed_markets:
            market_id = market.get("id", "")
            question = market.get("question", "")
            resolution = determine_resolution(market)

            if resolution is None:
                continue

            direction = parsed["direction"]
            threshold = parsed["threshold"]
            threshold_high = parsed["threshold_high"]

            # Цена YES — берём bestAsk или из outcomePrices
            yes_price_at_open = get_best_price(market)
            entry_yes_price = (
                yes_price_at_open if yes_price_at_open is not None else 0.5
            )
            no_price = 1.0 - entry_yes_price

            if no_price < 0.01:
                continue

            # Strategy 1: Always bet NO
            if resolution:  # resolved YES — проиграли
                pnl_no = -no_price
            else:  # resolved NO — выиграли
                pnl_no = entry_yes_price

            results.always_no_trades += 1
            results.always_no_pnl += pnl_no
            if pnl_no > 0:
                results.always_no_wins += 1

            # Strategy 2: Bet NO only on "exactly" and "between"
            if direction in ("exactly", "between"):
                results.exact_no_trades += 1
                results.exact_no_pnl += pnl_no
                if pnl_no > 0:
                    results.exact_no_wins += 1

            # Per-direction stats
            if direction not in results.direction_stats:
                results.direction_stats[direction] = {
                    "total": 0,
                    "resolved_yes": 0,
                    "resolved_no": 0,
                    "always_no_pnl": 0.0,
                    "always_no_wins": 0,
                }
            stats = results.direction_stats[direction]
            stats["total"] += 1
            if resolution:
                stats["resolved_yes"] += 1
            else:
                stats["resolved_no"] += 1
            stats["always_no_pnl"] += pnl_no
            if pnl_no > 0:
                stats["always_no_wins"] += 1

            results.trades.append(
                {
                    "market_id": market_id,
                    "question": question,
                    "direction": direction,
                    "threshold": threshold,
                    "threshold_high": threshold_high,
                    "city": parsed["city"],
                    "temp_type": parsed["temp_type"],
                    "date_str": parsed["date_str"],
                    "unit": parsed["unit"],
                    "resolved_yes": resolution,
                    "yes_price": entry_yes_price,
                    "no_price": no_price,
                    "always_no_pnl": pnl_no,
                    "volume": market.get("volume", 0),
                    "liquidity": market.get("liquidity", 0),
                    "end_date": market.get("endDate", ""),
                }
            )

    return results


def print_report(results: BacktestResults) -> None:
    """Вывести отчёт в консоль."""
    sep = "=" * 70
    print(f"\n{sep}")
    print("  BACKTEST REPORT: Weather Markets Strategy")
    print(sep)

    print("\n--- DATA OVERVIEW ---")
    print(f"  Total weather markets found:      {results.total_weather_markets}")
    print(f"  Successfully parsed:              {results.parsed_markets}")
    parse_rate = (
        results.parsed_markets / results.total_weather_markets * 100
        if results.total_weather_markets
        else 0
    )
    print(f"  Parse rate:                       {parse_rate:.1f}%")
    print(f"  Known cities (have coords):       {results.known_city_markets}")
    if results.unknown_cities:
        print(
            f"  Unknown cities ({len(results.unknown_cities)}): "
            f"{', '.join(results.unknown_cities[:15])}"
        )
    if results.unparsed_questions:
        print(f"\n  Unparsed examples ({len(results.unparsed_questions)}):")
        for q in results.unparsed_questions[:5]:
            print(f"    - {q[:100]}")

    print("\n--- STRATEGY 1: Always Bet NO ---")
    if results.always_no_trades > 0:
        wr = results.always_no_wins / results.always_no_trades * 100
        avg_pnl = results.always_no_pnl / results.always_no_trades
        print(f"  Trades:     {results.always_no_trades}")
        print(f"  Wins:       {results.always_no_wins}")
        print(f"  Win rate:   {wr:.1f}%")
        print(f"  Total PnL:  ${results.always_no_pnl:.2f} (per $1 per trade)")
        print(f"  Avg PnL:    ${avg_pnl:.4f} per trade")
    else:
        print("  No trades")

    print("\n--- STRATEGY 2: Bet NO on Exact Values Only ---")
    print("  (Only 'exactly' and 'between' directions)")
    if results.exact_no_trades > 0:
        wr = results.exact_no_wins / results.exact_no_trades * 100
        avg_pnl = results.exact_no_pnl / results.exact_no_trades
        print(f"  Trades:     {results.exact_no_trades}")
        print(f"  Wins:       {results.exact_no_wins}")
        print(f"  Win rate:   {wr:.1f}%")
        print(f"  Total PnL:  ${results.exact_no_pnl:.2f} (per $1 per trade)")
        print(f"  Avg PnL:    ${avg_pnl:.4f} per trade")
    else:
        print("  No trades")

    print("\n--- PER-DIRECTION BREAKDOWN ---")
    for direction, stats in sorted(results.direction_stats.items()):
        total = stats["total"]
        yes = stats["resolved_yes"]
        no = stats["resolved_no"]
        yes_pct = yes / total * 100 if total else 0
        no_pct = no / total * 100 if total else 0
        pnl = stats["always_no_pnl"]
        wins = stats["always_no_wins"]
        wr = wins / total * 100 if total else 0
        avg = pnl / total if total else 0
        print(
            f"  {direction:12s} | n={total:4d} | YES={yes_pct:5.1f}% NO={no_pct:5.1f}% | "
            f"NO strategy: WR={wr:5.1f}% PnL=${pnl:+8.2f} avg=${avg:+.4f}"
        )

    # Топ прибыльных и убыточных сделок
    if results.trades:
        sorted_trades = sorted(results.trades, key=lambda t: t["always_no_pnl"])
        print("\n--- TOP 5 LOSSES (Always NO) ---")
        for t in sorted_trades[:5]:
            print(
                f"  PnL=${t['always_no_pnl']:+.4f} | {t['direction']:8s} | "
                f"YES={t['yes_price']:.2f} | {t['question'][:60]}"
            )
        print("\n--- TOP 5 WINS (Always NO) ---")
        for t in sorted_trades[-5:]:
            print(
                f"  PnL=${t['always_no_pnl']:+.4f} | {t['direction']:8s} | "
                f"YES={t['yes_price']:.2f} | {t['question'][:60]}"
            )

    print(f"\n{sep}\n")


def save_results(results: BacktestResults) -> None:
    """Сохранить результаты в JSON."""
    output_path = DATA_DIR / "backtest_results.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "summary": {
            "total_weather_markets": results.total_weather_markets,
            "parsed_markets": results.parsed_markets,
            "known_city_markets": results.known_city_markets,
            "unknown_cities": results.unknown_cities,
            "unparsed_count": len(results.unparsed_questions),
        },
        "strategy_always_no": {
            "trades": results.always_no_trades,
            "wins": results.always_no_wins,
            "win_rate": (
                results.always_no_wins / results.always_no_trades
                if results.always_no_trades
                else 0
            ),
            "total_pnl": results.always_no_pnl,
            "avg_pnl": (
                results.always_no_pnl / results.always_no_trades
                if results.always_no_trades
                else 0
            ),
        },
        "strategy_exact_no": {
            "trades": results.exact_no_trades,
            "wins": results.exact_no_wins,
            "win_rate": (
                results.exact_no_wins / results.exact_no_trades
                if results.exact_no_trades
                else 0
            ),
            "total_pnl": results.exact_no_pnl,
            "avg_pnl": (
                results.exact_no_pnl / results.exact_no_trades
                if results.exact_no_trades
                else 0
            ),
        },
        "direction_stats": results.direction_stats,
        "trades": results.trades,
        "unparsed_questions": results.unparsed_questions[:50],
    }

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, default=str)

    logger.info("Результаты сохранены в %s", output_path)


async def main() -> None:
    """Entry point."""
    start = time.monotonic()

    logger.info("Starting weather backtest...")
    results = await run_backtest()

    elapsed = time.monotonic() - start
    logger.info("Backtest completed in %.1f seconds", elapsed)

    print_report(results)
    save_results(results)


if __name__ == "__main__":
    asyncio.run(main())
