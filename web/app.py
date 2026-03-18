"""Web dashboard для Polymarket Bot с WebSocket real-time обновлениями."""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

import requests
import uvicorn
from claude_auth import claude_auth_router
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from trader.storage import PortfolioStorage

logger = logging.getLogger(__name__)

# --- Polymarket Data API: real positions cache ---
_positions_cache: list[dict] = []
_positions_cache_ts: float = 0.0
_POSITIONS_CACHE_TTL: int = 30  # seconds

# --- SL/TP tracking: avoid duplicate sell orders (PERSISTENT) ---
_SL_TP_FILE = Path("data") / "sl_tp_triggered.json"
_SL_MAX_RETRIES = 3
_GAVE_UP_RETRY_INTERVAL = 600  # retry gave-up positions after 10 min


def _load_sl_tp_triggered() -> set[str]:
    """Load SL/TP triggered set from disk (survives container restarts)."""
    if _SL_TP_FILE.exists():
        try:
            data = json.loads(_SL_TP_FILE.read_text())
            return set(data.get("triggered", []))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _save_sl_tp_triggered(triggered: set[str]) -> None:
    """Save SL/TP triggered set to disk."""
    try:
        Path("data").mkdir(exist_ok=True)
        _SL_TP_FILE.write_text(json.dumps({"triggered": list(triggered)}, indent=2))
    except OSError as e:
        logging.getLogger(__name__).warning("Failed to save SL/TP state: %s", e)


_sl_tp_triggered: set[str] = _load_sl_tp_triggered()
_sl_tp_retries: dict[str, int] = {}  # token_id -> retry count
_sl_tp_gave_up_ts: dict[str, float] = {}  # token_id -> when gave up
_monitor_running: bool = False  # prevent concurrent monitor checks


class SellRequest(BaseModel):
    """Request body для продажи позиции."""

    token_id: str
    price: float
    size: float


class CancelOrderRequest(BaseModel):
    """Request body для отмены ордера."""

    order_id: str


def _fetch_live_positions() -> list[dict]:
    """Получить реальные позиции из Polymarket Data API.

    Data API (data-api.polymarket.com) не требует прокси.
    Результат кэшируется на 30 секунд.
    """
    global _positions_cache, _positions_cache_ts

    now = time.monotonic()
    if _positions_cache and (now - _positions_cache_ts) < _POSITIONS_CACHE_TTL:
        return _positions_cache

    from config import settings as _s

    wallet = _s.polygon_wallet_address
    if not wallet:
        return []

    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={"user": wallet.lower()},
            timeout=15,
        )
        resp.raise_for_status()
        raw_positions = resp.json()
    except requests.RequestException as e:
        logger.error("Data API positions error: %s", e)
        return _positions_cache  # return stale cache on error

    positions: list[dict] = []
    for p in raw_positions:
        size_val = float(p.get("size", 0))
        if size_val <= 0:
            continue

        avg_price = float(p.get("avgPrice", 0))
        cur_price = float(p.get("curPrice", 0))
        initial_value = float(p.get("initialValue", 0))
        current_value = float(p.get("currentValue", 0))
        cash_pnl = float(p.get("cashPnl", 0))
        percent_pnl = float(p.get("percentPnl", 0))

        outcome = p.get("outcome", "Yes")
        side = "BUY_YES" if outcome == "Yes" else "BUY_NO"

        positions.append(
            {
                "question": p.get("title", "Unknown"),
                "side": side,
                "entry": avg_price,
                "current": cur_price,
                "size": initial_value,
                "current_value": current_value,
                "pnl": cash_pnl,
                "pnl_pct": f"{percent_pnl:+.1f}%",
                "pnl_pct_raw": percent_pnl / 100.0,
                "market_id": p.get("conditionId", ""),
                "token_id": p.get("asset", ""),
                "shares": size_val,
                "slug": p.get("eventSlug", ""),
                "end_date": p.get("endDate", ""),
                "icon": p.get("icon", ""),
                "outcome": outcome,
                "redeemable": p.get("redeemable", False),
                "resolved": bool(p.get("redeemable", False))
                or (cur_price == 0.0 and current_value == 0.0 and size_val > 0),
                "event_id": p.get("eventId", ""),
            }
        )

    _positions_cache = positions
    _positions_cache_ts = now
    logger.info("Fetched %d live positions from Data API", len(positions))
    return positions


def _live_portfolio(balance: float) -> dict:
    """Сформировать portfolio summary для live режима."""
    all_positions = _fetch_live_positions()

    # Separate open vs resolved positions
    open_positions = [p for p in all_positions if not p.get("resolved")]
    resolved_positions = [p for p in all_positions if p.get("resolved")]

    # All positions shown in UI — no filtering
    active_positions = all_positions

    # Open positions: unrealized PnL
    invested = sum(p["size"] for p in open_positions)
    unrealized_pnl = sum(p["pnl"] for p in open_positions)

    # Resolved positions: realized PnL (won or lost)
    resolved_pnl = sum(p["pnl"] for p in resolved_positions)
    resolved_claimable = sum(p["current_value"] for p in resolved_positions)

    # Trade history: realized PnL from SL/TP sells
    from trader.live_history import get_live_history

    trade_history = get_live_history().history
    closes = [h for h in trade_history if h["action"] == "CLOSE"]
    history_pnl = sum(h.get("pnl", 0) for h in closes)
    history_wins = sum(1 for h in closes if h.get("pnl", 0) > 0)

    # Combined realized PnL and stats
    resolved_win_count = sum(1 for p in resolved_positions if p["pnl"] > 0)
    total_closed = len(resolved_positions) + len(closes)
    total_wins = resolved_win_count + history_wins
    realized_pnl = resolved_pnl + history_pnl

    # Total trades = current positions + fully closed (no longer in Data API)
    open_token_ids = {p.get("token_id", "") for p in all_positions}
    fully_closed = sum(1 for h in closes if h.get("token_id", "") not in open_token_ids)
    total_trades = len(all_positions) + fully_closed

    # Total equity = free balance + open positions at market value + claimable from resolved
    total_equity = balance + invested + unrealized_pnl + resolved_claimable
    total_initial_investment = sum(p["size"] for p in all_positions)
    roi_pct = (
        (
            (total_equity - balance - total_initial_investment)
            / max(total_initial_investment, 0.01)
            * 100
        )
        if total_initial_investment > 0
        else 0.0
    )
    exposure_pct = round(invested / total_equity * 100, 1) if total_equity > 0 else 0.0

    return {
        "balance_usd": balance,
        "invested_usd": round(invested, 4),
        "total_equity": round(total_equity, 4),
        "roi_pct": round(roi_pct, 2),
        "open_positions": len(open_positions),
        "total_trades": total_trades,
        "closed_trades": total_closed,
        "win_count": total_wins,
        "win_rate": round(total_wins / total_closed * 100, 1) if total_closed else 0,
        "unrealized_pnl": round(unrealized_pnl, 4),
        "realized_pnl": round(realized_pnl, 4),
        "avg_edge": 0,
        "exposure_pct": exposure_pct,
        "free_slots": max(0, 35 - len(open_positions)),
        "max_positions": 35,
        "positions": active_positions,
        "mode": "live",
    }


_balance_cache: float = 0.0
_balance_cache_ts: float = 0.0


def _fetch_usdc_balance() -> float:
    """Получить USDC баланс через Data API (без прокси, без CLOB).

    Fallback: если Data API не отдаёт баланс, пробуем CLOB API.
    Кэш 60 секунд.
    """
    global _balance_cache, _balance_cache_ts

    now = time.monotonic()
    if _balance_cache > 0 and (now - _balance_cache_ts) < 60:
        return _balance_cache

    from config import settings as _s

    wallet = _s.polygon_wallet_address
    if not wallet:
        return 0.0

    # 1. Попробовать Polygon RPC — прочитать USDC баланс напрямую
    try:
        usdc_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) selector = 0x70a08231
        data = "0x70a08231" + wallet.lower().replace("0x", "").zfill(64)
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_contract, "data": data}, "latest"],
            "id": 1,
        }
        resp = requests.post(
            "https://polygon-bor-rpc.publicnode.com",
            json=payload,
            timeout=10,
        )
        result = resp.json().get("result", "0x0")
        balance = int(result, 16) / 1e6
        if balance > 0:
            _balance_cache = balance
            _balance_cache_ts = now
            return balance
    except Exception as e:
        logger.warning("RPC balance check failed: %s", e)

    # 2. Fallback: CLOB API (через прокси)
    try:
        from trader.live_executor import get_live_executor

        balance = get_live_executor().get_balance()
        if balance > 0:
            _balance_cache = balance
            _balance_cache_ts = now
            return balance
    except Exception as e:
        logger.warning("CLOB balance check failed: %s", e)

    # 3. Return stale cache
    return _balance_cache


def _get_portfolio_summary():
    """Get portfolio summary - live or paper depending on mode."""
    from config import settings as _s

    if not _s.paper_trading:
        try:
            balance = _fetch_usdc_balance()
            return _live_portfolio(balance)
        except Exception:
            pass
    storage = PortfolioStorage()
    return storage.get_summary()


app = FastAPI(title="Polymarket Bot Dashboard")
app.include_router(claude_auth_router)

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

RESULTS_DIR = Path("results")


# --- Auth middleware ---
class APIKeyMiddleware(BaseHTTPMiddleware):
    """API key auth via cookie, query param, or Authorization header."""

    EXEMPT_PATHS = {
        "/api/portfolio",
        "/api/sse/status",
        "/api/scheduler/status",
        "/docs",
        "/openapi.json",
    }

    async def dispatch(self, request: Request, call_next):
        from config import settings as _s

        api_key = _s.bot_api_key
        if not api_key:
            return await call_next(request)

        path = request.url.path

        # Healthcheck exempt (docker healthcheck from localhost)
        if path in self.EXEMPT_PATHS:
            return await call_next(request)

        # Static files exempt
        if path.startswith("/static/"):
            return await call_next(request)

        # WebSocket — check query param
        if path == "/ws":
            if request.query_params.get("key") == api_key:
                return await call_next(request)
            if request.cookies.get("bot_key") == api_key:
                return await call_next(request)
            return Response("Unauthorized", status_code=401)

        # Check cookie
        if request.cookies.get("bot_key") == api_key:
            return await call_next(request)

        # Check query param — set cookie for future requests
        if request.query_params.get("key") == api_key:
            response = await call_next(request)
            response.set_cookie(
                "bot_key", api_key, httponly=True, max_age=86400 * 30, samesite="lax"
            )
            return response

        # Check Authorization header
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {api_key}":
            return await call_next(request)

        return Response("Unauthorized", status_code=401)


app.add_middleware(APIKeyMiddleware)


# WebSocket connections pool
ws_clients: set[WebSocket] = set()
# Shared state for background task status
bot_status: dict = {"state": "idle", "message": "", "last_run": ""}


async def broadcast(event: str, data: dict | str = "") -> None:
    """Отправить событие всем подключённым клиентам."""
    msg = json.dumps({"event": event, "data": data, "ts": datetime.now().isoformat()})
    dead: set[WebSocket] = set()
    for client in ws_clients:
        try:
            await client.send_text(msg)
        except Exception:
            dead.add(client)
    ws_clients.difference_update(dead)


_main_loop: asyncio.AbstractEventLoop | None = None


_sse_task: asyncio.Task | None = None
_sse_listener = None


def _on_breaking_match(article: dict, matches: list) -> None:
    """Callback when SSE listener finds breaking news matching a market."""
    from analyzer.claude import ClaudeAnalyzer
    from trader.risk import RiskManager
    from trader.storage import PortfolioStorage as _Storage

    analyzer = ClaudeAnalyzer()
    risk_mgr = RiskManager()
    storage = _Storage()

    for market, relevance in matches[:3]:  # Analyze top 3 matches
        sync_broadcast(
            "log",
            f"[BREAKING] Re-analyzing: {market.question[:60]} (relevance: {relevance:.2f})",
        )

        prediction = analyzer.rapid_reanalyze(market, article)
        if not prediction:
            continue

        if prediction.recommended_side == "SKIP":
            sync_broadcast(
                "log", f"[BREAKING] SKIP: edge {prediction.edge:+.0%} too small"
            )
            continue

        signal = risk_mgr.evaluate_signal(prediction, storage.balance)
        if signal:
            from polymarket.models import Position

            position = Position(
                market_id=market.id,
                token_id=market.clob_token_ids[0] if market.clob_token_ids else "",
                question=market.question,
                entry_price=signal.price,
                size_usd=signal.size_usd,
                current_price=signal.price,
                side="BUY",
                end_date=market.end_date,
                slug=market.slug,
                edge=prediction.edge,
                confidence=prediction.confidence,
                ai_probability=prediction.ai_probability,
                reasoning=prediction.reasoning,
                volume=market.volume,
                liquidity=market.liquidity,
            )
            new_balance = storage.balance - signal.size_usd
            storage.add_position(position, new_balance)
            sync_broadcast("portfolio", storage.get_summary())
            sync_broadcast(
                "log",
                f"[BREAKING] OPEN: {signal.prediction.recommended_side} "
                f"{market.question[:50]} @ {signal.price:.4f} (${signal.size_usd})",
            )
        else:
            sync_broadcast(
                "log", f"[BREAKING] Risk check failed for {market.question[:50]}"
            )

    analyzer.close()


@app.on_event("startup")
async def _capture_loop() -> None:
    global _main_loop, _monitor_task, _trading_task, _sse_task, _sse_listener
    _main_loop = asyncio.get_running_loop()
    # Auto-start scheduler for autonomous operation
    _monitor_task = asyncio.create_task(_price_monitor_loop(180))
    _trading_task = asyncio.create_task(_trading_loop(15))

    # Start SSE listener for breaking news
    from config import settings

    if settings.sse_enabled:
        from services.sse_listener import SSEListener

        _sse_listener = SSEListener(
            on_breaking_match=_on_breaking_match,
            on_log=_broadcast_log,
        )
        _sse_task = asyncio.create_task(_sse_listener.start())
        logger.info("SSE listener started for breaking news")

    logger.info(
        "Auto-scheduler started: price monitor (3 min) + trading scanner (15 min) + SSE listener"
    )


def sync_broadcast(event: str, data: dict | str = "") -> None:
    """Синхронная обёртка для broadcast (из background tasks / thread pool)."""
    if _main_loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast(event, data), _main_loop)
    except RuntimeError:
        pass


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    # Auth check (BaseHTTPMiddleware doesn't handle WebSocket)
    from config import settings as _s

    api_key = _s.bot_api_key
    if api_key:
        cookie_key = ws.cookies.get("bot_key", "")
        query_key = ws.query_params.get("key", "")
        if cookie_key != api_key and query_key != api_key:
            await ws.close(code=1008, reason="Unauthorized")
            return

    await ws.accept()
    ws_clients.add(ws)
    logger.info("WS connected (%d clients)", len(ws_clients))
    try:
        # Сразу отправляем текущее состояние
        await ws.send_text(
            json.dumps(
                {
                    "event": "portfolio",
                    "data": _get_portfolio_summary(),
                    "ts": datetime.now().isoformat(),
                }
            )
        )
        await ws.send_text(
            json.dumps(
                {
                    "event": "status",
                    "data": bot_status,
                    "ts": datetime.now().isoformat(),
                }
            )
        )
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        pass
    finally:
        ws_clients.discard(ws)
        logger.info("WS disconnected (%d clients)", len(ws_clients))


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    summary = _get_portfolio_summary()
    if summary.get("mode") == "live":
        from trader.live_history import get_live_history

        history = list(reversed(get_live_history().history))
    else:
        storage = PortfolioStorage()
        history = list(reversed(storage.history))
    analyses = _load_latest_analyses()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "summary": summary,
            "history": history,
            "analyses": analyses,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "bot_status": bot_status,
        },
    )


@app.get("/api/settings")
async def api_get_settings() -> JSONResponse:
    from config import settings

    return JSONResponse(
        {
            "min_hours_to_resolution": settings.min_hours_to_resolution,
            "max_hours_to_resolution": settings.max_hours_to_resolution,
            "min_liquidity_usd": settings.min_liquidity_usd,
            "min_edge_threshold": settings.min_edge_threshold * 100,
            "max_edge_threshold": settings.max_edge_threshold * 100,
            "min_confidence": settings.min_confidence * 100,
            "max_position_pct": settings.max_position_pct * 100,
            "max_total_exposure_pct": settings.max_total_exposure_pct * 100,
            "max_concurrent_positions": settings.max_concurrent_positions,
            "default_trade_size_usd": settings.default_trade_size_usd,
            "stop_loss_pct": settings.stop_loss_pct * 100,
            "take_profit_pct": settings.take_profit_pct * 100,
            "weather_stop_loss_pct": settings.weather_stop_loss_pct * 100,
            "weather_take_profit_pct": settings.weather_take_profit_pct * 100,
        }
    )


@app.post("/api/settings")
async def api_update_settings(request: Request) -> JSONResponse:
    from config import settings

    data = await request.json()
    mapping = {
        "min_hours_to_resolution": ("min_hours_to_resolution", 1.0),
        "max_hours_to_resolution": ("max_hours_to_resolution", 1.0),
        "min_liquidity_usd": ("min_liquidity_usd", 1.0),
        "min_edge_threshold": ("min_edge_threshold", 0.01),
        "max_edge_threshold": ("max_edge_threshold", 0.01),
        "min_confidence": ("min_confidence", 0.01),
        "max_position_pct": ("max_position_pct", 0.01),
        "max_total_exposure_pct": ("max_total_exposure_pct", 0.01),
        "max_concurrent_positions": ("max_concurrent_positions", 1.0),
        "default_trade_size_usd": ("default_trade_size_usd", 1.0),
        "stop_loss_pct": ("stop_loss_pct", 0.01),
        "take_profit_pct": ("take_profit_pct", 0.01),
        "weather_stop_loss_pct": ("weather_stop_loss_pct", 0.01),
        "weather_take_profit_pct": ("weather_take_profit_pct", 0.01),
    }
    updated = []
    for key, value in data.items():
        if key in mapping:
            attr, mult = mapping[key]
            setattr(settings, attr, float(value) * mult)
            updated.append(key)
    logger.info("Settings updated: %s", updated)
    sync_broadcast("log", f"Settings updated: {', '.join(updated)}")
    return JSONResponse({"status": "ok", "updated": updated})


@app.get("/api/analytics")
async def api_analytics() -> JSONResponse:
    """Аналитика: backtest results + signals stats."""
    backtest_path = Path("data") / "backtest_results.json"
    backtest: dict = {}
    if backtest_path.exists():
        try:
            backtest = json.loads(backtest_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    from trader.signals_history import signals_history

    signals_stats = signals_history.get_stats()

    return JSONResponse(
        {
            "backtest": backtest.get("summary", {}),
            "strategy_always_no": backtest.get("strategy_always_no", {}),
            "strategy_exact_no": backtest.get("strategy_exact_no", {}),
            "direction_stats": backtest.get("direction_stats", {}),
            "signals": signals_stats,
        }
    )


@app.get("/api/scans")
async def api_scans() -> JSONResponse:
    from trader.scan_log import scan_logger

    return JSONResponse(scan_logger.get_scans(limit=30))


@app.get("/api/signals")
async def api_signals(type: str | None = None, limit: int = 200) -> JSONResponse:
    from trader.signals_history import signals_history

    return JSONResponse(signals_history.get_signals(signal_type=type, limit=limit))


@app.get("/api/signals/stats")
async def api_signals_stats() -> JSONResponse:
    from trader.signals_history import signals_history

    return JSONResponse(signals_history.get_stats())


@app.get("/scans", response_class=HTMLResponse)
async def scans_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("scans.html", {"request": request})


@app.get("/signals", response_class=HTMLResponse)
async def signals_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("signals.html", {"request": request})


@app.get("/api/portfolio")
async def api_portfolio() -> JSONResponse:
    return JSONResponse(_get_portfolio_summary())


@app.get("/api/history")
async def api_history() -> JSONResponse:
    from config import settings as _s

    if not _s.paper_trading:
        from trader.live_history import get_live_history

        return JSONResponse(list(reversed(get_live_history().history)))
    storage = PortfolioStorage()
    return JSONResponse(list(reversed(storage.history)))


@app.post("/api/monitor")
async def api_monitor(background_tasks: BackgroundTasks) -> JSONResponse:
    background_tasks.add_task(_monitor_bg)
    return JSONResponse({"status": "started"})


@app.post("/api/run-paper")
async def api_run_paper(background_tasks: BackgroundTasks) -> JSONResponse:
    if bot_status["state"] == "running":
        return JSONResponse({"status": "already running"}, status_code=409)
    background_tasks.add_task(_run_paper_bg)
    return JSONResponse({"status": "started"})


@app.post("/api/run-analysis")
async def api_run_analysis(background_tasks: BackgroundTasks) -> JSONResponse:
    if bot_status["state"] == "running":
        return JSONResponse({"status": "already running"}, status_code=409)
    background_tasks.add_task(_run_analysis_bg)
    return JSONResponse({"status": "started"})


def _broadcast_log(msg: str) -> None:
    """Callback для трансляции логов paper trading в веб."""
    sync_broadcast("log", msg)


def _set_status(state: str, message: str = "") -> None:
    bot_status["state"] = state
    bot_status["message"] = message
    if state == "idle":
        bot_status["last_run"] = datetime.now().isoformat()
    sync_broadcast("status", bot_status)


def _monitor_bg() -> None:
    from config import settings as _s

    _set_status("monitoring", "Updating prices...")
    try:
        if not _s.paper_trading:
            _live_monitor_check()
        else:
            from trader.monitor import update_positions

            storage = PortfolioStorage()
            update_positions(storage)
            sync_broadcast("history", list(reversed(storage.history)))
        sync_broadcast("portfolio", _get_portfolio_summary())
        _set_status("idle", "Prices updated")
    except Exception as e:
        logger.error("Monitor error: %s", e)
        _set_status("idle", f"Error: {e}")


def _run_paper_bg() -> None:
    from config import settings

    if settings.paper_trading:
        from main import run_paper_trading

        _set_status("running", "Paper trading in progress...")
        run_paper_trading(max_markets=500, on_log=_broadcast_log)
    else:
        from main import run_live_trading

        _set_status("running", "Live trading in progress...")
        run_live_trading(max_markets=500)

    try:
        sync_broadcast("portfolio", _get_portfolio_summary())
        storage = PortfolioStorage()
        sync_broadcast("history", list(reversed(storage.history)))
        _set_status("idle", "Paper trading completed")
    except Exception as e:
        logger.exception("Paper trading error: %s", e)
        _set_status("idle", f"Error: {e}")


def _run_paper_bg_fast() -> None:
    """Быстрый режим для auto-scheduler: больше рынков, без thinking."""
    from config import settings

    _set_status("running", "Auto trading (500 markets)...")
    try:
        if settings.paper_trading:
            from main import run_paper_trading

            run_paper_trading(max_markets=500, on_log=_broadcast_log)
        else:
            from main import run_live_trading

            run_live_trading(max_markets=500)
        sync_broadcast("portfolio", _get_portfolio_summary())
        storage = PortfolioStorage()
        sync_broadcast("history", list(reversed(storage.history)))
        _set_status("idle", "Auto trading completed")
    except Exception as e:
        logger.exception("Auto trading error: %s", e)
        _set_status("idle", f"Error: {e}")


def _run_analysis_bg() -> None:
    from main import run_analysis

    _set_status("running", "Analysis in progress...")
    try:
        run_analysis(max_markets=50)
        sync_broadcast("analyses", _load_latest_analyses())
        _set_status("idle", "Analysis completed")
    except Exception as e:
        logger.error("Analysis error: %s", e)
        _set_status("idle", f"Error: {e}")


def _record_live_trade(
    pos: dict, sell_price: float, pnl_pct: float, reason: str
) -> None:
    """Record a live trade (SL/TP sell) to persistent history."""
    try:
        from trader.live_history import get_live_history

        history = get_live_history()
        entry_price = pos.get("entry", 0.0)
        size_usd = pos.get("size", 0.0)

        # Fallback: if Data API returns avgPrice=0, look up entry from OPEN records
        if entry_price <= 0:
            token_id = pos.get("token_id", "")
            market_id = pos.get("market_id", "")
            # First try exact token_id match (avoids YES/NO confusion on same market)
            for h in reversed(history.history):
                if h.get("action") != "OPEN":
                    continue
                if token_id and h.get("token_id") == token_id:
                    entry_price = h.get("entry_price", 0.0)
                    if entry_price > 0:
                        break
            # Fallback to market_id only if token_id didn't match
            if entry_price <= 0:
                for h in reversed(history.history):
                    if h.get("action") != "OPEN":
                        continue
                    if market_id and h.get("market_id") == market_id:
                        entry_price = h.get("entry_price", 0.0)
                        if entry_price > 0:
                            break

        # PnL based on actual exit price, not current market pnl_pct
        if entry_price > 0:
            pnl = (sell_price - entry_price) / entry_price * size_usd
        else:
            pnl = 0.0
        history.record_close(
            question=pos.get("question", ""),
            side=pos.get("side", ""),
            entry_price=entry_price,
            exit_price=sell_price,
            size_usd=size_usd,
            shares=pos.get("shares", 0.0),
            pnl=pnl,
            reason=reason,
            token_id=pos.get("token_id", ""),
            market_id=pos.get("market_id", ""),
        )
        sync_broadcast("history", list(reversed(history.history)))
    except Exception as e:
        logger.warning("Failed to record live trade: %s", e)


def _cancel_all_open_orders(executor: "LiveExecutor") -> int:
    """Cancel ALL open orders to free locked balance before SL/TP sells."""
    cancelled = 0
    try:
        orders = executor.get_open_orders()
        for order in orders:
            order_id = ""
            if isinstance(order, dict):
                order_id = order.get("id", "")
            else:
                order_id = getattr(order, "id", "")
            if order_id:
                executor.cancel_order(order_id)
                cancelled += 1
        if cancelled:
            logger.info("Cancelled %d open orders before SL/TP", cancelled)
    except Exception as e:
        logger.warning("Failed to cancel open orders: %s", e)
    return cancelled


def _live_monitor_check() -> None:
    """Проверить live позиции на SL/TP и выполнить sell при срабатывании."""
    global _sl_tp_triggered, _monitor_running

    if _monitor_running:
        logger.info("Monitor already running, skipping")
        return

    from config import settings as _s

    if _s.paper_trading:
        return

    _monitor_running = True
    try:
        _live_monitor_check_inner()
    finally:
        _monitor_running = False


def _live_monitor_check_inner() -> None:
    """Inner logic — SL/TP checks on live positions."""
    global _sl_tp_triggered

    from config import settings as _s

    # Reset gave-up positions after retry interval
    now = time.monotonic()
    for token_id in list(_sl_tp_gave_up_ts):
        if now - _sl_tp_gave_up_ts[token_id] >= _GAVE_UP_RETRY_INTERVAL:
            _sl_tp_triggered.discard(token_id)
            _sl_tp_retries.pop(token_id, None)
            del _sl_tp_gave_up_ts[token_id]
            logger.info("Reset GAVE UP for %s — will retry", token_id[:16])

    # Force cache refresh
    global _positions_cache_ts
    _positions_cache_ts = 0.0
    positions = _fetch_live_positions()

    if not positions:
        return

    from trader.live_executor import get_live_executor

    _POLYMARKET_MIN_SIZE = 5.0

    def _is_weather(q: str) -> bool:
        q_lower = q.lower()
        return any(
            kw in q_lower
            for kw in ("temperature", "°f", "°c", "highest temp", "lowest temp")
        )

    def _get_sl_tp(question: str, entry_price: float) -> tuple[float, float]:
        if _is_weather(question):
            # For cheap weather tokens (entry < $0.15), disable SL — hold to resolution.
            # Max risk is $3 per position, SL on volatile micro-price tokens kills winners.
            if 0 < entry_price < 0.15:
                return 999.0, _s.weather_take_profit_pct  # effectively no SL
            return _s.weather_stop_loss_pct, _s.weather_take_profit_pct
        return _s.stop_loss_pct, _s.take_profit_pct

    # Cancel stale unfilled SELL orders and allow retry
    try:
        executor = get_live_executor()
        open_orders = executor.get_open_orders()
        for order in open_orders:
            if isinstance(order, dict):
                side = order.get("side", "")
                order_id = order.get("id", "")
                matched = order.get("size_matched", "0")
                asset_id = order.get("asset_id", "")
            else:
                side = getattr(order, "side", "")
                order_id = getattr(order, "id", "")
                matched = getattr(order, "size_matched", "0")
                asset_id = getattr(order, "asset_id", "")

            try:
                matched_val = float(matched or "0")
            except (ValueError, TypeError):
                matched_val = 0.0

            if side == "SELL" and matched_val == 0:
                executor.cancel_order(order_id)
                logger.info("Cancelled unfilled SELL order %s", order_id[:16])
                if asset_id and asset_id in _sl_tp_triggered:
                    _sl_tp_triggered.discard(asset_id)
                    _save_sl_tp_triggered(_sl_tp_triggered)
                    logger.info("Removed %s from triggered — will retry", asset_id[:16])
    except Exception as e:
        logger.warning("Failed to cleanup stale sell orders: %s", e)

    # Check if any positions need SL/TP — if so, cancel all orders first
    needs_sl_tp = False
    for pos in positions:
        token_id = pos.get("token_id", "")
        if not token_id or token_id in _sl_tp_triggered:
            continue
        if pos.get("resolved"):
            continue
        shares = pos.get("shares", 0.0)
        if shares < _POLYMARKET_MIN_SIZE:
            continue
        pnl_pct = pos.get("pnl_pct_raw", 0.0)
        entry_price = pos.get("entry", 0.0)
        sl_pct, tp_pct = _get_sl_tp(pos.get("question", ""), entry_price)
        if pnl_pct <= -sl_pct or pnl_pct >= tp_pct:
            needs_sl_tp = True
            break

    if needs_sl_tp:
        try:
            executor = get_live_executor()
            _cancel_all_open_orders(executor)
        except Exception as e:
            logger.warning("Failed to get executor for order cancellation: %s", e)

    for pos in positions:
        token_id = pos.get("token_id", "")
        if not token_id or token_id in _sl_tp_triggered:
            continue

        # Skip resolved positions -- they cannot be sold, only redeemed
        if pos.get("resolved"):
            continue

        pnl_pct = pos.get("pnl_pct_raw", 0.0)
        cur_price = pos.get("current", 0.0)
        entry_price = pos.get("entry", 0.0)
        shares = pos.get("shares", 0.0)
        question = pos.get("question", "")[:50]
        sl_pct, tp_pct = _get_sl_tp(pos.get("question", ""), entry_price)

        # Skip positions below Polymarket minimum order size
        if shares < _POLYMARKET_MIN_SIZE:
            if pnl_pct <= -sl_pct or pnl_pct >= tp_pct:
                logger.info(
                    "SKIP SL/TP for %s: %.2f shares < min %d",
                    question,
                    shares,
                    _POLYMARKET_MIN_SIZE,
                )
                _sl_tp_triggered.add(token_id)
                _save_sl_tp_triggered(_sl_tp_triggered)
            continue

        # Stop-loss
        if pnl_pct <= -sl_pct:
            executor = get_live_executor()
            best_bid = executor.get_best_bid(token_id)
            if best_bid > 0:
                sell_price = max(0.001, min(0.999, round(best_bid * 0.98, 4)))
            elif cur_price > 0:
                sell_price = max(0.001, min(0.999, round(cur_price * 0.90, 4)))
            else:
                logger.error("No price data for %s — skipping SL sell", question)
                continue
            logger.warning(
                "STOP-LOSS: %s | PnL: %.1f%% | Selling %.2f shares @ %.4f (bid=%.4f)",
                question,
                pnl_pct * 100,
                shares,
                sell_price,
                best_bid,
            )
            try:
                result = executor.execute_sell_order(token_id, sell_price, shares)
                # Only mark as triggered if order was immediately matched
                order_status = ""
                if isinstance(result, dict):
                    order_status = result.get("status", "")
                if order_status == "matched":
                    _sl_tp_triggered.add(token_id)
                    _save_sl_tp_triggered(_sl_tp_triggered)
                    _record_live_trade(
                        pos,
                        sell_price,
                        pnl_pct,
                        "stop-loss",
                    )
                    sync_broadcast(
                        "log",
                        f"STOP-LOSS: {question} | PnL: {pnl_pct * 100:+.1f}% | Sell @ {sell_price}",
                    )
                else:
                    logger.warning(
                        "SL sell order NOT matched (status=%s) for %s — will retry next cycle",
                        order_status,
                        question,
                    )
            except Exception as e:
                _sl_tp_retries[token_id] = _sl_tp_retries.get(token_id, 0) + 1
                if _sl_tp_retries[token_id] >= _SL_MAX_RETRIES:
                    _sl_tp_triggered.add(token_id)
                    _save_sl_tp_triggered(_sl_tp_triggered)
                    _sl_tp_gave_up_ts[token_id] = time.monotonic()
                    logger.error(
                        "SL GAVE UP after %d retries for %s: %s",
                        _SL_MAX_RETRIES,
                        question,
                        e,
                    )
                else:
                    logger.error(
                        "SL sell error (%d/%d) for %s: %s",
                        _sl_tp_retries[token_id],
                        _SL_MAX_RETRIES,
                        question,
                        e,
                    )
            continue

        # Take-profit
        if pnl_pct >= tp_pct:
            executor = get_live_executor()
            best_bid = executor.get_best_bid(token_id)
            if best_bid > 0:
                sell_price = max(0.001, min(0.999, round(best_bid * 0.995, 4)))
            elif cur_price > 0:
                sell_price = max(0.001, min(0.999, round(cur_price * 0.95, 4)))
            else:
                logger.error("No price data for %s — skipping TP sell", question)
                continue
            logger.info(
                "TAKE-PROFIT: %s | PnL: %.1f%% | Selling %.2f shares @ %.4f (bid=%.4f)",
                question,
                pnl_pct * 100,
                shares,
                sell_price,
                best_bid,
            )
            try:
                result = executor.execute_sell_order(token_id, sell_price, shares)
                # Only mark as triggered if order was immediately matched
                order_status = ""
                if isinstance(result, dict):
                    order_status = result.get("status", "")
                if order_status == "matched":
                    _sl_tp_triggered.add(token_id)
                    _save_sl_tp_triggered(_sl_tp_triggered)
                    _record_live_trade(
                        pos,
                        sell_price,
                        pnl_pct,
                        "take-profit",
                    )
                    sync_broadcast(
                        "log",
                        f"TAKE-PROFIT: {question} | PnL: {pnl_pct * 100:+.1f}% | Sell @ {sell_price}",
                    )
                else:
                    logger.warning(
                        "TP sell order NOT matched (status=%s) for %s — will retry next cycle",
                        order_status,
                        question,
                    )
            except Exception as e:
                _sl_tp_retries[token_id] = _sl_tp_retries.get(token_id, 0) + 1
                if _sl_tp_retries[token_id] >= _SL_MAX_RETRIES:
                    _sl_tp_triggered.add(token_id)
                    _save_sl_tp_triggered(_sl_tp_triggered)
                    _sl_tp_gave_up_ts[token_id] = time.monotonic()
                    logger.error(
                        "TP GAVE UP after %d retries for %s: %s",
                        _SL_MAX_RETRIES,
                        question,
                        e,
                    )
                else:
                    logger.error(
                        "TP sell error (%d/%d) for %s: %s",
                        _sl_tp_retries[token_id],
                        _SL_MAX_RETRIES,
                        question,
                        e,
                    )

    # Auto-redeem ALL resolved positions (including losers with value=0 to clean portfolio)
    redeemable = [p for p in positions if p.get("redeemable")]
    if redeemable:
        try:
            from trader.redeemer import redeem_resolved_positions

            results = redeem_resolved_positions(redeemable)
            for r in results:
                if r["success"]:
                    from trader.live_history import get_live_history

                    # Find actual PnL from position data
                    redeem_pnl = 0.0
                    for p in redeemable:
                        if p.get("market_id") == r.get("condition_id"):
                            redeem_pnl = p.get("pnl", 0.0)
                            break
                    get_live_history().record_redeem(
                        question=r["question"],
                        pnl=redeem_pnl,
                        tx_hash=r.get("tx_hash", ""),
                        condition_id=r.get("condition_id", ""),
                    )
                    sync_broadcast(
                        "log",
                        f"REDEEMED: {r['question'][:50]} | tx: {r['tx_hash'][:16]}...",
                    )
                else:
                    logger.error(
                        "Redeem failed for %s: %s",
                        r["question"][:50],
                        r.get("error", "unknown"),
                    )
        except ValueError as e:
            logger.error("Auto-redeem config error: %s", e)
        except RuntimeError as e:
            logger.error("Auto-redeem RPC error: %s", e)

    # Clean up: remove tokens no longer in positions.
    # Guard: only cleanup if Data API returned reasonable data (prevents wiping
    # _sl_tp_triggered on transient API errors which would cause duplicate sells).
    if positions and len(positions) >= max(1, len(_sl_tp_triggered) // 2):
        current_tokens = {p.get("token_id", "") for p in positions}
        old_size = len(_sl_tp_triggered)
        _sl_tp_triggered &= current_tokens
        if len(_sl_tp_triggered) != old_size:
            _save_sl_tp_triggered(_sl_tp_triggered)
        for k in list(_sl_tp_retries):
            if k not in current_tokens:
                del _sl_tp_retries[k]
        for k in list(_sl_tp_gave_up_ts):
            if k not in current_tokens:
                del _sl_tp_gave_up_ts[k]

    _monitor_running = False


async def _price_monitor_loop(interval_sec: int = 180) -> None:
    """Мониторинг цен каждые N секунд. В live режиме — проверяет SL/TP."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            from config import settings as _s

            if not _s.paper_trading:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _live_monitor_check)
                sync_broadcast("portfolio", _get_portfolio_summary())
            else:
                storage = PortfolioStorage()
                if not storage.positions:
                    continue
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, _monitor_bg_silent)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Price monitor error: %s", e)


def _monitor_bg_silent() -> None:
    """Тихое обновление цен — без смены статуса бота (paper mode only)."""
    from trader.monitor import update_positions

    try:
        storage = PortfolioStorage()
        if not storage.positions:
            return
        update_positions(storage)
        sync_broadcast("portfolio", _get_portfolio_summary())
    except Exception as e:
        logger.error("Silent monitor error: %s", e)


async def _trading_loop(interval_min: int = 15) -> None:
    """Поиск новых сделок каждые N минут (с Claude API)."""
    run_count = 0
    await asyncio.sleep(10)  # пауза при старте
    while True:
        run_count += 1
        logger.info("=== TRADING RUN #%d ===", run_count)
        sync_broadcast("log", f"Trading scan #{run_count} started")

        if bot_status["state"] != "idle":
            logger.info("Skipping trading run — bot is busy: %s", bot_status["state"])
        else:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _run_paper_bg_fast)

        logger.info("Next trading scan in %d min", interval_min)
        sync_broadcast("log", f"Next trading scan in {interval_min} min")
        await asyncio.sleep(interval_min * 60)


_monitor_task: asyncio.Task | None = None
_trading_task: asyncio.Task | None = None


@app.post("/api/scheduler/start")
async def api_scheduler_start() -> JSONResponse:
    global _monitor_task, _trading_task, _sse_task, _sse_listener
    already_running = []
    started = []

    if _monitor_task and not _monitor_task.done():
        already_running.append("monitor")
    else:
        _monitor_task = asyncio.create_task(_price_monitor_loop(180))
        started.append("price monitor (every 3 min)")

    if _trading_task and not _trading_task.done():
        already_running.append("trading")
    else:
        _trading_task = asyncio.create_task(_trading_loop(15))
        started.append("trading scanner (every 15 min)")

    from config import settings as _s

    if _s.sse_enabled and (_sse_task is None or _sse_task.done()):
        from services.sse_listener import SSEListener

        _sse_listener = SSEListener(
            on_breaking_match=_on_breaking_match,
            on_log=_broadcast_log,
        )
        _sse_task = asyncio.create_task(_sse_listener.start())
        started.append("SSE listener")
    elif _sse_task and not _sse_task.done():
        already_running.append("sse")

    msg = f"Started: {', '.join(started)}" if started else "Already running"
    sync_broadcast("log", msg)
    return JSONResponse(
        {"status": msg, "monitor_interval": 180, "trading_interval": 15}
    )


@app.post("/api/scheduler/stop")
async def api_scheduler_stop() -> JSONResponse:
    global _monitor_task, _trading_task, _sse_task, _sse_listener
    stopped = []

    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        _monitor_task = None
        stopped.append("price monitor")

    if _trading_task and not _trading_task.done():
        _trading_task.cancel()
        _trading_task = None
        stopped.append("trading scanner")

    if _sse_listener and _sse_task and not _sse_task.done():
        await _sse_listener.stop()
        _sse_task.cancel()
        _sse_task = None
        _sse_listener = None
        stopped.append("SSE listener")

    if stopped:
        msg = f"Stopped: {', '.join(stopped)}"
        sync_broadcast("log", msg)
        return JSONResponse({"status": msg})
    return JSONResponse({"status": "not running"})


@app.get("/api/scheduler/status")
async def api_scheduler_status() -> JSONResponse:
    monitor_running = _monitor_task is not None and not _monitor_task.done()
    trading_running = _trading_task is not None and not _trading_task.done()
    sse_running = _sse_task is not None and not _sse_task.done()
    return JSONResponse(
        {
            "running": monitor_running or trading_running or sse_running,
            "monitor": monitor_running,
            "trading": trading_running,
            "sse_listener": sse_running,
        }
    )


@app.post("/api/sell")
async def api_sell(req: SellRequest) -> JSONResponse:
    """Продать позицию через CLOB API."""
    from config import settings as _s

    if _s.paper_trading:
        return JSONResponse(
            {"status": "error", "message": "Sell disabled in paper mode"},
            status_code=400,
        )

    # Validate sell against real positions
    positions = _fetch_live_positions()
    matching = [p for p in positions if p.get("token_id") == req.token_id]
    if not matching:
        return JSONResponse(
            {"status": "error", "message": "Position not found for this token_id"},
            status_code=400,
        )
    max_shares = matching[0].get("shares", 0)
    if req.size > max_shares * 1.01:  # 1% tolerance for rounding
        return JSONResponse(
            {
                "status": "error",
                "message": f"Size {req.size} exceeds position ({max_shares:.2f} shares)",
            },
            status_code=400,
        )

    try:
        from trader.live_executor import get_live_executor

        executor = get_live_executor()
        result = executor.execute_sell_order(
            token_id=req.token_id,
            price=req.price,
            size=req.size,
        )
        # Invalidate positions cache
        global _positions_cache_ts
        _positions_cache_ts = 0.0
        # Record manual sell
        pos = matching[0]
        pnl_pct = pos.get("pnl_pct_raw", 0.0)
        _record_live_trade(pos, req.price, pnl_pct, "manual")
        sync_broadcast(
            "log",
            f"SELL order placed: {req.size:.2f} shares @ {req.price:.4f}",
        )
        return JSONResponse({"status": "ok", "result": result})
    except ValueError as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)
    except Exception as e:
        logger.error("Sell endpoint error: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.get("/api/live-orders")
async def api_live_orders() -> JSONResponse:
    """Получить открытые ордера из CLOB API."""
    from config import settings as _s

    if _s.paper_trading:
        return JSONResponse([])

    try:
        from trader.live_executor import get_live_executor

        orders = get_live_executor().get_open_orders()
        return JSONResponse(orders if isinstance(orders, list) else [])
    except Exception as e:
        logger.error("Live orders error: %s", e)
        return JSONResponse([])


@app.post("/api/cancel-order")
async def api_cancel_order(req: CancelOrderRequest) -> JSONResponse:
    """Отменить открытый ордер."""
    from config import settings as _s

    if _s.paper_trading:
        return JSONResponse(
            {"status": "error", "message": "Cancel disabled in paper mode"},
            status_code=400,
        )

    try:
        from trader.live_executor import get_live_executor

        ok = get_live_executor().cancel_order(req.order_id)
        if ok:
            sync_broadcast("log", f"Order {req.order_id[:12]}... cancelled")
            return JSONResponse({"status": "ok"})
        return JSONResponse(
            {"status": "error", "message": "Cancel failed"},
            status_code=500,
        )
    except Exception as e:
        logger.error("Cancel order error: %s", e)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


@app.post("/api/redeem")
async def api_redeem(background_tasks: BackgroundTasks) -> JSONResponse:
    """Manually trigger redemption of resolved positions."""
    from config import settings as _s

    if _s.paper_trading:
        return JSONResponse(
            {"status": "error", "message": "Redeem disabled in paper mode"},
            status_code=400,
        )

    background_tasks.add_task(_redeem_bg)
    return JSONResponse({"status": "started"})


def _redeem_bg() -> None:
    """Background task for manual redeem."""
    global _positions_cache_ts
    _positions_cache_ts = 0.0
    positions = _fetch_live_positions()

    redeemable = [
        p for p in positions if p.get("redeemable") and p.get("current_value", 0) > 0
    ]
    if not redeemable:
        sync_broadcast("log", "No redeemable positions found (or all worthless)")
        return

    sync_broadcast("log", f"Redeeming {len(redeemable)} resolved position(s)...")
    try:
        from trader.redeemer import redeem_resolved_positions

        results = redeem_resolved_positions(redeemable)
        for r in results:
            if r["success"]:
                sync_broadcast(
                    "log",
                    f"REDEEMED: {r['question'][:50]} | tx: {r['tx_hash'][:16]}...",
                )
            else:
                sync_broadcast(
                    "log",
                    f"REDEEM FAILED: {r['question'][:50]} | {r.get('error', 'unknown')}",
                )
        if not results:
            sync_broadcast("log", "Redeem skipped (cooldown active or no redeemable)")
    except ValueError as e:
        logger.error("Redeem config error: %s", e)
        sync_broadcast("log", f"Redeem error: {e}")
    except RuntimeError as e:
        logger.error("Redeem RPC error: %s", e)
        sync_broadcast("log", f"Redeem RPC error: {e}")


@app.get("/api/live-positions")
async def api_live_positions() -> JSONResponse:
    """Получить реальные позиции из Data API (для дебага)."""
    return JSONResponse(_fetch_live_positions())


@app.get("/api/sse/status")
async def api_sse_status() -> JSONResponse:
    if _sse_listener is None:
        return JSONResponse({"enabled": False})
    return JSONResponse({"enabled": True, **_sse_listener.status})


def _load_latest_analyses(max_files: int = 3) -> list[dict]:
    if not RESULTS_DIR.exists():
        return []
    files = sorted(RESULTS_DIR.glob("analysis_*.json"), reverse=True)[:max_files]
    analyses = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            analyses.append(
                {
                    "filename": f.name,
                    "date": f.stem.replace("analysis_", ""),
                    "predictions": data[:10],
                    "total": len(data),
                }
            )
        except (json.JSONDecodeError, OSError):
            pass
    return analyses


def start_web(host: str = "0.0.0.0", port: int = 8899) -> None:
    logger.info("Starting web dashboard at http://localhost:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="info")
