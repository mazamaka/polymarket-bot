from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # API Keys
    anthropic_api_key: str = ""
    polygon_wallet_private_key: str = ""

    # Polymarket API
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    polygon_chain_id: int = 137

    # Claude model
    claude_model: str = "claude-sonnet-4-20250514"

    # Risk Management
    max_position_pct: float = 0.05  # 5% баланса на 1 рынок
    max_total_exposure_pct: float = 0.30  # 30% общая экспозиция
    min_edge_threshold: float = 0.08  # 8% минимальный edge
    stop_loss_pct: float = 0.30  # -30% stop-loss
    max_concurrent_positions: int = 20
    min_confidence: float = 0.30  # мин. уверенность Claude (paper trading)
    min_liquidity_usd: float = 500.0  # мин. ликвидность (для paper trading достаточно)

    # Timing
    max_hours_to_resolution: float = 48.0  # макс. часов до закрытия рынка
    min_hours_to_resolution: float = 1.0  # мин. часов (слишком близко — опасно)

    # Trading
    default_trade_size_usd: float = 5.0  # размер сделки для Фазы 2
    paper_trading: bool = True  # paper trading по умолчанию

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
