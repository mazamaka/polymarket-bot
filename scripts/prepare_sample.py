"""Подготовка выборки рынков для анализа цен."""

import json
import random
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from analyzer.weather import parse_weather_question

DATA_DIR = PROJECT_ROOT / "data"

with open(DATA_DIR / "historical_weather_markets.json") as f:
    markets = json.load(f)

with_tokens = []
for m in markets:
    parsed = parse_weather_question(m.get("question", ""))
    if not parsed:
        continue

    op = m.get("outcomePrices", "")
    if isinstance(op, str):
        prices = json.loads(op)
    else:
        prices = op
    p0 = float(prices[0])

    tids = m.get("clobTokenIds", "")
    if tids:
        if isinstance(tids, str):
            try:
                tids = json.loads(tids)
            except Exception:
                continue
        if tids:
            with_tokens.append(
                {
                    "market_id": m["id"],
                    "question": m["question"],
                    "direction": parsed["direction"],
                    "token_id": tids[0],
                    "resolved_yes": p0 > 0.9,
                }
            )

print(f"Markets with clobTokenIds: {len(with_tokens)}")

random.seed(42)
sample = random.sample(with_tokens, min(100, len(with_tokens)))

with open(DATA_DIR / "backtest_sample.json", "w") as f:
    json.dump(sample, f, indent=2)
print(f"Saved {len(sample)} sample markets")
