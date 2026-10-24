"""
ping_agent.py - send ONE test message to a local agent and print what comes back.
Run from the repo root with the venv active:

    python ping_agent.py price
    python ping_agent.py transport
    python ping_agent.py weather
    python ping_agent.py orchestrator
    python ping_agent.py orchestrator-hinglish

It reads the agent addresses from .env (PRICE_AGENT_ADDRESS, TRANSPORT_AGENT_ADDRESS,
WEATHER_AGENT_ADDRESS, ORCHESTRATOR_ADDRESS). Optional 2nd argument = seconds to wait for replies.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

load_dotenv()

PRESETS = {
    "price": ("PRICE_AGENT_ADDRESS", 25, json.dumps(
        {"request_id": "test-price", "crop": "tomato", "location": "Kolar", "quantity_quintals": 40})),
    "transport": ("TRANSPORT_AGENT_ADDRESS", 25, json.dumps(
        {"request_id": "test-transport",
         "origin": {"name": "Kolar", "latitude": 13.1358, "longitude": 78.1326},
         "destinations": [{"name": "Bengaluru APMC", "latitude": 13.0235, "longitude": 77.5470, "distance_km": 84}],
         "quantity_quintals": 40})),
    "weather": ("WEATHER_AGENT_ADDRESS", 25, json.dumps(
        {"request_id": "test-weather", "crop_type": "tomato", "crop_condition": "not specified",
         "harvest_status": "unknown", "location": "Kolar"})),
    "orchestrator": ("ORCHESTRATOR_ADDRESS", 60,
                     "I have 40 quintal tomatoes in Kolar, where should I sell?"),
    "orchestrator-hinglish": ("ORCHESTRATOR_ADDRESS", 60,
                              "Mere paas 40 quintal tamatar hai, Kolar mein hoon. Kahan bechun?"),
}

if len(sys.argv) < 2 or sys.argv[1] not in PRESETS:
    print("Usage: python ping_agent.py [price|transport|weather|orchestrator|orchestrator-hinglish] [wait_seconds]")
    sys.exit(1)

ENV_NAME, DEFAULT_WAIT, TEXT = PRESETS[sys.argv[1]]
TARGET = os.getenv(ENV_NAME, "").strip()
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_WAIT
if not TARGET.startswith("agent1q"):
    print(f"{ENV_NAME} is missing or invalid in .env. Start that agent, copy the 'agent1q...' address "
          "it prints, and put it in .env.")
    sys.exit(1)

client = Agent(name="ping-client", seed="ping-client-local-test-seed-xyz-2026", port=8099,
               endpoint=["http://127.0.0.1:8099/submit"])
proto = Protocol(spec=chat_protocol_spec)


async def _stop_later():
    await asyncio.sleep(WAIT)
    print(f"\n[done waiting {WAIT:.0f}s]", flush=True)
    os._exit(0)


@client.on_event("startup")
async def go(ctx: Context):
    print(f"Waiting 10 s for registration, then sending to {sys.argv[1]} ({TARGET[:18]}...)", flush=True)
    await asyncio.sleep(10)
    await ctx.send(TARGET, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=TEXT)]))
    print(f"Sent. Waiting up to {WAIT:.0f}s for replies...", flush=True)
    asyncio.create_task(_stop_later())


@proto.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                               acknowledged_msg_id=msg.msg_id))
    for item in msg.content:
        if isinstance(item, TextContent):
            try:
                shown = json.dumps(json.loads(item.text), indent=2, ensure_ascii=False)
            except ValueError:
                shown = item.text
            print("\n=== REPLY ===\n" + shown, flush=True)


@proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


client.include(proto)

if __name__ == "__main__":
    client.run()