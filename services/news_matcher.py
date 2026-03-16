"""Match breaking news with active Polymarket markets using keyword index."""

import logging
import re
import time
from typing import Any

from polymarket.api import PolymarketAPI
from polymarket.models import Market

logger = logging.getLogger(__name__)

# Common stop words to exclude from keyword extraction
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "and",
        "but",
        "or",
        "not",
        "no",
        "nor",
        "so",
        "yet",
        "both",
        "either",
        "neither",
        "each",
        "every",
        "all",
        "any",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "about",
        "up",
        "out",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "what",
        "which",
        "who",
        "whom",
        "how",
        "when",
        "where",
        "why",
        "if",
        "then",
        "here",
        "there",
        "over",
        "under",
    }
)

# Named entity patterns for better matching
_TICKER_PATTERN = re.compile(r"\b[A-Z]{1,5}\b")
_MONEY_PATTERN = re.compile(r"\$[\d,.]+[BMK]?\b")
_PERSON_INDICATORS = frozenset(
    {
        "trump",
        "biden",
        "powell",
        "yellen",
        "gensler",
        "musk",
        "harris",
        "desantis",
        "pence",
        "pelosi",
        "mccarthy",
        "schumer",
    }
)


def _extract_keywords(text: str) -> set[str]:
    """Extract meaningful keywords from text."""
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    return {w for w in words if w not in _STOP_WORDS and len(w) > 2}


def _extract_entities(text: str) -> set[str]:
    """Extract named entities: tickers, people, money amounts."""
    entities: set[str] = set()

    # Tickers
    tickers = _TICKER_PATTERN.findall(text)
    entities.update(t.lower() for t in tickers if len(t) >= 2)

    # Known people
    text_lower = text.lower()
    for person in _PERSON_INDICATORS:
        if person in text_lower:
            entities.add(person)

    return entities


class NewsMatcher:
    """Match breaking news articles with active Polymarket markets.

    Uses an inverted keyword index for O(1) lookup (~1ms per match).
    """

    def __init__(self) -> None:
        self._markets: list[Market] = []
        self._keyword_index: dict[str, list[int]] = {}  # keyword -> [market indices]
        self._cache_updated: float = 0
        self._api = PolymarketAPI()

    def refresh_markets(self, max_markets: int = 500) -> None:
        """Refresh market cache from Gamma API."""
        try:
            markets = self._api.get_active_markets(max_markets=max_markets)
            self._markets = self._api.filter_tradeable_markets(markets)
            self._build_keyword_index()
            self._cache_updated = time.time()
            logger.info(
                "NewsMatcher: refreshed %d tradeable markets, %d keywords",
                len(self._markets),
                len(self._keyword_index),
            )
        except Exception as e:
            logger.error("NewsMatcher: failed to refresh markets: %s", e)

    @property
    def cache_age_seconds(self) -> float:
        if self._cache_updated == 0:
            return float("inf")
        return time.time() - self._cache_updated

    def _build_keyword_index(self) -> None:
        """Build inverted index: keyword -> [market indices]."""
        self._keyword_index.clear()
        for idx, market in enumerate(self._markets):
            text = f"{market.question} {market.description[:500]}"
            keywords = _extract_keywords(text) | _extract_entities(text)
            for kw in keywords:
                if kw not in self._keyword_index:
                    self._keyword_index[kw] = []
                self._keyword_index[kw].append(idx)

    def find_affected_markets(
        self,
        article: dict[str, Any],
        min_relevance: float = 0.3,
        max_results: int = 10,
    ) -> list[tuple[Market, float]]:
        """Find markets affected by a news article.

        Returns list of (Market, relevance_score) sorted by relevance.
        """
        if not self._markets:
            return []

        title = article.get("title", "")
        summary = article.get("summary", "") or ""
        category = article.get("category", "")

        article_keywords = _extract_keywords(f"{title} {summary}")
        article_entities = _extract_entities(f"{title} {summary}")
        all_terms = article_keywords | article_entities

        # Score each market by keyword overlap
        market_scores: dict[int, float] = {}
        for term in all_terms:
            matched_indices = self._keyword_index.get(term, [])
            for idx in matched_indices:
                # Entity matches worth more than keyword matches
                weight = 3.0 if term in article_entities else 1.0
                market_scores[idx] = market_scores.get(idx, 0) + weight

        if not market_scores:
            return []

        # Normalize scores to 0-1 range
        max_score = max(market_scores.values())
        if max_score == 0:
            return []

        results: list[tuple[Market, float]] = []
        for idx, raw_score in market_scores.items():
            relevance = raw_score / max_score

            # Category bonus: matching category boosts relevance
            market = self._markets[idx]
            market_text = market.question.lower()
            if category == "macro" and any(
                kw in market_text for kw in ["rate", "fed", "gdp", "inflation", "cpi"]
            ):
                relevance = min(1.0, relevance * 1.3)
            elif category == "crypto" and any(
                kw in market_text for kw in ["bitcoin", "btc", "eth", "crypto"]
            ):
                relevance = min(1.0, relevance * 1.3)
            elif category == "politics" and any(
                kw in market_text for kw in ["trump", "biden", "election", "congress"]
            ):
                relevance = min(1.0, relevance * 1.3)

            if relevance >= min_relevance:
                results.append((market, round(relevance, 3)))

        results.sort(key=lambda x: x[1], reverse=True)
        return results[:max_results]
