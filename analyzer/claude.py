"""Claude AI анализатор рынков предсказаний.

Использует Claude Code CLI (подписка Max) вместо прямого API.
- Haiku для быстрого скрининга, Sonnet для глубокого анализа
- Параллельные subprocess вызовы для ускорения
- Web search (DuckDuckGo) для актуальных новостей
- Real-time цены крипто/акций
"""

import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

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

_CLAUDE_ENV: dict[str, str] | None = None


def _get_clean_env() -> dict[str, str]:
    """Env без CLAUDECODE (чтобы не было nested session error)."""
    global _CLAUDE_ENV
    if _CLAUDE_ENV is None:
        _CLAUDE_ENV = os.environ.copy()
        _CLAUDE_ENV.pop("CLAUDECODE", None)
    return _CLAUDE_ENV


def _call_claude(
    prompt: str,
    model: str = "sonnet",
    timeout: int = 120,
) -> str:
    """Вызов Claude через CLI (использует подписку Claude Code Max)."""
    cmd = [
        "claude",
        "-p",
        "--output-format",
        "text",
        "--model",
        model,
        "--permission-mode",
        "bypassPermissions",
        "--no-session-persistence",
    ]

    result = subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_get_clean_env(),
    )

    if result.returncode != 0:
        logger.error(
            "Claude CLI error (code %d): %s", result.returncode, result.stderr[:200]
        )
        return ""

    return result.stdout.strip()


class ClaudeAnalyzer:
    """Анализирует рынки Polymarket с помощью Claude Code CLI."""

    def __init__(self, use_thinking: bool = True) -> None:
        self.model = "opus"  # глубокий анализ — максимальное качество
        self.screen_model = "sonnet"  # скрининг — быстро и умно
        self.prices = PriceProvider()
        self.use_thinking = use_thinking

    def analyze_market(self, market: Market) -> AIPrediction | None:
        """Глубокий анализ одного рынка."""
        yes_price = market.outcome_prices[0] if market.outcome_prices else 0.5
        no_price = (
            market.outcome_prices[1]
            if len(market.outcome_prices) > 1
            else 1 - yes_price
        )

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

        full_prompt = SUPERFORECASTER_SYSTEM + "\n\n" + user_prompt

        try:
            content = _call_claude(full_prompt, model=self.model, timeout=120)
            if not content:
                return None

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
            logger.info(
                "Анализ: %s | AI: %.0f%% vs Market: %.0f%% | Edge: %+.0f%% | Conf: %.0f%% | %s%s%s",
                market.question[:50],
                ai_prob * 100,
                yes_price * 100,
                edge * 100,
                confidence * 100,
                recommended_side,
                has_news,
                has_prices,
            )

            return prediction

        except subprocess.TimeoutExpired:
            logger.error("Claude CLI timeout для %s", market.question[:50])
            return None

    def analyze_markets_parallel(
        self, markets: list[Market], max_workers: int = 3
    ) -> list[AIPrediction]:
        """Параллельный анализ нескольких рынков."""
        predictions: list[AIPrediction] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self.analyze_market, m): m for m in markets}
            for future in as_completed(futures):
                market = futures[future]
                try:
                    result = future.result()
                    if result:
                        predictions.append(result)
                except Exception as e:
                    logger.error("Ошибка анализа %s: %s", market.question[:40], e)
        return predictions

    def batch_screen_markets(
        self, markets: list[Market], batch_size: int = 30
    ) -> list[dict]:
        """Быстрый скрининг рынков батчами (haiku — быстро и дёшево)."""
        interesting: list[dict] = []

        for i in range(0, len(markets), batch_size):
            batch = markets[i : i + batch_size]
            markets_text = self._format_markets_for_screening(batch)
            user_prompt = BATCH_SCREEN_USER.format(
                markets_list=markets_text,
                today=datetime.now().strftime("%Y-%m-%d"),
            )
            full_prompt = BATCH_SCREEN_SYSTEM + "\n\n" + user_prompt

            try:
                content = _call_claude(
                    full_prompt, model=self.screen_model, timeout=120
                )
                if not content:
                    continue
                parsed = self._parse_json_response(content)
                if parsed and isinstance(parsed, list):
                    found = [x for x in parsed if x.get("worth_deeper_analysis")]
                    interesting.extend(found)
                    logger.info(
                        "Скрининг батча %d-%d: %d интересных из %d",
                        i,
                        i + len(batch),
                        len(found),
                        len(batch),
                    )
            except subprocess.TimeoutExpired:
                logger.error("Скрининг батча %d timeout", i)

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
