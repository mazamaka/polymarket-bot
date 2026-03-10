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
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    ws_clients -= dead


def sync_broadcast(event: str, data: dict | str = "") -> None:
    """Синхронная обёртка для broadcast (из background tasks)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(broadcast(event, data))
        else:
            loop.run_until_complete(broadcast(event, data))
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
        run_paper_trading(max_markets=50, use_thinking=True)
        storage = PortfolioStorage()
        sync_broadcast("portfolio", storage.get_summary())
        sync_broadcast("history", list(reversed(storage.history[-30:])))
        _set_status("idle", "Paper trading completed")
    except Exception as e:
        logger.error("Paper trading error: %s", e)
        _set_status("idle", f"Error: {e}")


def _run_analysis_bg() -> None:
    from main import run_analysis

    _set_status("running", "Analysis in progress...")
    try:
        run_analysis(max_markets=50, use_thinking=True)
        sync_broadcast("analyses", _load_latest_analyses())
        _set_status("idle", "Analysis completed")
    except Exception as e:
        logger.error("Analysis error: %s", e)
        _set_status("idle", f"Error: {e}")


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
