"""Web search для обогащения контекста анализа рынков."""

import logging
import os

from ddgs import DDGS

logger = logging.getLogger(__name__)


def search_market_context(question: str, max_results: int = 5) -> str:
    """Поиск актуальных новостей по теме рынка через DuckDuckGo (бесплатно).

    Возвращает сформатированный контекст для промпта.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.news(question, max_results=max_results, timelimit="w"))

        if not results:
            # Фоллбэк на обычный поиск
            with DDGS() as ddgs:
                results = list(
                    ddgs.text(question, max_results=max_results, timelimit="m")
                )

        if not results:
            return ""

        lines = ["**Recent news and context:**"]
        for r in results:
            title = r.get("title", "")
            body = r.get("body", r.get("content", ""))[:200]
            date = r.get("date", r.get("published", ""))
            source = r.get("source", r.get("url", ""))
            lines.append(f"- [{date}] {title}: {body} (source: {source})")

        context = "\n".join(lines)
        logger.debug("Найдено %d результатов для: %s", len(results), question[:50])
        return context

    except Exception as e:
        logger.debug("DuckDuckGo search failed: %s", e)
        return _tavily_fallback(question, max_results)


def _tavily_fallback(question: str, max_results: int = 5) -> str:
    """Tavily как fallback если DDG не работает."""
    tavily_key = os.getenv("TAVILY_API_KEY")
    if not tavily_key:
        return ""

    try:
        from tavily import TavilyClient

        client = TavilyClient(api_key=tavily_key)
        response = client.search(
            query=question,
            search_depth="basic",
            topic="news",
            max_results=max_results,
            include_answer=True,
            time_range="week",
        )

        lines = ["**Recent news and context:**"]
        answer = response.get("answer")
        if answer:
            lines.append(f"- Summary: {answer}")

        for r in response.get("results", []):
            title = r.get("title", "")
            content = r.get("content", "")[:200]
            lines.append(f"- {title}: {content}")

        return "\n".join(lines)

    except Exception as e:
        logger.debug("Tavily search failed: %s", e)
        return ""
