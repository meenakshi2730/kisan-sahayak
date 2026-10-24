"""
Kisan Sahayak - Transport Agent (HOSTED on Agentverse)
Estimates road distance, travel time, and transport costs.
Uses Fetch.ai Chat Protocol with JSON requests and responses.

Hosted-agent notes:
  * Paste this whole file into the agent's Build tab (agent.py). Do not call agent.run().
  * No .env, no seed, no port: Agentverse provides the identity. No Secrets are required.
"""

import asyncio
import json
import math
from datetime import datetime, timezone
from typing import Any, Dict
from uuid import uuid4

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

# ---------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------

DEFAULT_TRUCK_CAPACITY_QUINTALS = 100.0
DEFAULT_COST_PER_KM_PER_TRUCK = 35.0
DEFAULT_LOADING_COST_PER_TRUCK = 500.0
DEFAULT_AVERAGE_SPEED_KMPH = 30.0

OSRM_BASE_URL = (
    "https://router.project-osrm.org/route/v1/driving"
)

agent = Agent()
chat_protocol = Protocol(spec=chat_protocol_spec)


# ---------------------------------------------------------
# ROUTE DISTANCE AND TRAVEL TIME
# ---------------------------------------------------------

def get_route(
    origin: Dict[str, Any],
    destination: Dict[str, Any],
) -> Dict[str, Any]:
    """Get a road route or use a caller-supplied distance."""

    required = ("latitude", "longitude")

    has_origin_coordinates = all(
        key in origin and origin[key] is not None
        for key in required
    )
    has_destination_coordinates = all(
        key in destination and destination[key] is not None
        for key in required
    )

    if has_origin_coordinates and has_destination_coordinates:
        try:
            origin_lat = float(origin["latitude"])
            origin_lon = float(origin["longitude"])
            dest_lat = float(destination["latitude"])
            dest_lon = float(destination["longitude"])

            for lat, lon in (
                (origin_lat, origin_lon),
                (dest_lat, dest_lon),
            ):
                if not (
                    -90 <= lat <= 90
                    and -180 <= lon <= 180
                ):
                    raise ValueError(
                        "Invalid latitude or longitude."
                    )

            url = (
                f"{OSRM_BASE_URL}/"
                f"{origin_lon},{origin_lat};"
                f"{dest_lon},{dest_lat}"
            )

            response = requests.get(
                url,
                params={"overview": "false"},
                timeout=6,
                headers={
                    "User-Agent": "KisanSahayak-TransportAgent/1.0"
                },
            )
            response.raise_for_status()
            data = response.json()

            if data.get("code") == "Ok" and data.get("routes"):
                route = data["routes"][0]

                return {
                    "distance_km": round(
                        route["distance"] / 1000, 2
                    ),
                    "travel_time_minutes": round(
                        route["duration"] / 60
                    ),
                    "distance_source": "OSRM road-route estimate",
                    "route_available": True,
                }

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
        ):
            pass

    # Only use a fallback when the caller supplies a distance.
    if destination.get("distance_km") is not None:
        try:
            distance_km = float(destination["distance_km"])

            if (
                not math.isfinite(distance_km)
                or distance_km < 0
            ):
                raise ValueError(
                    "distance_km must be non-negative."
                )

            return {
                "distance_km": round(distance_km, 2),
                "travel_time_minutes": round(
                    distance_km
                    / DEFAULT_AVERAGE_SPEED_KMPH
                    * 60
                ),
                "distance_source": (
                    "Caller-supplied distance; not live routing"
                ),
                "route_available": True,
            }

        except (ValueError, TypeError):
            pass

    return {
        "distance_km": None,
        "travel_time_minutes": None,
        "distance_source": "Unavailable",
        "route_available": False,
        "error": (
            "Could not obtain a road route. Supply valid coordinates "
            "or a verified/estimated distance_km."
        ),
    }


# ---------------------------------------------------------
# TRANSPORT COST CALCULATION
# ---------------------------------------------------------

def calculate_transport(
    request_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Calculate estimated transport costs for candidate mandis."""

    if not isinstance(request_data, dict):
        raise ValueError("The request must be a JSON object.")

    origin = request_data.get("origin")
    destinations = request_data.get("destinations")

    if (
        not isinstance(origin, dict)
        or not isinstance(destinations, list)
    ):
        raise ValueError(
            "Required fields: origin (object) and destinations (list)."
        )

    if not destinations:
        raise ValueError("Provide at least one destination.")

    quantity = float(
        request_data.get("quantity_quintals", 0)
    )
    truck_capacity = float(
        request_data.get(
            "truck_capacity_quintals",
            DEFAULT_TRUCK_CAPACITY_QUINTALS,
        )
    )
    cost_per_km = float(
        request_data.get(
            "cost_per_km_per_truck",
            DEFAULT_COST_PER_KM_PER_TRUCK,
        )
    )
    loading_cost = float(
        request_data.get(
            "loading_cost_per_truck",
            DEFAULT_LOADING_COST_PER_TRUCK,
        )
    )

    values = (
        quantity,
        truck_capacity,
        cost_per_km,
        loading_cost,
    )

    if not all(math.isfinite(value) for value in values):
        raise ValueError("Numeric inputs must be finite.")

    if quantity <= 0:
        raise ValueError(
            "quantity_quintals must be greater than zero."
        )

    if truck_capacity <= 0:
        raise ValueError(
            "truck_capacity_quintals must be greater than zero."
        )

    if cost_per_km < 0 or loading_cost < 0:
        raise ValueError("Costs cannot be negative.")

    trucks_needed = math.ceil(quantity / truck_capacity)
    results = []

    for destination in destinations:
        if not isinstance(destination, dict):
            continue

        route = get_route(origin, destination)

        result = {
            "mandi": str(
                destination.get("name", "Unnamed mandi")
            ),
            "market_id": destination.get("market_id"),
            "origin": origin.get("name", "Farmer location"),
            "quantity_quintals": quantity,
            "trucks_needed": trucks_needed,
            **route,
        }

        if route["route_available"]:
            distance_km = route["distance_km"]
            transport_cost = (
                distance_km * cost_per_km * trucks_needed
            )
            total_loading_cost = (
                loading_cost * trucks_needed
            )

            result.update({
                "cost_per_km_per_truck": cost_per_km,
                "loading_cost_per_truck": loading_cost,
                "transport_cost": round(transport_cost, 2),
                "total_loading_cost": round(
                    total_loading_cost, 2
                ),
                "estimated_total_cost": round(
                    transport_cost + total_loading_cost, 2
                ),
                "currency": "INR",
                "cost_basis": (
                    "Illustrative rate assumptions; one-way estimate; "
                    "not a live transporter quote"
                ),
            })
        else:
            result.update({
                "transport_cost": None,
                "total_loading_cost": None,
                "estimated_total_cost": None,
            })

        results.append(result)

    valid_results = [
        item
        for item in results
        if item["estimated_total_cost"] is not None
    ]

    ranked_results = sorted(
        valid_results,
        key=lambda item: item["estimated_total_cost"],
    )

    for rank, item in enumerate(ranked_results, start=1):
        item["transport_cost_rank"] = rank

    return {
        "agent": "Kisan Sahayak Transport Agent",
        "status": (
            "success" if valid_results else "no_routes_available"
        ),
        "assumptions": {
            "truck_capacity_quintals": truck_capacity,
            "cost_per_km_per_truck": cost_per_km,
            "loading_cost_per_truck": loading_cost,
            "average_speed_kmph_for_fallback": (
                DEFAULT_AVERAGE_SPEED_KMPH
            ),
            "costs_are_illustrative": True,
        },
        "results": results,
        "lowest_estimated_transport_cost_mandi": (
            ranked_results[0]["mandi"] if ranked_results else None
        ),
        "warning": (
            "Estimates are not confirmed transporter quotes. "
            "Travel time excludes traffic and loading delays. "
            "Lowest transport cost does not necessarily mean "
            "highest farmer profit."
        ),
    }


# ---------------------------------------------------------
# CHAT PROTOCOL HANDLERS
# ---------------------------------------------------------

def extract_text(message: ChatMessage) -> str:
    """Extract text blocks from a ChatMessage."""

    return "\n".join(
        item.text
        for item in message.content
        if isinstance(item, TextContent)
    ).strip()


@chat_protocol.on_message(ChatMessage)
async def handle_transport_request(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(
        sender,
        ChatAcknowledgement(
            timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
        ),
    )

    request_id = None
    try:
        message_text = extract_text(msg)
        if not message_text:
            raise ValueError("Message is empty.")

        request_data = json.loads(message_text)
        if not isinstance(request_data, dict):
            raise ValueError("Send a JSON object, not a JSON list.")

        request_id = request_data.get("request_id")
        # OSRM calls are blocking HTTP, so run them off the event loop.
        result = await asyncio.to_thread(calculate_transport, request_data)
        result["request_id"] = request_id
        response_text = json.dumps(result, ensure_ascii=False)

    except (json.JSONDecodeError, ValueError, TypeError, OverflowError) as exc:
        response_text = json.dumps({
            "agent": "Kisan Sahayak Transport Agent",
            "status": "error",
            "request_id": request_id,
            "message": str(exc),
        }, ensure_ascii=False)

    await ctx.send(
        sender,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(type="text", text=response_text)],
        ),
    )
    ctx.logger.info("Transport request processed.")


@chat_protocol.on_message(ChatAcknowledgement)
async def handle_acknowledgement(
    ctx: Context,
    sender: str,
    msg: ChatAcknowledgement,
):
    ctx.logger.info(f"Message acknowledged by {sender}")


agent.include(chat_protocol, publish_manifest=True)