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
import asyncio
import json
import logging
import logging.handlers
import os
import sys
import threading
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

# PostgreSQL (optional — graceful fallback to JSON-only)
_db_available = False
try:
    import coldmath_db as db

    _db_available = True
except ImportError:
    db = None  # type: ignore[assignment]

_log_dir = Path(__file__).parent / "data"
_log_dir.mkdir(exist_ok=True)

_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _handlers.append(
        logging.handlers.RotatingFileHandler(
            _log_dir / "coldmath_bot.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    )
except PermissionError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=_handlers,
)
logger = logging.getLogger("coldmath")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
POSITIONS_FILE = DATA_DIR / "coldmath_positions.json"
HISTORY_FILE = DATA_DIR / "coldmath_history.json"

# Thread-safe file access
_file_lock = threading.Lock()


# ── On-chain helpers ────────────────────────────────────────────────────────

_w3_instance = None
_USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def _get_w3():
    """Cached Web3 instance."""
    global _w3_instance
    if _w3_instance is None:
        from web3 import Web3

        _w3_instance = Web3(Web3.HTTPProvider("https://polygon-bor.publicnode.com"))
    return _w3_instance


def _get_usdc_balance(wallet: str) -> float:
    """Get on-chain USDC balance for wallet address."""
    if not wallet:
        return 0.0
    from web3 import Web3

    w3 = _get_w3()
    usdc = w3.eth.contract(
        address=Web3.to_checksum_address(_USDC_ADDRESS), abi=_USDC_ABI
    )
    return usdc.functions.balanceOf(Web3.to_checksum_address(wallet)).call() / 1e6


# ── Proxy Check ────────────────────────────────────────────────────────────


_BLOCKED_COUNTRIES = {"US"}


@dataclass
class ProxyStatus:
    """Результат проверки прокси."""

    ok: bool
    ip: str = ""
    country: str = ""
    city: str = ""
    latency_ms: int = 0
    can_trade: bool = False
    clob_reachable: bool = False
    error: str = ""


def _proxy_status_dict(ps: ProxyStatus) -> dict:
    """Convert ProxyStatus to JSON-serializable dict."""
    return {
        "ok": ps.ok,
        "ip": ps.ip,
        "country": ps.country,
        "city": ps.city,
        "latency_ms": ps.latency_ms,
        "can_trade": ps.can_trade,
        "clob_reachable": ps.clob_reachable,
        "error": ps.error,
    }


# Cached proxy client — reuses TCP/TLS connections across checks
_proxy_check_client: tuple[str, "httpx.Client"] | None = None


def _get_proxy_check_client(proxy_url: str) -> "httpx.Client":
    """Get or create cached httpx.Client for proxy checks."""
    global _proxy_check_client
    import httpx

    if _proxy_check_client is not None:
        cached_url, client = _proxy_check_client
        if cached_url == proxy_url:
            return client
        try:
            client.close()
        except Exception:
            pass

    client = httpx.Client(proxy=proxy_url, timeout=15)
    _proxy_check_client = (proxy_url, client)
    return client


def check_proxy(proxy_url_template: str, full: bool = True) -> ProxyStatus:
    """Проверить валидность прокси.

    Args:
        proxy_url_template: URL прокси с возможным {session} placeholder.
        full: True = полная проверка (ipinfo + CLOB).
              False = только CLOB ping (быстрая pre-trade проверка).

    Returns:
        ProxyStatus с диагностикой.
    """
    if not proxy_url_template:
        return ProxyStatus(ok=False, error="No proxy configured")

    import httpx

    proxy_url = proxy_url_template.replace("{session}", "proxycheck")

    try:
        client = _get_proxy_check_client(proxy_url)
        ip = ""
        country = ""
        city = ""
        latency = 0

        if full:
            # Full: IP/geo via ipinfo + CLOB
            start = time.monotonic()
            resp = client.get("https://ipinfo.io/json")
            latency = int((time.monotonic() - start) * 1000)

            if resp.status_code != 200:
                return ProxyStatus(ok=False, error=f"ipinfo HTTP {resp.status_code}")

            data = resp.json()
            ip = data.get("ip", "")
            country = data.get("country", "")
            city = data.get("city", "")

            if country in _BLOCKED_COUNTRIES:
                return ProxyStatus(
                    ok=True,
                    ip=ip,
                    country=country,
                    city=city,
                    latency_ms=latency,
                    can_trade=False,
                    clob_reachable=False,
                    error=f"Blocked country: {country} (Polymarket unavailable)",
                )

        # Check CLOB API reachability
        clob_reachable = False
        try:
            clob_start = time.monotonic()
            clob_resp = client.get("https://clob.polymarket.com/time", timeout=10)
            clob_reachable = clob_resp.status_code == 200
            if not full:
                latency = int((time.monotonic() - clob_start) * 1000)
        except Exception:
            pass

        can_trade = clob_reachable and (not full or country not in _BLOCKED_COUNTRIES)
        error = ""
        if not clob_reachable:
            error = "CLOB API unreachable through proxy"

        return ProxyStatus(
            ok=True,
            ip=ip,
            country=country,
            city=city,
            latency_ms=latency,
            can_trade=can_trade,
            clob_reachable=clob_reachable,
            error=error,
        )
    except httpx.ProxyError as e:
        return ProxyStatus(ok=False, error=f"Proxy error: {e}")
    except httpx.ConnectError as e:
        return ProxyStatus(ok=False, error=f"Connect error: {e}")
    except httpx.TimeoutException:
        return ProxyStatus(ok=False, error="Timeout (15s)")
    except Exception as e:
        return ProxyStatus(ok=False, error=str(e))


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

    # Edge scaling — мин. edge растёт с дистанцией до резолюции
    # Чем дальше дата, тем менее точен прогноз → требуем больший edge
    edge_scaling: dict = field(
        default_factory=lambda: {
            0: 0.03,  # сегодня/завтра — 3%
            1: 0.03,  # 1 день — 3%
            2: 0.05,  # 2 дня — 5%
            3: 0.08,  # 3 дня — 8%
            4: 0.12,  # 4 дня — 12%
            5: 0.15,  # 5 дней — 15%
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

        # Apply direct+proxy fallback if proxy configured
        if config.proxy_url:
            from trader.proxy_patch import apply_proxy

            apply_proxy(config.proxy_url)
        logger.info(
            "CLOB mode: %s",
            "direct + proxy fallback" if config.proxy_url else "direct only",
        )

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


def _atomic_write(path: Path, data: str) -> None:
    """Атомарная запись файла (write tmp + rename)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    os.replace(tmp, path)


def load_positions() -> list[dict]:
    """Загрузить открытые позиции (thread-safe)."""
    with _file_lock:
        if POSITIONS_FILE.exists():
            try:
                return json.loads(POSITIONS_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                return []
    return []


def save_positions(positions: list[dict]) -> None:
    """Сохранить позиции (thread-safe, atomic write)."""
    with _file_lock:
        _atomic_write(POSITIONS_FILE, json.dumps(positions, indent=2, default=str))


def append_history(entry: dict) -> None:
    """Добавить запись в историю (thread-safe, atomic write)."""
    with _file_lock:
        history = []
        if HISTORY_FILE.exists():
            try:
                history = json.loads(HISTORY_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        history.append(entry)
        _atomic_write(HISTORY_FILE, json.dumps(history, indent=2, default=str))


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
    ensemble_temps: list[float] = field(default_factory=list)
    days_ahead: int = 0


def scan_weather_markets(config: BotConfig) -> tuple[list[ScanResult], dict]:
    """Сканировать Polymarket на weather маркеты с edge для NO.

    Returns:
        Tuple of (results, scan_stats).
    """
    api = PolymarketAPI()
    results: list[ScanResult] = []
    forecast_failed = 0

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
                forecast_failed += 1
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

            # Edge scaling — require higher edge for further dates
            min_edge = config.edge_scaling.get(
                days_ahead, max(config.edge_scaling.values())
            )
            if edge < min_edge:
                logger.debug(
                    "Edge scaling skip: %s %dd edge=%.1f%% < min=%.1f%%",
                    info.city,
                    days_ahead,
                    edge * 100,
                    min_edge * 100,
                )
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
                    ensemble_temps=temps,
                    days_ahead=days_ahead,
                )
            )

        logger.info(
            "Scan: %d weather markets, %d forecasts OK, %d failed, %d signals",
            weather_found,
            scanned,
            forecast_failed,
            len(results),
        )

    finally:
        api.close()

    results.sort(key=lambda r: r.edge, reverse=True)
    stats = {
        "weather_markets": weather_found,
        "forecasts_ok": scanned,
        "forecasts_failed": forecast_failed,
        "signals": len(results),
        "status": "ok"
        if forecast_failed == 0
        else ("degraded" if scanned > 0 else "error"),
    }
    return results, stats


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
) -> int:
    """Исполнить сделки по результатам сканирования. Returns count of trades made."""
    positions = load_positions()
    existing_markets = {p["market_id"] for p in positions}

    # Also check on-chain positions to avoid duplicates with other bots
    if not paper and config.funder_address:
        try:
            import httpx

            resp = httpx.get(
                "https://data-api.polymarket.com/positions",
                params={
                    "user": config.funder_address.lower(),
                    "sizeThreshold": "0",
                    "limit": "200",
                },
                timeout=10,
            )
            for lp in resp.json():
                cid = lp.get("conditionId", "")
                if cid:
                    existing_markets.add(cid)
        except Exception:
            pass

    current_exposure = sum(p["size_usd"] for p in positions)

    traded = 0

    # Check live balance before trading (on-chain USDC)
    available_cash = float("inf")  # unlimited for paper
    if not paper and trader:
        try:
            available_cash = _get_usdc_balance(config.funder_address)
            if available_cash < config.trade_size_usd:
                logger.info(
                    "SKIP trading: insufficient balance $%.2f < trade size $%.2f",
                    available_cash,
                    config.trade_size_usd,
                )
                return 0
            logger.info("Balance check OK: $%.2f available", available_cash)
        except Exception as e:
            logger.warning("Balance check failed (proceeding): %s", e)

    for r in results:
        # Skip if already have position
        if r.market.condition_id in existing_markets:
            continue

        # Check limits
        if len(positions) >= config.max_positions:
            logger.info("Max positions reached (%d)", config.max_positions)
            break

        if current_exposure + config.trade_size_usd > config.max_total_exposure:
            logger.info("Max exposure reached ($%.2f)", config.max_total_exposure)
            break

        # Check remaining cash (prevents 'not enough balance' spam)
        if available_cash < config.trade_size_usd:
            logger.info("Insufficient cash ($%.2f), stopping", available_cash)
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
                return 0

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
        available_cash -= config.trade_size_usd
        traded += 1

        # Save to PostgreSQL
        if _db_available:
            try:
                db.save_position(position)
            except Exception as e:
                logger.warning("DB save_position error: %s", e)

    save_positions(positions)
    logger.info(
        "Traded: %d new positions | Total: %d | Exposure: $%.2f",
        traded,
        len(positions),
        current_exposure,
    )
    return traded


# ── Position Monitor ────────────────────────────────────────────────────────


def check_positions(config: BotConfig) -> None:
    """Проверить статус позиций — resolved маркеты.

    Использует два метода:
    1. Data API — проверяет реальные on-chain позиции (redeemable = resolved)
    2. Дата — если target_date прошла, маркет resolved
    """
    positions = load_positions()
    if not positions:
        return

    import httpx

    updated = False
    now = datetime.now(tz=timezone.utc)

    # Method 1: Check via Data API for real on-chain positions
    wallet = config.funder_address
    live_positions: dict[str, dict] = {}
    if wallet:
        try:
            resp = httpx.get(
                "https://data-api.polymarket.com/positions",
                params={"user": wallet.lower(), "sizeThreshold": "0", "limit": "200"},
                timeout=15,
            )
            for p in resp.json():
                cid = p.get("conditionId", "")
                if cid:
                    live_positions[cid] = p
        except Exception as e:
            logger.warning("Data API positions check failed: %s", e)

    for pos in positions:
        if pos["status"] != "open":
            continue

        resolved = False
        no_won = True

        # Check 1: Data API — position is redeemable (market resolved)
        lp = live_positions.get(pos["market_id"])
        if lp:
            if lp.get("redeemable"):
                resolved = True
                # curPrice=1 means this outcome won
                cur = float(lp.get("curPrice", 0))
                no_won = cur >= 0.99  # NO token worth $1 = we won
                logger.info(
                    "Data API: %s redeemable, curPrice=%.2f, won=%s",
                    pos["question"][:40],
                    cur,
                    no_won,
                )

        # Check 2: Date-based — if target_date has passed by >24h
        if not resolved:
            try:
                target = datetime.strptime(pos["target_date"], "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                hours_past = (now - target).total_seconds() / 3600
                if hours_past > 24:
                    resolved = True
                    # If no Data API info, assume NO won (95%+ historical rate)
                    if not lp:
                        logger.info(
                            "Date-based: %s >24h past, no API data — skipping (will retry)",
                            pos["question"][:40],
                        )
                        resolved = False  # Don't assume — wait for API confirmation
            except (ValueError, KeyError):
                pass

        if resolved:
            pnl = pos["shares"] - pos["size_usd"] if no_won else -pos["size_usd"]
            pos["status"] = "won" if no_won else "lost"
            pos["pnl"] = round(pnl, 2)
            pos["resolved_at"] = now.isoformat()

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

            # Update in PostgreSQL
            if _db_available:
                try:
                    db.resolve_position(pos["market_id"], pos["status"], pos["pnl"])
                except Exception as e:
                    logger.warning("DB resolve_position error: %s", e)

    if updated:
        open_positions = [p for p in positions if p["status"] == "open"]
        save_positions(open_positions)
        logger.info(
            "Positions updated: %d resolved, %d still open",
            sum(1 for p in positions if p["status"] in ("won", "lost")),
            len(open_positions),
        )

    # Summary
    open_pos = [p for p in positions if p["status"] == "open"]
    if open_pos:
        total = sum(p["size_usd"] for p in open_pos)
        logger.info("Open positions: %d | Exposure: $%.2f", len(open_pos), total)


# ── Auto-Redeem ────────────────────────────────────────────────────────────


def _create_redeem_service(config: BotConfig) -> object | None:
    """Создать PolyWeb3Service для auto-redeem. Требует Builder API credentials."""
    builder_key = os.environ.get("BUILDER_KEY", "")
    builder_secret = os.environ.get("BUILDER_SECRET", "")
    builder_passphrase = os.environ.get("BUILDER_PASSPHRASE", "")

    if not all([config.private_key, builder_key, builder_secret, builder_passphrase]):
        return None

    try:
        from poly_web3 import RELAYER_URL, PolyWeb3Service
        from py_builder_relayer_client.client import RelayClient
        from py_builder_signing_sdk.config import BuilderConfig
        from py_builder_signing_sdk.sdk_types import BuilderApiKeyCreds

        funder = config.funder_address or None

        clob = ClobClient(
            host=config.clob_host,
            chain_id=config.chain_id,
            key=config.private_key,
            signature_type=2,
            funder=funder,
        )
        clob.set_api_creds(clob.create_or_derive_api_creds())

        relayer = RelayClient(
            RELAYER_URL,
            config.chain_id,
            config.private_key,
            BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=builder_key,
                    secret=builder_secret,
                    passphrase=builder_passphrase,
                )
            ),
        )

        service = PolyWeb3Service(
            clob_client=clob,
            relayer_client=relayer,
        )
        logger.info("Auto-redeem service initialized")
        return service
    except Exception as e:
        logger.error("Failed to create redeem service: %s", e)
        return None


def auto_redeem(config: BotConfig, redeem_service: object | None = None) -> int:
    """Auto-redeem выигранных позиций. Returns count of redeemed positions."""
    if redeem_service is None:
        return 0

    try:
        results = redeem_service.redeem_all(batch_size=10)
        if not results:
            logger.info("Auto-redeem: no redeemable positions")
            return 0

        redeemed = len([r for r in results if r is not None])
        failed = len([r for r in results if r is None])

        if redeemed > 0:
            logger.info("Auto-redeem: %d positions redeemed successfully", redeemed)
        if failed > 0:
            logger.warning("Auto-redeem: %d positions failed (will retry)", failed)

        return redeemed
    except Exception as e:
        logger.error("Auto-redeem error: %s", e)
        return 0


# ── Web Dashboard ───────────────────────────────────────────────────────────


def _signals_to_dicts(results: list[ScanResult]) -> list[dict]:
    """Convert ScanResult list to JSON-serializable dicts."""
    return [
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


def create_web_app() -> "FastAPI":
    """Create FastAPI dashboard app."""
    import secrets

    from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    from pydantic import BaseModel as PydanticBaseModel
    from pydantic import Field

    app = FastAPI(title="ColdMath Weather Bot")

    # In-memory log buffer for dashboard
    _log_buffer: list[dict] = []
    _LOG_MAX = 200

    # WebSocket broadcast for real-time logs
    _ws_clients: set[WebSocket] = set()
    _ws_loop: asyncio.AbstractEventLoop | None = None

    class DashboardLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            entry = {
                "t": datetime.now(tz=timezone.utc).strftime("%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            }
            _log_buffer.append(entry)
            if len(_log_buffer) > _LOG_MAX:
                del _log_buffer[: len(_log_buffer) - _LOG_MAX]
            # Broadcast to WebSocket clients (fire-and-forget from any thread)
            if _ws_clients and _ws_loop is not None:
                try:
                    _ws_loop.call_soon_threadsafe(
                        asyncio.ensure_future,
                        _broadcast_log(entry),
                    )
                except RuntimeError:
                    pass  # loop closed

    async def _broadcast_log(entry: dict) -> None:
        """Send log entry to all connected WebSocket clients."""
        if not _ws_clients:
            return
        payload = json.dumps(entry)
        closed: list[WebSocket] = []
        for ws in _ws_clients.copy():
            try:
                await ws.send_text(payload)
            except Exception:
                closed.append(ws)
        for ws in closed:
            _ws_clients.discard(ws)

    logging.getLogger("coldmath").addHandler(DashboardLogHandler())

    # File handler with rotation (filter HTTP request noise)
    class _HttpNoiseFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            msg = record.getMessage()
            if "HTTP Request" in msg or "HTTP/1.1" in msg:
                return False
            return True

    _log_file = Path("/app/data/coldmath.log")
    _log_file.parent.mkdir(parents=True, exist_ok=True)
    _file_handler = logging.handlers.RotatingFileHandler(
        _log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    _file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"),
    )
    _file_handler.addFilter(_HttpNoiseFilter())
    logging.getLogger("coldmath").addHandler(_file_handler)
    security = HTTPBasic()

    _api_user = os.environ.get("DASHBOARD_USER", "admin")
    _api_pass = os.environ.get("DASHBOARD_PASS", "coldmath")

    def verify_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
        if not (
            secrets.compare_digest(credentials.username, _api_user)
            and secrets.compare_digest(credentials.password, _api_pass)
        ):
            raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
        return credentials.username

    # State (protected by _state_lock for thread safety)
    _state_lock = threading.Lock()
    _state: dict = {
        "bot_running": False,
        "mode": os.environ.get("BOT_MODE", "paper"),
        "scan_interval_min": int(os.environ.get("SCAN_INTERVAL", "30")),
        "last_scan": None,
        "next_scan_at": 0,
        "signals": [],
        "trader": None,
        "stop_event": None,
        "redeem_service": None,
        "scan_stats": None,
        "proxy_status": None,
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
        trade_size_usd: float | None = Field(None, gt=0, le=1000)
        max_positions: int | None = Field(None, ge=1, le=100)
        max_total_exposure: float | None = Field(None, gt=0, le=10000)
        max_days_ahead: int | None = Field(None, ge=1, le=16)
        min_no_price: float | None = Field(None, ge=0.5, le=0.999)
        min_ensemble_members: int | None = Field(None, ge=3, le=200)
        scan_interval_min: int | None = Field(None, ge=5, le=120)
        mode: str | None = Field(None, pattern="^(scan|paper|live)$")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(
        credentials: HTTPBasicCredentials = Depends(security),
    ):
        if not (
            secrets.compare_digest(credentials.username, _api_user)
            and secrets.compare_digest(credentials.password, _api_pass)
        ):
            raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})
        tpl = Path(__file__).parent / "web" / "templates" / "coldmath.html"
        html = tpl.read_text()
        # Inject credentials for WebSocket auth (same user already authenticated)
        html = html.replace(
            "/*__WS_CREDS__*/",
            f"window._wsUser={json.dumps(credentials.username)};"
            f"window._wsPass={json.dumps(credentials.password)};",
        )
        return HTMLResponse(html)

    @app.get("/api/status")
    async def status(_user: str = Depends(verify_auth)):
        bot_positions = load_positions()
        bot_market_ids = {p["market_id"] for p in bot_positions}
        history = []
        if HISTORY_FILE.exists():
            history = json.loads(HISTORY_FILE.read_text())

        # Build display positions from Data API (all wallet positions)
        wallet = _config.funder_address or ""
        positions = []
        portfolio_value = 0
        if wallet:
            try:
                import httpx

                resp = httpx.get(
                    "https://data-api.polymarket.com/positions",
                    params={
                        "user": wallet.lower(),
                        "sizeThreshold": "0.1",
                        "limit": "200",
                    },
                    timeout=10,
                )
                live_data = resp.json()
                portfolio_value = sum(
                    float(lp.get("currentValue", 0)) for lp in live_data
                )

                from analyzer.weather import parse_weather_question, MONTH_MAP

                for lp in live_data:
                    cid = lp.get("conditionId", "")
                    if not cid or lp.get("redeemable"):
                        continue
                    bot_pos = next(
                        (p for p in bot_positions if p.get("market_id") == cid), None
                    )
                    # Parse city/direction/date from title if no bot data
                    city = ""
                    direction = ""
                    target_date = ""
                    if bot_pos:
                        city = bot_pos.get("city", "")
                        direction = bot_pos.get("direction", "")
                        target_date = bot_pos.get("target_date", "")
                    else:
                        parsed = parse_weather_question(lp.get("title", ""))
                        if parsed:
                            city = parsed.get("city", "")
                            direction = parsed.get("direction", "")
                            ds = parsed.get("date_str", "")
                            if ds:
                                parts = ds.strip().split()
                                if len(parts) >= 2:
                                    mon = MONTH_MAP.get(parts[0].lower())
                                    if mon:
                                        try:
                                            target_date = (
                                                f"2026-{mon:02d}-{int(parts[1]):02d}"
                                            )
                                        except ValueError:
                                            pass

                    positions.append(
                        {
                            "market_id": cid,
                            "question": lp.get("title", ""),
                            "city": city,
                            "direction": direction,
                            "target_date": target_date,
                            "entry_price": lp.get("avgPrice", 0),
                            "cur_price": lp.get("curPrice", 0),
                            "size_usd": lp.get("initialValue", 0),
                            "current_value": lp.get("currentValue", 0),
                            "initial_value": lp.get("initialValue", 0),
                            "cash_pnl": lp.get("cashPnl", 0),
                            "percent_pnl": lp.get("percentPnl", 0),
                            "edge": bot_pos.get("edge", 0) if bot_pos else 0,
                            "source": "coldmath" if cid in bot_market_ids else "other",
                        }
                    )
            except Exception:
                positions = bot_positions

        exposure = sum(p.get("initial_value", p.get("size_usd", 0)) for p in positions)
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
        elif _db_available:
            try:
                from coldmath_db import get_conn

                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT AVG(edge) FROM signals WHERE created_at > NOW() - INTERVAL '24 hours'"
                        )
                        row = cur.fetchone()
                        if row and row[0]:
                            avg_edge = float(row[0])
            except Exception:
                pass

        next_in = ""
        if _state["bot_running"] and _state["next_scan_at"] > 0:
            remaining = max(0, _state["next_scan_at"] - time.time())
            next_in = f"{int(remaining // 60)}m {int(remaining % 60)}s"

        # On-chain USDC balance (cached Web3)
        cash = 0
        if wallet:
            try:
                cash = _get_usdc_balance(wallet)
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
            "portfolio_value": portfolio_value,
            "scan_stats": _state.get("scan_stats"),
            "proxy_status": _state.get("proxy_status"),
            "logs": _log_buffer[-50:],
            "config": {
                "trade_size_usd": _config.trade_size_usd,
                "max_positions": _config.max_positions,
                "max_total_exposure": _config.max_total_exposure,
                "max_days_ahead": _config.max_days_ahead,
                "min_no_price": _config.min_no_price,
                "min_ensemble_members": _config.min_ensemble_members,
                "scan_interval_min": _state["scan_interval_min"],
                "mode": _state["mode"],
                "proxy_url": _config.proxy_url,
            },
        }

    def _run_scan_and_trade() -> int:
        """Shared scan+trade logic. Returns trades_made."""
        scan_start = time.monotonic()
        scan_id = None

        # Start scan in DB
        if _db_available:
            try:
                scan_id = db.start_scan()
            except Exception as e:
                logger.warning("DB start_scan error: %s", e)

        # Quick CLOB reachability check before live trading
        if _state["mode"] == "live":
            import httpx

            try:
                r = httpx.get("https://clob.polymarket.com/time", timeout=10)
                if r.status_code != 200:
                    logger.warning(
                        "CLOB direct check: HTTP %d (will use proxy fallback)",
                        r.status_code,
                    )
            except Exception:
                if not _config.proxy_url:
                    logger.warning(
                        "SKIP scan cycle: CLOB unreachable and no proxy configured"
                    )
                    return 0
                logger.info("CLOB direct unreachable, proxy fallback will be used")

        # Get balance before scan
        balance_before = None
        if _config.funder_address:
            try:
                balance_before = _get_usdc_balance(_config.funder_address)
            except Exception:
                pass

        results, scan_stats = scan_weather_markets(_config)
        with _state_lock:
            _state["signals"] = _signals_to_dicts(results)
            _state["last_scan"] = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
            _state["scan_stats"] = scan_stats

        # Save all signals to DB
        if _db_available and results:
            try:
                signal_dicts = [
                    {
                        "market_id": r.market.condition_id,
                        "question": r.market.question,
                        "city": r.city,
                        "direction": r.direction,
                        "threshold": r.threshold,
                        "target_date": r.target_date,
                        "temp_type": r.temp_type,
                        "model_prob_yes": r.model_prob_yes,
                        "model_prob_no": r.model_prob_no,
                        "market_price_yes": r.market_price_yes,
                        "market_price_no": r.market_price_no,
                        "edge": r.edge,
                        "ensemble_count": r.ensemble_count,
                        "ensemble_temps": r.ensemble_temps,
                        "days_ahead": r.days_ahead,
                        "action": "signal",
                    }
                    for r in results
                ]
                db.save_signals_batch(signal_dicts, scan_id=scan_id)
            except Exception as e:
                logger.warning("DB save_signals error: %s", e)

        trades_made = 0
        if _state["mode"] != "scan":
            is_paper = _state["mode"] == "paper"
            trader = None
            if not is_paper and _config.private_key:
                with _state_lock:
                    if not _state["trader"]:
                        _state["trader"] = ClobTrader(_config)
                    trader = _state["trader"]
            trades_made = execute_trades(
                results, _config, trader=trader, paper=is_paper
            )

        check_positions(_config)

        # Finish scan in DB
        if _db_available and scan_id:
            try:
                balance_after = None
                if _config.funder_address:
                    try:
                        balance_after = _get_usdc_balance(_config.funder_address)
                    except Exception:
                        pass
                db.finish_scan(
                    scan_id,
                    weather_markets=scan_stats.get("weather_markets", 0),
                    forecasts_ok=scan_stats.get("forecasts_ok", 0),
                    forecasts_failed=scan_stats.get("forecasts_failed", 0),
                    signals_found=len(results),
                    trades_made=trades_made,
                    balance_before=balance_before,
                    balance_after=balance_after,
                    status=scan_stats.get("status", "ok"),
                    duration_sec=round(time.monotonic() - scan_start, 1),
                )
            except Exception as e:
                logger.warning("DB finish_scan error: %s", e)

        # Auto-redeem resolved positions
        if _state["mode"] == "live":
            with _state_lock:
                if not _state["redeem_service"]:
                    _state["redeem_service"] = _create_redeem_service(_config)
            auto_redeem(_config, _state["redeem_service"])

        return trades_made

    @app.get("/api/logs")
    async def api_logs(_user: str = Depends(verify_auth)):
        return {"logs": _log_buffer[-50:]}

    @app.websocket("/ws/logs")
    async def ws_logs(ws: WebSocket):
        """WebSocket endpoint for real-time log streaming.

        Auth via query params: ?user=X&pass=Y
        """
        user = ws.query_params.get("user", "")
        passwd = ws.query_params.get("pass", "")
        if not (
            secrets.compare_digest(user, _api_user)
            and secrets.compare_digest(passwd, _api_pass)
        ):
            await ws.accept()
            await ws.close(code=4001, reason="Unauthorized")
            return

        await ws.accept()
        _ws_clients.add(ws)
        try:
            # Send recent logs on connect
            for entry in _log_buffer[-50:]:
                await ws.send_text(json.dumps(entry))
            # Keep connection alive, wait for client disconnect
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            _ws_clients.discard(ws)

    @app.post("/api/redeem")
    async def api_redeem(_user: str = Depends(verify_auth)):
        def _do_redeem() -> int:
            with _state_lock:
                if not _state["redeem_service"]:
                    _state["redeem_service"] = _create_redeem_service(_config)
            return auto_redeem(_config, _state["redeem_service"])

        redeemed = await asyncio.to_thread(_do_redeem)
        return {"redeemed": redeemed}

    @app.post("/api/scan")
    async def api_scan(_user: str = Depends(verify_auth)):
        trades_made = await asyncio.to_thread(_run_scan_and_trade)
        return {"signals_count": len(_state["signals"]), "trades_made": trades_made}

    def _start_bot_loop(source: str = "manual") -> bool:
        """Start the bot loop thread. Returns True if started."""
        if _state["bot_running"]:
            return False

        stop_evt = threading.Event()
        with _state_lock:
            _state["stop_event"] = stop_evt
            _state["bot_running"] = True

        def _seconds_until_next_slot(interval_min: int) -> float:
            """Calculate seconds until next fixed time slot (e.g. :00, :30)."""
            now = datetime.now(tz=timezone.utc)
            minute = now.minute
            second = now.second + now.microsecond / 1e6
            slots = list(range(0, 60, interval_min))  # e.g. [0, 30]
            for slot in slots:
                if minute < slot:
                    return (slot - minute) * 60 - second
            # Next slot is in the next hour
            return (60 - minute + slots[0]) * 60 - second

        def bot_loop():
            interval = _state["scan_interval_min"]
            wait_sec = _seconds_until_next_slot(interval)
            logger.info(
                "Bot started (%s, mode=%s, interval=%dm, first scan in %dm %ds)",
                source,
                _state["mode"],
                interval,
                int(wait_sec // 60),
                int(wait_sec % 60),
            )

            # Wait until next fixed slot before first scan
            with _state_lock:
                _state["next_scan_at"] = time.time() + wait_sec
            if stop_evt.wait(wait_sec):
                return

            while not stop_evt.is_set():
                try:
                    _run_scan_and_trade()
                except Exception as e:
                    logger.error("Bot loop error: %s", e)

                wait_sec = _seconds_until_next_slot(interval)
                with _state_lock:
                    _state["next_scan_at"] = time.time() + wait_sec
                stop_evt.wait(wait_sec)

            with _state_lock:
                _state["bot_running"] = False
            logger.info("Bot stopped")

        threading.Thread(target=bot_loop, daemon=True).start()
        return True

    @app.post("/api/start")
    async def api_start(_user: str = Depends(verify_auth)):
        if _start_bot_loop("api"):
            return {"status": "started"}
        return {"status": "already running"}

    @app.post("/api/stop")
    async def api_stop(_user: str = Depends(verify_auth)):
        with _state_lock:
            if _state["stop_event"]:
                _state["stop_event"].set()
                _state["bot_running"] = False
        return {"status": "stopped"}

    @app.post("/api/settings")
    async def api_settings(body: SettingsBody, _user: str = Depends(verify_auth)):
        with _state_lock:
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

    class ProxySetBody(PydanticBaseModel):
        proxy_url: str = Field(..., max_length=500)

    @app.post("/api/proxy-set")
    async def api_proxy_set(body: ProxySetBody, _user: str = Depends(verify_auth)):
        new_url = body.proxy_url.strip()

        def _do_set() -> dict:
            # Step 1: Validate new proxy
            ps = check_proxy(new_url) if new_url else None

            if new_url and (not ps or not ps.ok):
                error = ps.error if ps else "Empty URL"
                logger.warning("Proxy set REJECTED: %s", error)
                return {
                    "status": "error",
                    "error": error,
                    "proxy_status": _proxy_status_dict(ps) if ps else None,
                }

            if new_url and not ps.can_trade:
                logger.warning(
                    "Proxy set WARNING: %s — cannot trade (%s)", ps.ip, ps.error
                )

            # Step 2: Update config
            old_url = _config.proxy_url
            _config.proxy_url = new_url

            # Step 3: Re-apply proxy patch for CLOB client
            if new_url:
                from trader.proxy_patch import apply_proxy

                apply_proxy(new_url)

            # Step 4: Invalidate existing trader (will be re-created on next trade)
            with _state_lock:
                _state["trader"] = None
                _state["proxy_status"] = _proxy_status_dict(ps) if ps else None

            if new_url:
                logger.info(
                    "Proxy changed: %s (%s, %s) %dms | can_trade=%s",
                    ps.ip,
                    ps.country,
                    ps.city,
                    ps.latency_ms,
                    ps.can_trade,
                )
            else:
                logger.info("Proxy removed (direct connection)")

            return {
                "status": "ok",
                "old_proxy": old_url.split("@")[-1] if old_url else "",
                "proxy_status": _proxy_status_dict(ps) if ps else None,
            }

        return await asyncio.to_thread(_do_set)

    @app.post("/api/proxy-check")
    async def api_proxy_check(_user: str = Depends(verify_auth)):
        def _do_check() -> dict:
            import httpx

            # 1. Check direct connection (no proxy) — server's own IP
            direct: dict = {
                "ok": False,
                "ip": "",
                "country": "",
                "clob_reachable": False,
                "latency_ms": 0,
                "error": "",
            }
            try:
                start = time.monotonic()
                dr = httpx.get("https://ipinfo.io/json", timeout=10)
                direct["latency_ms"] = int((time.monotonic() - start) * 1000)
                if dr.status_code == 200:
                    dd = dr.json()
                    direct["ok"] = True
                    direct["ip"] = dd.get("ip", "")
                    direct["country"] = dd.get("country", "")
                    # Check CLOB direct
                    try:
                        cr = httpx.get("https://clob.polymarket.com/time", timeout=10)
                        direct["clob_reachable"] = cr.status_code == 200
                    except Exception:
                        pass
            except Exception as e:
                direct["error"] = str(e)

            # 2. Check proxy (full)
            proxy_result = None
            if _config.proxy_url:
                ps = check_proxy(_config.proxy_url, full=True)
                proxy_result = _proxy_status_dict(ps)
                with _state_lock:
                    _state["proxy_status"] = proxy_result
                if ps.ok and ps.can_trade:
                    logger.info(
                        "Proxy OK: %s (%s, %s) %dms | CLOB: OK",
                        ps.ip,
                        ps.country,
                        ps.city,
                        ps.latency_ms,
                    )
                elif ps.ok:
                    logger.warning(
                        "Proxy UP but CANNOT trade: %s (%s) | %s",
                        ps.ip,
                        ps.country,
                        ps.error,
                    )
                else:
                    logger.warning("Proxy FAILED: %s", ps.error)

            logger.info(
                "Direct: %s (%s) CLOB=%s %dms",
                direct["ip"],
                direct["country"],
                "OK" if direct["clob_reachable"] else "FAIL",
                direct["latency_ms"],
            )

            return {
                "direct": direct,
                "proxy": proxy_result,
                **(proxy_result or {}),
            }

        return await asyncio.to_thread(_do_check)

    @app.get("/api/analytics")
    async def api_analytics(_user: str = Depends(verify_auth)):
        if not _db_available:
            return {"error": "PostgreSQL not available"}

        def _do():
            try:
                return db.get_analytics()
            except Exception as e:
                logger.error("Analytics error: %s", e)
                return {"error": str(e)}

        return await asyncio.to_thread(_do)

    @app.post("/api/db/migrate")
    async def api_db_migrate(_user: str = Depends(verify_auth)):
        if not _db_available:
            return {"error": "PostgreSQL not available"}

        def _do():
            return db.migrate_from_json(str(POSITIONS_FILE), str(HISTORY_FILE))

        result = await asyncio.to_thread(_do)
        return {"status": "ok", "imported": result}

    @app.on_event("startup")
    async def _autostart_bot():
        nonlocal _ws_loop
        _ws_loop = asyncio.get_running_loop()

        # Initialize PostgreSQL
        if _db_available:
            try:
                db.init_db()
                logger.info("PostgreSQL connected and initialized")
                # Auto-migrate existing JSON data on first run
                await asyncio.to_thread(
                    db.migrate_from_json, str(POSITIONS_FILE), str(HISTORY_FILE)
                )
            except Exception as e:
                logger.warning("PostgreSQL unavailable, using JSON only: %s", e)

        # Check proxy on startup
        if _config.proxy_url:

            def _initial_proxy_check():
                ps = check_proxy(_config.proxy_url)
                with _state_lock:
                    _state["proxy_status"] = _proxy_status_dict(ps)
                if ps.ok and ps.can_trade:
                    logger.info(
                        "Startup proxy: %s (%s, %s) %dms | CLOB: OK",
                        ps.ip,
                        ps.country,
                        ps.city,
                        ps.latency_ms,
                    )
                elif ps.ok:
                    logger.warning(
                        "Startup proxy UP but CANNOT trade: %s (%s) | %s",
                        ps.ip,
                        ps.country,
                        ps.error,
                    )
                else:
                    logger.warning("Startup proxy FAILED: %s", ps.error)

            await asyncio.to_thread(_initial_proxy_check)

        if _state["mode"] in ("paper", "live"):
            await asyncio.sleep(2)
            _start_bot_loop("autostart")

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
        results, _stats = scan_weather_markets(config)
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
