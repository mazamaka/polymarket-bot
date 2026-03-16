"""SSE listener for News Intelligence breaking news stream.

Connects to News Intelligence SSE endpoint, matches breaking news
with Polymarket markets, and triggers rapid re-analysis.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

import httpx
from config import settings

from services.news_matcher import NewsMatcher

logger = logging.getLogger(__name__)


class SSEListener:
    """Listen to News Intelligence SSE stream and react to breaking news."""

    def __init__(
        self,
        on_breaking_match: Callable[[dict[str, Any], list[tuple[Any, float]]], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self._stream_url = (
            f"{settings.news_service_url}/api/v1/stream?importance={settings.sse_min_importance}"
        )
        self._matcher = NewsMatcher()
        self._on_breaking_match = on_breaking_match
        self._on_log = on_log
        self._running = False
        self._connected = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self._events_received = 0
        self._matches_found = 0
        self._last_event_time: float = 0

        # Rate limiting: max N breaking trades per hour
        self._trade_timestamps: deque[float] = deque()
        self._max_trades_per_hour = settings.breaking_max_trades_per_hour

        # Deduplication
        self._seen_articles: dict[str, float] = {}  # article_id -> timestamp
        self._analyzed_markets: dict[str, float] = {}  # market_id -> last_analysis_ts
        self._dedup_ttl = 300.0  # 5 min cooldown

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "connected": self._connected,
            "events_received": self._events_received,
            "matches_found": self._matches_found,
            "markets_cached": len(self._matcher._markets),
            "articles_deduped": len(self._seen_articles),
            "markets_on_cooldown": len(self._analyzed_markets),
            "trades_this_hour": len(self._trade_timestamps),
            "last_event": (
                datetime.fromtimestamp(self._last_event_time).isoformat()
                if self._last_event_time
                else None
            ),
        }

    def _log(self, msg: str) -> None:
        logger.info("[SSE] %s", msg)
        if self._on_log:
            self._on_log(f"[SSE] {msg}")

    def _can_trade(self) -> bool:
        """Check rate limit: max N trades per hour."""
        now = time.time()
        # Remove timestamps older than 1 hour
        while self._trade_timestamps and now - self._trade_timestamps[0] > 3600:
            self._trade_timestamps.popleft()
        return len(self._trade_timestamps) < self._max_trades_per_hour

    def _record_trade(self) -> None:
        self._trade_timestamps.append(time.time())

    def _is_article_seen(self, article_id: str) -> bool:
        """Check if article was already processed (dedup)."""
        now = time.time()
        # Cleanup old entries
        expired = [k for k, v in self._seen_articles.items() if now - v > 3600]
        for k in expired:
            del self._seen_articles[k]
        return article_id in self._seen_articles

    def _mark_article_seen(self, article_id: str) -> None:
        self._seen_articles[article_id] = time.time()

    def is_market_on_cooldown(self, market_id: str) -> bool:
        """Check if market was analyzed recently (dedup cooldown)."""
        ts = self._analyzed_markets.get(market_id)
        if ts is None:
            return False
        return (time.time() - ts) < self._dedup_ttl

    def mark_market_analyzed(self, market_id: str) -> None:
        self._analyzed_markets[market_id] = time.time()
        # Cleanup old entries
        now = time.time()
        expired = [k for k, v in self._analyzed_markets.items() if now - v > 3600]
        for k in expired:
            del self._analyzed_markets[k]

    async def start(self) -> None:
        """Start SSE listener with auto-reconnect."""
        self._running = True
        self._log("Starting SSE listener...")

        # Initial market refresh
        self._matcher.refresh_markets()

        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.error("[SSE] Connection error: %s", e)

            if not self._running:
                break

            # Exponential backoff
            self._log(f"Reconnecting in {self._reconnect_delay:.0f}s...")
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        self._connected = False
        self._log("SSE listener stopped")

    async def stop(self) -> None:
        """Stop the listener."""
        self._running = False

    async def _connect(self) -> None:
        """Connect to SSE stream and process events."""
        self._log(f"Connecting to {self._stream_url}")

        async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
            async with client.stream(
                "GET",
                self._stream_url,
                headers={"Accept": "text/event-stream"},
            ) as response:
                if response.status_code != 200:
                    logger.error("[SSE] HTTP %d from stream", response.status_code)
                    return

                self._connected = True
                self._reconnect_delay = 1.0  # Reset on successful connect
                self._log("Connected to SSE stream")

                event_type = ""
                data_lines: list[str] = []

                async for line in response.aiter_lines():
                    if not self._running:
                        break

                    if line.startswith("event: "):
                        event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                    elif line == "":
                        # End of event
                        if data_lines:
                            data_str = "\n".join(data_lines)
                            await self._handle_event(event_type, data_str)
                        event_type = ""
                        data_lines = []

                    # Periodic market refresh (every 5 min)
                    if (
                        self._matcher.cache_age_seconds
                        > settings.breaking_market_refresh_minutes * 60
                    ):
                        self._matcher.refresh_markets()

    async def _handle_event(self, event_type: str, data_str: str) -> None:
        """Handle a single SSE event."""
        if event_type == "heartbeat":
            return

        if event_type != "article":
            return

        try:
            article = json.loads(data_str)
        except json.JSONDecodeError:
            logger.warning("[SSE] Invalid JSON: %s", data_str[:100])
            return

        self._events_received += 1
        self._last_event_time = time.time()

        # Article deduplication
        article_id = article.get("id") or article.get("url") or article.get("title", "")
        if self._is_article_seen(str(article_id)):
            return
        self._mark_article_seen(str(article_id))

        importance = article.get("importance", "medium")
        title = article.get("title", "")[:80]
        source = article.get("source", "")
        self._log(f"[{importance.upper()}] {source}: {title}")

        # Find matching markets
        matches = self._matcher.find_affected_markets(
            article,
            min_relevance=settings.breaking_min_relevance,
            max_results=5,
        )

        if not matches:
            return

        # Filter out markets on cooldown (analyzed < 5 min ago)
        fresh_matches = [(m, r) for m, r in matches if not self.is_market_on_cooldown(m.id)]
        if not fresh_matches:
            self._log(f"Matched {len(matches)} markets but all on cooldown")
            return

        self._matches_found += 1
        match_info = ", ".join(f"{m.question[:40]}({r:.2f})" for m, r in fresh_matches[:3])
        self._log(f"Matched {len(fresh_matches)} markets (of {len(matches)}): {match_info}")

        # Rate limit check
        if not self._can_trade():
            self._log(
                f"Rate limit: {len(self._trade_timestamps)}/{self._max_trades_per_hour} trades/hour"
            )
            return

        # Mark markets as analyzed (cooldown)
        for m, _ in fresh_matches:
            self.mark_market_analyzed(m.id)

        # Callback for re-analysis
        if self._on_breaking_match:
            try:
                self._on_breaking_match(article, fresh_matches)
                self._record_trade()
            except Exception as e:
                logger.error("[SSE] Breaking match callback error: %s", e)
