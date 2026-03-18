"""Weather Analyzer — алгоритмический анализ погодных рынков Polymarket.

Использует Open-Meteo Ensemble API (бесплатно, без ключа) с 4 моделями:
- GFS (31 member) + ECMWF IFS (51 member) + ICON (40 member) + GEM (21 member)
= ~143 ensemble members для эмпирической CDF вероятности.

Дополнительно: NWS API (api.weather.gov) для US городов как cross-reference.
Сравнивает с рыночной ценой для поиска edge.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from config import settings  # noqa: E402
from polymarket.api import PolymarketAPI
from polymarket.models import AIPrediction, Market

logger = logging.getLogger(__name__)

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
NWS_API_URL = "https://api.weather.gov"
NEWS_SERVICE_URL = settings.news_service_url

# US города для NWS API (работает только для США)
US_CITIES = {
    "new york",
    "nyc",
    "new york city",
    "los angeles",
    "la",
    "chicago",
    "miami",
    "houston",
    "phoenix",
    "philadelphia",
    "san antonio",
    "san diego",
    "dallas",
    "austin",
    "denver",
    "washington",
    "dc",
    "seattle",
    "boston",
    "nashville",
    "atlanta",
    "san francisco",
    "sf",
    "las vegas",
    "detroit",
    "minneapolis",
    "charlotte",
    "portland",
    "orlando",
    "tampa",
    "sacramento",
    "kansas city",
    "salt lake city",
    "raleigh",
    "memphis",
    "oklahoma city",
    "milwaukee",
    "buffalo",
    "anchorage",
    "honolulu",
}

# Города Polymarket → координаты
CITY_COORDS: dict[str, tuple[float, float]] = {
    "new york": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "la": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "miami": (25.7617, -80.1918),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "philadelphia": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936),
    "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970),
    "austin": (30.2672, -97.7431),
    "denver": (39.7392, -104.9903),
    "washington": (38.9072, -77.0369),
    "dc": (38.9072, -77.0369),
    "seattle": (47.6062, -122.3321),
    "boston": (42.3601, -71.0589),
    "nashville": (36.1627, -86.7816),
    "atlanta": (33.7490, -84.3880),
    "san francisco": (37.7749, -122.4194),
    "sf": (37.7749, -122.4194),
    "las vegas": (36.1699, -115.1398),
    "detroit": (42.3314, -83.0458),
    "minneapolis": (44.9778, -93.2650),
    "charlotte": (35.2271, -80.8431),
    "portland": (45.5152, -122.6784),
    "orlando": (28.5383, -81.3792),
    "tampa": (27.9506, -82.4572),
    "sacramento": (38.5816, -121.4944),
    "kansas city": (39.0997, -94.5786),
    "salt lake city": (40.7608, -111.8910),
    "raleigh": (35.7796, -78.6382),
    "memphis": (35.1495, -90.0490),
    "oklahoma city": (35.4676, -97.5164),
    "milwaukee": (43.0389, -87.9065),
    "buffalo": (42.8864, -78.8784),
    "anchorage": (61.2181, -149.9003),
    "honolulu": (21.3069, -157.8583),
    # Международные города (Polymarket)
    "munich": (48.1351, 11.5820),
    "shanghai": (31.2304, 121.4737),
    "lucknow": (26.8467, 80.9462),
    "paris": (48.8566, 2.3522),
    "buenos aires": (-34.6037, -58.3816),
    "ankara": (39.9334, 32.8597),
    "singapore": (1.3521, 103.8198),
    "london": (51.5074, -0.1278),
    "tokyo": (35.6762, 139.6503),
    "sydney": (-33.8688, 151.2093),
    "berlin": (52.5200, 13.4050),
    "moscow": (55.7558, 37.6173),
    "dubai": (25.2048, 55.2708),
    "mumbai": (19.0760, 72.8777),
    "new delhi": (28.6139, 77.2090),
    "delhi": (28.6139, 77.2090),
    "beijing": (39.9042, 116.4074),
    "toronto": (43.6532, -79.3832),
    "mexico city": (19.4326, -99.1332),
    "cairo": (30.0444, 31.2357),
    "rome": (41.9028, 12.4964),
    "madrid": (40.4168, -3.7038),
    "new york city": (40.7128, -74.0060),
    "sao paulo": (-23.5505, -46.6333),
    "são paulo": (-23.5505, -46.6333),
    "johannesburg": (-26.2041, 28.0473),
    "seoul": (37.5665, 126.9780),
    "bangkok": (13.7563, 100.5018),
    "jakarta": (-6.2088, 106.8456),
    "lagos": (6.5244, 3.3792),
    "istanbul": (41.0082, 28.9784),
    "rio de janeiro": (-22.9068, -43.1729),
    "lima": (-12.0464, -77.0428),
    "bogota": (4.7110, -74.0721),
    "santiago": (-33.4489, -70.6693),
    "kuala lumpur": (3.1390, 101.6869),
    "nairobi": (-1.2921, 36.8219),
    "tel aviv": (32.0853, 34.7818),
    "manila": (14.5995, 120.9842),
    "hanoi": (21.0278, 105.8342),
}

# Паттерны для парсинга реальных погодных вопросов Polymarket
# Реальные примеры:
# "Will the highest temperature in Atlanta be 80°F or higher on March 14?"
# "Will the highest temperature in New York City be between 48-49°F on March 14?"
# "Will the highest temperature in Munich be 13°C on March 13?"
# "Will the highest temperature in Buenos Aires be 27°C or below on March 14?"


@dataclass
class WeatherSignalData:
    """Расширенные данные weather сигнала для backtesting."""

    prediction: AIPrediction
    city: str
    target_date: str
    temp_type: str
    direction: str
    threshold: float
    ensemble_temps: list[float]


def parse_weather_question(question: str) -> dict | None:
    """Парсинг погодного вопроса Polymarket → структурированные данные."""
    q = question.strip().rstrip("?")

    # Базовый паттерн: "highest/lowest temperature in CITY be VALUE on DATE"
    base = re.match(
        r"(?:will\s+)?(?:the\s+)?(highest|lowest)\s+temperature\s+in\s+(.+?)\s+"
        r"be\s+(.+?)\s+on\s+(\w+\s+\d{1,2})",
        q,
        re.IGNORECASE,
    )
    if not base:
        return None

    temp_type = base.group(1).lower()
    city = base.group(2).strip()
    value_part = base.group(3).strip()
    date_str = base.group(4).strip()

    # Определяем единицы (°F или °C)
    is_celsius = "°C" in value_part or "°c" in value_part.lower()
    unit = "C" if is_celsius else "F"

    # Парсинг value_part
    # "80°F or higher" / "80°F or above"
    m = re.match(r"(\d+)\s*°?\s*[FC]?\s+or\s+(higher|above)", value_part, re.IGNORECASE)
    if m:
        return {
            "temp_type": temp_type,
            "city": city,
            "direction": "above",
            "threshold": float(m.group(1)),
            "threshold_high": None,
            "date_str": date_str,
            "unit": unit,
        }

    # "27°C or below" / "27°C or lower"
    m = re.match(r"(\d+)\s*°?\s*[FC]?\s+or\s+(below|lower)", value_part, re.IGNORECASE)
    if m:
        return {
            "temp_type": temp_type,
            "city": city,
            "direction": "below",
            "threshold": float(m.group(1)),
            "threshold_high": None,
            "date_str": date_str,
            "unit": unit,
        }

    # "between 48-49°F" / "between 48 and 49°F"
    m = re.match(
        r"between\s+(\d+)\s*[-–]\s*(\d+)\s*°?\s*[FC]?",
        value_part,
        re.IGNORECASE,
    )
    if m:
        return {
            "temp_type": temp_type,
            "city": city,
            "direction": "between",
            "threshold": float(m.group(1)),
            "threshold_high": float(m.group(2)),
            "date_str": date_str,
            "unit": unit,
        }

    m = re.match(
        r"between\s+(\d+)\s*°?\s*[FC]?\s+and\s+(\d+)\s*°?\s*[FC]?",
        value_part,
        re.IGNORECASE,
    )
    if m:
        return {
            "temp_type": temp_type,
            "city": city,
            "direction": "between",
            "threshold": float(m.group(1)),
            "threshold_high": float(m.group(2)),
            "date_str": date_str,
            "unit": unit,
        }

    # "above/below/at least/at most X°F"
    m = re.match(
        r"(above|below|at least|at most)\s+(\d+)\s*°?\s*[FC]?",
        value_part,
        re.IGNORECASE,
    )
    if m:
        return {
            "temp_type": temp_type,
            "city": city,
            "direction": m.group(1).lower(),
            "threshold": float(m.group(2)),
            "threshold_high": None,
            "date_str": date_str,
            "unit": unit,
        }

    # "exactly X°F" / just "X°C" (exact value)
    m = re.match(r"(?:exactly\s+)?(\d+)\s*°?\s*[FC]?$", value_part, re.IGNORECASE)
    if m:
        return {
            "temp_type": temp_type,
            "city": city,
            "direction": "exactly",
            "threshold": float(m.group(1)),
            "threshold_high": None,
            "date_str": date_str,
            "unit": unit,
        }

    return None


MONTH_MAP = {
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
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass
class WeatherMarketInfo:
    """Распарсенная информация из погодного вопроса."""

    market: Market
    city: str
    lat: float
    lon: float
    temp_type: str  # "highest" or "lowest"
    direction: str  # "above", "below", "between", "exactly"
    threshold: float  # °F
    threshold_high: float | None  # для between
    target_date: datetime


def _parse_date(date_str: str, ref_year: int = 2026) -> datetime | None:
    """Парсинг даты типа 'March 14' → datetime."""
    parts = date_str.strip().split()
    if len(parts) < 2:
        return None
    month_str = parts[0].lower()
    month = MONTH_MAP.get(month_str)
    if not month:
        return None
    try:
        day = int(parts[1].rstrip(","))
    except ValueError:
        return None
    return datetime(ref_year, month, day, tzinfo=timezone.utc)


def parse_weather_market(market: Market) -> WeatherMarketInfo | None:
    """Парсинг вопроса Polymarket в структурированные данные."""
    parsed = parse_weather_question(market.question)
    if not parsed:
        return None

    city_lower = parsed["city"].lower()
    coords = CITY_COORDS.get(city_lower)
    if not coords:
        # Попытка частичного матча
        for name, c in CITY_COORDS.items():
            if name in city_lower or city_lower in name:
                coords = c
                break
    if not coords:
        logger.debug("Неизвестный город: %s", parsed["city"])
        return None

    target_date = _parse_date(parsed["date_str"])
    if not target_date:
        return None

    # Конвертация порогов из °C в °F если нужно
    threshold = parsed["threshold"]
    threshold_high = parsed["threshold_high"]
    if parsed["unit"] == "C":
        threshold = threshold * 9 / 5 + 32
        if threshold_high is not None:
            threshold_high = threshold_high * 9 / 5 + 32

    return WeatherMarketInfo(
        market=market,
        city=parsed["city"],
        lat=coords[0],
        lon=coords[1],
        temp_type=parsed["temp_type"],
        direction=parsed["direction"],
        threshold=threshold,
        threshold_high=threshold_high,
        target_date=target_date,
    )


def fetch_nws_forecast(
    lat: float, lon: float, target_date: datetime, temp_type: str
) -> float | None:
    """Получить прогноз от NWS API (только US) как дополнительный member.

    NWS API: /points/{lat},{lon} → /gridpoints/{office}/{x},{y}/forecast
    Возвращает температуру в °F или None если не удалось.
    """
    try:
        # Шаг 1: получить grid point
        resp = httpx.get(
            f"{NWS_API_URL}/points/{lat:.4f},{lon:.4f}",
            headers={"User-Agent": "PolymarketWeatherBot/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        forecast_url = resp.json()["properties"]["forecast"]

        # Шаг 2: получить прогноз
        resp = httpx.get(
            forecast_url,
            headers={"User-Agent": "PolymarketWeatherBot/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        periods = resp.json()["properties"]["periods"]

        target_str = target_date.strftime("%Y-%m-%d")
        for period in periods:
            start = period.get("startTime", "")
            if target_str not in start:
                continue
            temp = period.get("temperature")
            if temp is None:
                continue
            is_day = period.get("isDaytime", True)
            # highest → daytime period, lowest → nighttime period
            if temp_type == "highest" and is_day:
                return float(temp)
            if temp_type == "lowest" and not is_day:
                return float(temp)

    except Exception as e:
        logger.debug("NWS API error for %.2f,%.2f: %s", lat, lon, e)
    return None


# --- Forecast cache: city+date+temp_type → (temps, timestamp) ---
_forecast_cache: dict[str, tuple[list[float], float]] = {}
_FORECAST_CACHE_TTL: float = 7200.0  # 2 hours
_last_api_call: float = 0.0
_API_RATE_DELAY: float = 0.4  # seconds between API calls


def _rate_limit() -> None:
    """Простой rate limiter — не чаще 1 запроса в 0.4 сек."""
    import time

    global _last_api_call
    now = time.monotonic()
    elapsed = now - _last_api_call
    if elapsed < _API_RATE_DELAY:
        time.sleep(_API_RATE_DELAY - elapsed)
    _last_api_call = time.monotonic()


def fetch_ensemble_forecast(
    lat: float,
    lon: float,
    target_date: datetime,
    temp_type: str,
    city: str = "",
) -> list[float]:
    """Получить ансамблевый прогноз температуры.

    1. Проверяет кэш (2 часа TTL)
    2. Open-Meteo Ensemble API — все 4 модели в ОДНОМ запросе
    3. Fallback: Open-Meteo deterministic (6 моделей)
    4. Fallback: Visual Crossing (1000/day free)
    5. NWS API для US городов как доп. member
    """
    import time

    cache_key = f"{lat:.2f},{lon:.2f},{target_date.strftime('%Y-%m-%d')},{temp_type}"

    # Check cache
    if cache_key in _forecast_cache:
        cached_temps, cached_ts = _forecast_cache[cache_key]
        if (time.monotonic() - cached_ts) < _FORECAST_CACHE_TTL:
            logger.debug("Cache hit: %d members for %s", len(cached_temps), cache_key)
            return cached_temps

    date_str = target_date.strftime("%Y-%m-%d")
    daily_var = "temperature_2m_max" if temp_type == "highest" else "temperature_2m_min"
    all_temps: list[float] = []

    # 1. Open-Meteo Ensemble — ВСЕ модели в одном запросе (4x меньше API calls)
    _rate_limit()
    try:
        resp = httpx.get(
            ENSEMBLE_API_URL,
            params={
                "latitude": lat,
                "longitude": lon,
                "models": "gfs_seamless,ecmwf_ifs025,icon_seamless,gem_global",
                "daily": daily_var,
                "temperature_unit": "fahrenheit",
                "start_date": date_str,
                "end_date": date_str,
                "timezone": "auto",
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        daily = data.get("daily", {})
        for key, values in daily.items():
            if key.startswith(f"{daily_var}_member") and values:
                val = values[0]
                if val is not None:
                    all_temps.append(float(val))
        if not all_temps and daily_var in daily and daily[daily_var]:
            val = daily[daily_var][0]
            if val is not None:
                all_temps.append(float(val))
    except Exception as e:
        logger.warning("Open-Meteo ensemble error for %s: %s", date_str, e)

    # 2. Fallback: Open-Meteo deterministic (разные модели = "бедный ensemble")
    if len(all_temps) < 5:
        _rate_limit()
        try:
            det_models = "gfs_seamless,ecmwf_ifs025,icon_seamless,gem_global,jma_seamless,meteofrance_seamless"
            det_var = (
                "temperature_2m_max" if temp_type == "highest" else "temperature_2m_min"
            )
            resp = httpx.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "models": det_models,
                    "daily": det_var,
                    "temperature_unit": "fahrenheit",
                    "start_date": date_str,
                    "end_date": date_str,
                    "timezone": "auto",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            daily = data.get("daily", {})
            for key, values in daily.items():
                if det_var in key and values:
                    val = values[0]
                    if val is not None and val not in all_temps:
                        all_temps.append(float(val))
        except Exception as e:
            logger.warning("Open-Meteo deterministic fallback error: %s", e)

    # 3. Fallback: Visual Crossing (1000/day free, no key needed for limited use)
    if len(all_temps) < 3:
        _rate_limit()
        try:
            vc_url = f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/{lat},{lon}/{date_str}/{date_str}"
            resp = httpx.get(
                vc_url,
                params={
                    "unitGroup": "us",
                    "include": "days",
                    "key": "DEMO_KEY",
                    "contentType": "json",
                },
                timeout=10,
            )
            if resp.status_code == 200:
                days = resp.json().get("days", [])
                if days:
                    if temp_type == "highest" and days[0].get("tempmax") is not None:
                        all_temps.append(float(days[0]["tempmax"]))
                    elif temp_type == "lowest" and days[0].get("tempmin") is not None:
                        all_temps.append(float(days[0]["tempmin"]))
        except Exception as e:
            logger.debug("Visual Crossing fallback error: %s", e)

    # 4. NWS API для US городов
    if city.lower() in US_CITIES:
        nws_temp = fetch_nws_forecast(lat, lon, target_date, temp_type)
        if nws_temp is not None:
            all_temps.append(nws_temp)

    logger.info(
        "Ensemble forecast: %d members for %.2f,%.2f on %s (%s)",
        len(all_temps),
        lat,
        lon,
        date_str,
        temp_type,
    )

    # Cache result
    if all_temps:
        _forecast_cache[cache_key] = (all_temps, time.monotonic())

    return all_temps


def fetch_news_service_weather(city: str, target_date: datetime) -> dict | None:
    """Получить прогноз из News Intelligence Service для cross-reference."""
    try:
        date_str = target_date.strftime("%Y-%m-%d")
        resp = httpx.get(
            f"{NEWS_SERVICE_URL}/api/v1/weather/{city.lower()}",
            params={"date": date_str, "ensemble": "true"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return None
        forecast = data[0]
        logger.debug(
            "News Service weather for %s on %s: min=%.1f max=%.1f",
            city,
            date_str,
            forecast.get("temp_min", 0),
            forecast.get("temp_max", 0),
        )
        return forecast
    except Exception as e:
        logger.debug("News Service weather error for %s: %s", city, e)
    return None


def compute_probability(
    temps: list[float],
    direction: str,
    threshold: float,
    threshold_high: float | None = None,
) -> float | None:
    """Вычислить вероятность из ансамбля (эмпирическая CDF).

    Args:
        temps: температуры от ensemble members (°F)
        direction: "above", "below", "between", "exactly", "at least", "at most"
        threshold: порог температуры (°F)
        threshold_high: верхний порог для "between"

    Returns:
        Вероятность события [0, 1] или None если нет данных.
    """
    if not temps:
        return None

    n = len(temps)

    if direction in ("above", "at least"):
        count = sum(1 for t in temps if t >= threshold)
    elif direction in ("below", "at most"):
        count = sum(1 for t in temps if t <= threshold)
    elif direction == "between" and threshold_high is not None:
        count = sum(1 for t in temps if threshold <= t <= threshold_high)
    elif direction == "exactly":
        # "exactly 55°F" → обычно значит между 54.5 и 55.5 (округление)
        count = sum(1 for t in temps if abs(t - threshold) < 1.0)
    else:
        return None

    return count / n


def scan_weather_markets(
    min_liquidity: float = 100.0,
    max_days_ahead: int = 16,
    min_edge: float = 0.08,
    on_log: object = None,
) -> list[WeatherSignalData]:
    """Сканировать Polymarket на погодные рынки и вычислить edge.

    Returns:
        Список AIPrediction для рынков с положительным edge.
    """

    def _log(msg: str) -> None:
        logger.info(msg)
        if on_log:
            on_log(msg)

    api = PolymarketAPI()
    try:
        # Получаем рынки напрямую — weather отфильтрованы в filter_tradeable_markets
        markets = api.get_active_markets(
            limit=100, max_markets=1000, sort_by="liquidity"
        )
        now = datetime.now(tz=timezone.utc)

        weather_markets: list[WeatherMarketInfo] = []
        for m in markets:
            if not m.active or m.closed:
                continue
            if m.liquidity < min_liquidity:
                continue
            info = parse_weather_market(m)
            if not info:
                continue
            # Проверяем что дата в будущем и не слишком далеко
            days_ahead = (info.target_date - now).days
            if days_ahead < 0 or days_ahead > max_days_ahead:
                continue
            weather_markets.append(info)

        _log(f"Weather: найдено {len(weather_markets)} погодных рынков")
        if not weather_markets:
            return []

        # Группируем по (city, date, temp_type) для батчинга API запросов
        forecast_cache: dict[str, list[float]] = {}
        predictions: list[AIPrediction] = []

        for info in weather_markets:
            cache_key = (
                f"{info.lat},{info.lon},{info.target_date.date()},{info.temp_type}"
            )

            if cache_key not in forecast_cache:
                temps = fetch_ensemble_forecast(
                    info.lat,
                    info.lon,
                    info.target_date,
                    info.temp_type,
                    city=info.city,
                )
                forecast_cache[cache_key] = temps

            temps = forecast_cache[cache_key]
            if len(temps) < 4:
                _log(f"Weather: мало данных ({len(temps)} members) для {info.city}")
                continue

            model_prob = compute_probability(
                temps, info.direction, info.threshold, info.threshold_high
            )
            if model_prob is None:
                continue

            # Рыночная вероятность YES
            market_prob = (
                info.market.outcome_prices[0] if info.market.outcome_prices else 0.5
            )

            edge = model_prob - market_prob

            # Direction-specific min edge из backtest данных
            direction_min_edge = settings.weather_direction_min_edge.get(
                info.direction, min_edge
            )
            if abs(edge) < direction_min_edge:
                _log(
                    f"Weather SKIP: {info.city} {info.direction} — "
                    f"edge {abs(edge):.0%} < direction threshold {direction_min_edge:.0%}"
                )
                continue

            # Direction-specific max YES price фильтр
            max_yes = settings.weather_max_yes_price.get(info.direction, 0.25)
            if market_prob > max_yes:
                _log(
                    f"Weather SKIP: {info.city} {info.direction} — "
                    f"YES price {market_prob:.0%} > max {max_yes:.0%} for {info.direction}"
                )
                continue

            # Backtest NO rates по направлениям
            backtest_no_rates: dict[str, float] = {
                "below": 0.948,
                "above": 0.848,
                "exactly": 0.875,
                "between": 0.844,
            }

            # Strategy: use backtest base rates to inform side selection.
            # For "exactly"/"between", backtest shows 85-88% NO rate.
            # Only BUY_YES if model probability SIGNIFICANTLY exceeds backtest YES rate.
            no_rate = backtest_no_rates.get(info.direction, 0.85)
            backtest_yes_rate = 1.0 - no_rate

            if edge > 0:
                # Model says YES more likely than market price.
                # But check: is model_prob actually above the backtest YES rate?
                # If not, the "edge" is noise — backtest says NO is overwhelmingly likely.
                if (
                    info.direction in ("exactly", "between")
                    and model_prob < backtest_yes_rate + 0.05
                ):
                    # Model prob barely above backtest base rate — unreliable, skip
                    _log(
                        f"Weather SKIP: {info.city} {info.direction} — "
                        f"model YES {model_prob:.0%} < backtest YES rate {backtest_yes_rate:.0%}+5%, unreliable"
                    )
                    continue
                side = "BUY_YES"
                confidence = min(abs(edge) / 0.15, 1.0)
            else:
                side = "BUY_NO"
                confidence = min(abs(edge) / 0.15, 1.0)

            # Бонус уверенности за большое количество members
            if len(temps) >= 50:
                confidence = min(confidence * 1.1, 1.0)

            # Бонус confidence для "below" — исторически самое надёжное направление
            if info.direction == "below" and side == "BUY_NO":
                confidence = min(confidence * 1.15, 1.0)

            # Бонус confidence for BUY_NO on high-NO-rate directions
            if side == "BUY_NO" and no_rate >= 0.85:
                confidence = min(confidence * 1.1, 1.0)

            # Cross-reference с News Service прогнозом
            news_weather_str = ""
            news_forecast = fetch_news_service_weather(info.city, info.target_date)
            if news_forecast:
                ns_min = news_forecast.get("temp_min")
                ns_max = news_forecast.get("temp_max")
                ns_mean = news_forecast.get("temp_mean")
                ns_parts = []
                if ns_min is not None:
                    ns_parts.append(f"min={ns_min:.1f}")
                if ns_max is not None:
                    ns_parts.append(f"max={ns_max:.1f}")
                if ns_mean is not None:
                    ns_parts.append(f"mean={ns_mean:.1f}")
                ens_data = news_forecast.get("ensemble_data")
                if ens_data:
                    ns_parts.append(f"ensemble={ens_data}")
                if ns_parts:
                    news_weather_str = f" News Service forecast: {', '.join(ns_parts)}."
                    # Бонус confidence если оба источника согласны
                    if ns_max is not None and ns_min is not None:
                        mean_temp = sum(temps) / len(temps)
                        ns_mean_val = ns_mean if ns_mean else (ns_min + ns_max) / 2
                        if abs(mean_temp - ns_mean_val) < 3.0:
                            confidence = min(confidence * 1.05, 1.0)

            prediction = AIPrediction(
                market_id=info.market.id,
                question=info.market.question,
                ai_probability=model_prob,
                market_probability=market_prob,
                confidence=confidence,
                edge=edge,
                reasoning=(
                    f"Ensemble forecast ({len(temps)} members): "
                    f"P({info.direction} {info.threshold}°F) = {model_prob:.0%} vs market {market_prob:.0%}. "
                    f"Mean temp: {sum(temps) / len(temps):.1f}°F, "
                    f"range: {min(temps):.0f}-{max(temps):.0f}°F. "
                    f"Backtest: {info.direction} NO rate {no_rate:.0%} (12,776 markets)."
                    f"{news_weather_str}"
                ),
                recommended_side=side,
            )

            _log(
                f"WEATHER: {info.city} {info.temp_type} {info.direction} {info.threshold}°F "
                f"on {info.target_date.strftime('%b %d')} | "
                f"model: {model_prob:.0%} vs market: {market_prob:.0%} | "
                f"edge: {edge:+.0%} → {side}"
            )

            predictions.append(
                WeatherSignalData(
                    prediction=prediction,
                    city=info.city,
                    target_date=info.target_date.strftime("%Y-%m-%d"),
                    temp_type=info.temp_type,
                    direction=info.direction,
                    threshold=info.threshold,
                    ensemble_temps=temps,
                )
            )

        _log(f"Weather: {len(predictions)} сигналов с edge >= {min_edge:.0%}")
        return predictions

    finally:
        api.close()
