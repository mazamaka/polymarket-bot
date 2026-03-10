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
EQUITY_FILE = DATA_DIR / "equity_curve.json"


class PortfolioStorage:
    """Хранение позиций и истории в JSON файлах."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        self.positions: list[Position] = self._load_positions()
        self.history: list[dict] = self._load_history()
        self.equity_curve: list[dict] = self._load_equity()
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

    def _load_equity(self) -> list[dict]:
        if EQUITY_FILE.exists():
            return json.loads(EQUITY_FILE.read_text())
        return [{"ts": datetime.now().isoformat(), "equity": 100.0}]

    def _calc_balance(self) -> float:
        for entry in reversed(self.history):
            if "balance_after" in entry:
                return entry["balance_after"]
        spent = sum(p.size_usd for p in self.positions)
        return 100.0 - spent

    def save(self) -> None:
        """Сохранить все данные с file locking для защиты от race conditions."""
        import fcntl

        self.equity_curve = self.equity_curve[-500:]
        lock_file = DATA_DIR / ".storage.lock"

        with open(lock_file, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                positions_data = [p.model_dump(mode="json") for p in self.positions]
                POSITIONS_FILE.write_text(
                    json.dumps(
                        positions_data, indent=2, ensure_ascii=False, default=str
                    )
                )
                HISTORY_FILE.write_text(
                    json.dumps(self.history, indent=2, ensure_ascii=False, default=str)
                )
                EQUITY_FILE.write_text(
                    json.dumps(self.equity_curve, indent=2, default=str)
                )
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)

    def _record_equity(self) -> None:
        """Записать точку на equity curve."""
        total_equity = self.balance + sum(p.size_usd + p.pnl for p in self.positions)
        self.equity_curve.append(
            {
                "ts": datetime.now().isoformat(),
                "equity": round(total_equity, 2),
                "balance": round(self.balance, 2),
                "invested": round(sum(p.size_usd for p in self.positions), 2),
                "positions": len(self.positions),
            }
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
                "edge": round(position.edge * 100, 1),
                "confidence": round(position.confidence * 100, 0),
                "ai_prob": round(position.ai_probability * 100, 0),
                "end_date": position.end_date,
                "balance_after": balance_after,
                "timestamp": datetime.now().isoformat(),
            }
        )
        self._record_equity()
        self.save()

    def close_position(self, market_id: str, exit_price: float) -> float:
        """Закрыть позицию. exit_price = цена нашего токена (YES или NO)."""
        pos = next((p for p in self.positions if p.market_id == market_id), None)
        if not pos:
            return 0.0

        # Единая формула: exit_price и entry_price — оба цена нашего токена
        pnl = (
            (exit_price - pos.entry_price) * pos.size_usd / max(pos.entry_price, 0.001)
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
                "hold_hours": round(
                    (datetime.now() - pos.opened_at).total_seconds() / 3600, 1
                ),
                "balance_after": round(self.balance, 2),
                "timestamp": datetime.now().isoformat(),
            }
        )
        self._record_equity()
        self.save()
        return pnl

    def get_open_market_ids(self) -> set[str]:
        return {p.market_id for p in self.positions}

    def get_summary(self) -> dict:
        total_invested = sum(p.size_usd for p in self.positions)
        total_unrealized = sum(p.pnl for p in self.positions)
        closes = [e for e in self.history if e["action"] == "CLOSE"]
        realized = sum(e.get("pnl", 0) for e in closes)
        wins = sum(1 for e in closes if e.get("pnl", 0) > 0)
        total_equity = self.balance + total_invested + total_unrealized
        roi = (total_equity - 100.0) / 100.0 * 100

        avg_edge = 0.0
        edge_entries = [
            e for e in self.history if e["action"] == "OPEN" and "edge" in e
        ]
        if edge_entries:
            avg_edge = sum(abs(e["edge"]) for e in edge_entries) / len(edge_entries)

        return {
            "balance_usd": round(self.balance, 2),
            "invested_usd": round(total_invested, 2),
            "total_equity": round(total_equity, 2),
            "roi_pct": round(roi, 2),
            "open_positions": len(self.positions),
            "total_trades": len([h for h in self.history if h["action"] == "OPEN"]),
            "closed_trades": len(closes),
            "win_count": wins,
            "win_rate": round(wins / len(closes) * 100) if closes else 0,
            "unrealized_pnl": round(total_unrealized, 2),
            "realized_pnl": round(realized, 2),
            "avg_edge": round(avg_edge, 1),
            "positions": [
                {
                    "market_id": p.market_id,
                    "question": p.question,
                    "side": p.side,
                    "entry": p.entry_price,
                    "current": p.current_price,
                    "size": p.size_usd,
                    "pnl": round(p.pnl, 2),
                    "pnl_pct": f"{p.pnl_pct * 100:+.1f}%",
                    "opened": p.opened_at.isoformat(),
                    "end_date": p.end_date,
                    "slug": p.slug,
                    "edge": round(p.edge * 100, 1),
                    "confidence": round(p.confidence * 100),
                    "ai_prob": round(p.ai_probability * 100),
                    "reasoning": p.reasoning,
                    "volume": p.volume,
                    "liquidity": p.liquidity,
                }
                for p in self.positions
            ],
            "equity_curve": self.equity_curve[-100:],
        }
