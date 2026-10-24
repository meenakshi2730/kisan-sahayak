"""
Kisan Sahayak - Tomato Weather Agent
Hosted on Agentverse.
Provides current weather, a three-day forecast, and heuristic
tomato spoilage-risk screening.
"""

import asyncio
import json
import math
import time
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

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

LOCATIONS = {
    "bengaluru": (12.9716, 77.5946),
    "bengaluru, karnataka": (12.9716, 77.5946),
    "kolar": (13.1358, 78.1326),
    "kolar, karnataka": (13.1358, 78.1326),
    "ramanagara": (12.7150, 77.2810),
    "ramanagara, karnataka": (12.7150, 77.2810),
    "channapatna": (12.6514, 77.2067),
    "channapatna, karnataka": (12.6514, 77.2067),
}

FRESH_S = 3600
STALE_S = 43200

agent = Agent()
chat_protocol = Protocol(spec=chat_protocol_spec)


# --------------------------------------------------
# CHAT HELPERS
# --------------------------------------------------

def create_message(text):
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


def extract_text(msg):
    return "\n".join(
        item.text
        for item in msg.content
        if isinstance(item, TextContent)
    ).strip()


async def send_json(ctx, sender, data):
    await ctx.send(
        sender,
        create_message(json.dumps(data, ensure_ascii=False)),
    )


async def acknowledge_message(ctx, sender, msg):
    try:
        await ctx.send(
            sender,
            ChatAcknowledgement(
                timestamp=datetime.now(timezone.utc),
                acknowledged_msg_id=msg.msg_id,
            ),
        )
    except Exception as exc:
        ctx.logger.warning(f"Acknowledgement failed: {exc}")


# --------------------------------------------------
# CACHE
# --------------------------------------------------

def load_cache(ctx):
    try:
        raw = ctx.storage.get("weather_cache")
        cache = json.loads(raw) if raw else {}
        return cache if isinstance(cache, dict) else {}
    except Exception as exc:
        ctx.logger.warning(f"Could not load weather cache: {exc}")
        return {}


def save_cache(ctx, cache):
    try:
        now = time.time()
        cleaned = {
            key: value
            for key, value in cache.items()
            if isinstance(value, dict)
            and now - value.get("t", 0) < STALE_S
        }
        ctx.storage.set("weather_cache", json.dumps(cleaned))
    except Exception as exc:
        ctx.logger.warning(f"Could not save weather cache: {exc}")


# --------------------------------------------------
# WEATHER API
# --------------------------------------------------

def get_weather(cache, latitude, longitude):
    key = f"{latitude:.3f},{longitude:.3f}"
    now = time.time()
    cached = cache.get(key)

    if (
        isinstance(cached, dict)
        and "data" in cached
        and now - cached.get("t", 0) < FRESH_S
    ):
        return cached["data"]

    last_error = "Unknown weather API error"

    for attempt in range(2):
        try:
            response = requests.get(
                OPEN_METEO_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": (
                        "temperature_2m,relative_humidity_2m,precipitation"
                    ),
                    "daily": (
                        "temperature_2m_max,temperature_2m_min,"
                        "precipitation_sum,precipitation_probability_max"
                    ),
                    "forecast_days": 3,
                    "timezone": "auto",
                },
                timeout=15,
                headers={"User-Agent": "KisanSahayak/1.0"},
            )

            if response.status_code == 429:
                last_error = "Weather API rate limit reached (429)"
                time.sleep(1 + attempt)
                continue

            response.raise_for_status()
            data = response.json()

            if "current" not in data or "daily" not in data:
                raise ValueError("Weather API returned incomplete data.")

            cache[key] = {"t": time.time(), "data": data}
            return data

        except (
            requests.RequestException,
            ValueError,
        ) as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(1)

    # Return a recent cached forecast if the API is unavailable.
    if (
        isinstance(cached, dict)
        and "data" in cached
        and now - cached.get("t", 0) < STALE_S
    ):
        stale_data = dict(cached["data"])
        stale_data["_stale"] = True
        return stale_data

    raise RuntimeError(f"Weather data unavailable: {last_error}")


# --------------------------------------------------
# WEATHER PROCESSING
# --------------------------------------------------

def process_weather_request(request_data, cache):
    if not isinstance(request_data, dict):
        raise ValueError("Request must be a JSON object.")

    crop = str(
        request_data.get("crop_type", request_data.get("crop", "tomato"))
    ).strip().lower()

    if crop not in {"tomato", "tomatoes"}:
        raise ValueError("This agent supports tomatoes only.")

    location = str(request_data.get("location", "Bengaluru")).strip()

    if not location:
        raise ValueError("Location cannot be empty.")

    has_lat = request_data.get("latitude") not in (None, "")
    has_lon = request_data.get("longitude") not in (None, "")

    if has_lat != has_lon:
        raise ValueError("Provide both latitude and longitude.")

    if has_lat and has_lon:
        try:
            latitude = float(request_data["latitude"])
            longitude = float(request_data["longitude"])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("Coordinates must be valid numbers.")

        if not math.isfinite(latitude) or not -90 <= latitude <= 90:
            raise ValueError("Latitude must be between -90 and 90.")

        if not math.isfinite(longitude) or not -180 <= longitude <= 180:
            raise ValueError("Longitude must be between -180 and 180.")
    else:
        location_key = location.lower()

        if location_key not in LOCATIONS:
            raise ValueError(
                "Supported locations: Bengaluru, Kolar, Ramanagara, "
                "and Channapatna. Alternatively, provide coordinates."
            )

        latitude, longitude = LOCATIONS[location_key]

    data = get_weather(cache, latitude, longitude)
    current = data["current"]
    daily = data["daily"]

    temperature = float(current["temperature_2m"])
    humidity = float(current["relative_humidity_2m"])
    precipitation = float(current["precipitation"])

    rain_probs = daily.get("precipitation_probability_max") or []
    rain_amounts = daily.get("precipitation_sum") or []
    max_temps = daily.get("temperature_2m_max") or []
    dates = daily.get("time") or []

    valid_rain = [
        float(value) for value in rain_probs if value is not None
    ]
    valid_temps = [
        float(value) for value in max_temps if value is not None
    ]

    score = 0
    factors = []
    actions = []

    if temperature >= 30:
        score += 2
        factors.append("High temperature may accelerate deterioration.")
        actions.append("Keep harvested tomatoes shaded and cool.")
    elif temperature >= 25:
        score += 1
        factors.append("Warm weather requires careful handling.")

    if humidity >= 85:
        score += 2
        factors.append("Very high humidity requires moisture management.")
        actions.append("Keep tomatoes dry and inspect for decay.")
    elif humidity >= 70:
        score += 1
        factors.append("High humidity makes regular inspection important.")

    if valid_temps and max(valid_temps) >= 35:
        score += 1
        factors.append("Very hot daytime temperatures are forecast.")

    if valid_rain and max(valid_rain) >= 70:
        factors.append(
            f"Forecast rain probability reaches {max(valid_rain):.0f}%."
        )
        actions.append("Arrange covered transport if selling during rain.")
    elif valid_rain and max(valid_rain) >= 40:
        factors.append("Rain is possible during the forecast.")
        actions.append("Check the forecast before transporting tomatoes.")

    condition = str(
        request_data.get("crop_condition", "")
    ).strip().lower()

    if condition in {
        "damaged", "bruised", "overripe", "spoiling", "spoiled"
    }:
        score += 2
        factors.append("Tomatoes are reported as damaged or deteriorating.")
        actions.append("Sort tomatoes and separate damaged produce.")
    elif condition in {
        "ripe", "fully ripe", "mature", "ready", "ready to sell"
    }:
        score += 1
        factors.append("Tomatoes are reported as ripe or ready to sell.")
    else:
        factors.append("Inspect tomato ripeness and physical condition.")

    if score >= 5:
        risk_level = "HIGH"
    elif score >= 3:
        risk_level = "MODERATE"
    else:
        risk_level = "LOW"

    if not actions:
        actions.append(
            "Use clean handling, suitable storage, and regular quality checks."
        )

    forecast = []

    for index, day in enumerate(dates):
        forecast.append({
            "date": day,
            "maximum_temperature_c": (
                max_temps[index] if index < len(max_temps) else None
            ),
            "rainfall_mm": (
                rain_amounts[index]
                if index < len(rain_amounts) else None
            ),
            "rain_probability_percent": (
                rain_probs[index] if index < len(rain_probs) else None
            ),
        })

    high_rain_day = next(
        (
            day for day in forecast
            if isinstance(day["rain_probability_percent"], (int, float))
            and day["rain_probability_percent"] >= 70
        ),
        None,
    )

    if high_rain_day:
        advice = (
            f"Rain probability is high on {high_rain_day['date']}. "
            "Check crop readiness and market arrangements before travel."
        )
    else:
        advice = (
            "No forecast day has a rain probability of 70% or more. "
            "Recheck the weather and market arrangements before travel."
        )

    return {
        "agent": "Kisan Sahayak Tomato Weather Agent",
        "status": "success",
        "request_id": request_data.get("request_id"),
        "location": location,
        "crop": "tomato",
        "current_weather": {
            "temperature_c": temperature,
            "humidity_percent": humidity,
            "precipitation_mm": precipitation,
            "timezone": data.get("timezone", "Unknown"),
        },
        "forecast_next_3_days": forecast,
        "spoilage_risk": {
            "level": risk_level,
            "screening_score": score,
            "factors": factors,
            "recommended_actions": actions,
        },
        "farmer_advice": advice,
        "data_source": (
            "Open-Meteo cached forecast"
            if data.get("_stale")
            else "Open-Meteo"
        ),
        "warning": (
            "Heuristic screening only; not a validated spoilage probability "
            "or shelf-life prediction."
        ),
    }


# --------------------------------------------------
# CHAT MESSAGE HANDLER
# --------------------------------------------------

@chat_protocol.on_message(ChatMessage)
async def handle_weather_request(ctx: Context, sender: str, msg: ChatMessage):
    ctx.logger.info(f"Weather request received from {sender}")

    await acknowledge_message(ctx, sender, msg)

    request_id = None

    try:
        request_text = extract_text(msg)

        if not request_text:
            raise ValueError("Request message is empty.")

        # Accept greetings as well as JSON requests.
        if request_text.lower() in {
            "hi", "hello", "hey", "namaste", "namaskara"
        }:
            await send_json(ctx, sender, {
                "agent": "Kisan Sahayak Tomato Weather Agent",
                "status": "success",
                "request_id": None,
                "message": (
                    "Hello! I provide tomato weather forecasts and "
                    "weather-based handling advice for supported locations."
                ),
            })
            return

        request_data = json.loads(request_text)

        if not isinstance(request_data, dict):
            raise ValueError("Request must be a JSON object.")

        request_id = request_data.get("request_id")

        cache = load_cache(ctx)

        result = await asyncio.to_thread(
            process_weather_request,
            request_data,
            cache,
        )

        save_cache(ctx, cache)

    except (
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        RuntimeError,
        requests.RequestException,
    ) as exc:
        result = {
            "agent": "Kisan Sahayak Tomato Weather Agent",
            "status": "error",
            "request_id": request_id,
            "message": str(exc),
        }

    except Exception as exc:
        ctx.logger.exception(f"Unexpected weather error: {exc}")

        result = {
            "agent": "Kisan Sahayak Tomato Weather Agent",
            "status": "error",
            "request_id": request_id,
            "message": "Unexpected internal weather processing error.",
        }

    await send_json(ctx, sender, result)
    ctx.logger.info(f"Weather request processed: {request_id}")


# --------------------------------------------------
# CHAT ACKNOWLEDGEMENT HANDLER
# --------------------------------------------------

@chat_protocol.on_message(ChatAcknowledgement)
async def handle_acknowledgement(
    ctx: Context,
    sender: str,
    msg: ChatAcknowledgement,
):
    ctx.logger.info(f"Chat acknowledgement received from {sender}")


# --------------------------------------------------
# REGISTER PROTOCOL
# --------------------------------------------------

agent.include(chat_protocol, publish_manifest=True)