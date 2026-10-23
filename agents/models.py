from uagents import Model


class PriceRequest(Model):
    request_id: str
    crop: str
    location: str
    quantity_quintals: float


class PriceResponse(Model):
    request_id: str
    prices_json: str
    source: str
    data_age_hours: float
    warning: str


class TransportRequest(Model):
    request_id: str
    location: str
    mandi: str
    quantity_quintals: float


class TransportResponse(Model):
    request_id: str
    transport_cost: float
    distance_km: float
    travel_time_hours: float
    source: str
    warning: str


class WeatherRequest(Model):
    request_id: str
    location: str
    crop: str


class WeatherResponse(Model):
    request_id: str
    spoilage_loss_fraction: float
    summary: str
    source: str
    warning: str


class OptimizeRequest(Model):
    request_id: str
    crop: str
    location: str
    quantity_quintals: float
    prices_json: str
    transport_cost: float
    spoilage_loss_fraction: float


class OptimizeResponse(Model):
    request_id: str
    result_text: str