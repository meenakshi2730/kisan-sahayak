"""
Kisan Sahayak - Bengaluru Tomato Weather & Spoilage Agent

Tomato-focused weather-risk screening for Bengaluru and nearby tomato supply areas.
Fetches real weather and gives explainable handling and selling-planning advice.
This is a weather-risk screening tool, NOT a validated shelf-life model.

Optional request fields: latitude, longitude, location, crop_condition,
                         harvest_status, harvest_date, storage_type,
                         storage_temperature_c, planned_sale_date
If coordinates are omitted, use a recognized location name; Bengaluru is the default.
If coordinates are supplied, both latitude and longitude are required.
Tomato is the default crop.
"""

import json
import math
from datetime import date
from typing import Any, Dict, Optional

import requests
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
# Default location: Bengaluru, Karnataka. Pass coordinates to assess a nearby
# tomato-growing/sourcing area such as Kolar or Ramanagara instead.
BENGALURU_LATITUDE = 12.9716
BENGALURU_LONGITUDE = 77.5946
DEFAULT_LOCATION = "Bengaluru, Karnataka"
DEFAULT_CROP = "tomato"
LOCATION_PRESETS = {
    "bengaluru": (12.9716, 77.5946, "Bengaluru"),
    "bengaluru, karnataka": (12.9716, 77.5946, "Bengaluru"),
    "kolar": (13.1358, 78.1326, "Kolar"),
    "kolar, karnataka": (13.1358, 78.1326, "Kolar"),
    "ramanagara": (12.7150, 77.2810, "Ramanagara"),
    "ramanagara, karnataka": (12.7150, 77.2810, "Ramanagara"),
    "channapatna": (12.6514, 77.2067, "Channapatna"),
    "channapatna, karnataka": (12.6514, 77.2067, "Channapatna"),
}
agent = Agent(name="bengaluru_tomato_sahayak_agent")
chat_protocol = Protocol(spec=chat_protocol_spec)


def get_weather(latitude: float, longitude: float) -> Dict[str, Any]:
    """Fetch current weather and a 3-day local forecast from Open-Meteo."""
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("Latitude must be between -90 and 90.")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("Longitude must be between -180 and 180.")

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,relative_humidity_2m,precipitation",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
        "forecast_days": 3,
        "timezone": "auto",
    }
    response = requests.get(
        OPEN_METEO_URL, params=params, timeout=15,
        headers={"User-Agent": "KisanSahayak/1.0"},
    )
    response.raise_for_status()
    data = response.json()
    current, daily = data.get("current"), data.get("daily")

    if not isinstance(current, dict) or not isinstance(daily, dict):
        raise ValueError("Weather provider returned incomplete data.")
    if any(current.get(k) is None for k in (
        "temperature_2m", "relative_humidity_2m", "precipitation"
    )):
        raise ValueError("Current weather values are missing.")
    if not daily.get("time"):
        raise ValueError("Weather provider returned no forecast dates.")

    return {
        "current": current, "daily": daily,
        "timezone": data.get("timezone", "Unknown"),
        "source": "Open-Meteo",
    }


def _number(value: Any) -> Optional[float]:
    """Safely convert a value to a finite float."""
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def estimate_spoilage_risk(
    weather: Dict[str, Any],
    request_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Transparent screening estimate, not a scientific shelf-life prediction.
    Crop/context indicators add useful detail but are not calibrated probabilities.
    """
    request_data = request_data or {}
    current, daily = weather["current"], weather["daily"]
    temperature = float(current["temperature_2m"])
    humidity = float(current["relative_humidity_2m"])

    max_temps = [_number(v) for v in (daily.get("temperature_2m_max") or [])]
    rain_probs = [_number(v) for v in (daily.get("precipitation_probability_max") or [])]
    rain_mm = [_number(v) for v in (daily.get("precipitation_sum") or [])]
    valid_temps = [v for v in max_temps if v is not None]
    valid_probs = [v for v in rain_probs if v is not None]
    valid_mm = [v for v in rain_mm if v is not None]

    crop = str(request_data.get("crop_type", DEFAULT_CROP)).strip().lower()
    condition = str(request_data.get("crop_condition", "")).strip().lower()
    harvest_status = str(request_data.get("harvest_status", "")).strip().lower()
    storage_type = str(request_data.get("storage_type", "")).strip().lower()
    storage_temp = _number(request_data.get("storage_temperature_c"))

    # This agent is intentionally focused on tomatoes rather than generic crops.
    tomato_names = {"tomato", "tomatoes"}

    score, factors, actions, unknowns = 0, [], [], []

    if temperature >= 30:
        score += 2
        factors.append("Hot current conditions can speed deterioration in harvested produce.")
        actions.append("Keep harvested produce shaded and move it to suitable storage promptly.")
    elif temperature >= 25:
        score += 1
        factors.append("Warm conditions may shorten quality life for some harvested produce.")

    if humidity >= 85:
        score += 2
        factors.append("Very high humidity makes moisture management harder and can encourage decay if produce stays wet.")
        actions.append("Keep produce dry and inspect it for softening, mold or decay.")
    elif humidity >= 70:
        score += 1
        factors.append("High humidity makes careful drying, handling and inspection important.")

    max_prob = max(valid_probs) if valid_probs else None
    max_mm = max(valid_mm) if valid_mm else None
    if max_prob is not None and max_prob >= 70:
        
        factors.append(f"Rain is likely during the forecast (highest daily probability {max_prob:.0f}%).")
        actions.append("Protect harvested produce from rain and arrange covered transport if selling soon.")
    elif max_prob is not None and max_prob >= 40:
        
        factors.append("Rain is possible during the forecast; plan handling and transport carefully.")
    if max_mm is not None and max_mm >= 20:
        factors.append(f"Heavy rainfall is forecast (up to {max_mm:.1f} mm per day), which may disrupt harvest or transport.")

    if valid_temps and max(valid_temps) >= 35:
        score += 1
        factors.append("Very hot daytime weather is forecast.")

    if crop in tomato_names:
        # Tomatoes are perishable, but crop identity alone does not add risk points.
        factors.append("Tomatoes are perishable; maturity, bruising, ventilation and suitable storage affect quality after harvest.")
    else:
        unknowns.append("This agent is designed for tomatoes. The tomato-specific screening may not apply to another crop.")

    if condition in {"damaged", "overripe", "spoiling", "spoiled", "bruised"}:
        score += 2
        factors.append("The crop is reported as damaged, overripe or already deteriorating.")
        actions.append("Inspect and sort produce immediately; do not sell spoiled or unsafe produce.")
    elif condition in {"ripe", "fully ripe", "mature", "ready", "ready to sell"}:
        score += 1
        factors.append("The crop is reported as ripe or mature; check readiness and plan harvest and sale without avoidable delay.")
    else:
        unknowns.append("Crop condition was not supplied or was not recognized; inspect ripeness, bruising and visible decay.")

    if harvest_status in {"harvested", "picked", "yes", "true"}:
        factors.append("Produce is reported as harvested, so post-harvest shade, handling and storage matter now.")
    elif harvest_status in {"not harvested", "unharvested", "no", "false"}:
        factors.append("Crop is reported as not yet harvested; consider crop readiness and field conditions.")
    else:
        unknowns.append("Harvest status was not supplied; guidance cannot fully distinguish field risk from post-harvest risk.")

    if storage_temp is not None:
        if storage_temp >= 30:
            score += 2
            factors.append(f"Reported storage temperature ({storage_temp:g}°C) is hot for many fresh crops.")
            actions.append("Move produce to crop-appropriate cooler storage if available.")
        elif storage_temp >= 25:
            score += 1
            factors.append(f"Reported storage temperature ({storage_temp:g}°C) may be warm for many fresh crops.")
    elif storage_type not in {"cold storage", "refrigerated", "refrigeration", "cool room"}:
        unknowns.append("Actual storage temperature is unknown; risk may differ inside the storage area.")

    level = "HIGH" if score >= 7 else "MODERATE" if score >= 4 else "LOW"
    if not factors:
        factors.append("No strong warning indicators were found in the available weather and crop details.")
    if not actions:
        actions.append("Use clean handling, crop-appropriate storage and regular checks for damage or decay.")

    return {
        "risk_level": level,
        "screening_score": score,
        "risk_scope": "Weather, handling and reported storage-condition screening; not a direct measurement of actual post-harvest spoilage.",
        "score_interpretation": "Heuristic indicator only; not a percentage chance of spoilage, a scientifically validated spoilage prediction, or a validated shelf-life model.",
        "risk_factors": factors,
        "recommended_precautions": list(dict.fromkeys(actions)),
        "missing_information": unknowns,
        "crop_group_note": "Crop category adjusts the screening indicator only; this is not a calibrated crop-specific model.",
    }


def create_farmer_guidance(
    request_data: Dict[str, Any],
    weather: Dict[str, Any],
    risk: Dict[str, Any],
    forecast: list,
) -> Dict[str, Any]:
    """Turn weather and screening results into practical farmer decisions."""
    location = str(request_data.get("location", DEFAULT_LOCATION)).strip()
    crop = str(request_data.get("crop_type", DEFAULT_CROP)).strip()
    condition = str(request_data.get("crop_condition", "not specified")).strip().lower()
    harvest_status = str(request_data.get("harvest_status", "unknown")).strip().lower()

    today_text = weather["current"].get("time", "")[:10] or weather["daily"]["time"][0]
    try:
        today = date.fromisoformat(today_text)
    except ValueError:
        today = date.fromisoformat(weather["daily"]["time"][0])

    def parse_optional_date(field: str) -> Optional[date]:
        value = request_data.get(field)
        if value in (None, ""):
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"{field} must use YYYY-MM-DD format.") from exc

    harvest_date = parse_optional_date("harvest_date")
    sale_date = parse_optional_date("planned_sale_date")
    if harvest_date and harvest_date > today:
        raise ValueError("harvest_date cannot be in the future.")
    if sale_date and sale_date < today:
        raise ValueError("planned_sale_date cannot be in the past.")
    if harvest_date and sale_date and sale_date < harvest_date:
        raise ValueError("planned_sale_date cannot be before harvest_date.")

    # Normalize both historical/current key spellings to one canonical key.
    days = []
    for day in forecast:
        probability = day.get("rain_probability_percent")
        if probability is None:
            probability = day.get("precipitation_probability_percent")
        days.append({**day, "rain_probability_percent": _number(probability)})

    high_rain_days = [
        d for d in days
        if d["rain_probability_percent"] is not None
        and d["rain_probability_percent"] >= 70
        and date.fromisoformat(d["date"]) >= today
    ]
    next_rain_before = next(
        (d for d in high_rain_days if date.fromisoformat(d["date"]) > today), None
    )
    rain_today = next(
        (d for d in high_rain_days if date.fromisoformat(d["date"]) == today), None
    )
    known_rain_days = [d for d in days if d["rain_probability_percent"] is not None]
    upcoming_days = [
        d for d in known_rain_days
        if date.fromisoformat(d["date"]) >= today
    ]
    best_transport_day = min(
        upcoming_days,
        key=lambda d: d["rain_probability_percent"],
        default=None,
    )
    upcoming_rain_probs = [
        d["rain_probability_percent"]
        for d in upcoming_days
        if d["rain_probability_percent"] is not None
    ]
    max_upcoming_rain = (
        max(upcoming_rain_probs) if upcoming_rain_probs else None
    )
    if max_upcoming_rain is None:
        transport_alert_level = "UNKNOWN"
        transport_alert_message = "Rain probability data is unavailable."
    elif max_upcoming_rain >= 70:
        transport_alert_level = "HIGH"
        transport_alert_message = (
            f"High rain alert: rain probability reaches "
            f"{max_upcoming_rain:.0f}%. Arrange covered transport "
            "and check road conditions."
        )
    elif max_upcoming_rain >= 40:
        transport_alert_level = "MODERATE"
        transport_alert_message = (
            f"Moderate rain alert: rain probability reaches "
            f"{max_upcoming_rain:.0f}%. Plan covered transport."
        )
    else:
        transport_alert_level = "LOW"
        transport_alert_message = (
            "No high rain probability appears in the upcoming forecast."
        )

    harvested = harvest_status in {"harvested", "picked", "yes", "true"}
    ready = condition in {"ripe", "fully ripe", "mature", "ready", "ready to sell"}

    if next_rain_before:
        rain_date = next_rain_before["date"]
        rain_pct = next_rain_before["rain_probability_percent"]
        if harvested and ready:
            message = (
                f"{crop} in {location}: rain is likely on {rain_date} ({rain_pct:.0f}% chance). "
                "If the produce is healthy and market arrangements are ready, aim to sell and transport "
                "before then if practical. This is a weather-planning suggestion, not a spoilage deadline."
            )
            selling_window = f"Aim to sell before {rain_date} if practical; confirm market access and the latest forecast."
        elif harvested:
            message = (
                f"{crop} in {location}: rain is likely on {rain_date} ({rain_pct:.0f}% chance). "
                "Protect harvested produce and check its condition before deciding whether it is ready to sell."
            )
            selling_window = "Check crop condition and market arrangements; consider selling before rain if produce is ready."
        else:
            message = (
                f"{crop} in {location}: rain is likely on {rain_date} ({rain_pct:.0f}% chance). "
                "Plan field work and transport around the rain. Harvest only when the crop is ready and conditions are suitable."
            )
            selling_window = "No firm sale date without harvest readiness and market information."
        action_before_date = rain_date
    elif rain_today:
        rain_pct = rain_today["rain_probability_percent"]
        message = (
            f"{crop} in {location}: rain is likely today ({rain_pct:.0f}% chance). "
            "Protect harvested produce from rain and check road and weather conditions before transporting."
        )
        selling_window = "If produce is harvested and ready, sell or transport today only if conditions and arrangements are safe."
        action_before_date = None
    else:
        possible_rain = next(
            (d for d in days if d["rain_probability_percent"] is not None
             and d["rain_probability_percent"] >= 40
             and date.fromisoformat(d["date"]) >= today), None
        )
        if possible_rain:
            message = (
                f"{crop} in {location}: rain is possible on {possible_rain['date']} "
                f"({possible_rain['rain_probability_percent']:.0f}% chance). "
                "Check the forecast again before harvesting or transporting."
            )
        else:
            message = (
                f"{crop} in {location}: no high-rain day (70% or above) appears in this 3-day forecast. "
                "Use the risk factors and crop condition below to plan handling and sale."
            )
        selling_window = "Choose sale timing using crop readiness, visible condition and market arrangements; this forecast does not set a spoilage deadline."
        action_before_date = None

    actions = list(risk.get("recommended_precautions", []))
    humidity = float(weather["current"]["relative_humidity_2m"])
    temperature = float(weather["current"]["temperature_2m"])
    if humidity >= 80:
        actions.append("Humidity is high: keep harvested produce dry and inspect it daily for softening, mold or decay.")
    if temperature >= 30:
        actions.append("It is hot: keep produce shaded and move it to suitable storage promptly.")
    if high_rain_days:
        rain_dates = ", ".join(
            f"{d['date']} ({d['rain_probability_percent']:.0f}%)" for d in high_rain_days
        )
        actions.append(f"High rain chances are forecast for {rain_dates}; plan covered loading and transport.")
    if condition in {"damaged", "overripe", "spoiling", "spoiled", "bruised"}:
        actions.append("Sort damaged produce promptly; never sell produce that is spoiled or unsafe.")
    if not harvested:
        actions.append("If the crop is still in the field, decide harvest timing from crop maturity and local field conditions, not rain probability alone.")
    actions = list(dict.fromkeys(actions))

    if sale_date:
        sale_day = next((d for d in days if d["date"] == sale_date.isoformat()), None)
        if sale_day is None:
            sale_advice = "That date is outside the 3-day forecast; check the weather again closer to the planned sale."
        else:
            probability = sale_day["rain_probability_percent"]
            if probability is not None and probability >= 70:
                sale_advice = f"Rain chance is {probability:.0f}% on your planned sale date. Consider an earlier sale if practical or arrange covered transport."
            elif probability is not None and probability >= 40:
                sale_advice = f"Rain chance is {probability:.0f}% on your planned sale date. Keep a covered-transport plan and recheck the forecast."
            else:
                sale_advice = "No high rain probability is shown for this date, but recheck the forecast and road conditions before travelling."
    else:
        sale_advice = "Provide planned_sale_date (YYYY-MM-DD) to check a specific sale date against this forecast."

    storage_temp = _number(request_data.get("storage_temperature_c"))
    storage_type = str(request_data.get("storage_type", "not specified")).strip()
    if storage_temp is not None:
        storage_note = f"Reported storage temperature is {storage_temp:g}°C. Suitability depends on the crop; use crop-specific storage guidance."
    elif storage_type.lower() in {"cold storage", "refrigerated", "refrigeration", "cool room"}:
        storage_note = f"Storage is reported as {storage_type}, but its actual temperature is unknown. Check the temperature and use crop-appropriate settings."
    else:
        storage_note = "Storage conditions are unknown or insufficient to estimate shelf life. Weather alone cannot establish exact safe-storage days."

    return {
        "farmer_message": message,
        "spoilage_screening_risk": risk["risk_level"],
        "weather_related_risk": transport_alert_level,
        "weather_transport_alert": {
            "level": transport_alert_level,
            "message": transport_alert_message,
            "highest_upcoming_rain_probability_percent": max_upcoming_rain,
        },
        "risk_reasons": risk["risk_factors"],
        "recommended_selling_window": selling_window,
        "action_before_date": action_before_date,
        "planned_sale_date": sale_date.isoformat() if sale_date else None,
        "planned_sale_date_advice": sale_advice,
        "what_to_do_now": actions,
        "transport_suggestion": (
            {
                "date_with_lowest_rain_probability": best_transport_day["date"],
                "rain_probability_percent": best_transport_day["rain_probability_percent"],
                "note": "Lowest forecast rain chance only; it does not guarantee dry weather or safe roads.",
            } if best_transport_day else {
                "date_with_lowest_rain_probability": None,
                "rain_probability_percent": None,
                "note": "Rain probability data is unavailable.",
            }
        ),
        "safe_storage_days": None,
        "storage_note": storage_note,
        "important_limit": "This is a heuristic weather, transport and reported-condition screening tool, not a direct assessment of actual post-harvest spoilage, a validated shelf-life model, or a market-price predictor. Actual spoilage depends on maturity, physical damage, handling, sanitation, time since harvest and real storage conditions.",
    }


def process_weather_request(request_data: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch weather and return farmer advice plus structured forecast data."""
    if not isinstance(request_data, dict):
        raise ValueError("Request must be a JSON object.")
    # Bengaluru is the default. A named location must be recognized unless the
    # caller supplies a complete latitude/longitude pair for a custom location.
    # Never silently combine one supplied coordinate with Bengaluru's other one.
    request_data = dict(request_data)
    location_was_supplied = bool(str(request_data.get("location", "")).strip())
    request_data.setdefault("location", DEFAULT_LOCATION)
    request_data.setdefault("crop_type", DEFAULT_CROP)

    has_latitude = request_data.get("latitude") not in (None, "")
    has_longitude = request_data.get("longitude") not in (None, "")
    if has_latitude != has_longitude:
        raise ValueError(
            "Provide both latitude and longitude together, or provide a recognized location name "
            "such as Bengaluru, Kolar, Ramanagara, or Channapatna."
        )

    location_key = str(request_data.get("location", DEFAULT_LOCATION)).strip().lower()
    preset = LOCATION_PRESETS.get(location_key)
    if has_latitude and has_longitude:
        try:
            latitude = float(request_data["latitude"])
            longitude = float(request_data["longitude"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Latitude and longitude must be numbers when supplied.") from exc
        # Do not label custom coordinates as Bengaluru unless the caller said so.
        if not location_was_supplied:
            request_data["location"] = "Supplied coordinates"
    elif preset:
        latitude, longitude, display_location = preset
        request_data["location"] = display_location
    else:
        raise ValueError(
            "Unrecognized location. Use Bengaluru, Kolar, Ramanagara, or Channapatna, "
            "or provide both latitude and longitude."
        )

    weather = get_weather(latitude, longitude)
    risk = estimate_spoilage_risk(weather, request_data)
    current, daily = weather["current"], weather["daily"]

    forecast = []
    fields = {
        "temperature_max_c": daily.get("temperature_2m_max", []),
        "temperature_min_c": daily.get("temperature_2m_min", []),
        "rainfall_mm": daily.get("precipitation_sum", []),
        "rain_probability_percent": daily.get("precipitation_probability_max", []),
    }
    for i, day_date in enumerate(daily.get("time", [])):
        row = {"date": day_date}
        for output_key, values in fields.items():
            row[output_key] = values[i] if i < len(values) else None
        forecast.append(row)

    guidance = create_farmer_guidance(request_data, weather, risk, forecast)
    return {
        "agent": "Kisan Sahayak - Bengaluru Tomato Weather & Spoilage Agent",
        "status": "success",
        "farmer_message": guidance["farmer_message"],
        "location": request_data.get("location", DEFAULT_LOCATION),
        "crop_type": request_data.get("crop_type", DEFAULT_CROP),
        "input_crop_condition": request_data.get("crop_condition", ""),
        "input_harvest_status": request_data.get("harvest_status", ""),
        "current_weather": {
            "temperature_c": current["temperature_2m"],
            "humidity_percent": current["relative_humidity_2m"],
            "rainfall_mm": current["precipitation"],
            "timezone": weather["timezone"],
        },
        "forecast_next_3_days": forecast,
        "spoilage_risk": risk,
        "farmer_guidance": guidance,
        "data_source": weather["source"],
        "warning": "Risk categories are heuristic screening indicators, not calibrated spoilage probabilities. Tomato shelf life depends on maturity, handling, bruising and actual storage conditions; weather alone cannot predict exact safe-storage days.",
    }



def format_farmer_report(result: Dict[str, Any]) -> str:
    """Render a farmer-friendly report instead of raw JSON in the chat."""
    if result.get("status") != "success":
        return f"❌ Unable to generate advice\n\n{result.get('message', 'Unknown error')}"

    location = result.get("location", "Bengaluru")
    weather = result.get("current_weather", {})
    forecast = result.get("forecast_next_3_days", [])
    guidance = result.get("farmer_guidance", {})
    risk = result.get("spoilage_risk", {})
    request_condition = str(result.get("input_crop_condition", "")).lower()
    request_harvest = str(result.get("input_harvest_status", "")).lower()

    today = forecast[0] if forecast else {}
    tomorrow = forecast[1] if len(forecast) > 1 else {}
    rain_tomorrow = tomorrow.get("rain_probability_percent")
    rain_text = f"{rain_tomorrow:.0f}%" if isinstance(rain_tomorrow, (int, float)) else "Unavailable"

    ready = request_condition in {"ripe", "fully ripe", "mature", "ready", "ready to sell"}
    harvested = request_harvest in {"harvested", "picked", "yes", "true"}
    risk_level = risk.get("risk_level", "UNKNOWN")
    transport_alert = guidance.get("weather_transport_alert", {})
    transport_alert_level = transport_alert.get("level", "UNKNOWN")
    transport_alert_message = transport_alert.get(
        "message", "Weather/transport alert unavailable."
    )
    high_rain = isinstance(rain_tomorrow, (int, float)) and rain_tomorrow >= 70
    hot = isinstance(weather.get("temperature_c"), (int, float)) and weather["temperature_c"] >= 30
    humid = isinstance(weather.get("humidity_percent"), (int, float)) and weather["humidity_percent"] >= 80

    if harvested and ready and (high_rain or (hot and humid) or risk_level == "HIGH"):
        decision = "🟡 CONSIDER SELLING SOON — especially before forecast rain, if the tomatoes are healthy and market arrangements are ready."
        sale_window = "Consider a sale within the next 24 hours if practical, after checking tomato condition, market arrangements and the latest forecast. This is not a spoilage deadline."
    elif harvested and ready:
        decision = "🟡 PLAN SALE — check tomato condition and local market arrangements before deciding the timing."
        sale_window = "Choose a suitable nearby sale window after checking the latest forecast and market conditions."
    elif harvested:
        decision = "🟡 INSPECT FIRST — sort tomatoes by ripeness and condition before deciding what is ready to sell."
        sale_window = "Decide after checking ripeness, visible damage and market arrangements."
    else:
        decision = "🟡 PLAN HARVEST — use tomato maturity and field conditions to decide when to harvest."
        sale_window = "No fixed sale window can be recommended until harvest status and tomato readiness are known."

    reasons = []
    if ready:
        reasons.append("Tomatoes are reported as ripe or ready to sell.")
    if hot:
        reasons.append("High temperature can speed softening and quality loss after harvest.")
    if humid:
        reasons.append("High humidity makes careful moisture management and inspection important.")
    if high_rain:
        reasons.append(f"Rain may affect handling or transport tomorrow (forecast probability: {rain_text}).")
    if not reasons:
        reasons.extend(risk.get("risk_factors", [])[:4])
    if not reasons:
        reasons.append("Check tomato maturity, bruising and visible decay before making a selling decision.")

    storage_advice = "Keep tomatoes shaded, ventilated and dry; avoid leaving large quantities in hot conditions. Use suitable storage and inspect regularly."
    storage_note = guidance.get("storage_note")
    if storage_note:
        storage_advice += "\nStorage note: " + storage_note

    transport_advice = "Use covered transport if rain is forecast, avoid rough handling, and check road conditions before departure."
    transport = guidance.get("transport_suggestion", {})
    if transport.get("date_with_lowest_rain_probability"):
        transport_advice += f" Lowest forecast rain probability is on {transport['date_with_lowest_rain_probability']} ({transport.get('rain_probability_percent')}%)."

    lines = [
        f"📍 Location: {location}",
        "",
        "🌤 Weather Today",
        f"Temperature: {weather.get('temperature_c', 'N/A')}°C",
        f"Humidity: {weather.get('humidity_percent', 'N/A')}%",
        "",
        "🌧 Rain Outlook",
        f"Rain probability tomorrow: {rain_text}",
        "",
        "🍅 Tomato Market Advice",
        "",
        decision,
        "",
        "Why?",
    ]
    lines.extend(f"• {reason}" for reason in reasons)
    lines.extend([
        "",
        "📦 Storage Advice",
        storage_advice,
        "",
        "🚚 Transport Advice",
        transport_advice,
        "",
        "🕒 Suggested Sale Window",
        sale_window,
        "",
        f"Weather/handling screening: {risk_level} (heuristic indicator, not a scientifically validated spoilage prediction).",
        f"Weather/transport alert: {transport_alert_level}.",
        transport_alert_message,
        "Important: This score reflects weather and reported handling/storage indicators; it does not confirm actual post-harvest spoilage or determine exact shelf life.",
        f"Weather source: {result.get('data_source', 'Open-Meteo')}.",
    ])
    return "\n".join(lines)


def extract_text(message: ChatMessage) -> str:
    return "\n".join(
        item.text for item in message.content if isinstance(item, TextContent)
    ).strip()


@chat_protocol.on_message(ChatMessage)
async def handle_weather_request(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    try:
        text = extract_text(msg)
        if not text:
            raise ValueError("Message is empty.")
        request_data = json.loads(text)
        if not isinstance(request_data, dict):
            raise ValueError("Send a JSON object, not a JSON list.")
        result = process_weather_request(request_data)
        # Keep the same JSON-in / JSON-out message contract as transport.py.
        # The readable report is included as a field so a frontend/coordinator
        # can display it without losing structured data for agent-to-agent use.
        result["formatted_report"] = format_farmer_report(result)
        response_text = json.dumps(result, ensure_ascii=False)
    except (
        json.JSONDecodeError, ValueError, TypeError, OverflowError,
        requests.RequestException,
    ) as exc:
        response_text = json.dumps({
            "agent": "Kisan Sahayak - Bengaluru Tomato Weather & Spoilage Agent",
            "status": "error",
            "message": str(exc),
            "expected_request_example": {
                "location": "Kolar",
                "crop_type": "tomato",
                "crop_condition": "ripe",
                "harvest_status": "harvested"
            }
        }, ensure_ascii=False)

    await ctx.send(
        sender,
        ChatMessage(content=[TextContent(type="text", text=response_text)]),
    )
    ctx.logger.info("Weather request processed.")


@chat_protocol.on_message(ChatAcknowledgement)
async def handle_acknowledgement(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"Message acknowledged by {sender}")


agent.include(chat_protocol, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
