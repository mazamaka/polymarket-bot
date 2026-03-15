"""Web dashboard для Polymarket Bot с WebSocket real-time обновлениями."""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from claude_auth import claude_auth_router
from trader.storage import PortfolioStorage

logger = logging.getLogger(__name__)

app = FastAPI(title="Polymarket Bot Dashboard")
app.include_router(claude_auth_router)

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

RESULTS_DIR = Path("results")

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


@app.on_event("startup")
async def _capture_loop() -> None:
    global _main_loop, _monitor_task, _trading_task
    _main_loop = asyncio.get_running_loop()
    # Auto-start scheduler for autonomous operation
    _monitor_task = asyncio.create_task(_price_monitor_loop(180))
    _trading_task = asyncio.create_task(_trading_loop(15))
    logger.info(
        "Auto-scheduler started: price monitor (3 min) + trading scanner (15 min)"
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
    await ws.accept()
    ws_clients.add(ws)
    logger.info("WS connected (%d clients)", len(ws_clients))
    try:
        # Сразу отправляем текущее состояние
        storage = PortfolioStorage()
        await ws.send_text(
            json.dumps(
                {
                    "event": "portfolio",
                    "data": storage.get_summary(),
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
    storage = PortfolioStorage()
    summary = storage.get_summary()
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
    storage = PortfolioStorage()
    return JSONResponse(storage.get_summary())


@app.get("/api/history")
async def api_history() -> JSONResponse:
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
    from trader.monitor import update_positions

    _set_status("monitoring", "Updating prices...")
    try:
        storage = PortfolioStorage()
        update_positions(storage)
        sync_broadcast("portfolio", storage.get_summary())
        sync_broadcast("history", list(reversed(storage.history)))
        _set_status("idle", "Prices updated")
    except Exception as e:
        logger.error("Monitor error: %s", e)
        _set_status("idle", f"Error: {e}")


def _run_paper_bg() -> None:
    from main import run_paper_trading

    _set_status("running", "Paper trading in progress...")
    try:
        run_paper_trading(max_markets=500, on_log=_broadcast_log)
        storage = PortfolioStorage()
        sync_broadcast("portfolio", storage.get_summary())
        sync_broadcast("history", list(reversed(storage.history)))
        _set_status("idle", "Paper trading completed")
    except Exception as e:
        logger.exception("Paper trading error: %s", e)
        _set_status("idle", f"Error: {e}")


def _run_paper_bg_fast() -> None:
    """Быстрый режим для auto-scheduler: больше рынков, без thinking."""
    from main import run_paper_trading

    _set_status("running", "Auto paper trading (500 markets, short-term)...")
    try:
        run_paper_trading(max_markets=500, on_log=_broadcast_log)
        storage = PortfolioStorage()
        sync_broadcast("portfolio", storage.get_summary())
        sync_broadcast("history", list(reversed(storage.history)))
        _set_status("idle", "Auto paper trading completed")
    except Exception as e:
        logger.exception("Auto paper trading error: %s", e)
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


async def _price_monitor_loop(interval_sec: int = 180) -> None:
    """Быстрый мониторинг цен каждые N секунд (без Claude, только Gamma API)."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
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
    """Тихое обновление цен — без смены статуса бота."""
    from trader.monitor import update_positions

    try:
        storage = PortfolioStorage()
        if not storage.positions:
            return
        update_positions(storage)
        sync_broadcast("portfolio", storage.get_summary())
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
    global _monitor_task, _trading_task
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

    msg = f"Started: {', '.join(started)}" if started else "Already running"
    sync_broadcast("log", msg)
    return JSONResponse(
        {"status": msg, "monitor_interval": 180, "trading_interval": 15}
    )


@app.post("/api/scheduler/stop")
async def api_scheduler_stop() -> JSONResponse:
    global _monitor_task, _trading_task
    stopped = []

    if _monitor_task and not _monitor_task.done():
        _monitor_task.cancel()
        _monitor_task = None
        stopped.append("price monitor")

    if _trading_task and not _trading_task.done():
        _trading_task.cancel()
        _trading_task = None
        stopped.append("trading scanner")

    if stopped:
        msg = f"Stopped: {', '.join(stopped)}"
        sync_broadcast("log", msg)
        return JSONResponse({"status": msg})
    return JSONResponse({"status": "not running"})


@app.get("/api/scheduler/status")
async def api_scheduler_status() -> JSONResponse:
    monitor_running = _monitor_task is not None and not _monitor_task.done()
    trading_running = _trading_task is not None and not _trading_task.done()
    return JSONResponse(
        {
            "running": monitor_running or trading_running,
            "monitor": monitor_running,
            "trading": trading_running,
        }
    )


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
