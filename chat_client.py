"""
Kisan Sahayak - terminal chat client.
Lets you type as the farmer and chat with the orchestrator from the command line.

Setup: add ORCHESTRATOR_ADDRESS=agent1... to the root .env
(the orchestrator prints its address when it starts).
Run:   python chat_client.py      (in a NEW terminal, with the other agents running)
"""
import asyncio
import os
import queue
import sys
import threading
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

ORCHESTRATOR_ADDRESS = os.getenv("ORCHESTRATOR_ADDRESS", "").strip()
if not ORCHESTRATOR_ADDRESS:
    raise RuntimeError("Set ORCHESTRATOR_ADDRESS (agent1...) in the root .env file.")

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

client = Agent(
    name="kisan-farmer-client",
    seed=os.getenv("CLIENT_SEED", "kisan farmer terminal client seed"),
    port=8010,
    endpoint=["http://127.0.0.1:8010/submit"],
    loop=loop,
)
chat_protocol = Protocol(spec=chat_protocol_spec)
inbox: "queue.Queue[str]" = queue.Queue()


def read_keyboard():
    while True:
        try:
            line = input()
        except EOFError:
            return
        if line.strip():
            inbox.put(line.strip())


@client.on_event("startup")
async def startup(ctx: Context):
    threading.Thread(target=read_keyboard, daemon=True).start()
    print("\n=== Kisan Sahayak chat ===")
    print("Type 'hi' to begin. Type 'reset' to start over. Ctrl+C to quit.\n")


@client.on_interval(period=0.5)
async def send_typed_messages(ctx: Context):
    while not inbox.empty():
        text = inbox.get()
        await ctx.send(
            ORCHESTRATOR_ADDRESS,
            ChatMessage(
                timestamp=datetime.now(timezone.utc),
                msg_id=uuid4(),
                content=[TextContent(type="text", text=text)],
            ),
        )


@chat_protocol.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ),
    )
    text = "\n".join(i.text for i in msg.content if isinstance(i, TextContent))
    print(f"\nKisan Sahayak:\n{text}\n")


@chat_protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


client.include(chat_protocol)

if __name__ == "__main__":
    client.run()