from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API Keys
    polygon_wallet_private_key: str = ""

    # Polymarket API
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    polygon_chain_id: int = 137

    # Risk Management
    max_position_pct: float = 0.05  # 5% баланса на 1 рынок
    max_total_exposure_pct: float = (
        0.60  # 60% общая экспозиция (увеличена: weather + AI раздельные бюджеты)
    )
    min_edge_threshold: float = (
        0.08  # 8% минимальный edge для AI (было 5% — слишком много skip)
    )
    max_edge_threshold: float = 0.40  # 40% макс edge (больше = AI ошибается)
    stop_loss_pct: float = 0.40  # -40% stop-loss (default, used for AI markets)
    take_profit_pct: float = 0.50  # +50% take-profit (default, used for AI markets)

    # Weather-specific SL/TP (binary markets, ~90%+ win rate → hold longer)
    weather_stop_loss_pct: float = (
        0.50  # -50% SL (wider: weather markets recover often)
    )
    weather_take_profit_pct: float = (
        0.80  # +80% TP (let winners run closer to resolution)
    )
    max_concurrent_positions: int = 35  # Общий лимит (AI + weather)
    min_confidence: float = (
        0.40  # 40% мин. уверенность Claude (было 30% — пускал мусор)
    )
    min_liquidity_usd: float = 300.0  # мин. ликвидность (снижена для охвата)
    ai_min_confidence: float = 0.50  # AI-specific: минимум 50% confidence
    ai_max_positions: int = 10  # AI-specific: макс позиций
    ai_require_end_date: bool = True  # AI: не входить в рынки без end_date
    ai_max_hours_to_resolution: float = 168.0  # AI: макс 7 дней до закрытия

    # Timing
    max_hours_to_resolution: float = (
        168.0  # 7 дней макс. — быстрый оборот для статистики
    )
    min_hours_to_resolution: float = 0.0  # без ограничения снизу

    # Weather Analyzer
    weather_enabled: bool = True
    weather_min_edge: float = 0.08  # 8% мин. edge для погодных рынков (fallback)
    weather_max_days_ahead: int = 16  # макс. дней вперёд (лимит ensemble API)
    weather_min_liquidity: float = 100.0  # мин. ликвидность для погодных рынков
    weather_trade_size_usd: float = 3.0  # размер ставки на погодный рынок ($3)
    weather_max_positions: int = (
        25  # макс. одновременных weather позиций (было 15 — упирались в лимит)
    )

    # Backtest-optimized: direction-specific min edge (12,776 markets)
    # "below" = 94.8% NO rate, "above" = 84.8%, "exactly" = 87.5%, "between" = 84.4%
    weather_direction_min_edge: dict[str, float] = {
        "below": 0.06,  # highest NO rate → lower edge threshold
        "above": 0.06,
        "exactly": 0.10,  # higher risk, need more edge
        "between": 0.12,  # lowest NO rate → strictest threshold
    }

    # Backtest-optimized: max YES price per direction
    # When YES price is too high, potential loss on wrong bet outweighs profit
    weather_max_yes_price: dict[str, float] = {
        "below": 0.25,
        "above": 0.25,
        "exactly": 0.12,  # strict — avg loss $0.60-0.80 on YES resolve
        "between": 0.10,  # strictest — 15.6% YES resolution rate
    }

    # News Intelligence Service
    news_service_url: str = "https://news.maxbob.xyz"

    # SSE Listener (breaking news)
    sse_enabled: bool = True
    sse_min_importance: str = "high"
    sse_reconnect_max_seconds: int = 30

    # Breaking news trading
    breaking_min_relevance: float = 0.4
    breaking_reanalyze_model: str = "sonnet"
    breaking_reanalyze_timeout: int = 60
    breaking_max_trades_per_hour: int = 5
    breaking_market_refresh_minutes: int = 5

    # Trading
    default_trade_size_usd: float = 20.0  # размер сделки (AI + live)
    paper_trading: bool = True  # paper trading по умолчанию

    # Proxy for CLOB API (geo-block bypass, Canada residential)
    clob_proxy_url: str = ""
    polygon_wallet_address: str = ""  # EOA wallet (funder for sig_type=2)

    # Dashboard auth
    bot_api_key: str = ""  # API key for dashboard access

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
