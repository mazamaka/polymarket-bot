"""Web search для обогащения контекста анализа рынков.

Порядок: Tavily (оптимизирован для AI, даёт синтез) → DuckDuckGo (бесплатный fallback).
"""

import logging
import os

logger = logging.getLogger(__name__)


def search_market_context(question: str, max_results: int = 5) -> str:
    """Поиск актуальных новостей по теме рынка.

    Tavily — primary (AI-оптимизированный, include_answer).
    DuckDuckGo — fallback (бесплатно, без ключа).
    """
    context = _tavily_search(question, max_results)
    if context:
        return context
    return _ddg_search(question, max_results)


def _tavily_search(question: str, max_results: int = 5) -> str:
    """Tavily — AI-оптимизированный поиск с синтезом ответа."""
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
            lines.append(f"- AI Summary: {answer}")

        for r in response.get("results", []):
            title = r.get("title", "")
            content = r.get("content", "")[:200]
            lines.append(f"- {title}: {content}")

        if len(lines) > 1:
            logger.debug(
                "Tavily: %d результатов для: %s", len(lines) - 1, question[:50]
            )
            return "\n".join(lines)
        return ""

    except Exception as e:
        logger.debug("Tavily search failed: %s", e)
        return ""


def _ddg_search(question: str, max_results: int = 5) -> str:
    """DuckDuckGo — бесплатный fallback поиск."""
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.news(question, max_results=max_results, timelimit="w"))

        if not results:
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

        logger.debug("DDG: %d результатов для: %s", len(results), question[:50])
        return "\n".join(lines)

    except Exception as e:
        logger.debug("DuckDuckGo search failed: %s", e)
        return ""
