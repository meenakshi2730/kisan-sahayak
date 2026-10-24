
import os
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
)

load_dotenv()

ORCHESTRATOR_ADDRESS = (
    "agent1qvwfp8g5hvqsrqt3saprn9ndf9d7xlx0ztctd5ajakpnacp2ppujg5z64lq"
)

test_agent = Agent(
    name="kisan-sahayak-test-client",
    seed=os.getenv(
        "TEST_CLIENT_SEED",
        "kisan-sahayak-local-test-client-seed",
    ),
    port=8004,
    mailbox=True,
)

chat_protocol = Protocol(name="KisanSahayakTestChat")


@chat_protocol.on_message(model=ChatAcknowledgement)
async def on_acknowledgement(
    ctx: Context, sender: str, msg: ChatAcknowledgement
):
    ctx.logger.info("Acknowledgement received from %s", sender)


@chat_protocol.on_message(model=ChatMessage)
async def on_message(ctx: Context, sender: str, msg: ChatMessage):
    for item in msg.content:
        if getattr(item, "type", None) == "text":
            ctx.logger.info("FARMER REPORT:\n%s", item.text)


@test_agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info("Test client started: %s", ctx.agent.address)

    message = (
        "I am a tomato farmer in Kolar with 40 quintals of tomatoes. "
        "Compare Bengaluru markets using mandi prices, weather and "
        "spoilage risk, and transportation cost. Recommend which "
        "market I should sell to."
    )

    await ctx.send(
        ORCHESTRATOR_ADDRESS,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=message)],
        ),
    )
    ctx.logger.info("Farmer request sent to orchestrator.")


test_agent.include(chat_protocol)

if __name__ == "__main__":
    test_agent.run()
