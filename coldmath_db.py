"""ColdMath PostgreSQL storage.

Хранит позиции, историю, сигналы и сканы.
Поддерживает аналитику: win rate по городам/directions, ROI timeline.
"""

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger("coldmath.db")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_dsn() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql://coldmath:coldmath@coldmath-db:5432/coldmath",
    )


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        with _pool_lock:
            if _pool is None or _pool.closed:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1, maxconn=5, dsn=_get_dsn()
                )
    return _pool


@contextmanager
def get_conn() -> Generator:
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def init_db() -> None:
    """Create tables if not exist."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id SERIAL PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    city TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL DEFAULT '',
                    threshold REAL DEFAULT 0,
                    target_date DATE,
                    no_token_id TEXT DEFAULT '',
                    entry_price REAL NOT NULL,
                    size_usd REAL NOT NULL,
                    shares REAL NOT NULL DEFAULT 0,
                    model_prob_no REAL DEFAULT 0,
                    edge REAL DEFAULT 0,
                    ensemble_count INT DEFAULT 0,
                    order_id TEXT DEFAULT '',
                    opened_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resolved_at TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'open',
                    pnl REAL,
                    paper BOOLEAN DEFAULT FALSE,
                    UNIQUE(market_id, opened_at)
                );

                CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
                CREATE INDEX IF NOT EXISTS idx_positions_market_id ON positions(market_id);

                CREATE TABLE IF NOT EXISTS signals (
                    id SERIAL PRIMARY KEY,
                    scan_id INT,
                    market_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    city TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL DEFAULT '',
                    threshold REAL DEFAULT 0,
                    target_date DATE,
                    temp_type TEXT DEFAULT '',
                    model_prob_yes REAL DEFAULT 0,
                    model_prob_no REAL DEFAULT 0,
                    market_price_yes REAL DEFAULT 0,
                    market_price_no REAL DEFAULT 0,
                    edge REAL DEFAULT 0,
                    ensemble_count INT DEFAULT 0,
                    ensemble_temps JSONB,
                    days_ahead INT DEFAULT 0,
                    threshold_high REAL,
                    action TEXT DEFAULT 'skip',
                    skip_reason TEXT DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
                CREATE INDEX IF NOT EXISTS idx_signals_city ON signals(city);
                CREATE INDEX IF NOT EXISTS idx_signals_direction ON signals(direction);

                -- Add columns if missing (for existing DBs)
                ALTER TABLE signals ADD COLUMN IF NOT EXISTS days_ahead INT DEFAULT 0;
                ALTER TABLE signals ADD COLUMN IF NOT EXISTS threshold_high REAL;

                CREATE TABLE IF NOT EXISTS price_snapshots (
                    id SERIAL PRIMARY KEY,
                    market_id TEXT NOT NULL,
                    no_price REAL NOT NULL,
                    yes_price REAL NOT NULL,
                    scan_id INT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_market ON price_snapshots(market_id);
                CREATE INDEX IF NOT EXISTS idx_snapshots_created ON price_snapshots(created_at);

                CREATE TABLE IF NOT EXISTS scans (
                    id SERIAL PRIMARY KEY,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    weather_markets INT DEFAULT 0,
                    forecasts_ok INT DEFAULT 0,
                    forecasts_failed INT DEFAULT 0,
                    signals_found INT DEFAULT 0,
                    trades_made INT DEFAULT 0,
                    balance_before REAL,
                    balance_after REAL,
                    status TEXT DEFAULT 'ok',
                    duration_sec REAL DEFAULT 0
                );
            """)
    logger.info("Database initialized")


# ── Position CRUD ────────────────────────────────────────────────────────


def save_position(pos: dict[str, Any]) -> int:
    """Insert or update position. Returns position id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO positions (
                    market_id, question, city, direction, threshold,
                    target_date, no_token_id, entry_price, size_usd, shares,
                    model_prob_no, edge, ensemble_count, order_id,
                    opened_at, status, pnl, paper
                ) VALUES (
                    %(market_id)s, %(question)s, %(city)s, %(direction)s, %(threshold)s,
                    %(target_date)s, %(no_token_id)s, %(entry_price)s, %(size_usd)s, %(shares)s,
                    %(model_prob_no)s, %(edge)s, %(ensemble_count)s, %(order_id)s,
                    %(opened_at)s, %(status)s, %(pnl)s, %(paper)s
                )
                ON CONFLICT (market_id, opened_at) DO UPDATE SET
                    status = EXCLUDED.status,
                    pnl = EXCLUDED.pnl,
                    resolved_at = EXCLUDED.resolved_at
                RETURNING id
                """,
                {
                    "market_id": pos.get("market_id", ""),
                    "question": pos.get("question", ""),
                    "city": pos.get("city", ""),
                    "direction": pos.get("direction", ""),
                    "threshold": pos.get("threshold", 0),
                    "target_date": pos.get("target_date") or None,
                    "no_token_id": pos.get("no_token_id", ""),
                    "entry_price": pos.get("entry_price", 0),
                    "size_usd": pos.get("size_usd", 0),
                    "shares": pos.get("shares", 0),
                    "model_prob_no": pos.get("model_prob_no", 0),
                    "edge": pos.get("edge", 0),
                    "ensemble_count": pos.get("ensemble_count", 0),
                    "order_id": pos.get("order_id", ""),
                    "opened_at": pos.get(
                        "opened_at", datetime.now(tz=timezone.utc).isoformat()
                    ),
                    "status": pos.get("status", "open"),
                    "pnl": pos.get("pnl"),
                    "paper": pos.get("paper", False),
                },
            )
            row = cur.fetchone()
            return row[0] if row else 0


def resolve_position(market_id: str, status: str, pnl: float) -> None:
    """Mark position as won/lost."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE positions
                SET status = %s, pnl = %s, resolved_at = NOW()
                WHERE market_id = %s AND status = 'open'
                """,
                (status, pnl, market_id),
            )


def save_price_snapshots(snapshots: list[dict], scan_id: int | None = None) -> int:
    """Bulk insert price snapshots for open positions."""
    if not snapshots:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            values = []
            for s in snapshots:
                values.append(
                    cur.mogrify(
                        "(%s,%s,%s,%s)",
                        (
                            s["market_id"],
                            s["no_price"],
                            s["yes_price"],
                            scan_id,
                        ),
                    )
                )
            query = (
                b"INSERT INTO price_snapshots (market_id, no_price, yes_price, scan_id) VALUES "
                + b",".join(values)
            )
            cur.execute(query)
            return len(snapshots)


def get_price_history(market_id: str) -> list[dict]:
    """Get price history for a specific position."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT no_price, yes_price, created_at
                FROM price_snapshots
                WHERE market_id = %s
                ORDER BY created_at
                """,
                (market_id,),
            )
            return [dict(r) for r in cur.fetchall()]


def get_open_positions() -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM positions WHERE status = 'open' ORDER BY opened_at"
            )
            return [dict(r) for r in cur.fetchall()]


def get_open_market_ids() -> set[str]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT market_id FROM positions WHERE status = 'open'")
            return {r[0] for r in cur.fetchall()}


# ── Signals ──────────────────────────────────────────────────────────────


def save_signal(sig: dict[str, Any], scan_id: int | None = None) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signals (
                    scan_id, market_id, question, city, direction,
                    threshold, target_date, temp_type,
                    model_prob_yes, model_prob_no,
                    market_price_yes, market_price_no,
                    edge, ensemble_count, ensemble_temps,
                    action, skip_reason
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s
                ) RETURNING id
                """,
                (
                    scan_id,
                    sig.get("market_id", ""),
                    sig.get("question", ""),
                    sig.get("city", ""),
                    sig.get("direction", ""),
                    sig.get("threshold", 0),
                    sig.get("target_date") or None,
                    sig.get("temp_type", ""),
                    sig.get("model_prob_yes", 0),
                    sig.get("model_prob_no", 0),
                    sig.get("market_price_yes", 0),
                    sig.get("market_price_no", 0),
                    sig.get("edge", 0),
                    sig.get("ensemble_count", 0),
                    json.dumps(sig.get("ensemble_temps", [])),
                    sig.get("action", "skip"),
                    sig.get("skip_reason", ""),
                ),
            )
            row = cur.fetchone()
            return row[0] if row else 0


def save_signals_batch(signals: list[dict], scan_id: int | None = None) -> int:
    """Bulk insert signals. Returns count inserted."""
    if not signals:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            values = []
            for sig in signals:
                values.append(
                    cur.mogrify(
                        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            scan_id,
                            sig.get("market_id", ""),
                            sig.get("question", "")[:500],
                            sig.get("city", ""),
                            sig.get("direction", ""),
                            sig.get("threshold", 0),
                            sig.get("target_date") or None,
                            sig.get("temp_type", ""),
                            sig.get("model_prob_yes", 0),
                            sig.get("model_prob_no", 0),
                            sig.get("market_price_yes", 0),
                            sig.get("market_price_no", 0),
                            sig.get("edge", 0),
                            sig.get("ensemble_count", 0),
                            json.dumps(sig.get("ensemble_temps", [])),
                            sig.get("days_ahead", 0),
                            sig.get("action", "skip"),
                            sig.get("skip_reason", ""),
                        ),
                    )
                )
            query = b"""
                INSERT INTO signals (
                    scan_id, market_id, question, city, direction,
                    threshold, target_date, temp_type,
                    model_prob_yes, model_prob_no,
                    market_price_yes, market_price_no,
                    edge, ensemble_count, ensemble_temps,
                    days_ahead, action, skip_reason
                ) VALUES """ + b",".join(values)
            cur.execute(query)
            return len(signals)


# ── Scans ────────────────────────────────────────────────────────────────


def start_scan() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO scans DEFAULT VALUES RETURNING id")
            row = cur.fetchone()
            return row[0] if row else 0


def finish_scan(
    scan_id: int,
    *,
    weather_markets: int = 0,
    forecasts_ok: int = 0,
    forecasts_failed: int = 0,
    signals_found: int = 0,
    trades_made: int = 0,
    balance_before: float | None = None,
    balance_after: float | None = None,
    status: str = "ok",
    duration_sec: float = 0,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scans SET
                    weather_markets = %s, forecasts_ok = %s, forecasts_failed = %s,
                    signals_found = %s, trades_made = %s,
                    balance_before = %s, balance_after = %s,
                    status = %s, duration_sec = %s
                WHERE id = %s
                """,
                (
                    weather_markets,
                    forecasts_ok,
                    forecasts_failed,
                    signals_found,
                    trades_made,
                    balance_before,
                    balance_after,
                    status,
                    duration_sec,
                    scan_id,
                ),
            )


# ── Analytics ────────────────────────────────────────────────────────────


def get_analytics() -> dict:
    """Аналитика для дашборда."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Overall stats
            cur.execute("""
                SELECT
                    COUNT(*) FILTER (WHERE status = 'open') as open_count,
                    COUNT(*) FILTER (WHERE status = 'won') as won_count,
                    COUNT(*) FILTER (WHERE status = 'lost') as lost_count,
                    COALESCE(SUM(pnl) FILTER (WHERE status IN ('won','lost')), 0) as total_pnl,
                    COALESCE(SUM(size_usd) FILTER (WHERE status = 'open'), 0) as exposure,
                    ROUND(AVG(edge)::numeric FILTER (WHERE status = 'won'), 4) as avg_edge_won,
                    ROUND(AVG(edge)::numeric FILTER (WHERE status = 'lost'), 4) as avg_edge_lost
                FROM positions
            """)
            overall = dict(cur.fetchone())

            # Win rate by direction
            cur.execute("""
                SELECT
                    direction,
                    COUNT(*) FILTER (WHERE status = 'won') as wins,
                    COUNT(*) FILTER (WHERE status = 'lost') as losses,
                    COALESCE(SUM(pnl), 0) as pnl
                FROM positions
                WHERE status IN ('won', 'lost') AND direction != ''
                GROUP BY direction
                ORDER BY pnl DESC
            """)
            by_direction = [dict(r) for r in cur.fetchall()]

            # Win rate by city (top 10)
            cur.execute("""
                SELECT
                    city,
                    COUNT(*) FILTER (WHERE status = 'won') as wins,
                    COUNT(*) FILTER (WHERE status = 'lost') as losses,
                    COALESCE(SUM(pnl), 0) as pnl
                FROM positions
                WHERE status IN ('won', 'lost') AND city != ''
                GROUP BY city
                ORDER BY (COUNT(*) FILTER (WHERE status = 'won') + COUNT(*) FILTER (WHERE status = 'lost')) DESC
                LIMIT 10
            """)
            by_city = [dict(r) for r in cur.fetchall()]

            # Daily P&L (last 14 days)
            cur.execute("""
                SELECT
                    DATE(resolved_at) as day,
                    COUNT(*) as trades,
                    COALESCE(SUM(pnl), 0) as pnl,
                    COUNT(*) FILTER (WHERE status = 'won') as wins
                FROM positions
                WHERE resolved_at IS NOT NULL
                    AND resolved_at > NOW() - INTERVAL '14 days'
                GROUP BY DATE(resolved_at)
                ORDER BY day
            """)
            daily_pnl = [dict(r) for r in cur.fetchall()]

            # Signal stats (last 24h)
            cur.execute("""
                SELECT
                    COUNT(*) as total_signals,
                    COUNT(*) FILTER (WHERE action = 'trade') as traded,
                    COUNT(*) FILTER (WHERE action = 'skip') as skipped,
                    ROUND(AVG(edge)::numeric, 4) as avg_edge
                FROM signals
                WHERE created_at > NOW() - INTERVAL '24 hours'
            """)
            signal_stats = dict(cur.fetchone())

            # Scan stats (last 24h)
            cur.execute("""
                SELECT
                    COUNT(*) as scan_count,
                    COALESCE(SUM(trades_made), 0) as total_trades,
                    COALESCE(SUM(signals_found), 0) as total_signals,
                    ROUND(AVG(duration_sec)::numeric, 1) as avg_duration
                FROM scans
                WHERE started_at > NOW() - INTERVAL '24 hours'
            """)
            scan_stats = dict(cur.fetchone())

            return {
                "overall": overall,
                "by_direction": by_direction,
                "by_city": by_city,
                "daily_pnl": daily_pnl,
                "signal_stats_24h": signal_stats,
                "scan_stats_24h": scan_stats,
            }


# ── Migration from JSON ─────────────────────────────────────────────────


def migrate_from_json(positions_file: str, history_file: str) -> dict[str, int]:
    """Import existing JSON data into PostgreSQL."""
    imported = {"positions": 0, "history": 0}

    # Import open positions
    try:
        with open(positions_file) as f:
            positions = json.load(f)
        for pos in positions:
            pos.setdefault("status", "open")
            pos.setdefault("pnl", None)
            save_position(pos)
            imported["positions"] += 1
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error("Failed to import positions: %s", e)

    # Import history (resolved positions)
    try:
        with open(history_file) as f:
            history = json.load(f)
        for pos in history:
            save_position(pos)
            imported["history"] += 1
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error("Failed to import history: %s", e)

    logger.info(
        "Migration complete: %d positions, %d history",
        imported["positions"],
        imported["history"],
    )
    return imported
