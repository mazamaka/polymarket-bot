"""Persistent storage для paper trading позиций и истории."""

import json
import logging
from datetime import datetime
from pathlib import Path

from polymarket.models import Position

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
POSITIONS_FILE = DATA_DIR / "positions.json"
HISTORY_FILE = DATA_DIR / "trade_history.json"


class PortfolioStorage:
    """Хранение позиций и истории в JSON файлах."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self.positions: list[Position] = self._load_positions()
        self.history: list[dict] = self._load_history()
        self.balance: float = self._calc_balance()

    def _load_positions(self) -> list[Position]:
        if POSITIONS_FILE.exists():
            data = json.loads(POSITIONS_FILE.read_text())
            return [Position(**p) for p in data]
        return []

    def _load_history(self) -> list[dict]:
        if HISTORY_FILE.exists():
            return json.loads(HISTORY_FILE.read_text())
        return []

    def _calc_balance(self) -> float:
        """Вычисляем баланс: начальный - вложенное в позиции."""
        initial = 100.0
        # Ищем последний баланс в истории
        for entry in reversed(self.history):
            if "balance_after" in entry:
                return entry["balance_after"]
        spent = sum(p.size_usd for p in self.positions)
        return initial - spent

    def save(self) -> None:
        positions_data = [p.model_dump(mode="json") for p in self.positions]
        POSITIONS_FILE.write_text(
            json.dumps(positions_data, indent=2, ensure_ascii=False, default=str)
        )
        HISTORY_FILE.write_text(
            json.dumps(self.history, indent=2, ensure_ascii=False, default=str)
        )

    def add_position(self, position: Position, balance_after: float) -> None:
        self.positions.append(position)
        self.balance = balance_after
        self.history.append(
            {
                "action": "OPEN",
                "market_id": position.market_id,
                "question": position.question[:80],
                "side": position.side,
                "entry_price": position.entry_price,
                "size_usd": position.size_usd,
                "balance_after": balance_after,
                "timestamp": datetime.now().isoformat(),
            }
        )
        self.save()

    def close_position(self, market_id: str, exit_price: float) -> float:
        """Закрыть позицию, вернуть PnL."""
        pos = next((p for p in self.positions if p.market_id == market_id), None)
        if not pos:
            return 0.0

        if pos.side == "BUY_YES":
            pnl = (
                (exit_price - pos.entry_price)
                * pos.size_usd
                / max(pos.entry_price, 0.001)
            )
        else:
            pnl = (
                (pos.entry_price - exit_price)
                * pos.size_usd
                / max(1 - pos.entry_price, 0.001)
            )

        self.balance += pos.size_usd + pnl
        self.positions = [p for p in self.positions if p.market_id != market_id]
        self.history.append(
            {
                "action": "CLOSE",
                "market_id": market_id,
                "question": pos.question[:80],
                "side": pos.side,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "size_usd": pos.size_usd,
                "pnl": round(pnl, 4),
                "balance_after": round(self.balance, 2),
                "timestamp": datetime.now().isoformat(),
            }
        )
        self.save()
        return pnl

    def get_open_market_ids(self) -> set[str]:
        return {p.market_id for p in self.positions}

    def get_summary(self) -> dict:
        total_invested = sum(p.size_usd for p in self.positions)
        total_pnl = sum(p.pnl for p in self.positions)
        closed_pnl = sum(
            e.get("pnl", 0) for e in self.history if e["action"] == "CLOSE"
        )
        return {
            "balance_usd": round(self.balance, 2),
            "invested_usd": round(total_invested, 2),
            "open_positions": len(self.positions),
            "total_trades": len([h for h in self.history if h["action"] == "OPEN"]),
            "closed_trades": len([h for h in self.history if h["action"] == "CLOSE"]),
            "unrealized_pnl": round(total_pnl, 2),
            "realized_pnl": round(closed_pnl, 2),
            "positions": [
                {
                    "market_id": p.market_id,
                    "question": p.question[:60],
                    "side": p.side,
                    "entry": p.entry_price,
                    "current": p.current_price,
                    "size": p.size_usd,
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": f"{p.pnl_pct * 100:+.1f}%",
                    "opened": p.opened_at.isoformat(),
                }
                for p in self.positions
            ],
        }
