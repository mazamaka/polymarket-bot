# Polymarket AI Trading Bot

## Концепция

AI-бот для торговли на Polymarket (рынок предсказаний на Polygon).
Две стратегии: **AI Mispricing Detection** + **Information Arbitrage**.

### Стратегия 1: Event Mispricing (AI анализ)

- Claude Opus анализирует рынки как Superforecaster (5 фреймворков)
- Haiku для скрининга батчами → Opus для глубокого анализа
- Если AI-оценка сильно отличается от рыночной цены → открываем позицию
- Web search (DuckDuckGo) + real-time цены крипто/акций для контекста

### Стратегия 2: Information Arbitrage (breaking news)

- Получаем breaking news из гос. источников БЫСТРЕЕ чем рынок реагирует
- News Intelligence → SSE push → keyword matching → Claude Sonnet rapid re-analysis
- Реакция за 10-30 секунд (vs минуты для обычных участников)

```
Gov RSS (2 min) → News Intelligence → EventBus → SSE stream
                                                      ↓
                                              SSE Listener → NewsMatcher (1ms)
                                                      ↓
                                              Article dedup → Market cooldown (5 min)
                                                      ↓
                                              Rate limit (5/h) → Claude Sonnet (10-30s)
                                                      ↓
                                              Risk check → Open position
```

## Технологии

- **Python 3.12** + asyncio
- **Claude Code CLI** (подписка Max) — Opus/Sonnet/Haiku через subprocess
- **py-clob-client** — Polymarket CLOB API (live trading)
- **httpx** — HTTP клиент (Gamma API, SSE stream)
- **FastAPI** — web dashboard + API
- **pydantic-settings** — конфигурация

## Структура

```
polymarket-bot/
├── CLAUDE.md
├── config.py                  # Настройки (pydantic-settings, .env)
├── claude_auth.py             # OAuth token management для Claude CLI
├── main.py                    # Entry point (CLI)
├── analyzer/
│   ├── claude.py              # Claude AI: screen (Haiku), analyze (Opus), rapid_reanalyze (Sonnet)
│   ├── prompts.py             # Superforecaster промпты (5 frameworks)
│   ├── weather.py             # Weather market analyzer (Open-Meteo ensemble)
│   └── correlations.py        # Market correlation analysis
├── polymarket/
│   ├── api.py                 # Gamma API + CLOB client wrapper
│   └── models.py              # Pydantic модели (Market, AIPrediction, Position, TradeSignal)
├── services/
│   ├── sse_listener.py        # SSE клиент для News Intelligence (breaking news)
│   └── news_matcher.py        # Keyword index для матчинга новостей → рынков
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
│   └── search.py              # DuckDuckGo search + News Service context
├── web/
│   └── app.py                 # FastAPI dashboard (WebSocket, scheduling, SSE integration)
├── scripts/
│   ├── backtest_weather.py    # Weather strategy backtester
│   └── backtest_report.py     # Backtest report generator
└── tests/
    └── test_pnl.py
```

## Claude CLI вызовы

Бот использует Claude Code CLI (подписка Max), НЕ прямой API:

```python
# analyzer/claude.py — _call_claude()
cmd = ["claude", "-p", "--output-format", "text", "--model", model,
       "--permission-mode", "bypassPermissions", "--no-session-persistence"]
subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
```

- **Haiku** — batch screening (15 рынков за раз, 30s)
- **Opus** — deep analysis (1 рынок, 5 frameworks, 60-120s)
- **Sonnet** — rapid re-analysis для breaking news (10-30s)

Токен: `claude_auth.py` → `~/.claude/.credentials.json` (OAuth, auto-refresh)

## SSE Listener (Information Arbitrage)

```python
# services/sse_listener.py
SSEListener(on_breaking_match=callback, on_log=logger)
```

- Подключается к `news.maxbob.xyz/api/v1/stream?importance=high`
- Auto-reconnect с exponential backoff (1s → 30s)
- Article deduplication (1h TTL)
- Market cooldown (5 min — не ре-анализировать тот же рынок)
- Rate limit: max 5 breaking trades/hour
- Market refresh: каждые 5 мин из Gamma API

## Risk Management

| Параметр | Значение | Описание |
|----------|----------|----------|
| `max_position_pct` | 5% | Макс. размер позиции от баланса |
| `max_total_exposure_pct` | 60% | Общая экспозиция (AI + weather) |
| `min_edge_threshold` | 8% | Мин. edge для AI рынков |
| `max_edge_threshold` | 40% | Макс. edge (больше = AI ошибается) |
| `stop_loss_pct` | 40% | Stop-loss (AI markets) |
| `take_profit_pct` | 50% | Take-profit (AI markets) |
| `weather_stop_loss_pct` | 50% | Stop-loss (weather markets) |
| `weather_take_profit_pct` | 80% | Take-profit (weather markets) |
| `max_concurrent_positions` | 35 | AI (10) + Weather (25) |
| `min_confidence` | 40% | Мин. уверенность Claude |
| `ai_min_confidence` | 50% | AI-specific minimum |
| `default_trade_size_usd` | $20 | Размер сделки (AI) |
| `weather_trade_size_usd` | $3 | Размер ставки (weather) |

## Weather Trading

Отдельная стратегия на погодных рынках:
- **Open-Meteo ensemble API** (16 моделей) → точный прогноз
- Direction-specific min edge: below=6%, above=6%, exactly=10%, between=12%
- Max YES price по направлению: below/above=0.25, exactly=0.12, between=0.10
- Backtest-optimized на 12,776 исторических рынках

## Web Dashboard

- **URL**: https://poly.maxbob.xyz/ (production)
- **Порт**: 8899
- **Auth**: basic auth middleware
- **WebSocket**: real-time обновления portfolio, logs
- **API endpoints**:
  - `GET /api/portfolio` — портфолио (paper или live)
  - `GET /api/sse/status` — статус SSE listener
  - `GET /api/scheduler/status` — статус scheduler (monitor, trading, sse)
  - `POST /api/scheduler/start|stop` — управление (включая SSE listener)
  - `POST /api/run-paper` — запуск paper trading scan
  - `GET /api/live-positions` — реальные позиции из Data API
  - `POST /api/sell` — продажа позиции (live mode)
  - `GET /api/live-orders` — открытые ордера
  - `POST /api/cancel-order` — отмена ордера
  - `GET /api/settings` / `POST /api/settings` — настройки

## ColdMath Weather Bot

Отдельный сервис внутри этого репо — погодный арбитражный бот.

- **URL**: https://coldmath.maxbob.xyz/
- **Порт**: 8866
- **Контейнер**: `coldmath-bot`
- **Docker**: `docker-compose.coldmath.yml` + `Dockerfile.coldmath`
- **Основной файл**: `coldmath_bot.py`
- **Auth**: admin / coldmath (HTTP Basic)
- **Mode**: `BOT_MODE=live` (автостарт при запуске контейнера)

### Auto-Redeem (poly-web3)

Автоматическое получение выигрышей через Polymarket Builder API:
- Библиотека: `poly-web3` (`PolyWeb3Service.redeem_all()`)
- Builder profile: `MaxBobWeatherBot`
- Credentials: `BUILDER_KEY`, `BUILDER_SECRET`, `BUILDER_PASSPHRASE` в `.env`
- Вызывается после каждого скана в live режиме + кнопка "Redeem All" в дашборде

### Dashboard

Polymarket-style dark theme:
- Portfolio/Cash (on-chain USDC) / P&L / Win Rate / Signals / Edge
- Позиции с live ценами, P&L, Today/Tomorrow
- API: `/api/status`, `/api/scan`, `/api/redeem`, `/api/start`, `/api/stop`, `/api/settings`

### Архитектура coldmath_bot.py

- `_get_w3()` — cached Web3 singleton
- `_get_usdc_balance()` — on-chain USDC баланс
- `_start_bot_loop()` — единая функция для autostart/manual start
- `_create_redeem_service()` — lazy init PolyWeb3Service
- Data API `/positions` — live цены, P&L, portfolio value (один запрос)
- Balance check перед торговлей использует on-chain USDC, не Data API

## Деплой

- **Polymarket Bot**: https://poly.maxbob.xyz/ (контейнер `polymarket-bot`)
- **ColdMath Bot**: https://coldmath.maxbob.xyz/ (контейнер `coldmath-bot`)
- **Сервер**: 94.156.232.242 (admin)
- **Путь**: `/opt/polymarket-bot/`
- **Обновление polymarket-bot**: `git pull && docker compose up -d --build`
- **Обновление coldmath**: `git pull && docker compose -f docker-compose.coldmath.yml up -d --build`
- **Claude credentials**: `~/.claude/.credentials.json` монтируется в polymarket-bot

## Git

- GitHub: `github.com/mazamaka321-rgb/polymarket-bot` (origin)
- Ветка: `main`

## Зависимости от других сервисов

- **News Intelligence** (`news.maxbob.xyz`) — SSE stream для breaking news, market context, economic calendar
- **Open-Meteo API** — погодные прогнозы для weather trading (ensemble: GFS, ECMWF, ICON, GEM)
- **NWS API** (`api.weather.gov`) — cross-reference для US городов
- **Polymarket Gamma API** — список рынков, цены
- **Polymarket CLOB API** — исполнение ордеров (live mode)
- **Polymarket Data API** — реальные позиции кошелька, live цены
- **Polymarket Builder Relayer** — auto-redeem выигрышей (gasless)
- **Polygon RPC** (`polygon-bor.publicnode.com`) — on-chain USDC баланс
