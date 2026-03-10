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
    max_total_exposure_pct: float = 0.30  # 30% общая экспозиция
    min_edge_threshold: float = 0.08  # 8% минимальный edge
    max_edge_threshold: float = 0.35  # 35% макс edge (больше = AI ошибается)
    stop_loss_pct: float = 0.30  # -30% stop-loss
    take_profit_pct: float = 0.20  # +20% take-profit
    max_concurrent_positions: int = 20
    min_confidence: float = 0.40  # мин. уверенность Claude
    min_liquidity_usd: float = 500.0  # мин. ликвидность (для paper trading достаточно)

    # Timing
    max_hours_to_resolution: float = 168.0  # 7 дней макс. до закрытия рынка
    min_hours_to_resolution: float = 0.0  # без ограничения снизу (paper trading)

    # Trading
    default_trade_size_usd: float = 5.0  # размер сделки для Фазы 2
    paper_trading: bool = True  # paper trading по умолчанию

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
