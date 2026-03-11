"""Логирование всех решений бота — для страницы анализа."""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
SCAN_LOG_FILE = DATA_DIR / "scan_log.json"
MAX_SCANS = 50  # хранить последние N сканов


class ScanLogger:
    """Записывает результаты каждого скана для UI анализа."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self._current_scan: dict | None = None

    def start_scan(self) -> None:
        self._current_scan = {
            "timestamp": datetime.now().isoformat(),
            "markets_loaded": 0,
            "markets_filtered": 0,
            "skipped_time": 0,
            "skipped_type": 0,
            "screened": [],
            "analyzed": [],
            "trades": [],
        }

    def set_filter_stats(
        self, loaded: int, filtered: int, skipped_time: int, skipped_type: int
    ) -> None:
        if not self._current_scan:
            return
        self._current_scan["markets_loaded"] = loaded
        self._current_scan["markets_filtered"] = filtered
        self._current_scan["skipped_time"] = skipped_time
        self._current_scan["skipped_type"] = skipped_type

    def add_screened_market(
        self,
        market_id: str,
        question: str,
        yes_price: float,
        volume: float,
        liquidity: float,
        interesting: bool,
        reason: str = "",
    ) -> None:
        if not self._current_scan:
            return
        self._current_scan["screened"].append(
            {
                "market_id": market_id,
                "question": question,
                "yes_price": round(yes_price, 4),
                "volume": round(volume, 0),
                "liquidity": round(liquidity, 0),
                "interesting": interesting,
                "reason": reason,
            }
        )

    def add_analyzed_market(
        self,
        market_id: str,
        question: str,
        ai_prob: float,
        market_prob: float,
        edge: float,
        confidence: float,
        spread: float,
        side: str,
        skip_reason: str = "",
        has_news: bool = False,
        has_price: bool = False,
    ) -> None:
        if not self._current_scan:
            return
        self._current_scan["analyzed"].append(
            {
                "market_id": market_id,
                "question": question,
                "ai_prob": round(ai_prob * 100, 1),
                "market_prob": round(market_prob * 100, 1),
                "edge": round(edge * 100, 1),
                "confidence": round(confidence * 100, 0),
                "spread": round(spread * 100, 0),
                "side": side,
                "skip_reason": skip_reason,
                "traded": not bool(skip_reason),
                "has_news": has_news,
                "has_price": has_price,
            }
        )

    def add_trade(self, market_id: str, side: str, price: float, size: float) -> None:
        if not self._current_scan:
            return
        self._current_scan["trades"].append(
            {
                "market_id": market_id,
                "side": side,
                "price": round(price, 4),
                "size": round(size, 2),
            }
        )

    def finish_scan(self) -> None:
        if not self._current_scan:
            return
        scans = self._load()
        scans.insert(0, self._current_scan)
        scans = scans[:MAX_SCANS]
        self._save(scans)
        self._current_scan = None

    def get_scans(self, limit: int = 20) -> list[dict]:
        return self._load()[:limit]

    def get_latest_scan(self) -> dict | None:
        scans = self._load()
        return scans[0] if scans else None

    def _load(self) -> list[dict]:
        if SCAN_LOG_FILE.exists():
            try:
                return json.loads(SCAN_LOG_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save(self, scans: list[dict]) -> None:
        SCAN_LOG_FILE.write_text(
            json.dumps(scans, indent=2, ensure_ascii=False, default=str)
        )


# Singleton
scan_logger = ScanLogger()
