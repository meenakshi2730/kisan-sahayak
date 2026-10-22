
import json
from pathlib import Path
from typing import Any

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "sample_prices.json"


def get_mandi_prices(crop: str = "tomato") -> dict[str, Any]:
    """Return available mandi prices for the requested crop."""

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


if __name__ == "__main__":
    result = get_mandi_prices("tomato")
    print(json.dumps(result, indent=2, ensure_ascii=False))
