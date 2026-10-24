import asyncio
import os
import sys
import json
import time
from pathlib import Path
from datetime import date
from typing import Any

import requests
from dotenv import load_dotenv
load_dotenv()

# Windows compatibility for aiohttp / asyncio.
# Set this BEFORE importing uagents or creating the event loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    ChatAcknowledgement,
    TextContent,
    chat_protocol_spec,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "sample_prices.json"
CACHE_FILE = ROOT / "price_cache.json"

AGMARKNET_URL = (
    "https://api.agmarknet.gov.in/v1/"
    "prices-and-arrivals/commodity-price/lastweek"
)

# To add more live mandis, add {"id": <agmarknet market id>, "name": "<name>"}.
MARKETS = [
    {"id": 99, "name": "Bengaluru APMC"},
    {"id": 100, "name": "Binny Mill (FF&V) Bengaluru APMC"},
]

STATE_ID = 16
TOMATO_ID = 65

MIN_OPTIONS = 3          # always give the farmer at least this many mandis to compare
FRESH_S = 3600           # reuse live prices for 1 hour
STALE_S = 86400          # if Agmarknet fails, reuse live prices up to 24 hours old

# Last-resort illustrative rows, used only if the sample file has too few mandis.
DEFAULT_SAMPLE = [
    {"mandi": "Kolar APMC", "price_per_kg": 8.0},
    {"mandi": "Yeshwanthpur APMC", "price_per_kg": 14.0},
    {"mandi": "KR Market Bengaluru", "price_per_kg": 12.5},
    {"mandi": "Chikkaballapur APMC", "price_per_kg": 10.0},
]


# --------------------------------------------------
# SAMPLE DATA
# --------------------------------------------------

def get_mandi_prices(crop: str = "tomato") -> dict[str, Any]:
    """Return illustrative sample prices."""

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


def top_up_with_samples(live: list[dict], crop: str) -> list[dict]:
    """Return extra illustrative rows so live + extra has at least MIN_OPTIONS."""
    names = {str(p.get("mandi", "")).strip().casefold() for p in live}
    pool = list(get_mandi_prices(crop).get("prices", [])) + DEFAULT_SAMPLE
    added: list[dict] = []

    for record in pool:
        if len(live) + len(added) >= MIN_OPTIONS:
            break
        name = str(record.get("mandi", "")).strip()
        if not name or name.casefold() in names:
            continue
        try:
            price_kg = round(float(record.get("price_per_kg", 0)), 2)
        except (TypeError, ValueError):
            continue
        if price_kg <= 0:
            continue
        names.add(name.casefold())
        added.append({
            "mandi": name,
            "crop": "tomato",
            "price_per_kg": price_kg,
            "price_per_quintal": round(price_kg * 100, 2),
            "source": "illustrative_sample",
        })

    return added


# --------------------------------------------------
# LIVE DATA (Agmarknet) WITH DISK CACHE
# --------------------------------------------------

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(prices: list[dict]) -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps({"t": time.time(), "prices": prices}, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass


def fetch_live_prices() -> list[dict]:
    """Query Agmarknet for each market. Returns only markets with a usable price."""

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
                AGMARKNET_URL, params=params, headers=headers, timeout=20
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            continue

        if not isinstance(payload, dict) or not payload.get("success"):
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

        unit = tomato_rows[0].get("unitOfPrice")

        # Convert only when the API explicitly reports quintals.
        if not unit or "quintal" not in str(unit).casefold():
            continue

        latest = None
        for row in tomato_rows:
            for observed_date, key in date_columns:
                value = row.get(key)
                if value is None or str(value).strip().upper() in {"", "NR", "N/A", "-"}:
                    continue
                try:
                    amount = float(value)
                except (TypeError, ValueError):
                    continue
                if amount <= 0:
                    continue
                if latest is None or observed_date > latest["date"]:
                    latest = {"date": observed_date, "price_per_quintal": amount}

        if latest is None:
            continue

        ppq = latest["price_per_quintal"]
        prices.append({
            "mandi": market["name"],
            "crop": "tomato",
            "price_per_kg": round(ppq / 100, 2),
            "price_per_quintal": ppq,
            "unit": unit,
            "observation_date": latest["date"].isoformat(),
            "source": "AGMARKNET",
            "market_id": market["id"],
        })

    return prices


def get_live_mandi_prices(crop: str = "tomato") -> dict[str, Any]:
    """Return live tomato prices, topped up with labelled samples to MIN_OPTIONS."""

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
            "message": "Live lookup currently supports tomatoes in Bengaluru only.",
        }

    cache = _load_cache()
    age_hours = 0.0
    live: list[dict] = []
    note = ""

    if cache.get("prices") and time.time() - cache.get("t", 0) < FRESH_S:
        live = cache["prices"]
        age_hours = round((time.time() - cache["t"]) / 3600, 1)
    else:
        live = fetch_live_prices()
        if live:
            _save_cache(live)
        elif cache.get("prices") and time.time() - cache.get("t", 0) < STALE_S:
            live = cache["prices"]
            age_hours = round((time.time() - cache["t"]) / 3600, 1)
            note = f" Agmarknet did not respond, so saved prices about {age_hours:.0f} hours old are shown."

    extra = top_up_with_samples(live, crop)
    combined = live + extra
    combined.sort(key=lambda item: item["price_per_kg"], reverse=True)

    if live:
        message = (
            "Latest reported AGMARKNET tomato prices for Bengaluru "
            "(historical observations, not guaranteed to be today's prices)."
        )
        if extra:
            message += (
                " Added ILLUSTRATIVE SAMPLE prices for comparison: "
                + ", ".join(p["mandi"] for p in extra) + "."
            )
        message += note
        return {
            "agent": "price",
            "status": "success" if not extra else "partial",
            "source": "AGMARKNET",
            "prices": combined,
            "sample_mandis": [p["mandi"] for p in extra],
            "cache_age_hours": age_hours,
            "message": message,
        }

    return {
        "agent": "price",
        "status": "fallback",
        "source": "illustrative_sample",
        "prices": combined,
        "sample_mandis": [p["mandi"] for p in extra],
        "cache_age_hours": 0.0,
        "message": (
            "Live Bengaluru tomato prices are unavailable or unreported. "
            "Showing ILLUSTRATIVE SAMPLE prices, not verified live market prices."
        ),
    }


# --------------------------------------------------
# AGENT
# --------------------------------------------------

# Require a stable seed from the environment.
price_seed = os.getenv("KISAN_PRICE_SEED")

if not price_seed:
    raise RuntimeError(
        "Set KISAN_PRICE_SEED in your environment before running the agent."
    )

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

price_agent = Agent(
    name="kisan_mandi_price_agent",
    seed=price_seed,
    port=8001,
    endpoint=["http://127.0.0.1:8001/submit"],
    loop=loop,
)

chat_protocol = Protocol(spec=chat_protocol_spec)


@chat_protocol.on_message(ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(timestamp=msg.timestamp, acknowledged_msg_id=msg.msg_id),
    )

    for item in msg.content:
        if not isinstance(item, TextContent):
            continue

        query = item.text.strip()

        # The orchestrator sends JSON with a request_id; humans send plain text.
        request_id = None
        crop_text = query
        try:
            data = json.loads(query)
            if isinstance(data, dict):
                request_id = data.get("request_id")
                crop_text = str(data.get("crop", "tomato"))
        except json.JSONDecodeError:
            pass

        words = [word.strip(".,!?;:") for word in crop_text.casefold().split()]
        crop = "tomato" if any(word in {"tomato", "tomatoes"} for word in words) else crop_text

        result = await asyncio.to_thread(get_live_mandi_prices, crop)

        if request_id:
            # Structured reply for the orchestrator
            response = json.dumps(
                {**result, "request_id": request_id},
                ensure_ascii=False,
                default=str,
            )
        elif result["prices"]:
            lines = []
            for price in result["prices"]:
                if price.get("source") == "AGMARKNET":
                    lines.append(
                        f'{price["mandi"]}: ₹{price["price_per_kg"]}/kg '
                        f'(reported {price.get("observation_date", "n/a")})'
                    )
                else:
                    lines.append(
                        f'{price["mandi"]}: ₹{price["price_per_kg"]}/kg '
                        "(illustrative sample, not live)"
                    )
            response = result["message"] + "\n" + "\n".join(lines)
        else:
            response = result["message"]

        await ctx.send(sender, ChatMessage(content=[TextContent(text=response)]))


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