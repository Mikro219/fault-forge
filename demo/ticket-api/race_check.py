"""Sends concurrent reservations to a running ticket API and prints what happened.

Usage: python race_check.py --base-url http://127.0.0.1:8000 --attempts 10 --requests 20
"""

import argparse
import asyncio
import collections
import json

import httpx


async def run_attempt(base_url: str, requests: int, stock: int = 1) -> dict:
    limits = httpx.Limits(max_connections=requests + 2)
    async with httpx.AsyncClient(base_url=base_url, timeout=15, limits=limits) as client:
        (await client.post("/__ff/reset", json={"stock": stock})).raise_for_status()
        responses = await asyncio.gather(
            *[
                client.post("/reserve", json={"item_id": 1, "buyer": f"buyer-{i}"})
                for i in range(requests)
            ]
        )
        state = (await client.get("/__ff/state")).json()
    statuses = collections.Counter(response.status_code for response in responses)
    return {"statuses": {str(k): v for k, v in sorted(statuses.items())}, "state": state}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--requests", type=int, default=20)
    args = parser.parse_args()
    for attempt in range(1, args.attempts + 1):
        result = asyncio.run(run_attempt(args.base_url, args.requests))
        state = {key: result["state"][key] for key in (
            "stock", "orders_confirmed", "payments_unattached", "orders_without_payment"
        )}
        print(json.dumps({"attempt": attempt, "statuses": result["statuses"], "state": state}))


if __name__ == "__main__":
    main()
