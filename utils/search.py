"""Web search для обогащения контекста анализа рынков.

Порядок: Tavily → Google News (pygooglenews) → DuckDuckGo fallback.
Google News бесплатен и без лимитов, newspaper4k парсит полный текст статей.
"""

import logging
import os

logger = logging.getLogger(__name__)


def search_market_context(question: str, max_results: int = 5) -> str:
    """Поиск актуальных новостей по теме рынка.

    Tavily — primary (AI-оптимизированный, include_answer).
    Google News — secondary (бесплатно, без ключа, без лимитов).
    DuckDuckGo — fallback.
    """
    context = _tavily_search(question, max_results)
    if context:
        return context
    context = _google_news_search(question, max_results)
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


def _google_news_search(question: str, max_results: int = 5) -> str:
    """Google News через pygooglenews — бесплатно, без API ключа, без лимитов."""
    try:
        from pygooglenews import GoogleNews

        gn = GoogleNews(lang="en", country="US")
        result = gn.search(question, when="7d")

        entries = result.get("entries", [])
        if not entries:
            return ""

        lines = ["**Recent news (Google News):**"]
        for entry in entries[:max_results]:
            title = entry.get("title", "")
            published = entry.get("published", "")
            source = entry.get("source", {}).get("title", "")
            summary = entry.get("summary", "")
            if summary:
                # Google News summary содержит HTML — убираем теги и entities
                import html
                import re

                summary = re.sub(r"<[^>]+>", "", html.unescape(summary))[:200]
                lines.append(f"- [{published}] {title} ({source}): {summary}")
            else:
                lines.append(f"- [{published}] {title} ({source})")

        logger.debug("Google News: %d результатов для: %s", len(entries), question[:50])
        return "\n".join(lines)

    except Exception as e:
        logger.debug("Google News search failed: %s", e)
        return ""


def fetch_article_text(url: str, max_chars: int = 1000) -> str:
    """Извлечь полный текст статьи через newspaper4k (для глубокого анализа)."""
    if not url:
        return ""
    try:
        from newspaper import Article

        article = Article(url, request_timeout=10)
        article.download()
        article.parse()
        text = article.text
        if text and len(text) > 50:
            return text[:max_chars].rsplit(" ", 1)[0] + "..."
        return ""
    except Exception:
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
