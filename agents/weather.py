"""
Kisan Sahayak - Tomato Weather Agent
Fetches current weather and a three-day forecast.
Spoilage risk is a heuristic, not a validated prediction.
Forecasts are cached on disk so rate limits (429) and restarts do not break the agent.
"""

# Windows event-loop policy must be set before importing uagents.
import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

import json
import math
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

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

weather_seed = os.getenv("KISAN_WEATHER_SEED")
if not weather_seed:
    raise RuntimeError("Set KISAN_WEATHER_SEED in the root .env file.")

agent = Agent(
    name="bengaluru_tomato_sahayak_agent",
    seed=weather_seed,
    port=8004,
    endpoint=["http://127.0.0.1:8004/submit"],
)
chat_protocol = Protocol(spec=chat_protocol_spec)

# --------------------------------------------------
# DISK CACHE (survives restarts)
# --------------------------------------------------

CACHE_FILE = Path(__file__).resolve().parent.parent / "weather_cache.json"
FRESH_S = 3600      # reuse a forecast for 1 hour
STALE_S = 43200     # if the API fails, reuse one up to 12 hours old

try:
    _mem = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
except Exception:
    _mem = {}


def _save_disk():
    try:
        CACHE_FILE.write_text(json.dumps(_mem), encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------
# WEATHER API
# --------------------------------------------------

def get_weather(latitude, longitude):
    key = f"{latitude:.3f},{longitude:.3f}"
    now = time.time()
    hit = _mem.get(key)
    if hit and now - hit["t"] < FRESH_S:
        return hit["data"]

    last_err = None
    for attempt in range(3):
        try:
            response = requests.get(
                OPEN_METEO_URL,
                params={
                    "latitude": latitude,
                    "longitude": longitude,
                    "current": "temperature_2m,relative_humidity_2m,precipitation",
                    "daily": (
                        "temperature_2m_max,temperature_2m_min,"
                        "precipitation_sum,precipitation_probability_max"
                    ),
                    "forecast_days": 3,
                    "timezone": "auto",
                },
                timeout=20,
                headers={"User-Agent": "KisanSahayak/1.0"},
            )
            if response.status_code == 429:
                last_err = "429 Too Many Requests"
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json()
            if "current" not in data or "daily" not in data:
                raise ValueError("Weather provider returned incomplete data.")
            _mem[key] = {"t": now, "data": data}
            _save_disk()
            return data
        except requests.RequestException as exc:
            last_err = str(exc)
            time.sleep(2 * (attempt + 1))

    if hit and now - hit["t"] < STALE_S:
        data = dict(hit["data"])
        data["_stale"] = True          # an old forecast beats none
        return data
    raise requests.RequestException(f"Weather provider unavailable: {last_err}")


# --------------------------------------------------
# PROCESS REQUEST
# --------------------------------------------------

def process_weather_request(request_data):
    if not isinstance(request_data, dict):
        raise ValueError("Request must be a JSON object.")

    crop = str(
        request_data.get("crop_type", request_data.get("crop", "tomato"))
    ).strip().lower()

    if crop not in ("tomato", "tomatoes"):
        raise ValueError("This agent supports tomatoes only.")

    location = str(request_data.get("location", "Bengaluru")).strip()

    if not location:
        raise ValueError("Location cannot be empty.")

    location_key = location.lower()

    has_lat = request_data.get("latitude") not in (None, "")
    has_lon = request_data.get("longitude") not in (None, "")

    if has_lat != has_lon:
        raise ValueError("Provide both latitude and longitude.")

    if has_lat and has_lon:
        latitude = float(request_data["latitude"])
        longitude = float(request_data["longitude"])

        if not math.isfinite(latitude) or not -90 <= latitude <= 90:
            raise ValueError("Latitude must be between -90 and 90.")

        if not math.isfinite(longitude) or not -180 <= longitude <= 180:
            raise ValueError("Longitude must be between -180 and 180.")
    else:
        if location_key not in LOCATIONS:
            raise ValueError(
                "Location must be Bengaluru, Kolar, Ramanagara, "
                "or Channapatna, or provide latitude and longitude."
            )
        latitude, longitude = LOCATIONS[location_key]

    data = get_weather(latitude, longitude)
    current = data["current"]
    daily = data["daily"]

    temperature = float(current["temperature_2m"])
    humidity = float(current["relative_humidity_2m"])
    precipitation = float(current["precipitation"])

    rain_probs = daily.get("precipitation_probability_max") or []
    rain_amounts = daily.get("precipitation_sum") or []
    max_temps = daily.get("temperature_2m_max") or []

    valid_rain = [float(v) for v in rain_probs if v is not None]
    valid_temps = [float(v) for v in max_temps if v is not None]

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
        factors.append(f"Forecast rain probability reaches {max(valid_rain):.0f}%.")
        actions.append("Arrange covered transport if selling during rain.")
    elif valid_rain and max(valid_rain) >= 40:
        factors.append("Rain is possible during the forecast.")
        actions.append("Check the forecast before transporting tomatoes.")

    condition = str(request_data.get("crop_condition", "")).strip().lower()

    if condition in {"damaged", "bruised", "overripe", "spoiling", "spoiled"}:
        score += 2
        factors.append("Tomatoes are reported as damaged or deteriorating.")
        actions.append("Sort tomatoes and separate damaged produce.")
    elif condition in {"ripe", "fully ripe", "mature", "ready", "ready to sell"}:
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
        actions.append("Use clean handling, suitable storage, and regular quality checks.")

    forecast = []
    for i, day in enumerate(daily.get("time", [])):
        forecast.append({
            "date": day,
            "maximum_temperature_c": max_temps[i] if i < len(max_temps) else None,
            "rainfall_mm": rain_amounts[i] if i < len(rain_amounts) else None,
            "rain_probability_percent": rain_probs[i] if i < len(rain_probs) else None,
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
            "Check crop readiness and confirmed market arrangements before travel."
        )
    else:
        advice = (
            "No day with at least 70% rain probability appears in this forecast. "
            "Recheck weather and market arrangements before travel."
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
        "data_source": "Open-Meteo (cached forecast)" if data.get("_stale") else "Open-Meteo",
        "warning": (
            "Heuristic weather and handling screening only; "
            "not a validated spoilage probability or shelf-life prediction."
        ),
    }


# --------------------------------------------------
# CHAT HELPERS
# --------------------------------------------------

def extract_text(msg):
    return "\n".join(
        item.text for item in msg.content if isinstance(item, TextContent)
    ).strip()


# --------------------------------------------------
# REQUEST HANDLER
# --------------------------------------------------

@chat_protocol.on_message(ChatMessage)
async def handle_weather_request(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))

    request_id = None

    try:
        request_text = extract_text(msg)

        if not request_text:
            raise ValueError("Request message is empty.")

        request_data = json.loads(request_text)

        if not isinstance(request_data, dict):
            raise ValueError("Request must be a JSON object.")

        request_id = request_data.get("request_id")

        # Keep blocking HTTP work outside the agent event loop.
        result = await asyncio.to_thread(process_weather_request, request_data)

    except (
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
        requests.RequestException,
    ) as exc:
        result = {
            "agent": "Kisan Sahayak Tomato Weather Agent",
            "status": "error",
            "request_id": request_id,
            "message": str(exc),
        }

    except Exception:
        ctx.logger.exception("Unexpected weather-agent error.")
        result = {
            "agent": "Kisan Sahayak Tomato Weather Agent",
            "status": "error",
            "request_id": request_id,
            "message": "Unexpected internal error while processing weather.",
        }

    await ctx.send(
        sender,
        ChatMessage(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        ),
    )

    ctx.logger.info("Weather request processed. request_id=%s", request_id)


# --------------------------------------------------
# ACKNOWLEDGEMENT HANDLER
# --------------------------------------------------

@chat_protocol.on_message(ChatAcknowledgement)
async def handle_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info("Message acknowledged by %s", sender)


# --------------------------------------------------
# STARTUP WARM-UP (fills the cache for all four places)
# --------------------------------------------------

def _warm_cache():
    for name in ("bengaluru", "kolar", "ramanagara", "channapatna"):
        try:
            get_weather(*LOCATIONS[name])
        except Exception:
            pass
        time.sleep(3)


@agent.on_event("startup")
async def warm(ctx: Context):
    asyncio.get_running_loop().run_in_executor(None, _warm_cache)
    ctx.logger.info("Weather cache warm-up started.")


# --------------------------------------------------
# START AGENT
# --------------------------------------------------

agent.include(chat_protocol, publish_manifest=True)

if __name__ == "__main__":
    agent.run()