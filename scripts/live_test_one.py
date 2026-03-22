"""Quick live test: buy 1 NO position for $2 on best weather signal."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coldmath_bot import (
    BotConfig,
    ClobTrader,
    execute_trades,
    print_scan_results,
    scan_weather_markets,
)

config = BotConfig(
    trade_size_usd=2.0,
    max_positions=1,
    max_total_exposure=5.0,
    max_days_ahead=5,
    private_key=os.environ["PK"],
    funder_address=os.environ.get("FUNDER", ""),
)

print("Scanning for best signal...")
results = scan_weather_markets(config)
print_scan_results(results)

if not results:
    print("No signals found")
    sys.exit(0)

best = results[0]
print(f"\nBest signal: {best.market.question}")
print(
    f"  City: {best.city} | Direction: {best.direction} | Threshold: {best.threshold}°F"
)
print(
    f"  Model NO: {best.model_prob_no:.1%} | Market NO: {best.market_price_no:.1%} | Edge: {best.edge:+.1%}"
)
print(f"  NO token: {best.no_token_id[:20]}...")

confirm = input("\nBuy NO for $2.00? [y/N]: ")
if confirm.lower() != "y":
    print("Cancelled")
    sys.exit(0)

print("\nConnecting to CLOB API...")
trader = ClobTrader(config)

print("Placing order...")
execute_trades([best], config, trader=trader, paper=False)
print("Done!")
