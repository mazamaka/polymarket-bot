# Polymarket AI Trading Bot

## Концепция

AI-бот для торговли на Polymarket (рынок предсказаний на Polygon).
Стратегия: **Information Edge / Mispricing Detection**.

Бот ищет рынки, где текущая цена (вероятность) сильно отклоняется от реальной
вероятности события, оцененной Claude AI на основе анализа данных.

### Как работает

```
1. Сканирование → получаем активные рынки с Gamma API
2. Фильтрация  → отбираем рынки с достаточной ликвидностью
3. Анализ       → Claude оценивает реальную P(event) как Superforecaster
4. Поиск edge   → сравниваем AI-оценку с рыночной ценой
5. Решение      → если edge > порог → генерируем трейд
6. Исполнение   → размещаем ордер через CLOB API
7. Мониторинг   → отслеживаем позиции, P&L, stop-loss
```

### Стратегия (Event Mispricing)

- НЕ HFT, НЕ арбитраж — информационное преимущество
- Claude анализирует: новости, исторические данные, base rates
- Если рынок даёт 28%, а Claude оценивает 55% → покупаем YES по 0.28
- Когда рынок корректируется к реальности → продаём с прибылью
- Целевые рынки: политика, крипто-регуляция, макро-события

### Фазы разработки

| Фаза | Описание | Риск |
|------|----------|------|
| 0 | Read-only: анализ рынков, без торговли | Нулевой |
| 1 | Paper trading: симуляция сделок | Нулевой |
| 2 | Micro trading: реальные сделки $1-5 | Минимальный |
| 3 | Scale up: увеличение размеров | Контролируемый |

## Технологии

- **Python 3.12** + asyncio
- **anthropic SDK** — Claude для анализа вероятностей
- **py-clob-client** — Polymarket CLOB API
- **httpx** — HTTP клиент для Gamma API
- **pydantic** — модели данных

## Структура

```
polymarket-bot/
├── CLAUDE.md
├── .env                    # ANTHROPIC_API_KEY, POLYGON_WALLET_PRIVATE_KEY
├── config.py               # Настройки, лимиты, risk parameters
├── main.py                 # Entry point
├── polymarket/
│   ├── __init__.py
│   ├── api.py              # Gamma API + CLOB client wrapper
│   └── models.py           # Pydantic модели (Event, Market, Trade)
├── analyzer/
│   ├── __init__.py
│   ├── claude.py           # Claude AI анализ вероятностей
│   └── prompts.py          # Superforecaster промпты
├── trader/
│   ├── __init__.py
│   ├── executor.py         # Исполнение ордеров
│   └── risk.py             # Risk management
└── utils/
    ├── __init__.py
    └── news.py             # Сбор данных из новостных API
```

## Risk Management

- **Max position size**: 5% от баланса на 1 рынок
- **Max total exposure**: 30% от баланса
- **Min edge threshold**: 15% (разница AI vs Market)
- **Stop-loss**: -30% от позиции
- **Max concurrent positions**: 10
- **Confidence threshold**: Claude должен быть уверен >= 0.7

## API Reference

- Gamma API: `https://gamma-api.polymarket.com`
- CLOB API: `https://clob.polymarket.com`
- Polygon Chain ID: 137
- Docs: `https://docs.polymarket.com`

## Полезные репозитории

- `~/PycharmProjects/polymarket-agents/` — официальный Polymarket/agents (для справки)
- `https://github.com/Polymarket/py-clob-client` — Python CLOB клиент
- `https://github.com/discountry/polymarket-trading-bot` — пример бота с WebSocket

## Запуск

```bash
cd ~/PycharmProjects/polymarket-bot
source .venv/bin/activate
python main.py              # Фаза 0: только анализ
python main.py --paper      # Фаза 1: paper trading
python main.py --live       # Фаза 2+: реальная торговля (ОСТОРОЖНО!)
```
