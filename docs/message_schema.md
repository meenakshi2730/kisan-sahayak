# Kisan Sahayak — Agent Message Schema

## Project
Kisan Sahayak: From Mandi to Money

## Purpose
Define a common request and response format for the Orchestrator, Mandi Price, Weather/Spoilage, and Transport agents.

## 1. Farmer Request

All agents should use these shared fields when applicable:

```json
{
  "crop": "tomato",
  "quantity_kg": 1000,
  "origin": "Kolar",
  "preferred_language": "English"
}
```

- `crop`: Crop the farmer wants to sell.
- `quantity_kg`: Quantity available for sale, in kilograms.
- `origin`: Farmer's location or village.
- `preferred_language`: Language for the final recommendation.

## 2. Mandi Price Agent Response

```json
{
  "agent": "price",
  "status": "success",
  "mandi": "Example Mandi",
  "price_per_kg": 20.0,
  "currency": "INR",
  "source": "sample_data",
  "updated_at": null
}
```

`Example Mandi` and the price are illustrative placeholders, not live market data.

## 3. Weather/Spoilage Agent Response

```json
{
  "agent": "weather",
  "status": "success",
  "weather_risk": "medium",
  "estimated_spoilage_fraction": 0.05,
  "source": "sample_data"
}
```

`estimated_spoilage_fraction` is a decimal between 0 and 1. The value above is illustrative.

## 4. Transport Agent Response

```json
{
  "agent": "transport",
  "status": "success",
  "destination_mandi": "Example Mandi",
  "distance_km": 30,
  "estimated_transport_cost_inr": 800,
  "source": "mock_estimate"
}
```

The example distance and cost are placeholders, not verified route quotes.

## 5. Final Recommendation

The Orchestrator combines the specialist responses and ranks mandis by estimated net earnings.

```json
{
  "crop": "tomato",
  "quantity_kg": 1000,
  "recommendations": [
    {
      "mandi": "Example Mandi",
      "price_per_kg": 20.0,
      "estimated_gross_revenue_inr": 20000,
      "estimated_spoilage_loss_inr": 1000,
      "estimated_transport_cost_inr": 800,
      "estimated_other_costs_inr": 200,
      "estimated_net_earnings_inr": 18000
    }
  ],
  "data_quality_note": "Illustrative sample values"
}
```

## 6. Net Earnings Formula

`Net earnings = price × quantity × (1 − spoilage fraction) − transport cost − other costs`

Other costs may include mandi commission and loading/unloading.

## 7. Error and Fallback Convention

Every agent should return:
- `status`: `success` or `fallback`
- `source`: where the result came from
- A clear explanation if live data is unavailable

Never present mock prices, distances, or costs as verified live information.

## 8. Integration Rules

1. Use the field names defined in this document.
2. Prices are in INR per kg.
3. Quantity is in kilograms.
4. Spoilage is represented as a fraction between 0 and 1.
5. The Orchestrator handles missing data and combines responses.
6. Agent communication and the final ASI:One workflow must be tested before deployment.
