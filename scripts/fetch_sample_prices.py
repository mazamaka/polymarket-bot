"""Получение ценовой истории для выборки рынков и расчёт PnL."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
CLOB_API_URL = "https://clob.polymarket.com"


async def fetch_price_history(
    client: httpx.AsyncClient, token_id: str
) -> list[dict] | None:
    """Получить историю цен."""
    try:
        resp = await client.get(
            f"{CLOB_API_URL}/prices-history",
            params={"market": token_id, "interval": "max", "fidelity": 60},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("history", [])
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        print(f"  Error: {e}")
        return None


async def main() -> None:
    with open(DATA_DIR / "backtest_sample.json") as f:
        sample = json.load(f)

    results = []

    async with httpx.AsyncClient() as client:
        for i, m in enumerate(sample):
            history = await fetch_price_history(client, m["token_id"])

            if history and len(history) > 2:
                # Берём медианную цену (не первую и не последнюю)
                prices = [float(h.get("p", 0)) for h in history]
                # Убираем пост-resolution цены (0 или 1)
                trading_prices = [p for p in prices if 0.01 < p < 0.99]

                if trading_prices:
                    avg_yes_price = sum(trading_prices) / len(trading_prices)
                    mid_yes_price = trading_prices[len(trading_prices) // 2]
                else:
                    avg_yes_price = None
                    mid_yes_price = None

                m["price_points"] = len(prices)
                m["trading_prices_count"] = len(trading_prices)
                m["avg_yes_price"] = avg_yes_price
                m["mid_yes_price"] = mid_yes_price
                m["first_price"] = prices[0] if prices else None
                m["last_trading_price"] = trading_prices[-1] if trading_prices else None
            else:
                m["price_points"] = 0
                m["trading_prices_count"] = 0
                m["avg_yes_price"] = None
                m["mid_yes_price"] = None

            results.append(m)

            status = "YES" if m["resolved_yes"] else "NO"
            avg = (
                f"{m['avg_yes_price']:.3f}"
                if m.get("avg_yes_price") is not None
                else "N/A"
            )
            print(
                f"  [{i + 1}/{len(sample)}] {status} avg_yes={avg} "
                f"pts={m['price_points']} | {m['question'][:60]}"
            )

            await asyncio.sleep(0.3)

    with open(DATA_DIR / "backtest_sample_with_prices.json", "w") as f:
        json.dump(results, f, indent=2)

    # Анализ PnL
    print("\n" + "=" * 70)
    print("  PnL ANALYSIS (Always Bet NO strategy)")
    print("=" * 70)

    total_pnl = 0.0
    wins = 0
    losses = 0
    trades = 0

    by_direction = {}

    for m in results:
        avg = m.get("avg_yes_price")
        if avg is None:
            continue

        trades += 1
        no_price = 1.0 - avg

        if m["resolved_yes"]:
            pnl = -no_price  # loss
            losses += 1
        else:
            pnl = avg  # profit = yes_price
            wins += 1

        total_pnl += pnl

        d = m["direction"]
        if d not in by_direction:
            by_direction[d] = {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0}
        by_direction[d]["count"] += 1
        by_direction[d]["pnl"] += pnl
        if pnl > 0:
            by_direction[d]["wins"] += 1
        else:
            by_direction[d]["losses"] += 1

    if trades > 0:
        print(f"\n  Total trades:  {trades}")
        print(f"  Wins:          {wins}")
        print(f"  Losses:        {losses}")
        print(f"  Win rate:      {wins / trades * 100:.1f}%")
        print(f"  Total PnL:     ${total_pnl:.4f} (per $1 per trade)")
        print(f"  Avg PnL:       ${total_pnl / trades:.4f}")
        print(f"  ROI ($20/trade): ${total_pnl * 20:.2f} on {trades} trades")

        print("\n  Per-direction:")
        for d in sorted(by_direction):
            s = by_direction[d]
            wr = s["wins"] / s["count"] * 100 if s["count"] > 0 else 0
            avg_pnl = s["pnl"] / s["count"] if s["count"] > 0 else 0
            print(
                f"    {d:12s} | n={s['count']:3d} | WR={wr:5.1f}% | "
                f"PnL=${s['pnl']:+.4f} | avg=${avg_pnl:+.4f}"
            )


if __name__ == "__main__":
    asyncio.run(main())
