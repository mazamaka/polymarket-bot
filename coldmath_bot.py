"""ColdMath Weather Arbitrage Bot.

Стратегия: покупка NO на погодных рынках Polymarket где ensemble прогноз
показывает что событие маловероятно. Основано на анализе трейдера ColdMath
($77K profit, 95%+ win rate).

Ключевые принципы:
- Покупаем NO по 0.94-0.99 на "exactly"/"between" маркетах
- Ensemble forecast (143 members) даёт точную вероятность
- 2-5% ROI за 1-3 дня при высоком win rate
- Малый размер позиции, много параллельных ставок

Usage:
    # Scan only (no trading)
    python coldmath_bot.py scan

    # Paper trading (simulate)
    python coldmath_bot.py paper

    # Live trading
    python coldmath_bot.py live

    # Live trading with custom settings
    python coldmath_bot.py live --size 3.0 --max-positions 15
"""

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

from analyzer.weather import (
    compute_probability,
    fetch_ensemble_forecast,
    parse_weather_market,
)
from polymarket.api import PolymarketAPI
from polymarket.models import Market

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("coldmath")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
POSITIONS_FILE = DATA_DIR / "coldmath_positions.json"
HISTORY_FILE = DATA_DIR / "coldmath_history.json"


# ── Config ──────────────────────────────────────────────────────────────────


@dataclass
class BotConfig:
    """Конфигурация бота."""

    # Trading
    trade_size_usd: float = 3.0  # $ на позицию
    max_positions: int = 10  # макс одновременных позиций
    max_total_exposure: float = 50.0  # макс $ во всех позициях

    # Market selection
    min_liquidity: float = 50.0  # мин. ликвидность рынка
    max_days_ahead: int = 5  # макс. дней до резолюции (ColdMath: 1-3 дня)
    min_days_ahead: int = 0  # мин. дней до резолюции

    # Edge thresholds — когда покупать NO
    # ColdMath в основном ставит NO на exactly (97%) и below/above (98%)
    min_no_price: float = 0.90  # мин. цена NO (не покупать дешевле)
    max_no_price: float = 0.995  # макс. цена NO (не покупать выше)

    # Ensemble requirements
    min_ensemble_members: int = 10  # мин. ensemble members для решения
    min_model_prob_no: float = 0.85  # мин. вероятность NO по модели

    # Direction-specific thresholds (из backtest ColdMath)
    direction_min_no_prob: dict = field(
        default_factory=lambda: {
            "exactly": 0.85,  # 87.5% исторический NO rate
            "between": 0.82,  # 84.4% NO rate
            "above": 0.80,  # 84.8% NO rate
            "below": 0.90,  # 94.8% NO rate — выше порог, т.к. base rate уже высокий
        }
    )

    # CLOB API
    clob_host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    proxy_url: str = ""

    # Wallet
    private_key: str = ""
    funder_address: str = ""


# ── CLOB Client ─────────────────────────────────────────────────────────────


class ClobTrader:
    """Обёртка для торговли через CLOB API."""

    def __init__(self, config: BotConfig) -> None:
        if not config.private_key:
            raise ValueError("Private key not set")

        # Apply proxy if configured
        if config.proxy_url:
            from trader.proxy_patch import apply_proxy

            apply_proxy(config.proxy_url)

        funder = config.funder_address or None

        # Derive API credentials
        tmp = ClobClient(
            host=config.clob_host,
            chain_id=config.chain_id,
            key=config.private_key,
            signature_type=2,
            funder=funder,
        )
        creds = tmp.derive_api_key()

        self.client = ClobClient(
            host=config.clob_host,
            chain_id=config.chain_id,
            key=config.private_key,
            creds=creds,
            signature_type=2,
            funder=funder,
        )
        logger.info("CLOB client ready | funder=%s", funder or "none")

    def buy_no(self, token_id_no: str, price: float, size_usd: float) -> dict | None:
        """Купить NO токен.

        Args:
            token_id_no: CLOB token ID для NO outcome
            price: цена NO (0.90-0.99)
            size_usd: сколько $ потратить

        Returns:
            Order result dict или None при ошибке.
        """
        shares = size_usd / price
        order_args = OrderArgs(
            token_id=token_id_no,
            price=round(price, 4),
            size=round(shares, 2),
            side="BUY",
        )

        try:
            signed = self.client.create_order(order_args)
            result = self.client.post_order(signed)
            logger.info("Order posted: %s", result)
            return result
        except Exception as e:
            logger.error("Order error: %s", e)
            return None

    def get_orderbook(self, token_id: str) -> dict | None:
        """Получить стакан для token_id."""
        try:
            ob = self.client.get_order_book(token_id)
            asks = getattr(ob, "asks", []) or []
            return {
                "asks": [
                    {
                        "price": float(getattr(a, "price", 0)),
                        "size": float(getattr(a, "size", 0)),
                    }
                    for a in asks
                ],
            }
        except Exception as e:
            logger.error("Orderbook error: %s", e)
            return None

    def get_best_ask(self, token_id: str) -> float:
        """Лучшая цена продажи (ask) — цена по которой можем купить."""
        ob = self.get_orderbook(token_id)
        if not ob or not ob["asks"]:
            return 0.0
        return min(a["price"] for a in ob["asks"] if a["price"] > 0)


# ── Position Storage ────────────────────────────────────────────────────────


def load_positions() -> list[dict]:
    """Загрузить открытые позиции."""
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return []


def save_positions(positions: list[dict]) -> None:
    """Сохранить позиции."""
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2, default=str))


def append_history(entry: dict) -> None:
    """Добавить запись в историю."""
    history = []
    if HISTORY_FILE.exists():
        history = json.loads(HISTORY_FILE.read_text())
    history.append(entry)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))


# ── Scanner ─────────────────────────────────────────────────────────────────


@dataclass
class ScanResult:
    """Результат сканирования одного рынка."""

    market: Market
    city: str
    direction: str
    threshold: float
    target_date: str
    temp_type: str
    ensemble_count: int
    model_prob_yes: float
    model_prob_no: float
    market_price_yes: float
    market_price_no: float
    no_token_id: str
    edge: float  # model_prob_no - market_price_no


def scan_weather_markets(config: BotConfig) -> list[ScanResult]:
    """Сканировать Polymarket на weather маркеты с edge для NO."""
    api = PolymarketAPI()
    results: list[ScanResult] = []

    try:
        markets = api.get_active_markets(
            limit=100, max_markets=2000, sort_by="liquidity"
        )
        now = datetime.now(tz=timezone.utc)
        scanned = 0
        weather_found = 0

        for m in markets:
            if not m.active or m.closed:
                continue
            if m.liquidity < config.min_liquidity:
                continue

            info = parse_weather_market(m)
            if not info:
                continue

            weather_found += 1
            days_ahead = (info.target_date - now).days
            if days_ahead < config.min_days_ahead or days_ahead > config.max_days_ahead:
                continue

            # Ensure we have NO token ID
            if len(m.clob_token_ids) < 2:
                continue
            no_token_id = m.clob_token_ids[1]

            # Fetch ensemble forecast
            temps = fetch_ensemble_forecast(
                info.lat, info.lon, info.target_date, info.temp_type, city=info.city
            )
            if len(temps) < config.min_ensemble_members:
                continue

            scanned += 1

            # Compute probability
            prob_yes = compute_probability(
                temps, info.direction, info.threshold, info.threshold_high
            )
            if prob_yes is None:
                continue

            prob_no = 1.0 - prob_yes
            market_yes = m.outcome_prices[0] if m.outcome_prices else 0.5
            market_no = 1.0 - market_yes

            # Edge for NO side
            edge = prob_no - market_no

            # Filter: only markets where model strongly says NO
            min_prob = config.direction_min_no_prob.get(info.direction, 0.85)
            if prob_no < min_prob:
                continue

            # Filter: NO price in acceptable range
            if market_no < config.min_no_price or market_no > config.max_no_price:
                continue

            results.append(
                ScanResult(
                    market=m,
                    city=info.city,
                    direction=info.direction,
                    threshold=info.threshold,
                    target_date=info.target_date.strftime("%Y-%m-%d"),
                    temp_type=info.temp_type,
                    ensemble_count=len(temps),
                    model_prob_yes=prob_yes,
                    model_prob_no=prob_no,
                    market_price_yes=market_yes,
                    market_price_no=market_no,
                    no_token_id=no_token_id,
                    edge=edge,
                )
            )

        logger.info(
            "Scan: %d weather markets found, %d with forecast, %d with edge",
            weather_found,
            scanned,
            len(results),
        )

    finally:
        api.close()

    # Sort by edge (best first)
    results.sort(key=lambda r: r.edge, reverse=True)
    return results


# ── Display ─────────────────────────────────────────────────────────────────


def print_scan_results(results: list[ScanResult]) -> None:
    """Красивый вывод результатов сканирования."""
    if not results:
        print("\nНет подходящих маркетов.\n")
        return

    print(f"\n{'=' * 100}")
    print(f"  COLDMATH SCANNER — {len(results)} signals found")
    print(f"{'=' * 100}")
    print(
        f"  {'City':<16} {'Dir':<8} {'Thr':>5} {'Date':<12} "
        f"{'Model NO':>9} {'Mkt NO':>8} {'Edge':>6} "
        f"{'Ens':>4} {'Liq':>7} {'Question':<30}"
    )
    print(f"  {'-' * 96}")

    for r in results:
        question_short = r.market.question[:30]
        print(
            f"  {r.city:<16} {r.direction:<8} {r.threshold:>5.0f} {r.target_date:<12} "
            f"{r.model_prob_no:>8.1%} {r.market_price_no:>7.1%} {r.edge:>+5.1%} "
            f"{r.ensemble_count:>4} ${r.market.liquidity:>6,.0f} {question_short}"
        )

    print(f"{'=' * 100}\n")


# ── Trading Logic ───────────────────────────────────────────────────────────


def execute_trades(
    results: list[ScanResult],
    config: BotConfig,
    trader: ClobTrader | None = None,
    paper: bool = True,
) -> None:
    """Исполнить сделки по результатам сканирования."""
    positions = load_positions()
    existing_markets = {p["market_id"] for p in positions}
    current_exposure = sum(p["size_usd"] for p in positions)

    traded = 0

    # Check live balance before trading
    if not paper and trader:
        try:
            import httpx

            wallet = config.funder_address
            if wallet:
                resp = httpx.get(
                    f"https://data-api.polymarket.com/value?user={wallet.lower()}",
                    timeout=10,
                )
                vals = resp.json()
                cash = vals[0].get("value", 0) if vals else 0
                if cash < config.trade_size_usd:
                    logger.info(
                        "SKIP trading: insufficient balance $%.2f < trade size $%.2f",
                        cash,
                        config.trade_size_usd,
                    )
                    return
                logger.info("Balance check OK: $%.2f available", cash)
        except Exception as e:
            logger.warning("Balance check failed (proceeding): %s", e)

    for r in results:
        # Skip if already have position
        if r.market.condition_id in existing_markets:
            logger.info("SKIP (already have): %s", r.market.question[:50])
            continue

        # Check limits
        if len(positions) >= config.max_positions:
            logger.info("Max positions reached (%d)", config.max_positions)
            break

        if current_exposure + config.trade_size_usd > config.max_total_exposure:
            logger.info("Max exposure reached ($%.2f)", config.max_total_exposure)
            break

        # Determine actual price — use market NO price
        buy_price = r.market_price_no

        if paper:
            logger.info(
                "PAPER BUY NO: %s | $%.2f @ %.4f | edge=%+.1f%%",
                r.market.question[:50],
                config.trade_size_usd,
                buy_price,
                r.edge * 100,
            )
            order_id = f"paper_{int(time.time())}_{traded}"
        else:
            if not trader:
                logger.error("No trader for live mode")
                return

            # Check orderbook for best ask
            best_ask = trader.get_best_ask(r.no_token_id)
            if best_ask > 0 and best_ask <= config.max_no_price:
                buy_price = best_ask

            logger.info(
                "LIVE BUY NO: %s | $%.2f @ %.4f | edge=%+.1f%%",
                r.market.question[:50],
                config.trade_size_usd,
                buy_price,
                r.edge * 100,
            )
            result = trader.buy_no(r.no_token_id, buy_price, config.trade_size_usd)
            if not result:
                logger.error("Order failed for %s", r.market.question[:40])
                continue
            order_id = result.get("orderID", str(result))

        # Save position
        position = {
            "market_id": r.market.condition_id,
            "question": r.market.question,
            "city": r.city,
            "direction": r.direction,
            "threshold": r.threshold,
            "target_date": r.target_date,
            "no_token_id": r.no_token_id,
            "entry_price": buy_price,
            "size_usd": config.trade_size_usd,
            "shares": round(config.trade_size_usd / buy_price, 2),
            "model_prob_no": r.model_prob_no,
            "edge": r.edge,
            "ensemble_count": r.ensemble_count,
            "order_id": order_id,
            "opened_at": datetime.now(tz=timezone.utc).isoformat(),
            "status": "open",
            "paper": paper,
        }
        positions.append(position)
        existing_markets.add(r.market.condition_id)
        current_exposure += config.trade_size_usd
        traded += 1

    save_positions(positions)
    logger.info(
        "Traded: %d new positions | Total: %d | Exposure: $%.2f",
        traded,
        len(positions),
        current_exposure,
    )


# ── Position Monitor ────────────────────────────────────────────────────────


def check_positions(config: BotConfig) -> None:
    """Проверить статус позиций — resolved маркеты."""
    positions = load_positions()
    if not positions:
        return

    api = PolymarketAPI()
    updated = False

    try:
        for pos in positions:
            if pos["status"] != "open":
                continue

            market = api.get_market_by_id(pos["market_id"])
            if not market:
                continue

            if market.closed:
                # Market resolved
                yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5
                no_won = yes_price < 0.5  # NO wins if YES resolved < 50%

                pnl = pos["shares"] - pos["size_usd"] if no_won else -pos["size_usd"]
                pos["status"] = "won" if no_won else "lost"
                pos["pnl"] = round(pnl, 2)
                pos["resolved_at"] = datetime.now(tz=timezone.utc).isoformat()

                result_str = "WON" if no_won else "LOST"
                logger.info(
                    "%s: %s | PnL: $%.2f | %s",
                    result_str,
                    pos["question"][:50],
                    pnl,
                    pos["city"],
                )

                append_history(pos)
                updated = True
    finally:
        api.close()

    if updated:
        # Remove resolved positions
        open_positions = [p for p in positions if p["status"] == "open"]
        save_positions(open_positions)

    # Summary
    open_pos = [p for p in positions if p["status"] == "open"]
    if open_pos:
        total = sum(p["size_usd"] for p in open_pos)
        print(f"\nOpen positions: {len(open_pos)} | Exposure: ${total:.2f}")
        for p in open_pos:
            print(
                f"  {p['city']:<16} {p['direction']:<8} {p['threshold']:>5.0f}°F "
                f"{p['target_date']} | NO @ {p['entry_price']:.3f} | ${p['size_usd']:.2f} "
                f"| edge={p['edge']:+.1%}"
            )


# ── Web Dashboard ───────────────────────────────────────────────────────────


def create_web_app() -> "FastAPI":
    """Create FastAPI dashboard app."""
    import threading

    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel as PydanticBaseModel

    app = FastAPI(title="ColdMath Weather Bot")

    # State
    _state: dict = {
        "bot_running": False,
        "mode": os.environ.get("BOT_MODE", "paper"),
        "scan_interval_min": int(os.environ.get("SCAN_INTERVAL", "30")),
        "last_scan": None,
        "next_scan_at": 0,
        "signals": [],
        "trader": None,
        "stop_event": None,
    }

    _config = BotConfig(
        trade_size_usd=float(os.environ.get("TRADE_SIZE", "2.0")),
        max_positions=int(os.environ.get("MAX_POSITIONS", "10")),
        max_total_exposure=float(os.environ.get("MAX_EXPOSURE", "50.0")),
        max_days_ahead=int(os.environ.get("MAX_DAYS", "5")),
        private_key=os.environ.get("POLYGON_WALLET_PRIVATE_KEY", ""),
        funder_address=os.environ.get("POLYGON_WALLET_ADDRESS", ""),
        proxy_url=os.environ.get("CLOB_PROXY_URL", ""),
    )

    class SettingsBody(PydanticBaseModel):
        trade_size_usd: float | None = None
        max_positions: int | None = None
        max_total_exposure: float | None = None
        max_days_ahead: int | None = None
        min_no_price: float | None = None
        min_ensemble_members: int | None = None
        scan_interval_min: int | None = None
        mode: str | None = None

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        tpl = Path(__file__).parent / "web" / "templates" / "coldmath.html"
        return HTMLResponse(tpl.read_text())

    @app.get("/api/status")
    async def status():
        positions = load_positions()
        history = []
        if HISTORY_FILE.exists():
            history = json.loads(HISTORY_FILE.read_text())

        exposure = sum(p["size_usd"] for p in positions)
        wins = sum(1 for h in history if h.get("status") == "won")
        losses = sum(1 for h in history if h.get("status") == "lost")
        total_pnl = sum(h.get("pnl", 0) for h in history)
        win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0

        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        today_trades = sum(
            1 for h in history if h.get("opened_at", "").startswith(today)
        )

        avg_edge = 0
        sigs = _state["signals"]
        if sigs:
            avg_edge = sum(s.get("edge", 0) for s in sigs) / len(sigs)

        next_in = ""
        if _state["bot_running"] and _state["next_scan_at"] > 0:
            remaining = max(0, _state["next_scan_at"] - time.time())
            next_in = f"{int(remaining // 60)}m {int(remaining % 60)}s"

        # Get cash balance from Data API
        cash = 0
        wallet = _config.funder_address or ""
        if wallet:
            try:
                import httpx

                resp = httpx.get(
                    f"https://data-api.polymarket.com/value?user={wallet.lower()}",
                    timeout=5,
                )
                vals = resp.json()
                if vals:
                    cash = vals[0].get("value", 0)
            except Exception:
                pass

        return {
            "bot_running": _state["bot_running"],
            "mode": _state["mode"],
            "positions": positions,
            "history": history,
            "signals": _state["signals"],
            "signals_count": len(sigs),
            "exposure": exposure,
            "total_pnl": total_pnl,
            "win_rate": win_rate,
            "total_trades": wins + losses,
            "today_trades": today_trades,
            "last_scan": _state["last_scan"],
            "next_scan_in": next_in,
            "avg_edge": avg_edge,
            "cash_balance": cash,
            "config": {
                "trade_size_usd": _config.trade_size_usd,
                "max_positions": _config.max_positions,
                "max_total_exposure": _config.max_total_exposure,
                "max_days_ahead": _config.max_days_ahead,
                "min_no_price": _config.min_no_price,
                "min_ensemble_members": _config.min_ensemble_members,
                "scan_interval_min": _state["scan_interval_min"],
                "mode": _state["mode"],
            },
        }

    @app.post("/api/scan")
    async def api_scan():
        results = scan_weather_markets(_config)
        _state["signals"] = [
            {
                "city": r.city,
                "direction": r.direction,
                "threshold": r.threshold,
                "target_date": r.target_date,
                "model_prob_no": r.model_prob_no,
                "market_price_no": r.market_price_no,
                "edge": r.edge,
                "ensemble_count": r.ensemble_count,
                "question": r.market.question,
            }
            for r in results
        ]
        _state["last_scan"] = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")

        trades_made = 0
        if _state["mode"] != "scan":
            is_paper = _state["mode"] == "paper"
            trader = None
            if not is_paper and _config.private_key:
                if not _state["trader"]:
                    _state["trader"] = ClobTrader(_config)
                trader = _state["trader"]
            execute_trades(results, _config, trader=trader, paper=is_paper)
            trades_made = min(
                len(results), _config.max_positions - len(load_positions())
            )

        check_positions(_config)
        return {"signals_count": len(results), "trades_made": max(0, trades_made)}

    @app.post("/api/start")
    async def api_start():
        if _state["bot_running"]:
            return {"status": "already running"}

        stop_evt = threading.Event()
        _state["stop_event"] = stop_evt
        _state["bot_running"] = True

        def bot_loop():
            logger.info(
                "Bot loop started (mode=%s, interval=%dm)",
                _state["mode"],
                _state["scan_interval_min"],
            )
            while not stop_evt.is_set():
                try:
                    results = scan_weather_markets(_config)
                    _state["signals"] = [
                        {
                            "city": r.city,
                            "direction": r.direction,
                            "threshold": r.threshold,
                            "target_date": r.target_date,
                            "model_prob_no": r.model_prob_no,
                            "market_price_no": r.market_price_no,
                            "edge": r.edge,
                            "ensemble_count": r.ensemble_count,
                            "question": r.market.question,
                        }
                        for r in results
                    ]
                    _state["last_scan"] = datetime.now(tz=timezone.utc).strftime(
                        "%H:%M:%S"
                    )

                    is_paper = _state["mode"] == "paper"
                    trader = None
                    if not is_paper and _config.private_key:
                        if not _state["trader"]:
                            _state["trader"] = ClobTrader(_config)
                        trader = _state["trader"]
                    execute_trades(results, _config, trader=trader, paper=is_paper)
                    check_positions(_config)
                except Exception as e:
                    logger.error("Bot loop error: %s", e)

                _state["next_scan_at"] = time.time() + _state["scan_interval_min"] * 60
                stop_evt.wait(_state["scan_interval_min"] * 60)

            _state["bot_running"] = False
            logger.info("Bot loop stopped")

        t = threading.Thread(target=bot_loop, daemon=True)
        t.start()
        return {"status": "started"}

    @app.post("/api/stop")
    async def api_stop():
        if _state["stop_event"]:
            _state["stop_event"].set()
            _state["bot_running"] = False
        return {"status": "stopped"}

    @app.post("/api/settings")
    async def api_settings(body: SettingsBody):
        if body.trade_size_usd is not None:
            _config.trade_size_usd = body.trade_size_usd
        if body.max_positions is not None:
            _config.max_positions = body.max_positions
        if body.max_total_exposure is not None:
            _config.max_total_exposure = body.max_total_exposure
        if body.max_days_ahead is not None:
            _config.max_days_ahead = body.max_days_ahead
        if body.min_no_price is not None:
            _config.min_no_price = body.min_no_price
        if body.min_ensemble_members is not None:
            _config.min_ensemble_members = body.min_ensemble_members
        if body.scan_interval_min is not None:
            _state["scan_interval_min"] = body.scan_interval_min
        if body.mode is not None:
            _state["mode"] = body.mode
        return {"status": "ok"}

    return app


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="ColdMath Weather Arbitrage Bot")
    parser.add_argument(
        "mode", choices=["scan", "paper", "live", "web"], help="Operating mode"
    )
    parser.add_argument(
        "--size", type=float, default=3.0, help="Trade size in USD (default: 3.0)"
    )
    parser.add_argument(
        "--max-positions", type=int, default=10, help="Max positions (default: 10)"
    )
    parser.add_argument(
        "--max-days", type=int, default=5, help="Max days ahead (default: 5)"
    )
    parser.add_argument(
        "--loop", type=int, default=0, help="Loop interval in minutes (0=once)"
    )
    parser.add_argument(
        "--check", action="store_true", help="Check existing positions only"
    )
    parser.add_argument("--port", type=int, default=8877, help="Web dashboard port")
    args = parser.parse_args()

    if args.mode == "web":
        import uvicorn

        app = create_web_app()
        logger.info("Starting ColdMath dashboard on port %d", args.port)
        uvicorn.run(app, host="0.0.0.0", port=args.port)
        return

    config = BotConfig(
        trade_size_usd=args.size,
        max_positions=args.max_positions,
        max_days_ahead=args.max_days,
        private_key=os.environ.get("POLYGON_WALLET_PRIVATE_KEY", ""),
        funder_address=os.environ.get("POLYGON_WALLET_ADDRESS", ""),
        proxy_url=os.environ.get("CLOB_PROXY_URL", ""),
    )

    if args.check:
        check_positions(config)
        return

    trader = None
    if args.mode == "live":
        if not config.private_key:
            logger.error("Set POLYGON_WALLET_PRIVATE_KEY env var for live trading")
            sys.exit(1)
        trader = ClobTrader(config)

    def run_cycle() -> None:
        logger.info("=== Scan cycle started (%s mode) ===", args.mode)

        # Check existing positions first
        check_positions(config)

        # Scan for new opportunities
        results = scan_weather_markets(config)
        print_scan_results(results)

        if args.mode != "scan":
            execute_trades(
                results,
                config,
                trader=trader,
                paper=(args.mode == "paper"),
            )

    run_cycle()

    if args.loop > 0:
        logger.info("Looping every %d minutes. Ctrl+C to stop.", args.loop)
        while True:
            time.sleep(args.loop * 60)
            try:
                run_cycle()
            except Exception as e:
                logger.error("Cycle error: %s", e)


if __name__ == "__main__":
    main()
