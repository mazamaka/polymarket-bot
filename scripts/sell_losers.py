"""Sell losing YES positions from old bot."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coldmath_bot import BotConfig, ClobTrader

config = BotConfig(
    private_key=os.environ["PK"],
    funder_address=os.environ.get("FUNDER", ""),
)

# Losing YES positions to sell at market
losers = [
    {
        "name": "Munich 10°C Mar 20",
        "token_id": "93717964665916372942434814478358203729448445113001506269290435086501247475739",
        "size": 7.96,
        "price": 0.015,  # sell at 1.5¢ (cur 1.75¢, give margin)
    },
    {
        "name": "Paris 13°C Mar 21",
        "token_id": "67225663869849614194460289000662707792509509261888405578352049455350610658783",
        "size": 5.0,
        "price": 0.025,  # sell at 2.5¢ (cur 2.95¢)
    },
    {
        "name": "Buenos Aires 27°C Mar 20",
        "token_id": "31749081006726038127827713403926043537921266928653505277887052634720105895002",
        "size": 5.0,
        "price": 0.06,  # sell at 6¢ (cur 7¢)
    },
    {
        "name": "Paris 15°C Mar 19",
        "token_id": "48377367777934168757893861661412009493667117701915075427808249446691653227008",
        "size": 1.53,
        "price": 0.008,  # sell at 0.8¢ (cur 1¢)
    },
]

print("Connecting to CLOB API...")
trader = ClobTrader(config)

for pos in losers:
    print(f"\nSelling {pos['name']}: {pos['size']} shares @ {pos['price']}")
    try:
        from py_clob_client.clob_types import OrderArgs

        order_args = OrderArgs(
            token_id=pos["token_id"],
            price=round(pos["price"], 4),
            size=round(pos["size"], 2),
            side="SELL",
        )
        signed = trader.client.create_order(order_args)
        result = trader.client.post_order(signed)
        print(f"  Result: {result}")
    except Exception as e:
        print(f"  Error: {e}")
    time.sleep(2)

print("\nDone!")
