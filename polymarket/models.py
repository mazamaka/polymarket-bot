from datetime import datetime

from pydantic import BaseModel, Field


class Market(BaseModel):
    """Рынок на Polymarket."""

    id: str
    question: str
    description: str = ""
    end_date: str = ""
    active: bool = True
    closed: bool = False
    outcomes: list[str] = Field(default_factory=lambda: ["Yes", "No"])
    outcome_prices: list[float] = Field(default_factory=lambda: [0.5, 0.5])
    clob_token_ids: list[str] = Field(default_factory=list)
    volume: float = 0.0
    liquidity: float = 0.0
    spread: float = 0.0
    slug: str = ""
    condition_id: str = ""


class Event(BaseModel):
    """Событие на Polymarket (может содержать несколько рынков)."""

    id: str
    title: str
    slug: str = ""
    description: str = ""
    active: bool = True
    closed: bool = False
    markets: list[Market] = Field(default_factory=list)


class AIPrediction(BaseModel):
    """Результат анализа Claude."""

    market_id: str
    question: str
    ai_probability: float = Field(ge=0.0, le=1.0)
    market_probability: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    edge: float = 0.0  # ai_probability - market_probability
    reasoning: str = ""
    recommended_side: str = ""  # BUY_YES, BUY_NO, SKIP
    end_date: str = ""  # для проверки в risk manager
    timestamp: datetime = Field(default_factory=datetime.now)


class TradeSignal(BaseModel):
    """Сигнал на сделку."""

    market_id: str
    token_id: str
    side: str  # BUY / SELL
    price: float
    size_usd: float
    prediction: AIPrediction
    status: str = "pending"  # pending, executed, cancelled, failed


class Position(BaseModel):
    """Открытая позиция."""

    market_id: str
    token_id: str
    question: str
    entry_price: float
    size_usd: float
    current_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    side: str = "BUY"
    opened_at: datetime = Field(default_factory=datetime.now)
    # Extended info for dashboard
    end_date: str = ""
    slug: str = ""
    edge: float = 0.0
    confidence: float = 0.0
    ai_probability: float = 0.0
    reasoning: str = ""
    volume: float = 0.0
    liquidity: float = 0.0
