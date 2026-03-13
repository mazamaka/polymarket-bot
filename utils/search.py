"""Web search для обогащения контекста анализа рынков.

Источники (все используются для максимальной картины):
1. News Intelligence Service (news.maxbob.xyz) — наш агрегатор
2. Tavily — AI-оптимизированный поиск
3. Google News — бесплатный RSS
4. DuckDuckGo — fallback
"""

import logging
import os

import httpx

from config import settings

logger = logging.getLogger(__name__)

_news_client: httpx.Client | None = None


def _get_news_client() -> httpx.Client:
    global _news_client
    if _news_client is None:
        _news_client = httpx.Client(timeout=15.0)
    return _news_client


def fetch_news_service_context(question: str) -> dict:
    """Получить полный контекст из News Intelligence Service.

    Возвращает dict с ключами: articles, economic_events, weather, earnings.
    """
    try:
        resp = _get_news_client().get(
            f"{settings.news_service_url}/api/v1/market-context",
            params={"question": question},
        )
        resp.raise_for_status()
        data = resp.json()
        logger.debug(
            "News Service: %d articles, %d events, %d weather, %d earnings для: %s",
            len(data.get("relevant_articles", [])),
            len(data.get("economic_events", [])),
            len(data.get("weather_forecasts", [])),
            len(data.get("earnings", [])),
            question[:50],
        )
        return data
    except Exception as e:
        logger.warning("News Service error: %s", e)
        return {}


def format_news_service_articles(data: dict, max_articles: int = 5) -> str:
    """Форматирование статей из News Service для промпта."""
    articles = data.get("relevant_articles", [])
    if not articles:
        return ""

    lines = ["**News Intelligence (our aggregator):**"]
    for a in articles[:max_articles]:
        score = a.get("relevance_score")
        score_str = f" [relevance: {score:.2f}]" if score else ""
        summary = a.get("summary", "")
        if summary and len(summary) > 200:
            summary = summary[:200] + "..."
        lines.append(
            f"- [{a.get('published_at', '')[:10]}] {a.get('title', '')} "
            f"({a.get('source', '')}){score_str}: {summary}"
        )
    return "\n".join(lines)


def format_economic_events(data: dict, max_events: int = 10) -> str:
    """Форматирование экономических событий для промпта."""
    events = data.get("economic_events", [])
    if not events:
        return ""

    lines = ["**Economic Calendar (upcoming/recent):**"]
    for e in events[:max_events]:
        parts = [
            f"[{e.get('event_dt', '')[:16]}]",
            f"{e.get('country', '')}",
            f"{e.get('event_name', '')}",
        ]
        if e.get("importance"):
            parts.append(f"({e['importance']})")
        if e.get("actual"):
            parts.append(f"actual: {e['actual']}")
        if e.get("forecast"):
            parts.append(f"forecast: {e['forecast']}")
        if e.get("previous"):
            parts.append(f"prev: {e['previous']}")
        lines.append(f"- {' | '.join(parts)}")
    return "\n".join(lines)


def format_earnings(data: dict, max_items: int = 5) -> str:
    """Форматирование earnings для промпта."""
    earnings = data.get("earnings", [])
    if not earnings:
        return ""

    lines = ["**Earnings Reports:**"]
    for e in earnings[:max_items]:
        parts = [f"{e.get('ticker', '')}"]
        if e.get("company_name"):
            parts.append(e["company_name"])
        parts.append(f"date: {e.get('report_date', '')[:10]}")
        if e.get("eps_actual") is not None:
            parts.append(f"EPS: {e['eps_actual']}")
            if e.get("eps_estimate") is not None:
                parts.append(f"(est: {e['eps_estimate']})")
        if e.get("surprise_pct") is not None:
            parts.append(f"surprise: {e['surprise_pct']:+.1f}%")
        if e.get("revenue_actual") is not None:
            parts.append(f"rev: {e['revenue_actual']}")
        lines.append(f"- {' | '.join(parts)}")
    return "\n".join(lines)


def search_market_context(question: str, max_results: int = 5) -> str:
    """Поиск актуальных новостей по теме рынка.

    Объединяет все источники для максимальной картины.
    """
    all_parts: list[str] = []

    # 1. News Intelligence Service — наш агрегатор (primary)
    news_data = fetch_news_service_context(question)
    if news_data:
        articles = format_news_service_articles(news_data, max_articles=max_results)
        if articles:
            all_parts.append(articles)
        econ = format_economic_events(news_data)
        if econ:
            all_parts.append(econ)
        earnings = format_earnings(news_data)
        if earnings:
            all_parts.append(earnings)

    # 2. Tavily — дополнительный AI-поиск
    tavily = _tavily_search(question, max_results)
    if tavily:
        all_parts.append(tavily)

    # 3. Google News / DuckDuckGo — fallback если ничего не нашлось
    if not all_parts:
        context = _google_news_search(question, max_results)
        if context:
            all_parts.append(context)
        else:
            ddg = _ddg_search(question, max_results)
            if ddg:
                all_parts.append(ddg)

    return "\n\n".join(all_parts)


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
