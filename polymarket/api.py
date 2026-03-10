"""Клиент для Polymarket Gamma API и CLOB API."""

import json
import logging

import httpx

from config import settings
from polymarket.models import Event, Market

logger = logging.getLogger(__name__)


class PolymarketAPI:
    """Обёртка над Gamma API для получения рынков и событий."""

    def __init__(self) -> None:
        self.gamma_url = settings.gamma_api_url
        self.client = httpx.Client(timeout=30.0)

    def get_active_markets(
        self, limit: int = 100, max_markets: int = 500, sort_by: str = "liquidity"
    ) -> list[Market]:
        """Получить активные рынки, отсортированные по ликвидности/объёму.

        Args:
            limit: размер батча для API запроса
            max_markets: максимум рынков для загрузки (не грузить все 18000+)
            sort_by: сортировка — 'liquidity' или 'volume'
        """
        all_markets: list[Market] = []
        offset = 0

        while len(all_markets) < max_markets:
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": limit,
                "offset": offset,
                "order": sort_by,
                "ascending": "false",
            }
            resp = self.client.get(f"{self.gamma_url}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()

            if not data:
                break

            for raw in data:
                market = self._parse_market(raw)
                if market:
                    all_markets.append(market)

            if len(data) < limit:
                break
            offset += limit

        all_markets = all_markets[:max_markets]
        logger.info(
            "Получено %d активных рынков (top by %s)", len(all_markets), sort_by
        )
        return all_markets

    def get_active_events(self, limit: int = 100) -> list[Event]:
        """Получить активные события."""
        all_events: list[Event] = []
        offset = 0

        while True:
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": limit,
                "offset": offset,
            }
            resp = self.client.get(f"{self.gamma_url}/events", params=params)
            resp.raise_for_status()
            data = resp.json()

            if not data:
                break

            for raw in data:
                event = self._parse_event(raw)
                if event:
                    all_events.append(event)

            if len(data) < limit:
                break
            offset += limit

        logger.info("Получено %d активных событий", len(all_events))
        return all_events

    def get_market_by_id(self, market_id: str) -> Market | None:
        """Получить конкретный рынок по ID."""
        resp = self.client.get(f"{self.gamma_url}/markets/{market_id}")
        if resp.status_code == 200:
            return self._parse_market(resp.json())
        return None

    def filter_tradeable_markets(
        self, markets: list[Market], min_liquidity: float | None = None
    ) -> list[Market]:
        """Отфильтровать рынки по ликвидности и активности."""
        min_liq = min_liquidity or settings.min_liquidity_usd
        tradeable = []
        for m in markets:
            if (
                m.active
                and not m.closed
                and m.liquidity >= min_liq
                and m.clob_token_ids
            ):
                tradeable.append(m)

        logger.info(
            "Отфильтровано %d торгуемых рынков (мин. ликвидность: $%.0f)",
            len(tradeable),
            min_liq,
        )
        return tradeable

    def _parse_market(self, raw: dict) -> Market | None:
        """Парсинг сырых данных рынка в модель."""
        try:
            outcome_prices_raw = raw.get("outcomePrices", "[]")
            if isinstance(outcome_prices_raw, str):
                outcome_prices = [float(p) for p in json.loads(outcome_prices_raw)]
            else:
                outcome_prices = [float(p) for p in outcome_prices_raw]

            clob_ids_raw = raw.get("clobTokenIds", "[]")
            if isinstance(clob_ids_raw, str):
                clob_token_ids = json.loads(clob_ids_raw)
            else:
                clob_token_ids = clob_ids_raw or []

            outcomes_raw = raw.get("outcomes", '["Yes", "No"]')
            if isinstance(outcomes_raw, str):
                outcomes = json.loads(outcomes_raw)
            else:
                outcomes = outcomes_raw

            return Market(
                id=str(raw.get("id", "")),
                question=raw.get("question", ""),
                description=raw.get("description", ""),
                end_date=raw.get("endDate", ""),
                active=raw.get("active", False),
                closed=raw.get("closed", True),
                outcomes=outcomes,
                outcome_prices=outcome_prices,
                clob_token_ids=clob_token_ids,
                volume=float(raw.get("volume", 0) or 0),
                liquidity=float(raw.get("liquidity", 0) or 0),
                spread=float(raw.get("spread", 0) or 0),
                slug=raw.get("slug", ""),
                condition_id=raw.get("conditionId", ""),
            )
        except Exception as e:
            logger.debug("Ошибка парсинга рынка %s: %s", raw.get("id"), e)
            return None

    def _parse_event(self, raw: dict) -> Event | None:
        """Парсинг сырых данных события в модель."""
        try:
            markets = []
            for m in raw.get("markets", []):
                market = self._parse_market(m)
                if market:
                    markets.append(market)

            return Event(
                id=str(raw.get("id", "")),
                title=raw.get("title", ""),
                slug=raw.get("slug", ""),
                description=raw.get("description", ""),
                active=raw.get("active", False),
                closed=raw.get("closed", True),
                markets=markets,
            )
        except Exception as e:
            logger.debug("Ошибка парсинга события %s: %s", raw.get("id"), e)
            return None

    def close(self) -> None:
        self.client.close()
