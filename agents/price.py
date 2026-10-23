
import json
import os
from pathlib import Path
from typing import Any

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


def get_mandi_prices(crop: str = "tomato") -> dict[str, Any]:
    """Return available mandi prices for the requested crop."""

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


# Fetch.ai Chat Protocol interface
# Set KISAN_PRICE_SEED in your environment before running.
# Never commit a real deployment seed to GitHub.
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
    # Acknowledge the incoming chat message.
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

        # Basic crop extraction for the first version.
        words = query.lower().split()
        crop = "tomato" if "tomato" in words else query

        result = get_mandi_prices(crop)

        if result["prices"]:
            lines = [
                f'{p["mandi"]}: ₹{p["price_per_kg"]}/kg'
                for p in result["prices"]
            ]
            response = (
                "Illustrative sample mandi prices "
                "(NOT verified live market data):\n"
                + "\n".join(lines)
            )
        else:
            response = result["message"]

        await ctx.send(
            sender,
            ChatMessage(
                content=[TextContent(text=response)],
            ),
        )

@chat_protocol.on_message(ChatAcknowledgement)
async def handle_chat_acknowledgement(
    ctx: Context,
    sender: str,
    msg: ChatAcknowledgement,
):
    # Acknowledgements confirm receipt; no further action is needed.
    pass

price_agent.include(chat_protocol, publish_manifest=True)


if __name__ == "__main__":
    # Keep direct script execution focused on running the agent.
    price_agent.run()
