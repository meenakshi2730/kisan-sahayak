
"""
Kisan Sahayak - Transport Agent
Hosted on Agentverse.
"""

import asyncio
import json
import math
from datetime import datetime, timezone
from uuid import uuid4

import requests

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

# ---------------- CONFIGURATION ----------------

DEFAULT_TRUCK_CAPACITY_QUINTALS = 100.0
DEFAULT_COST_PER_KM_PER_TRUCK = 35.0
DEFAULT_LOADING_COST_PER_TRUCK = 500.0
DEFAULT_AVERAGE_SPEED_KMPH = 30.0

OSRM_BASE_URL = "https://router.project-osrm.org/route/v1/driving"

agent = Agent()
chat_protocol = Protocol(spec=chat_protocol_spec)


# ---------------- CHAT HELPERS ----------------

def create_message(text):
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


async def send_text(ctx, sender, text):
    await ctx.send(sender, create_message(text))


async def send_json(ctx, sender, data):
    await send_text(ctx, sender, json.dumps(data))


def extract_text(message):
    return "\n".join(
        item.text
        for item in message.content
        if isinstance(item, TextContent)
    ).strip()


# ---------------- ROUTE CALCULATION ----------------

def get_route(origin, destination):
    origin_lat = origin.get("latitude")
    origin_lon = origin.get("longitude")
    dest_lat = destination.get("latitude")
    dest_lon = destination.get("longitude")

    if all(
        value is not None
        for value in (origin_lat, origin_lon, dest_lat, dest_lon)
    ):
        try:
            url = (
                f"{OSRM_BASE_URL}/"
                f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
            )

            response = requests.get(
                url,
                params={"overview": "false"},
                timeout=6,
            )
            response.raise_for_status()
            data = response.json()

            if data.get("routes"):
                route = data["routes"][0]
                return {
                    "distance_km": round(route["distance"] / 1000, 2),
                    "travel_time_hours": round(
                        route["duration"] / 3600, 2
                    ),
                    "route_source": "OSRM road route",
                    "status": "success",
                }

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
        ):
            pass

    supplied_distance = destination.get("distance_km")

    if supplied_distance is not None:
        try:
            distance = float(supplied_distance)

            if not math.isfinite(distance) or distance < 0:
                raise ValueError("Invalid distance")

            return {
                "distance_km": round(distance, 2),
                "travel_time_hours": round(
                    distance / DEFAULT_AVERAGE_SPEED_KMPH, 2
                ),
                "route_source": "Supplied distance estimate",
                "status": "estimated",
            }

        except (ValueError, TypeError, OverflowError):
            pass

    return {
        "distance_km": None,
        "travel_time_hours": None,
        "route_source": "Unavailable",
        "status": "unavailable",
    }


# ---------------- TRANSPORT CALCULATION ----------------

def calculate_transport(request_data):
    if not isinstance(request_data, dict):
        raise ValueError("Request must be a JSON object.")

    origin = request_data.get("origin")
    destinations = request_data.get("destinations")

    try:
        quantity = float(request_data.get("quantity_quintals"))
    except (ValueError, TypeError, OverflowError):
        raise ValueError("quantity_quintals must be a valid number.")

    if not isinstance(origin, dict):
        raise ValueError("Missing or invalid origin object.")

    if not isinstance(destinations, list) or not destinations:
        raise ValueError("Destinations must be a non-empty list.")

    if not math.isfinite(quantity) or quantity <= 0:
        raise ValueError("Quantity must be greater than zero.")

    truck_count = math.ceil(
        quantity / DEFAULT_TRUCK_CAPACITY_QUINTALS
    )

    results = []
    total_costs = []

    for destination in destinations:
        if not isinstance(destination, dict):
            continue

        route = get_route(origin, destination)
        distance = route["distance_km"]

        if distance is None:
            results.append({
                "mandi": destination.get("name", "Unknown destination"),
                "status": "unavailable",
                "message": "Road distance could not be determined.",
            })
            continue

        transport_cost = (
            distance * DEFAULT_COST_PER_KM_PER_TRUCK * truck_count
        )
        loading_cost = DEFAULT_LOADING_COST_PER_TRUCK * truck_count
        total_cost = transport_cost + loading_cost

        total_costs.append(total_cost)

        results.append({
            "mandi": destination.get("name", "Unknown destination"),
            "status": "success",
            "distance_km": route["distance_km"],
            "travel_time_hours": route["travel_time_hours"],
            "route_source": route["route_source"],
            "quantity_quintals": quantity,
            "truck_capacity_quintals": DEFAULT_TRUCK_CAPACITY_QUINTALS,
            "trucks_required": truck_count,
            "transport_cost": round(transport_cost, 2),
            "loading_cost": round(loading_cost, 2),
            "estimated_total_cost": round(total_cost, 2),
            "estimated_cost_per_quintal": round(
                total_cost / quantity, 2
            ),
        })

    return {
        "agent": "Kisan Sahayak Transport Agent",
        "status": "success" if total_costs else "error",
        "request_id": request_data.get("request_id"),
        "origin": origin.get("name", "Unknown origin"),
        "quantity_quintals": quantity,
        "trucks_required": truck_count,
        "results": results,
        "estimated_total_cost": (
            round(min(total_costs), 2) if total_costs else None
        ),
        "message": (
            "Transport estimates calculated."
            if total_costs
            else "No destination could be routed."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------- CHAT MESSAGE HANDLER ----------------

@chat_protocol.on_message(model=ChatMessage)
async def handle_transport_request(
    ctx: Context,
    sender: str,
    message: ChatMessage,
):
    ctx.logger.info(f"Received transport message from {sender}")

    try:
        await ctx.send(
            sender,
            ChatAcknowledgement(
                timestamp=datetime.now(timezone.utc),
                acknowledged_msg_id=message.msg_id,
            ),
        )
    except Exception as exc:
        ctx.logger.warning(f"Acknowledgement failed: {exc}")

    message_text = extract_text(message)

    if not message_text:
        await send_json(ctx, sender, {
            "agent": "Kisan Sahayak Transport Agent",
            "status": "error",
            "request_id": None,
            "message": "Please send a greeting or transport request.",
        })
        return

    if message_text.lower().strip() in {
        "hi", "hello", "hey", "namaste", "namaskara",
        "good morning", "good afternoon", "good evening",
    }:
        await send_json(ctx, sender, {
            "agent": "Kisan Sahayak Transport Agent",
            "status": "success",
            "request_id": None,
            "message": (
                "Hello! I can estimate road distance, travel time, "
                "truck requirements, and transportation costs. "
                "Send a JSON transport request through Kisan Sahayak."
            ),
        })
        return

    request_data = None

    try:
        request_data = json.loads(message_text)

        if not isinstance(request_data, dict):
            raise ValueError("Request must be a JSON object.")

        result = await asyncio.to_thread(
            calculate_transport, request_data
        )
        await send_json(ctx, sender, result)

    except json.JSONDecodeError:
        await send_json(ctx, sender, {
            "agent": "Kisan Sahayak Transport Agent",
            "status": "error",
            "request_id": None,
            "message": "Invalid JSON. Send Hi for help or use Kisan Sahayak.",
        })

    except (ValueError, TypeError, OverflowError) as exc:
        await send_json(ctx, sender, {
            "agent": "Kisan Sahayak Transport Agent",
            "status": "error",
            "request_id": (
                request_data.get("request_id")
                if isinstance(request_data, dict)
                else None
            ),
            "message": str(exc),
        })

    except Exception:
        ctx.logger.exception("Transport processing failed")
        await send_json(ctx, sender, {
            "agent": "Kisan Sahayak Transport Agent",
            "status": "error",
            "request_id": (
                request_data.get("request_id")
                if isinstance(request_data, dict)
                else None
            ),
            "message": "Transport processing failed.",
        })


# ---------------- ACKNOWLEDGEMENT HANDLER ----------------

@chat_protocol.on_message(model=ChatAcknowledgement)
async def handle_acknowledgement(
    ctx: Context,
    sender: str,
    message: ChatAcknowledgement,
):
    ctx.logger.info(
        f"Received acknowledgement from {sender} "
        f"for message {message.acknowledged_msg_id}"
    )


# ---------------- REGISTER PROTOCOL ----------------

agent.include(chat_protocol, publish_manifest=True)


if __name__ == "__main__":
    agent.run()
