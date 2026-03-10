"""Web dashboard для Polymarket Bot."""

import json
import logging
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    storage = PortfolioStorage()
    summary = storage.get_summary()
    history = storage.history[-20:]  # последние 20 записей
    history.reverse()

    # Последние анализы
    analyses = _load_latest_analyses()

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "summary": summary,
            "history": history,
            "analyses": analyses,
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


@app.get("/api/portfolio")
async def api_portfolio() -> JSONResponse:
    storage = PortfolioStorage()
    return JSONResponse(storage.get_summary())


@app.get("/api/history")
async def api_history() -> JSONResponse:
    storage = PortfolioStorage()
    return JSONResponse(storage.history[-50:])


@app.post("/api/monitor")
async def api_monitor(background_tasks: BackgroundTasks) -> JSONResponse:
    from trader.monitor import update_positions

    storage = PortfolioStorage()
    background_tasks.add_task(update_positions, storage)
    return JSONResponse({"status": "monitoring started"})


@app.post("/api/run-paper")
async def api_run_paper(background_tasks: BackgroundTasks) -> JSONResponse:
    background_tasks.add_task(_run_paper_bg)
    return JSONResponse({"status": "paper trading started"})


def _run_paper_bg() -> None:
    from main import run_paper_trading

    try:
        run_paper_trading(max_markets=50, use_thinking=True)
    except Exception as e:
        logger.error("Background paper trading error: %s", e)


def _load_latest_analyses(max_files: int = 3) -> list[dict]:
    """Загрузить последние результаты анализа."""
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
                    "predictions": data[:10],  # top 10
                    "total": len(data),
                }
            )
        except (json.JSONDecodeError, OSError):
            pass
    return analyses


def start_web(host: str = "0.0.0.0", port: int = 8899) -> None:
    logger.info("Starting web dashboard at http://localhost:%d", port)
    uvicorn.run(app, host=host, port=port, log_level="info")
