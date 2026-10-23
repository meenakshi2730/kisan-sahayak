
import asyncio
import contextlib
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from uuid import uuid4

# Configure Windows event-loop policy before importing uagents.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )

import requests
from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

load_dotenv()

HOSTED = os.getenv("AGENTVERSE_HOSTED", "false").lower() == "true"
SEED = os.getenv("ORCHESTRATOR_SEED", "").strip()
ASI_ONE_API_KEY = os.getenv("ASI_ONE_API_KEY", "").strip()

ASI_ONE_URL = "https://api.asi1.ai/v1/chat/completions"

# Kisan Sahayak supports tomato farming only.
SUPPORTED_CROPS = {"tomato"}

# Use an explicitly managed loop for the local agent.
loop = None

if HOSTED:
    agent = Agent()
else:
    if not SEED or SEED.startswith("PASTE_"):
        raise RuntimeError(
            "Set a valid ORCHESTRATOR_SEED in the root .env file."
        )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    agent = Agent(
        name="kisan-sahayak-orchestrator",
        seed=SEED,
        port=8000,
        endpoint=["http://127.0.0.1:8000/submit"],
        loop=loop,
    )

chat_protocol = Protocol(spec=chat_protocol_spec)


def make_chat_message(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


async def reply(ctx: Context, recipient: str, text: str) -> None:
    await ctx.send(recipient, make_chat_message(text))


def extract_farmer_request(message: str) -> dict:
    """Extract and validate a tomato-selling request using ASI:One."""

    if not ASI_ONE_API_KEY or ASI_ONE_API_KEY.startswith("PASTE_"):
        raise RuntimeError(
            "Configure your actual ASI_ONE_API_KEY in the root .env file."
        )

    response = requests.post(
        ASI_ONE_URL,
        headers={
            "Authorization": f"Bearer {ASI_ONE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": "asi1",
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You extract structured information from requests "
                        "made by tomato farmers who want to sell tomatoes. "
                        "Return only one valid JSON object with exactly "
                        "these keys: crop, location, quantity_quintals. "
                        "Use null when information is missing. "
                        "The crop must reflect what the user actually "
                        "mentioned; never assume a missing crop is tomato. "
                        "Use a numeric value for quantity_quintals. "
                        "Do not invent prices, weather, or recommendations."
                    ),
                },
                {"role": "user", "content": message},
            ],
        },
        timeout=25,
    )
    response.raise_for_status()

    data = response.json()
    content = data["choices"][0]["message"]["content"]

    if not isinstance(content, str):
        raise ValueError("ASI:One returned an unexpected response.")

    content = content.strip()

    # Support JSON enclosed in Markdown code fences.
    content = re.sub(
        r"^\s*```(?:json)?\s*",
        "",
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(r"\s*```\s*$", "", content)

    # Extract the JSON object.
    match = re.search(r"\{.*\}", content, re.DOTALL)

    if not match:
        raise ValueError(
            "ASI:One did not return a valid JSON object."
        )

    result = json.loads(match.group(0))

    if not isinstance(result, dict):
        raise ValueError("ASI:One returned an invalid request.")

    crop = result.get("crop")
    location = result.get("location")
    quantity = result.get("quantity_quintals")

    if not isinstance(crop, str) or not crop.strip():
        raise ValueError(
            "Please specify the crop. Kisan Sahayak currently supports "
            "tomato farming only."
        )

    # Normalize common tomato naming variations.
    crop = crop.strip().lower()

    if crop in {"tomatoes", "tomato"}:
        crop = "tomato"

    if crop not in SUPPORTED_CROPS:
        raise ValueError(
            "Kisan Sahayak currently supports tomato farming only. "
            "Please submit a request about tomatoes."
        )

    if not isinstance(location, str) or not location.strip():
        raise ValueError(
            "Please specify your location so your tomato-selling "
            "request can be processed."
        )

    if isinstance(quantity, bool) or not isinstance(
        quantity, (int, float)
    ):
        raise ValueError(
            "Please specify the quantity of tomatoes in quintals."
        )

    quantity = float(quantity)

    if not math.isfinite(quantity) or not 0 < quantity <= 100000:
        raise ValueError(
            "Quantity must be greater than zero and at most "
            "100000 quintals."
        )

    return {
        "crop": crop,
        "location": location.strip(),
        "quantity_quintals": quantity,
    }


@chat_protocol.on_message(ChatMessage)
async def handle_farmer_message(
    ctx: Context, sender: str, msg: ChatMessage
):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc),
            acknowledged_msg_id=msg.msg_id,
        ),
    )

    for item in msg.content:
        if not isinstance(item, TextContent):
            continue

        message = item.text.strip()

        if not message:
            continue

        try:
            request = await asyncio.to_thread(
                extract_farmer_request, message
            )

            await reply(
                ctx,
                sender,
                "KISAN SAHAYAK - TOMATO FARMING ASSISTANT\n\n"
                f"Crop: {request['crop'].title()}\n"
                f"Quantity: {request['quantity_quintals']:g} quintals\n"
                f"Location: {request['location']}\n\n"
                "Your tomato-selling request has been received and "
                "validated.\n\n"
                "This version extracts and validates your request. "
                "Live tomato mandi prices, transport estimates and "
                "weather information are not yet retrieved by this "
                "handler. Those agents must be integrated and tested "
                "before a market recommendation can be provided.",
            )

        except requests.RequestException:
            ctx.logger.exception("ASI:One API request failed")
            await reply(
                ctx,
                sender,
                "ASI:One is temporarily unavailable. "
                "Please try again later.",
            )

        except (KeyError, TypeError, json.JSONDecodeError):
            ctx.logger.exception("Invalid ASI:One response")
            await reply(
                ctx,
                sender,
                "I couldn't process that request. Please specify "
                "tomato, the quantity in quintals and your location.",
            )

        except (ValueError, RuntimeError) as exc:
            await reply(ctx, sender, str(exc))


@chat_protocol.on_message(ChatAcknowledgement)
async def handle_acknowledgement(
    ctx: Context, sender: str, msg: ChatAcknowledgement
):
    ctx.logger.info(
        f"Chat acknowledgement received from {sender}"
    )


@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info(
        f"Kisan Sahayak Orchestrator started: {ctx.agent.address}"
    )


agent.include(chat_protocol, publish_manifest=True)


if __name__ == "__main__" and not HOSTED:
    print("Starting Kisan Sahayak orchestrator...")

    try:
        loop.create_task(agent.run_async())
        loop.run_forever()

    except KeyboardInterrupt:
        print("Stopping Kisan Sahayak orchestrator...")

    finally:
        pending = asyncio.all_tasks(loop)

        for task in pending:
            task.cancel()

        with contextlib.suppress(Exception):
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )

        with contextlib.suppress(Exception):
            loop.run_until_complete(asyncio.sleep(0.25))

        loop.close()
