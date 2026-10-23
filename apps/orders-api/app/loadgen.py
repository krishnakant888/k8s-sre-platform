"""Steady background traffic so dashboards and the SLO have data. ~RATE requests per second."""
import asyncio
import os
import random

import httpx

TARGET = os.getenv("TARGET", "http://orders-api")
RATE = float(os.getenv("RATE", "5"))
ITEMS = ["widget", "gadget", "bolt", "nut", "gear"]


async def one(client: httpx.AsyncClient) -> None:
    roll = random.random()
    try:
        if roll < 0.5:
            await client.get("/orders")
        elif roll < 0.9:
            await client.post("/orders", json={"item": random.choice(ITEMS), "qty": random.randint(1, 5)})
        else:
            await client.get(f"/orders/{random.randint(1, 50)}")
    except httpx.HTTPError:
        pass  # the api being down is a valid state during incidents


async def main() -> None:
    async with httpx.AsyncClient(base_url=TARGET, timeout=10) as client:
        while True:
            asyncio.create_task(one(client))
            await asyncio.sleep(random.expovariate(RATE))


if __name__ == "__main__":
    asyncio.run(main())
