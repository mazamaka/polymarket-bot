"""Кросс-маркет корреляционный анализ — поиск логических противоречий между связанными рынками."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from polymarket.api import PolymarketAPI
from polymarket.models import Event, Market

logger = logging.getLogger(__name__)

# Паттерны для парсинга дат из вопросов
DATE_PATTERNS = [
    r"by\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,?\s*(\d{4}))?",
    r"by\s+(March|June|September|December)\s+(\d{1,2}),?\s*(\d{4})?",
    r"in\s+(\d{4})",
    r"before\s+(March|June|September|December)\s+(\d{1,2})",
]

# Паттерны для парсинга числовых порогов
THRESHOLD_PATTERNS = [
    r"[>≥]\s*\$?([\d,.]+)\s*([KkMmBbTt])?",
    r"above\s+\$?([\d,.]+)\s*([KkMmBbTt])?",
    r"over\s+\$?([\d,.]+)\s*([KkMmBbTt])?",
    r"at least\s+\$?([\d,.]+)\s*([KkMmBbTt])?",
]

MONTH_ORDER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

MULTIPLIERS = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}


@dataclass
class CorrelationSignal:
    """Сигнал от корреляционного анализа."""

    event_title: str
    signal_type: str  # temporal_violation, threshold_violation
    market_buy: Market  # рынок который недооценён
    market_sell: Market  # рынок который переоценён (для контекста)
    expected_relation: str  # "buy.price should be >= sell.price"
    actual_spread: float  # разница (отрицательная = нарушение)
    confidence: float = 0.0


def _parse_date_from_question(question: str) -> datetime | None:
    """Извлечь дату дедлайна из вопроса."""
    q = question.lower()
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, q, re.IGNORECASE)
        if match:
            groups = match.groups()
            try:
                if len(groups) >= 2 and groups[0].isdigit():
                    return datetime(int(groups[0]), 1, 1, tzinfo=timezone.utc)
                month_str = groups[0].lower()
                if month_str in MONTH_ORDER:
                    month = MONTH_ORDER[month_str]
                    day = int(groups[1]) if len(groups) > 1 and groups[1] else 1
                    year = int(groups[2]) if len(groups) > 2 and groups[2] else 2026
                    return datetime(year, month, day, tzinfo=timezone.utc)
            except (ValueError, IndexError):
                continue
    return None


def _parse_threshold(question: str) -> float | None:
    """Извлечь числовой порог из вопроса (напр. >$69,000 → 69000)."""
    for pattern in THRESHOLD_PATTERNS:
        match = re.search(pattern, question, re.IGNORECASE)
        if match:
            try:
                num_str = match.group(1).replace(",", "")
                value = float(num_str)
                if match.lastindex >= 2 and match.group(2):
                    mult = match.group(2).lower()
                    value *= MULTIPLIERS.get(mult, 1)
                return value
            except (ValueError, IndexError):
                continue
    return None


def _get_yes_price(market: Market) -> float:
    return market.outcome_prices[0] if market.outcome_prices else 0.5


def detect_temporal_violations(event: Event) -> list[CorrelationSignal]:
    """Найти нарушения временной монотонности в рамках события.

    'X by March' <= 'X by June' — если March > June, это нарушение.
    """
    signals: list[CorrelationSignal] = []
    active = [
        m for m in event.markets if m.active and not m.closed and m.liquidity > 100
    ]
    if len(active) < 2:
        return signals

    dated: list[tuple[datetime, Market]] = []
    for m in active:
        dt = _parse_date_from_question(m.question)
        if dt:
            dated.append((dt, m))

    dated.sort(key=lambda x: x[0])

    for i in range(len(dated) - 1):
        dt_early, m_early = dated[i]
        dt_late, m_late = dated[i + 1]

        p_early = _get_yes_price(m_early)
        p_late = _get_yes_price(m_late)

        # Более поздний дедлайн должен иметь >= вероятность
        if p_early > p_late + 0.03:  # 3% порог шума
            spread = p_late - p_early
            signals.append(
                CorrelationSignal(
                    event_title=event.title,
                    signal_type="temporal_violation",
                    market_buy=m_late,  # недооценён — купить
                    market_sell=m_early,  # переоценён
                    expected_relation=f"'{m_late.question[:40]}' YES >= '{m_early.question[:40]}' YES",
                    actual_spread=spread,
                    confidence=min(abs(spread) / 0.10, 1.0),
                )
            )

    return signals


def detect_threshold_violations(event: Event) -> list[CorrelationSignal]:
    """Найти нарушения пороговой монотонности.

    '>$600M' YES >= '>$800M' YES >= '>$1B' YES — если нижний порог < верхнего, нарушение.
    """
    signals: list[CorrelationSignal] = []
    active = [
        m for m in event.markets if m.active and not m.closed and m.liquidity > 100
    ]
    if len(active) < 2:
        return signals

    thresholded: list[tuple[float, Market]] = []
    for m in active:
        th = _parse_threshold(m.question)
        if th:
            thresholded.append((th, m))

    thresholded.sort(key=lambda x: x[0])

    for i in range(len(thresholded) - 1):
        th_low, m_low = thresholded[i]
        th_high, m_high = thresholded[i + 1]

        p_low = _get_yes_price(m_low)
        p_high = _get_yes_price(m_high)

        # Нижний порог должен иметь >= вероятность, чем верхний
        if p_high > p_low + 0.03:
            spread = p_low - p_high
            signals.append(
                CorrelationSignal(
                    event_title=event.title,
                    signal_type="threshold_violation",
                    market_buy=m_low,  # нижний порог недооценён
                    market_sell=m_high,  # верхний порог переоценён
                    expected_relation=f"'{m_low.question[:40]}' YES >= '{m_high.question[:40]}' YES",
                    actual_spread=spread,
                    confidence=min(abs(spread) / 0.10, 1.0),
                )
            )

    return signals


_SPORT_KEYWORDS = re.compile(
    r"(points|assists|rebounds|touchdowns|goals|saves|strikeouts|"
    r"yards|tackles|rushing|passing|home runs|RBIs|steals|blocks|"
    r"esports|BO3|BO5|group stage|playoffs?|match\s+\d|game\s+\d)",
    re.IGNORECASE,
)


def _is_sport_market(question: str) -> bool:
    """Проверка что рынок спортивный (нельзя торговать через correlation)."""
    return bool(_SPORT_KEYWORDS.search(question))


def scan_correlations(
    min_liquidity: float = 200.0,
    max_events: int = 200,
    on_log: object = None,
) -> list[CorrelationSignal]:
    """Сканировать все events на кросс-маркет противоречия."""

    def _log(msg: str) -> None:
        logger.info(msg)
        if on_log:
            on_log(msg)

    api = PolymarketAPI()
    try:
        events = api.get_active_events(limit=100, max_events=500)
        multi = [e for e in events if len(e.markets) >= 2]
        _log(f"Корреляции: {len(events)} events, {len(multi)} с множеством рынков")

        all_signals: list[CorrelationSignal] = []

        for event in multi[:max_events]:
            # Фильтруем спортивные события
            if _is_sport_market(event.title):
                continue

            active = [
                m
                for m in event.markets
                if m.active
                and not m.closed
                and m.liquidity >= min_liquidity
                and not _is_sport_market(m.question)
            ]
            if len(active) < 2:
                continue

            temporal = detect_temporal_violations(event)
            threshold = detect_threshold_violations(event)

            for sig in temporal + threshold:
                # Дополнительная проверка что buy market не спортивный
                if _is_sport_market(sig.market_buy.question):
                    continue
                all_signals.append(sig)
                _log(
                    f"CORRELATION: {sig.signal_type} in '{sig.event_title[:50]}' | "
                    f"spread: {sig.actual_spread:+.2f} | conf: {sig.confidence:.0%}"
                )

        _log(f"Найдено {len(all_signals)} корреляционных сигналов")
        return all_signals

    finally:
        api.close()
