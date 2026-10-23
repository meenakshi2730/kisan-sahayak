
"""
Kisan Sahayak - Transport Agent

Responsibilities:
- Estimate road distance and travel time to candidate mandis.
- Estimate transport and loading costs.
- Account for quantity and truck capacity.
- Respond using Fetch.ai's Agent Chat Protocol (ACP).

Requests must contain JSON inside the ChatMessage text.
Coordinates enable live road-route lookup through OSRM.
If routing fails, an explicitly supplied distance_km can be used as a fallback.
"""

import json
import math
from typing import Any, Dict, Optional

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)


# ---------------------------------------------------------
# CONFIGURATION: illustrative defaults; verify locally
# ---------------------------------------------------------

DEFAULT_TRUCK_CAPACITY_QUINTALS = 100.0
DEFAULT_COST_PER_KM_PER_TRUCK = 35.0
DEFAULT_LOADING_COST_PER_TRUCK = 500.0
DEFAULT_AVERAGE_SPEED_KMPH = 30.0

OSRM_BASE_URL = "https://router.project-osrm.org/route/v1/driving"


# ---------------------------------------------------------
# ROUTE DISTANCE AND TRAVEL TIME
# ---------------------------------------------------------

def get_route(
    origin: Dict[str, Any],
    destination: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Get road distance and driving duration from OSRM.

    Coordinates must be decimal latitude/longitude.
    OSRM expects longitude,latitude in its URL.
    """

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
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise ValueError("Invalid latitude or longitude.")

            url = (
                f"{OSRM_BASE_URL}/"
                f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
            )

            response = requests.get(
                url,
                params={"overview": "false"},
                timeout=6,
                headers={"User-Agent": "KisanSahayak-TransportAgent/1.0"},
            )
            response.raise_for_status()
            data = response.json()

            if data.get("code") == "Ok" and data.get("routes"):
                route = data["routes"][0]

                return {
                    "distance_km": round(route["distance"] / 1000, 2),
                    "travel_time_minutes": round(route["duration"] / 60),
                    "distance_source": "OSRM road-route estimate",
                    "route_available": True,
                }

        except (requests.RequestException, ValueError, KeyError, TypeError):
            # If live routing fails, try the supplied distance below.
            pass

    # Fallback is allowed only when the caller supplies a distance.
    if destination.get("distance_km") is not None:
        try:
            distance_km = float(destination["distance_km"])

            if not math.isfinite(distance_km) or distance_km < 0:
                raise ValueError("distance_km must be a non-negative number.")

            return {
                "distance_km": round(distance_km, 2),
                "travel_time_minutes": round(
                    distance_km / DEFAULT_AVERAGE_SPEED_KMPH * 60
                ),
                "distance_source": "Caller-supplied distance; not live routing",
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
    """
    Input example:
    {
        "origin": {
            "name": "Kolar",
            "latitude": 13.1367,
            "longitude": 78.1292
        },
        "destinations": [
            {
                "name": "Example Mandi",
                "latitude": 12.9716,
                "longitude": 77.5946
            }
        ],
        "quantity_quintals": 40,
        "truck_capacity_quintals": 100,
        "cost_per_km_per_truck": 35,
        "loading_cost_per_truck": 500
    }
    """

    if not isinstance(request_data, dict):
        raise ValueError("The request must be a JSON object.")

    origin = request_data.get("origin")
    destinations = request_data.get("destinations")

    if not isinstance(origin, dict) or not isinstance(destinations, list):
        raise ValueError(
            "Required fields: origin (object) and destinations (list)."
        )

    if not destinations:
        raise ValueError("Provide at least one destination.")

    quantity = float(request_data.get("quantity_quintals", 0))
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

    numeric_values = (quantity, truck_capacity, cost_per_km, loading_cost)

    if not all(math.isfinite(value) for value in numeric_values):
        raise ValueError("Numeric inputs must be finite numbers.")

    if quantity <= 0:
        raise ValueError("quantity_quintals must be greater than zero.")

    if truck_capacity <= 0:
        raise ValueError("truck_capacity_quintals must be greater than zero.")

    if cost_per_km < 0 or loading_cost < 0:
        raise ValueError("Transport and loading costs cannot be negative.")

    # One or more trucks may be needed for the produce quantity.
    trucks_needed = math.ceil(quantity / truck_capacity)

    results = []

    for destination in destinations:
        if not isinstance(destination, dict):
            continue

        name = str(destination.get("name", "Unnamed mandi"))
        route = get_route(origin, destination)

        result = {
            "mandi": name,
            "market_id": destination.get("market_id"),
            "origin": origin.get("name", "Farmer location"),
            "quantity_quintals": quantity,
            "trucks_needed": trucks_needed,
            **route,
        }

        if route["route_available"]:
            distance_km = route["distance_km"]

            # One-way trip estimate. This does not assume a return trip.
            transport_cost = distance_km * cost_per_km * trucks_needed
            total_loading_cost = loading_cost * trucks_needed

            result.update({
                "cost_per_km_per_truck": cost_per_km,
                "loading_cost_per_truck": loading_cost,
                "transport_cost": round(transport_cost, 2),
                "total_loading_cost": round(total_loading_cost, 2),
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
        item for item in results
        if item["estimated_total_cost"] is not None
    ]

    # Rank by estimated transport cost, not overall mandi profitability.
    ranked_results = sorted(
        valid_results,
        key=lambda item: item["estimated_total_cost"],
    )

    for rank, item in enumerate(ranked_results, start=1):
        item["transport_cost_rank"] = rank

    return {
        "agent": "Kisan Sahayak Transport Agent",
        "status": "success" if valid_results else "no_routes_available",
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
            "The lowest transport cost does not necessarily mean "
            "the highest farmer profit."
        ),
    }


# ---------------------------------------------------------
# FETCH.AI AGENT AND CHAT PROTOCOL
# ---------------------------------------------------------

agent = Agent()
chat_protocol = Protocol(spec=chat_protocol_spec)


def extract_text(message: ChatMessage) -> str:
    """Collect text blocks from an incoming chat message."""

    parts = [
        item.text
        for item in message.content
        if isinstance(item, TextContent)
    ]
    return "\n".join(parts).strip()


@chat_protocol.on_message(ChatMessage)
async def handle_transport_request(
    ctx: Context,
    sender: str,
    msg: ChatMessage,
):
    """Accept a JSON request and return one JSON response."""

    # Acknowledge receipt of the original request.
    await ctx.send(
        sender,
        ChatAcknowledgement(acknowledged_msg_id=msg.msg_id),
    )

    text = extract_text(msg)

    try:
        if not text:
            raise ValueError("Message is empty.")

        request_data = json.loads(text)

        if not isinstance(request_data, dict):
            raise ValueError("Send a JSON object, not a JSON list.")

        result = calculate_transport(request_data)

        response_text = json.dumps(result, ensure_ascii=False)

    except (json.JSONDecodeError, ValueError, TypeError, OverflowError) as exc:
        response_text = json.dumps({
            "agent": "Kisan Sahayak Transport Agent",
            "status": "error",
            "message": str(exc),
            "expected_request_example": {
                "origin": {
                    "name": "Kolar",
                    "latitude": 13.1367,
                    "longitude": 78.1292,
                },
                "destinations": [
                    {
                        "name": "Bengaluru example",
                        "latitude": 12.9716,
                        "longitude": 77.5946,
                    },
                    {
                        "name": "Mandi with supplied distance",
                        "distance_km": 65,
                    },
                ],
                "quantity_quintals": 40,
                "truck_capacity_quintals": 100,
                "cost_per_km_per_truck": 35,
                "loading_cost_per_truck": 500,
            },
        }, ensure_ascii=False)

    await ctx.send(
        sender,
        ChatMessage(
            content=[
                TextContent(type="text", text=response_text)
            ]
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


# Publish the Chat Protocol manifest for discovery.
agent.include(chat_protocol, publish_manifest=True)


# Local execution only.
# When pasting into an Agentverse hosted-agent editor, remove this block:
# Agentverse runs hosted agents for you.
if __name__ == "__main__":
    agent.run()
