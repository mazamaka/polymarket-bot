"""Claude AI анализатор рынков предсказаний.

Использует:
- Extended thinking для глубокого анализа
- Web search (DuckDuckGo) для актуальных новостей
- Real-time цены крипто/акций
"""

import json
import logging
from datetime import datetime

import anthropic

from analyzer.prompts import (
    ANALYZE_MARKET_USER,
    BATCH_SCREEN_SYSTEM,
    BATCH_SCREEN_USER,
    SUPERFORECASTER_SYSTEM,
)
from config import settings
from polymarket.models import AIPrediction, Market
from utils.prices import PriceProvider
from utils.search import search_market_context

logger = logging.getLogger(__name__)


class ClaudeAnalyzer:
    """Анализирует рынки Polymarket с помощью Claude + Extended Thinking."""

    def __init__(self, use_thinking: bool = True) -> None:
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.claude_model
        self.prices = PriceProvider()
        self.use_thinking = use_thinking

    def analyze_market(self, market: Market) -> AIPrediction | None:
        """Глубокий анализ одного рынка с extended thinking + web search."""
        yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5
        no_price = (
            market.outcome_prices[1]
            if len(market.outcome_prices) > 1
            else 1 - yes_price
        )

        # Обогащаем контекст: цены + новости
        price_context = self.prices.enrich_market_context(market.question)
        news_context = search_market_context(market.question, max_results=5)

        user_prompt = ANALYZE_MARKET_USER.format(
            question=market.question,
            description=market.description[:2000],
            market_price_yes=yes_price,
            market_pct_yes=yes_price * 100,
            market_price_no=no_price,
            market_pct_no=no_price * 100,
            end_date=market.end_date,
            liquidity=market.liquidity,
            volume=market.volume,
            today=datetime.now().strftime("%Y-%m-%d"),
        )

        if price_context:
            user_prompt += "\n" + price_context
        if news_context:
            user_prompt += "\n\n" + news_context

        try:
            # Extended thinking для глубокого анализа
            if self.use_thinking:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    thinking={
                        "type": "enabled",
                        "budget_tokens": 8000,
                    },
                    messages=[
                        {
                            "role": "user",
                            "content": SUPERFORECASTER_SYSTEM + "\n\n" + user_prompt,
                        },
                    ],
                )
            else:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=1024,
                    system=SUPERFORECASTER_SYSTEM,
                    messages=[{"role": "user", "content": user_prompt}],
                )

            # Извлекаем текст (пропускаем thinking blocks)
            content = ""
            thinking_text = ""
            for block in response.content:
                if block.type == "thinking":
                    thinking_text = block.thinking
                elif block.type == "text":
                    content = block.text

            parsed = self._parse_json_response(content)
            if not parsed:
                return None

            ai_prob = float(parsed["probability"])
            confidence = float(parsed["confidence"])
            edge = ai_prob - yes_price

            if abs(edge) < settings.min_edge_threshold:
                recommended_side = "SKIP"
            elif edge > 0:
                recommended_side = "BUY_YES"
            else:
                recommended_side = "BUY_NO"

            reasoning = parsed.get("reasoning", "")
            # Добавляем ключевые моменты из thinking если есть
            if thinking_text and len(thinking_text) > 100:
                reasoning += f" [Thinking: {len(thinking_text)} tokens used]"

            prediction = AIPrediction(
                market_id=market.id,
                question=market.question,
                ai_probability=ai_prob,
                market_probability=yes_price,
                confidence=confidence,
                edge=edge,
                reasoning=reasoning,
                recommended_side=recommended_side,
            )

            has_news = " +news" if news_context else ""
            has_prices = " +price" if price_context else ""
            has_thinking = " +think" if thinking_text else ""
            logger.info(
                "Анализ: %s | AI: %.0f%% vs Market: %.0f%% | Edge: %+.0f%% | Conf: %.0f%% | %s%s%s%s",
                market.question[:50],
                ai_prob * 100,
                yes_price * 100,
                edge * 100,
                confidence * 100,
                recommended_side,
                has_news,
                has_prices,
                has_thinking,
            )

            return prediction

        except anthropic.APIError as e:
            logger.error("Claude API ошибка: %s", e)
            return None

    def batch_screen_markets(
        self, markets: list[Market], batch_size: int = 20
    ) -> list[dict]:
        """Быстрый скрининг рынков батчами (без thinking — экономим токены)."""
        interesting: list[dict] = []

        for i in range(0, len(markets), batch_size):
            batch = markets[i : i + batch_size]
            markets_text = self._format_markets_for_screening(batch)

            user_prompt = BATCH_SCREEN_USER.format(
                markets_list=markets_text,
                today=datetime.now().strftime("%Y-%m-%d"),
            )

            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=BATCH_SCREEN_SYSTEM,
                    messages=[{"role": "user", "content": user_prompt}],
                )

                content = response.content[0].text
                parsed = self._parse_json_response(content)
                if parsed and isinstance(parsed, list):
                    for item in parsed:
                        if item.get("worth_deeper_analysis"):
                            interesting.append(item)

                logger.info(
                    "Скрининг батча %d-%d: %d интересных из %d",
                    i,
                    i + len(batch),
                    len(
                        [
                            x
                            for x in (parsed or [])
                            if isinstance(x, dict) and x.get("worth_deeper_analysis")
                        ]
                    ),
                    len(batch),
                )

            except anthropic.APIError as e:
                logger.error("Claude API ошибка при скрининге: %s", e)

        return interesting

    def _format_markets_for_screening(self, markets: list[Market]) -> str:
        """Форматирование рынков для батч-скрининга."""
        lines = []
        for m in markets:
            yes_price = m.outcome_prices[0] if m.outcome_prices else 0.5
            line = (
                f"- ID: {m.id} | Q: {m.question} | YES: {yes_price:.2f} | "
                f"Vol: ${m.volume:,.0f} | Liq: ${m.liquidity:,.0f} | End: {m.end_date}"
            )
            price_ctx = self.prices.enrich_market_context(m.question)
            if price_ctx:
                line += f" | {price_ctx.strip()}"
            lines.append(line)
        return "\n".join(lines)

    def _parse_json_response(self, text: str) -> dict | list | None:
        """Извлечь JSON из ответа Claude."""
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            json_str = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            json_str = text[start:end].strip()
        else:
            json_str = text.strip()

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning("Не удалось распарсить JSON: %s...", text[:200])
            return None
