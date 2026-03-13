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
from utils.search import (
    fetch_news_service_context,
    format_economic_events,
    search_market_context,
)

logger = logging.getLogger(__name__)

_CLAUDE_ENV: dict[str, str] | None = None


def _fetch_breaking_news(hours: int = 12, limit: int = 10) -> str:
    """Получить свежие breaking news из News Service для контекста скрининга."""
    try:
        import httpx

        resp = httpx.get(
            f"{settings.news_service_url}/api/v1/articles",
            params={"category": "breaking", "hours": hours, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json()
        if not articles:
            return ""
        lines = ["**Breaking News (last 12h):**"]
        for a in articles:
            title = a.get("title", "")
            summary = a.get("summary", "")
            if summary and len(summary) > 150:
                summary = summary[:150] + "..."
            date = a.get("published_at", "")[:16]
            if summary:
                lines.append(f"- [{date}] {title}: {summary}")
            else:
                lines.append(f"- [{date}] {title}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("Breaking news fetch error: %s", e)
        return ""


def _get_clean_env() -> dict[str, str]:
    """Env без CLAUDECODE (чтобы не было nested session error)."""
    global _CLAUDE_ENV
    if _CLAUDE_ENV is None:
        _CLAUDE_ENV = os.environ.copy()
        _CLAUDE_ENV.pop("CLAUDECODE", None)
        _CLAUDE_ENV.setdefault("HOME", os.path.expanduser("~"))
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
        err = result.stderr[:200] or result.stdout[:200]
        logger.error("Claude CLI error (code %d): %s", result.returncode, err)
        return ""

    return result.stdout.strip()


class ClaudeAnalyzer:
    """Анализирует рынки Polymarket с помощью Claude Code CLI."""

    def __init__(self) -> None:
        self.model = "opus"  # глубокий анализ — максимальное качество
        self.screen_model = "haiku"  # скрининг — быстрый первичный отбор
        self.prices = PriceProvider()

    def close(self) -> None:
        """Закрыть HTTP клиенты."""
        self.prices.close()

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
            content = _call_claude(full_prompt, model=self.model, timeout=300)
            if not content:
                return None

            parsed = self._parse_json_response(content)
            if not parsed:
                return None

            ai_prob = max(0.0, min(1.0, float(parsed.get("probability", 0.5))))
            confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.3))))
            edge = ai_prob - yes_price
            spread = max(0.0, min(1.0, float(parsed.get("framework_spread", 0))))

            # Снижаем confidence при очень высоком разбросе фреймворков
            if spread > 0.30 and confidence > 0.5:
                confidence = min(confidence, 0.40)

            if abs(edge) < settings.min_edge_threshold:
                recommended_side = "SKIP"
            elif edge > 0:
                recommended_side = "BUY_YES"
            else:
                recommended_side = "BUY_NO"

            # Собираем reasoning из frameworks + summary
            reasoning = parsed.get("reasoning", "")
            frameworks = parsed.get("frameworks", {})
            if frameworks:
                parts = []
                for name, data in frameworks.items():
                    if isinstance(data, dict) and "probability" in data:
                        parts.append(f"{name}: {data['probability']:.0%}")
                if parts:
                    reasoning = f"[{', '.join(parts)}] {reasoning}"

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
                "Анализ: %s | AI: %.0f%% vs Market: %.0f%% | Edge: %+.0f%% | Conf: %.0f%% | Spread: %.0f%% | %s%s%s",
                market.question[:50],
                ai_prob * 100,
                yes_price * 100,
                edge * 100,
                confidence * 100,
                spread * 100,
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
        self, markets: list[Market], batch_size: int = 15
    ) -> list[dict]:
        """Быстрый скрининг рынков батчами."""
        interesting: list[dict] = []

        # Глобальный контекст — один раз на весь скрининг
        global_context = ""
        try:
            news_data = fetch_news_service_context("economic events today")
            econ = format_economic_events(news_data, max_events=15)
            if econ:
                global_context += econ
            # Breaking news для общей картины
            breaking = _fetch_breaking_news(hours=12, limit=10)
            if breaking:
                global_context += "\n\n" + breaking
        except Exception as e:
            logger.debug("News Service для скрининга: %s", e)

        for i in range(0, len(markets), batch_size):
            batch = markets[i : i + batch_size]
            markets_text = self._format_markets_for_screening(batch)
            user_prompt = BATCH_SCREEN_USER.format(
                markets_list=markets_text,
                today=datetime.now().strftime("%Y-%m-%d"),
            )
            if global_context:
                user_prompt += "\n\n" + global_context
            full_prompt = BATCH_SCREEN_SYSTEM + "\n\n" + user_prompt

            try:
                content = _call_claude(
                    full_prompt, model=self.screen_model, timeout=180
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
            vol_liq = m.volume / m.liquidity if m.liquidity > 0 else 0
            line = (
                f"- ID: {m.id} | Q: {m.question} | YES: {yes_price:.2f} | "
                f"Vol: ${m.volume:,.0f} | Liq: ${m.liquidity:,.0f} | "
                f"V/L: {vol_liq:.1f} | End: {m.end_date}"
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
