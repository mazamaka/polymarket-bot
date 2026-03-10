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

from trader.storage import PortfolioStorage

logger = logging.getLogger(__name__)

app = FastAPI(title="Polymarket Bot Dashboard")

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
    global _main_loop
    _main_loop = asyncio.get_running_loop()


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
    history = list(reversed(storage.history[-30:]))
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


@app.get("/api/portfolio")
async def api_portfolio() -> JSONResponse:
    storage = PortfolioStorage()
    return JSONResponse(storage.get_summary())


@app.get("/api/history")
async def api_history() -> JSONResponse:
    storage = PortfolioStorage()
    return JSONResponse(list(reversed(storage.history[-50:])))


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
        sync_broadcast("history", list(reversed(storage.history[-30:])))
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
        sync_broadcast("history", list(reversed(storage.history[-30:])))
        _set_status("idle", "Paper trading completed")
    except Exception as e:
        logger.error("Paper trading error: %s", e)
        _set_status("idle", f"Error: {e}")


def _run_paper_bg_fast() -> None:
    """Быстрый режим для auto-scheduler: больше рынков, без thinking."""
    from main import run_paper_trading

    _set_status("running", "Auto paper trading (500 markets, short-term)...")
    try:
        run_paper_trading(max_markets=500, on_log=_broadcast_log)
        storage = PortfolioStorage()
        sync_broadcast("portfolio", storage.get_summary())
        sync_broadcast("history", list(reversed(storage.history[-30:])))
        _set_status("idle", "Auto paper trading completed")
    except Exception as e:
        logger.error("Auto paper trading error: %s", e)
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
