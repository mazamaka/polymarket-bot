# Polymarket AI Trading Bot

An autonomous AI-powered trading bot for [Polymarket](https://polymarket.com/) prediction markets. Uses Claude AI (Opus/Sonnet/Haiku) as the analytical engine with three distinct trading strategies: AI mispricing detection, weather arbitrage, and information arbitrage.

## Strategies

### 1. AI Mispricing Detection

Claude Opus analyzes prediction markets as a Superforecaster using 5 analytical frameworks. The pipeline:

1. **Screening** (Haiku) -- batch-evaluates ~15 markets at once (30s)
2. **Deep analysis** (Opus) -- single market with 5 frameworks (60-120s)
3. **Signal generation** -- if AI probability significantly differs from market price, open a position

Enriched with real-time context: web search (Tavily, Google News, DuckDuckGo), crypto/stock/forex prices, economic calendar, and earnings data.

### 2. Weather Arbitrage (ColdMath)

A quantitative strategy for weather prediction markets:

- **Open-Meteo ensemble API** (16 weather models) provides precise forecasts
- Direction-specific minimum edge thresholds (backtest-optimized on 12,776 historical markets)
- Maximum YES price caps per direction type to limit downside
- ~90%+ historical win rate on NO bets with strict entry criteria

### 3. Information Arbitrage

Reacts to breaking news faster than the market:

```
Gov RSS (2 min) --> News Intelligence --> EventBus --> SSE stream
                                                        |
                                                SSE Listener --> NewsMatcher (1ms)
                                                        |
                                                Article dedup --> Market cooldown (5 min)
                                                        |
                                                Rate limit (5/h) --> Claude Sonnet (10-30s)
                                                        |
                                                Risk check --> Open position
```

Target reaction time: 10-30 seconds (vs minutes for typical participants).

## Architecture

```
+------------------+     +------------------+     +------------------+
|  Polymarket APIs |     |   Claude Code    |     |  News Service    |
|  (Gamma + CLOB)  |     |   CLI (Max)      |     |  (SSE stream)   |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         v                        v                        v
+--------+---------+     +--------+---------+     +--------+---------+
|  Market Scanner  |     |    AI Analyzer   |     |  SSE Listener    |
|  (api.py)        |     |  (claude.py)     |     | (sse_listener.py)|
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         +------------+-----------+----------+-------------+
                      |                      |
                      v                      v
             +--------+---------+   +--------+---------+
             |  Risk Manager    |   | News Matcher      |
             |  (risk.py)       |   | (news_matcher.py) |
             +--------+---------+   +--------+---------+
                      |                      |
                      v                      v
             +--------+----------------------+---------+
             |           Trade Executor                |
             |  (live_executor.py / paper mode)        |
             +--------+-------------------------------+
                      |
                      v
             +--------+---------+
             |  Web Dashboard   |
             |  (FastAPI + WS)  |
             +-----------------+
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12, asyncio |
| AI Engine | Claude Code CLI (Max subscription) -- Opus, Sonnet, Haiku |
| Trading API | py-clob-client (Polymarket CLOB) |
| Market Data | Polymarket Gamma API, Data API |
| Weather Data | Open-Meteo Ensemble API (16 models) |
| Web Search | Tavily, Google News (pygooglenews), DuckDuckGo |
| Web Framework | FastAPI + WebSocket |
| Configuration | pydantic-settings (.env) |
| Containerization | Docker, Docker Compose |

## Quick Start

### Prerequisites

- Python 3.12+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with Max subscription
- Polygon wallet with USDC (for live trading)
- Docker & Docker Compose (for deployment)

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/polymarket-bot.git
cd polymarket-bot

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your settings (see Configuration section)
```

### Running locally

```bash
# Paper trading mode (default, no real money)
python main.py

# Access dashboard
open http://localhost:8899
```

### Running with Docker

```bash
# Make sure Claude credentials are at ~/.claude/.credentials.json
docker compose up -d --build

# View logs
docker compose logs -f polymarket-bot
```

## Configuration

All settings are managed via environment variables or `.env` file. Key parameters:

### API Keys

| Variable | Description |
|----------|-------------|
| `POLYGON_WALLET_PRIVATE_KEY` | Polygon wallet private key (for live trading) |
| `POLYGON_WALLET_ADDRESS` | EOA wallet address |
| `CLOB_PROXY_URL` | CLOB API proxy (for geo-restricted regions) |
| `BOT_API_KEY` | Dashboard authentication key |
| `TAVILY_API_KEY` | Tavily search API key (optional) |
| `NEWS_SERVICE_URL` | News Intelligence service URL (optional, for info arbitrage) |

### Risk Management

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_POSITION_PCT` | 5% | Max position size (% of balance) |
| `MAX_TOTAL_EXPOSURE_PCT` | 60% | Total portfolio exposure cap |
| `MIN_EDGE_THRESHOLD` | 8% | Minimum edge for AI markets |
| `MAX_EDGE_THRESHOLD` | 40% | Maximum edge (higher = likely AI error) |
| `STOP_LOSS_PCT` | 40% | Stop-loss for AI markets |
| `TAKE_PROFIT_PCT` | 50% | Take-profit for AI markets |
| `MAX_CONCURRENT_POSITIONS` | 35 | Max simultaneous positions (AI + weather) |
| `DEFAULT_TRADE_SIZE_USD` | $20 | Trade size for AI markets |
| `PAPER_TRADING` | true | Paper trading mode (no real money) |

### Weather Strategy

| Parameter | Default | Description |
|-----------|---------|-------------|
| `WEATHER_ENABLED` | true | Enable weather trading |
| `WEATHER_TRADE_SIZE_USD` | $3 | Trade size per weather market |
| `WEATHER_MAX_POSITIONS` | 25 | Max weather positions |
| `WEATHER_STOP_LOSS_PCT` | 50% | Stop-loss for weather markets |
| `WEATHER_TAKE_PROFIT_PCT` | 80% | Take-profit for weather markets |

### Information Arbitrage

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SSE_ENABLED` | true | Enable SSE news listener |
| `SSE_MIN_IMPORTANCE` | high | Minimum news importance level |
| `BREAKING_MIN_RELEVANCE` | 0.4 | Min relevance score for matching |
| `BREAKING_MAX_TRADES_PER_HOUR` | 5 | Rate limit for news-driven trades |

## Risk Management

The bot implements multiple layers of risk control:

- **Position sizing** -- each position capped at 5% of total balance
- **Total exposure limit** -- never exceed 60% of balance across all positions
- **Edge bounds** -- only trade when AI edge is between 8-40% (too high = likely error)
- **Confidence threshold** -- Claude must be at least 40% confident (50% for AI markets)
- **Stop-loss / Take-profit** -- automatic exit triggers per position
- **Separate budgets** -- AI and weather strategies have independent position limits
- **Rate limiting** -- max 5 breaking news trades per hour
- **Market cooldown** -- 5-minute cooldown per market after news-driven analysis
- **Article deduplication** -- prevents re-analyzing the same news (1h TTL)

## Project Structure

```
polymarket-bot/
├── config.py                  # Settings (pydantic-settings, .env)
├── claude_auth.py             # OAuth token management for Claude CLI
├── main.py                    # Entry point (CLI)
├── analyzer/
│   ├── claude.py              # Claude AI: screen (Haiku), analyze (Opus), rapid_reanalyze (Sonnet)
│   ├── prompts.py             # Superforecaster prompts (5 frameworks)
│   ├── weather.py             # Weather market analyzer (Open-Meteo ensemble)
│   └── correlations.py        # Market correlation analysis
├── polymarket/
│   ├── api.py                 # Gamma API + CLOB client wrapper
│   └── models.py              # Pydantic models (Market, AIPrediction, Position, TradeSignal)
├── services/
│   ├── sse_listener.py        # SSE client for News Intelligence (breaking news)
│   └── news_matcher.py        # Keyword index for matching news to markets
├── trader/
│   ├── risk.py                # Risk management (edge, confidence, exposure checks)
│   ├── storage.py             # Portfolio storage (JSON)
│   ├── live_executor.py       # CLOB API order execution (live trading)
│   ├── monitor.py             # Price monitor + stop-loss/take-profit
│   ├── signals_history.py     # Signal history tracking
│   ├── scan_log.py            # Scan results logging
│   └── proxy_patch.py         # CLOB API proxy support
├── utils/
│   ├── prices.py              # Real-time prices (crypto, stocks, forex)
│   └── search.py              # Web search + News Service context
├── web/
│   └── app.py                 # FastAPI dashboard (WebSocket, scheduling)
├── scripts/
│   ├── backtest_weather.py    # Weather strategy backtester
│   └── backtest_report.py     # Backtest report generator
└── tests/
    └── test_pnl.py
```

## Disclaimer

This software is provided for **educational and research purposes only**. Trading on prediction markets involves significant financial risk.

- This is not financial advice
- Past performance does not guarantee future results
- Use at your own risk
- Always start with paper trading mode before risking real funds
- Ensure compliance with your local regulations regarding prediction markets

## License

This project is licensed under the MIT License -- see the [LICENSE](LICENSE) file for details.
