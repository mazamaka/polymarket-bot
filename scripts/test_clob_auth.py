"""Test CLOB API authentication and wallet balance."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py_clob_client.client import ClobClient


def main() -> None:
    host = "https://clob.polymarket.com"
    key = os.environ["PK"]
    funder = "0x842F71005C45Ca1Ea355512EA9F162a00051C363"

    print("1. Creating CLOB client...")
    client = ClobClient(host, chain_id=137, key=key, signature_type=2)

    print("2. Deriving API credentials...")
    creds = client.derive_api_key()
    print(f"   API Key: {creds.api_key[:20]}...")
    print("   OK - credentials derived successfully")

    print("3. Creating authenticated client with funder...")
    client2 = ClobClient(
        host, chain_id=137, key=key, creds=creds, signature_type=2, funder=funder
    )

    print("4. Checking balance/allowance...")
    try:
        bal = client2.get_balance_allowance()
        print(f"   Balance: {json.dumps(bal, indent=2)}")
    except Exception as e:
        print(f"   Balance error (may need approval): {e}")

    print("5. Testing market fetch...")
    try:
        markets = client2.get_markets(next_cursor="")
        print(f"   Markets fetched: {len(markets.get('data', []))} items")
    except Exception as e:
        print(f"   Markets error: {e}")

    print("\nDone! CLOB API auth works.")


if __name__ == "__main__":
    main()
