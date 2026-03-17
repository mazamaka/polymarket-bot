"""Persistent trade history for live trading mode."""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "live_trade_history.json"


class LiveTradeHistory:
    """Thread-safe JSON-backed trade history."""

    def __init__(self) -> None:
        self._history: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if HISTORY_FILE.exists():
            try:
                return json.loads(HISTORY_FILE.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.error("Failed to load live history: %s", e)
        return []

    def _save(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        HISTORY_FILE.write_text(
            json.dumps(self._history, indent=2, ensure_ascii=False, default=str)
        )

    @property
    def history(self) -> list[dict]:
        return self._history

    def record_open(
        self,
        question: str,
        side: str,
        entry_price: float,
        size_usd: float,
        shares: float,
        token_id: str = "",
        market_id: str = "",
        edge: float = 0.0,
        confidence: float = 0.0,
        source: str = "ai",
    ) -> None:
        self._history.append(
            {
                "action": "OPEN",
                "question": question[:80],
                "side": side,
                "entry_price": entry_price,
                "size_usd": round(size_usd, 2),
                "shares": round(shares, 2),
                "token_id": token_id,
                "market_id": market_id,
                "edge": round(edge, 1),
                "confidence": round(confidence, 0),
                "source": source,
                "timestamp": datetime.now().isoformat(),
            }
        )
        self._save()

    def record_close(
        self,
        question: str,
        side: str,
        entry_price: float,
        exit_price: float,
        size_usd: float,
        shares: float,
        pnl: float,
        reason: str = "",
        token_id: str = "",
        market_id: str = "",
        order_id: str = "",
    ) -> None:
        self._history.append(
            {
                "action": "CLOSE",
                "question": question[:80],
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "size_usd": round(size_usd, 2),
                "shares": round(shares, 2),
                "pnl": round(pnl, 4),
                "reason": reason,
                "token_id": token_id,
                "market_id": market_id,
                "order_id": order_id,
                "timestamp": datetime.now().isoformat(),
            }
        )
        self._save()

    def record_redeem(
        self,
        question: str,
        pnl: float,
        tx_hash: str = "",
        condition_id: str = "",
    ) -> None:
        self._history.append(
            {
                "action": "REDEEM",
                "question": question[:80],
                "pnl": round(pnl, 4),
                "tx_hash": tx_hash,
                "market_id": condition_id,
                "timestamp": datetime.now().isoformat(),
            }
        )
        self._save()


# Singleton
_instance: LiveTradeHistory | None = None


def get_live_history() -> LiveTradeHistory:
    global _instance
    if _instance is None:
        _instance = LiveTradeHistory()
    return _instance
