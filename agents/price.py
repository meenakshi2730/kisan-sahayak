
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

DATA_FILE = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "sample_prices.json"
)

AGMARKNET_URL = (
    "https://api.agmarknet.gov.in/v1/"
    "prices-and-arrivals/commodity-price/lastweek"
)

MARKETS = [
    {"id": 99, "name": "Bengaluru APMC"},
    {"id": 100, "name": "Binny Mill (FF&V) Bengaluru APMC"},
]

STATE_ID = 16
TOMATO_ID = 65


def get_mandi_prices(crop: str = "tomato") -> dict[str, Any]:
    """Return illustrative sample prices. Kept compatible with unit tests."""

    if not crop or not crop.strip():
        return {
            "agent": "price",
            "status": "fallback",
            "source": "none",
            "prices": [],
            "message": "Please provide a crop name.",
        }

    try:
        with DATA_FILE.open("r", encoding="utf-8") as file:
            records = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {
            "agent": "price",
            "status": "fallback",
            "source": "none",
            "prices": [],
            "message": "Mandi price data is currently unavailable.",
        }

    matches = [
        record
        for record in records
        if isinstance(record.get("crop"), str)
        and record["crop"].strip().casefold() == crop.strip().casefold()
    ]

    matches.sort(
        key=lambda record: float(record.get("price_per_kg", 0)),
        reverse=True,
    )

    return {
        "agent": "price",
        "status": "success" if matches else "fallback",
        "source": "illustrative_sample" if matches else "none",
        "prices": matches,
        "message": (
            "Sample prices only; these are not verified live market prices."
            if matches
            else f"No mandi prices found for crop: {crop}."
        ),
    }


def get_live_mandi_prices(crop: str = "tomato") -> dict[str, Any]:
    """Fetch latest available reported tomato prices for Bengaluru markets."""

    if not crop or not crop.strip():
        return {
            "agent": "price",
            "status": "fallback",
            "source": "none",
            "prices": [],
            "message": "Please provide a crop name.",
        }

    if crop.strip().casefold() not in {"tomato", "tomatoes"}:
        return {
            "agent": "price",
            "status": "fallback",
            "source": "none",
            "prices": [],
            "message": (
                "Live lookup currently supports tomatoes in Bengaluru only."
            ),
        }

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://agmarknet.gov.in",
        "Referer": "https://agmarknet.gov.in/",
        "User-Agent": "Mozilla/5.0",
    }

    prices = []

    for market in MARKETS:
        params = {
            "marketId": market["id"],
            "stateId": STATE_ID,
            "commodityId": TOMATO_ID,
            "includeExcel": "false",
        }

        try:
            response = requests.get(
                AGMARKNET_URL,
                params=params,
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            continue

        if not payload.get("success"):
            continue

        date_columns = []

        for column in payload.get("columns", []):
            key = column.get("key", "")
            try:
                date_columns.append((date.fromisoformat(key), key))
            except (TypeError, ValueError):
                continue

        tomato_rows = [
            row
            for row in payload.get("data", [])
            if "tomato" in str(row.get("variety", "")).casefold()
        ]

        if not tomato_rows:
            continue

        # The API's unit applies to the returned market price values.
        unit = tomato_rows[0].get("unitOfPrice")

        # Only convert values when the source explicitly reports quintals.
        if not unit or "quintal" not in str(unit).casefold():
            continue

        latest = None

        for row in tomato_rows:
            for observed_date, key in date_columns:
                value = row.get(key)

                if value is None or str(value).strip().upper() in {
                    "", "NR", "N/A", "-"
                }:
                    continue

                try:
                    amount = float(value)
                except (TypeError, ValueError):
                    continue

                if amount <= 0:
                    continue

                if latest is None or observed_date > latest["date"]:
                    latest = {
                        "date": observed_date,
                        "price_per_quintal": amount,
                    }

        if latest is None:
            continue

        price_per_quintal = latest["price_per_quintal"]

        prices.append({
            "mandi": market["name"],
            "crop": "tomato",
            "price_per_kg": round(price_per_quintal / 100, 2),
            "price_per_quintal": price_per_quintal,
            "unit": unit,
            "observation_date": latest["date"].isoformat(),
            "source": "AGMARKNET",
            "market_id": market["id"],
        })

    prices.sort(
        key=lambda item: item["price_per_kg"],
        reverse=True,
    )

    if prices:
        return {
            "agent": "price",
            "status": (
                "success" if len(prices) == len(MARKETS) else "partial"
            ),
            "source": "AGMARKNET",
            "prices": prices,
            "message": (
                "Latest available reported tomato prices for Bengaluru. "
                "Prices are historical observations, not guaranteed "
                "to be today's prices."
            ),
        }

    fallback = get_mandi_prices(crop)

    return {
        **fallback,
        "status": "fallback",
        "message": (
            "Live Bengaluru tomato prices are unavailable or unreported. "
            + fallback["message"]
        ),
    }


# Fetch.ai Chat Protocol agent
price_seed = os.getenv("KISAN_PRICE_SEED")

if not price_seed:
    raise RuntimeError(
        "Set the KISAN_PRICE_SEED environment variable before running the agent."
    )

price_agent = Agent(
    name="kisan_mandi_price_agent",
    seed=price_seed,
    port=8001,
    mailbox=True,
)

chat_protocol = Protocol(spec=chat_protocol_spec)


@chat_protocol.on_message(ChatMessage)
async def handle_chat_message(
    ctx: Context,
    sender: str,
    msg: ChatMessage,
):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=msg.timestamp,
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    for item in msg.content:
        if not isinstance(item, TextContent):
            continue

        query = item.text.strip()
        words = [
            word.strip(".,!?;:")
            for word in query.casefold().split()
        ]

        crop = (
            "tomato"
            if any(word in {"tomato", "tomatoes"} for word in words)
            else query
        )

        result = get_live_mandi_prices(crop)

        if result["prices"]:
            lines = []

            for price in result["prices"]:
                if result["source"] == "AGMARKNET":
                    lines.append(
                        f'{price["mandi"]}: '
                        f'₹{price["price_per_kg"]}/kg '
                        f'(reported {price["observation_date"]})'
                    )
                else:
                    lines.append(
                        f'{price["mandi"]}: '
                        f'₹{price["price_per_kg"]}/kg '
                        "(illustrative sample, not live)"
                    )

            response = result["message"] + "\n" + "\n".join(lines)
        else:
            response = result["message"]

        await ctx.send(
            sender,
            ChatMessage(content=[TextContent(text=response)]),
        )


@chat_protocol.on_message(ChatAcknowledgement)
async def handle_chat_acknowledgement(
    ctx: Context,
    sender: str,
    msg: ChatAcknowledgement,
):
    pass


price_agent.include(chat_protocol, publish_manifest=True)


if __name__ == "__main__":
    price_agent.run()
