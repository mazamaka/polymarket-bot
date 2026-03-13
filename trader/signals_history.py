"""Полное историческое хранилище всех сигналов для backtesting."""

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
SIGNALS_FILE = DATA_DIR / "signals_history.json"
WEATHER_FILE = DATA_DIR / "weather_history.json"
MARKET_SNAPSHOTS_FILE = DATA_DIR / "market_snapshots.json"


class SignalsHistory:
    """Сохраняет ВСЕ сигналы — принятые, отклонённые, weather, AI."""

    def __init__(self) -> None:
        DATA_DIR.mkdir(exist_ok=True)

    def record_ai_signal(
        self,
        market_id: str,
        question: str,
        ai_prob: float,
        market_prob: float,
        edge: float,
        confidence: float,
        side: str,
        reasoning: str,
        action: str,
        skip_reason: str = "",
        entry_price: float = 0.0,
        size_usd: float = 0.0,
        end_date: str = "",
        volume: float = 0.0,
        liquidity: float = 0.0,
    ) -> None:
        record = {
            "ts": datetime.now().isoformat(),
            "type": "ai",
            "market_id": market_id,
            "question": question,
            "ai_prob": round(ai_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge": round(edge, 4),
            "confidence": round(confidence, 4),
            "side": side,
            "reasoning": reasoning[:500],
            "action": action,
            "skip_reason": skip_reason,
            "entry_price": round(entry_price, 4),
            "size_usd": round(size_usd, 2),
            "end_date": end_date,
            "volume": round(volume, 0),
            "liquidity": round(liquidity, 0),
        }
        self._append(SIGNALS_FILE, record)

    def record_weather_signal(
        self,
        market_id: str,
        question: str,
        city: str,
        target_date: str,
        temp_type: str,
        direction: str,
        threshold: float,
        ensemble_temps: list[float],
        model_prob: float,
        market_prob: float,
        edge: float,
        confidence: float,
        side: str,
        action: str,
        skip_reason: str = "",
        entry_price: float = 0.0,
        size_usd: float = 0.0,
        end_date: str = "",
        volume: float = 0.0,
        liquidity: float = 0.0,
    ) -> None:
        # Сохраняем статистику ensemble, не все 80 значений
        temps = sorted(ensemble_temps) if ensemble_temps else []
        record = {
            "ts": datetime.now().isoformat(),
            "type": "weather",
            "market_id": market_id,
            "question": question,
            "city": city,
            "target_date": target_date,
            "temp_type": temp_type,
            "direction": direction,
            "threshold": threshold,
            "ensemble_count": len(temps),
            "ensemble_min": round(min(temps), 1) if temps else 0,
            "ensemble_max": round(max(temps), 1) if temps else 0,
            "ensemble_mean": round(sum(temps) / len(temps), 1) if temps else 0,
            "ensemble_median": round(temps[len(temps) // 2], 1) if temps else 0,
            "ensemble_p10": round(temps[int(len(temps) * 0.1)], 1) if temps else 0,
            "ensemble_p90": round(temps[int(len(temps) * 0.9)], 1) if temps else 0,
            "model_prob": round(model_prob, 4),
            "market_prob": round(market_prob, 4),
            "edge": round(edge, 4),
            "confidence": round(confidence, 4),
            "side": side,
            "action": action,
            "skip_reason": skip_reason,
            "entry_price": round(entry_price, 4),
            "size_usd": round(size_usd, 2),
            "end_date": end_date,
            "volume": round(volume, 0),
            "liquidity": round(liquidity, 0),
        }
        self._append(SIGNALS_FILE, record)

    def record_correlation_signal(
        self,
        market_id: str,
        question: str,
        correlated_market_id: str,
        correlated_question: str,
        spread: float,
        side: str,
        action: str,
        skip_reason: str = "",
        entry_price: float = 0.0,
        size_usd: float = 0.0,
    ) -> None:
        record = {
            "ts": datetime.now().isoformat(),
            "type": "correlation",
            "market_id": market_id,
            "question": question,
            "correlated_market_id": correlated_market_id,
            "correlated_question": correlated_question,
            "spread": round(spread, 4),
            "side": side,
            "action": action,
            "skip_reason": skip_reason,
            "entry_price": round(entry_price, 4),
            "size_usd": round(size_usd, 2),
        }
        self._append(SIGNALS_FILE, record)

    def record_market_snapshot(
        self,
        markets: list[dict],
    ) -> None:
        """Снэпшот всех рынков на момент скана — цены, ликвидность, volume."""
        snapshot = {
            "ts": datetime.now().isoformat(),
            "count": len(markets),
            "markets": markets,
        }
        self._append(MARKET_SNAPSHOTS_FILE, snapshot, max_items=100)

    def get_signals(
        self, signal_type: str | None = None, limit: int = 500
    ) -> list[dict]:
        data = self._load(SIGNALS_FILE)
        if signal_type:
            data = [d for d in data if d.get("type") == signal_type]
        return data[-limit:]

    def get_stats(self) -> dict:
        data = self._load(SIGNALS_FILE)
        total = len(data)
        by_type = {}
        by_action = {}
        for d in data:
            t = d.get("type", "unknown")
            a = d.get("action", "unknown")
            by_type[t] = by_type.get(t, 0) + 1
            by_action[a] = by_action.get(a, 0) + 1
        return {
            "total_signals": total,
            "by_type": by_type,
            "by_action": by_action,
        }

    def _append(self, filepath: Path, record: dict, max_items: int = 5000) -> None:
        data = self._load(filepath)
        data.append(record)
        if len(data) > max_items:
            data = data[-max_items:]
        filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str))

    def _load(self, filepath: Path) -> list[dict]:
        if filepath.exists():
            try:
                return json.loads(filepath.read_text())
            except (json.JSONDecodeError, OSError):
                return []
        return []


# Singleton
signals_history = SignalsHistory()
