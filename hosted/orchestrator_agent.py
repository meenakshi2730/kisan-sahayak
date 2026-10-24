"""
Kisan Sahayak - Orchestrator Agent (HOSTED on Agentverse)

Hosted-agent notes:
  * Paste this whole file into the agent's Build tab (agent.py). Do not call agent.run().
  * Settings come from the agent's Secrets tab (see CONFIG below). There is no .env on Agentverse.
  * Agent addresses cannot be derived from seeds here, so set PRICE_AGENT_ADDRESS,
    TRANSPORT_AGENT_ADDRESS and WEATHER_AGENT_ADDRESS as Secrets (the hosted agents' agent1q... addresses).

Talks to ASI:One / Agentverse chat on one side and to the Price, Transport and Weather
agents (Chat Protocol, JSON text) on the other. It never blocks inside a handler:
each farmer request is a small state machine stored in ctx.storage.

  farmer text -> parse -> PRICE agent -> (TRANSPORT agent + WEATHER agent in parallel)
              -> net-profit ranking -> reply to the farmer
"""
import asyncio
import json
import math
import os
import re
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

# ------------------------------------------------------------------
# CONFIG  (set these in the agent's Secrets tab; all are read with os.getenv)
#   PRICE_AGENT_ADDRESS, TRANSPORT_AGENT_ADDRESS, WEATHER_AGENT_ADDRESS   (required)
#   ASI_ONE_API_KEY (optional), ASI_MODEL, AGENT_TIMEOUT_S, COMMISSION_PCT,
#   ASK_CONDITION, CONFIRM_BEFORE_RUN (all optional)
# ------------------------------------------------------------------
ASI_ONE_API_KEY = os.getenv("ASI_ONE_API_KEY", "").strip()
ASI_MODEL = os.getenv("ASI_MODEL", "asi1").strip()
ASI_ONE_URL = "https://api.asi1.ai/v1/chat/completions"

PRICE_AGENT_ADDRESS = os.getenv("PRICE_AGENT_ADDRESS", "").strip()
TRANSPORT_AGENT_ADDRESS = os.getenv("TRANSPORT_AGENT_ADDRESS", "").strip()
WEATHER_AGENT_ADDRESS = os.getenv("WEATHER_AGENT_ADDRESS", "").strip()
AGENT_ADDRESSES = {a for a in (PRICE_AGENT_ADDRESS, TRANSPORT_AGENT_ADDRESS, WEATHER_AGENT_ADDRESS) if a}

AGENT_TIMEOUT_S = float(os.getenv("AGENT_TIMEOUT_S", "30"))
COMMISSION_PCT = float(os.getenv("COMMISSION_PCT", "0.05"))     # ASSUMPTION, shown to the user
DEFAULT_SPOILAGE = 0.02                                         # used if the weather agent fails
DEFAULT_TRANSPORT_PER_QUINTAL = 60.0                            # INR, used if the transport agent fails
RISK_TO_SPOILAGE = {"LOW": 0.01, "MODERATE": 0.03, "HIGH": 0.06}  # ASSUMPTION: weather risk -> loss

# Same places the weather agent supports (lat, lon)
LOCATION_COORDS = {
    "bengaluru": (12.9716, 77.5946),
    "kolar": (13.1358, 78.1326),
    "ramanagara": (12.7150, 77.2810),
    "channapatna": (12.6514, 77.2067),
}
LOCATION_ALIASES = {"bangalore": "bengaluru", "bengaluru": "bengaluru", "kolar": "kolar",
                    "ramanagara": "ramanagara", "channapatna": "channapatna"}

# APPROXIMATE mandi coordinates (checked in this order). Replace with exact ones when you have them.
MANDI_COORDS = [
    ("binny", (12.9610, 77.5700)),
    ("bengaluru apmc", (13.0235, 77.5470)),
]
DEFAULT_MANDI_COORD = (12.9716, 77.5946)

# Hosted agents provide their own identity: no seed, port or endpoint here.
agent = Agent()

chat_protocol = Protocol(spec=chat_protocol_spec)

def make_chat_message(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text)])

async def send_text(ctx: Context, to: str, text: str) -> None:
    await ctx.send(to, make_chat_message(text))


def inr(x: float) -> str:
    n = int(round(x))
    s = str(abs(n))
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + "\u20b9" + s

def haversine_km(a, b) -> float:
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))

def mandi_coord(name: str):
    low = name.lower()
    for key, coord in MANDI_COORDS:
        if key in low:
            return coord
    return DEFAULT_MANDI_COORD

def load_state(ctx: Context, rid: str):
    raw = ctx.storage.get(f"req:{rid}")
    return json.loads(raw) if raw else None

def save_state(ctx: Context, st: dict) -> None:
    ctx.storage.set(f"req:{st['rid']}", json.dumps(st))

def get_pending(ctx: Context) -> list:
    raw = ctx.storage.get("pending")
    return json.loads(raw) if raw else []

def set_pending(ctx: Context, ids: list) -> None:
    ctx.storage.set("pending", json.dumps(ids))

CONV_TTL = 1800  # a conversation is remembered for 30 minutes

def load_conv(ctx: Context, user: str) -> dict:
    raw = ctx.storage.get(f"conv:{user}")
    data = json.loads(raw) if raw else {}
    if data and time.time() - data.get("ts", 0) > CONV_TTL:
        return {}
    return data

def save_conv(ctx: Context, user: str, conv: dict) -> None:
    conv["ts"] = time.time()
    ctx.storage.set(f"conv:{user}", json.dumps(conv))

def clear_conv(ctx: Context, user: str) -> None:
    ctx.storage.set(f"conv:{user}", json.dumps({}))

def normalize_location(value):
    if not value:
        return None
    low = str(value).lower()
    for alias, canon in LOCATION_ALIASES.items():
        if alias in low:
            return canon
    return None

def fallback_parse(message: str) -> dict:
    low = message.lower()
    out = {"crop": None, "location": normalize_location(low), "quantity_quintals": None}
    if re.search(r"tomato|tamatar|tamaatar|\u091f\u092e\u093e\u091f\u0930", low):
        out["crop"] = "tomato"
    elif re.search(r"onion|pyaz|pyaaz|potato|aloo|wheat|gehu|soybean|rice|chawal|maize|corn|cotton|sugarcane", low):
        out["crop"] = "other"      # a crop we do not support yet
    m = re.search(r"(\d+(?:\.\d+)?)\s*(quintals?|qtls?|kgs?|kilos?|tons?|tonnes?)", low)
    if m:
        num, unit = float(m.group(1)), m.group(2)
        if unit.startswith(("kg", "kilo")):
            num /= 100.0
        elif unit.startswith(("ton",)):
            num *= 10.0
        out["quantity_quintals"] = num
    return out

def llm_parse(message: str) -> dict:
    r = requests.post(
        ASI_ONE_URL,
        headers={"Authorization": f"Bearer {ASI_ONE_API_KEY}", "Content-Type": "application/json"},
        json={"model": ASI_MODEL, "temperature": 0, "messages": [
            {"role": "system", "content": (
                "Extract a crop-selling request from the farmer's message (English, Hindi, Hinglish or "
                "Kannada). Return ONLY a JSON object with keys crop (lowercase English), location "
                "(town in English letters) and quantity_quintals (number; 100 kg = 1 quintal). "
                "Use null for anything not stated. Never invent values.")},
            {"role": "user", "content": message}]},
        timeout=25,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.DOTALL)
    data = json.loads(m.group(0))
    crop = str(data.get("crop") or "").strip().lower()
    return {"crop": "tomato" if crop in ("tomato", "tomatoes") else (crop or None),
            "location": normalize_location(data.get("location")),
            "quantity_quintals": data.get("quantity_quintals")}

def parse_farmer_message(message: str) -> dict:
    """ASI1 first (if a key is configured); the rule-based parser fills gaps or takes over on failure."""
    fb = fallback_parse(message)
    if not ASI_ONE_API_KEY or ASI_ONE_API_KEY.startswith(("PASTE_", "replace_")):
        return fb
    try:
        out = llm_parse(message)
    except Exception:
        return fb
    for k in ("crop", "location", "quantity_quintals"):
        if not out.get(k):
            out[k] = fb[k]
    return out

# ------------------------------------------------------------------
# CONVERSATION: the agent asks one question at a time, confirms, then runs
#   crop -> quantity -> location -> condition -> confirm -> running -> idle
# ------------------------------------------------------------------
SLOT_ORDER = ["crop", "quantity_quintals", "location", "condition"]
# Fewer questions for the farmer: by default only crop, quantity and location are needed.
ASK_CONDITION = os.getenv("ASK_CONDITION", "false").lower() == "true"      # ask how the tomatoes look
CONFIRM_BEFORE_RUN = os.getenv("CONFIRM_BEFORE_RUN", "false").lower() == "true"  # ask YES before running
REQUIRED = SLOT_ORDER if ASK_CONDITION else SLOT_ORDER[:3]
LOCATION_LIST = "Bengaluru, Kolar, Ramanagara or Channapatna"
CONDITION_LABELS = {"fresh": "fresh and firm", "ripe": "ripe, ready to sell",
                    "damaged": "some damaged or overripe", "not specified": "not specified"}

WELCOME = ("Namaste! I am Kisan Sahayak. I compare mandi prices, transport cost and weather risk "
           "to tell you where you will earn the most from your tomatoes.")

QUESTIONS = {
    "crop": "Which crop would you like to sell? (Right now I can help with tomatoes.)",
    "quantity_quintals": "How much tomato do you have? You can say quintals or kg (100 kg = 1 quintal).",
    "both": (f"How much tomato do you have, and where is it? Example: \"40 quintal, Kolar\" "
             f"(quintals or kg; I work with {LOCATION_LIST})."),
    "location": f"Where are the tomatoes right now? I can work with {LOCATION_LIST}.",
    "condition": ("How are your tomatoes at the moment?\n1. Fresh and firm\n2. Ripe, ready to sell\n"
                  "3. Some are damaged or overripe\nReply 1, 2 or 3 (or say \"not sure\")."),
}

SLOT_KEYWORDS = {
    "crop": ("crop",),
    "quantity_quintals": ("quantity", "amount", "weight", "how much", "quintal", "kg"),
    "location": ("location", "place", "town", "where", "city"),
    "condition": ("condition", "quality", "ripe"),
}

YES_WORDS = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "haan", "ha", "han", "correct", "confirm",
             "go", "proceed", "theek", "sahi", "right", "please", "ji", "sir", "now", "ahead", "it", "is", "that"}
NO_WORDS = {"no", "n", "nope", "nahi", "nahin", "wrong", "change", "edit", "not", "correct"}
GREETINGS = {"hi", "hello", "hey", "namaste", "namaskara", "namaskar", "hii", "helo", "start", "help"}

def tokens(text: str) -> set:
    return set(re.findall(r"\w+", text.lower()))

def is_reset(low: str) -> bool:
    return low in {"reset", "cancel", "restart", "new", "stop", "clear"} or "start over" in low \
        or "start again" in low or "new request" in low

def clean_qty(v):
    try:
        q = float(v)
    except (TypeError, ValueError):
        return None
    if isinstance(v, bool) or not math.isfinite(q) or not 0 < q <= 100000:
        return None
    return q

def parse_condition(text: str, allow_digit: bool = True, allow_skip: bool = True):
    low = text.lower().strip()
    if allow_digit and re.fullmatch(r"[1-3]\.?", low):
        return {"1": "fresh", "2": "ripe", "3": "damaged"}[low[0]]
    if re.search(r"damag|bruis|over\s?ripe|spoil|rott|\brot\b|soft|split|crack", low):
        return "damaged"
    if re.search(r"\bripe\b|ready|\bred\b|pakka", low):
        return "ripe"
    if re.search(r"fresh|firm|green|just harvested|taaza|taza", low):
        return "fresh"
    if allow_skip and re.search(r"not sure|don.?t know|skip|unknown|no idea|pata nahi|any", low):
        return "not specified"
    return None

def parse_step_answer(step: str, text: str):
    """Interpret a short reply to the question we just asked."""
    if step == "crop":
        fb = fallback_parse(text)["crop"]
        if fb:
            return fb
        return "tomato" if tokens(text) & YES_WORDS else None
    if step == "quantity_quintals":
        q = fallback_parse(text)["quantity_quintals"]
        if q is None:
            m = re.search(r"(\d+(?:\.\d+)?)", text)       # a bare number means quintals
            q = float(m.group(1)) if m else None
        return q
    if step == "location":
        return normalize_location(text)
    if step == "condition":
        return parse_condition(text)
    return None

def parse_free(message: str) -> dict:
    out = parse_farmer_message(message)
    out["condition"] = parse_condition(message, allow_digit=False, allow_skip=False)
    return out

def absorb(conv: dict, parsed: dict, overwrite: bool = False) -> list:
    changed = []
    for slot in SLOT_ORDER:
        v = parsed.get(slot)
        if slot == "quantity_quintals":
            v = clean_qty(v)
        if v and (overwrite or conv.get(slot) is None) and conv.get(slot) != v:
            conv[slot] = v
            changed.append(slot)
    return changed

def next_missing(conv: dict):
    for slot in REQUIRED:
        if conv.get(slot) is None:
            return slot
    return None

def retry_text(slot: str, conv: dict) -> str:
    if conv.get("quantity_quintals") is None and conv.get("location") is None and slot != "crop":
        return "Sorry, I didn't catch that. " + QUESTIONS["both"]
    if slot == "location":
        return f"I can't check that place yet. Please choose one of: {LOCATION_LIST}."
    return "Sorry, I didn't catch that. " + QUESTIONS[slot]

def summary_text(conv: dict) -> str:
    return ("Let me confirm what I have:\n"
            f"- Crop: Tomato\n"
            f"- Quantity: {conv['quantity_quintals']:g} quintal\n"
            f"- Location: {conv['location'].title()}\n"
            f"- Condition: {CONDITION_LABELS[conv.get('condition', 'not specified')]}\n\n"
            "Shall I check mandi prices, transport and weather now? Reply YES to go, "
            "or tell me what to change (for example \"60 quintals\" or \"Kolar\").")

async def ask_next(ctx: Context, user: str, conv: dict, prefix: str = "") -> None:
    slot = next_missing(conv)
    if slot is None:
        conv.setdefault("condition", "not specified")
        if CONFIRM_BEFORE_RUN:
            conv["step"] = "confirm"
            save_conv(ctx, user, conv)
            await send_text(ctx, user, prefix + summary_text(conv))
        else:
            await start_request(ctx, user, conv)       # everything known: go straight to the report
        return
    conv["step"] = slot
    save_conv(ctx, user, conv)
    both = slot == "quantity_quintals" and conv.get("location") is None
    await send_text(ctx, user, prefix + QUESTIONS["both" if both else slot])

async def handle_farmer(ctx: Context, sender: str, text: str) -> None:
    low = text.lower().strip()
    words = tokens(text)
    conv = load_conv(ctx, sender)
    step = conv.get("step")

    # 1. start over at any time
    if is_reset(low):
        await ask_next(ctx, sender, {}, "Okay, starting fresh.\n\n")
        return

    # 2. a request is already being processed
    if step == "running":
        st = load_state(ctx, conv["rid"]) if conv.get("rid") else None
        if st and not st["done"]:
            await send_text(ctx, sender, "I am still checking the mandis for you. Your report will arrive in a few seconds.")
            return
        step = conv["step"] = "idle"

    # 3. a finished request: allow quick follow-ups like "what if 60 quintals?"
    if step == "idle":
        if not (words and words <= GREETINGS):
            changed = absorb(conv, await asyncio.to_thread(parse_free, text), overwrite=True)
            if changed:
                await ask_next(ctx, sender, conv, "Sure, updating that. ")
                return
        conv, step = {}, None

    # 4. confirmation / editing
    if step in ("confirm", "edit"):
        if step == "confirm" and words and words <= YES_WORDS and not (words & NO_WORDS - YES_WORDS):
            await start_request(ctx, sender, conv)
            return
        if step == "confirm" and words and words <= NO_WORDS:
            conv["step"] = "edit"
            save_conv(ctx, sender, conv)
            await send_text(ctx, sender, "No problem. What should I change: quantity, location or condition? "
                                         "You can also just type the new value.")
            return
        changed = absorb(conv, await asyncio.to_thread(parse_free, text), overwrite=True)
        if changed:
            await ask_next(ctx, sender, conv, "Updated. ")
            return
        for slot, keys in SLOT_KEYWORDS.items():
            if any(k in low for k in keys):
                conv[slot] = None
                await ask_next(ctx, sender, conv)
                return
        await send_text(ctx, sender, "Please reply YES to continue, or tell me what to change "
                                     "(quantity, location or condition).")
        return

    # 5. greeting with nothing else
    if not step and words and words <= GREETINGS:
        await ask_next(ctx, sender, {}, WELCOME + "\n\n")
        return

    # 6. collecting details, one question at a time
    parsed = {}
    if step in SLOT_ORDER:
        v = parse_step_answer(step, text)
        if v is not None:
            parsed[step] = v
    if not parsed or len(text.split()) > 1:      # longer message: it may answer several questions
        for k, v in (await asyncio.to_thread(parse_free, text)).items():
            if v and not parsed.get(k):
                parsed[k] = v
    prefix = ""
    if parsed.get("crop") == "other":
        parsed.pop("crop")
        prefix = "I can only help with tomatoes for now. "
    changed = absorb(conv, parsed)
    if not changed and not prefix:
        if step in SLOT_ORDER:
            conv["step"] = step
            save_conv(ctx, sender, conv)
            await send_text(ctx, sender, retry_text(step, conv))
            return
        prefix = WELCOME + "\n\n"
    elif changed and not prefix:
        prefix = "Got it. "
    await ask_next(ctx, sender, conv, prefix)

async def start_request(ctx: Context, farmer: str, conv: dict) -> None:
    rid = uuid4().hex[:8]
    st = {"rid": rid, "farmer": farmer, "created": time.time(), "crop": "tomato",
          "location": conv["location"], "qty": conv["quantity_quintals"],
          "condition": conv.get("condition", "not specified"), "stage": "price", "done": False,
          "prices": None, "price_source": "", "transport": None, "weather": None,
          "transport_in": False, "weather_in": False, "warnings": [], "fanout_at": 0.0, "error": ""}
    save_state(ctx, st)
    set_pending(ctx, get_pending(ctx) + [rid])
    conv["step"], conv["rid"] = "running", rid
    save_conv(ctx, farmer, conv)
    ctx.logger.info(f"[{rid}] new request: {st['qty']:g} quintal tomato at {st['location']}, condition={st['condition']}")
    await send_text(ctx, farmer, f"Thank you! Checking prices, weather and transport for {st['qty']:g} quintal "
                                 f"tomato near {st['location'].title()}. This takes up to {AGENT_TIMEOUT_S:.0f} seconds...")
    if not PRICE_AGENT_ADDRESS:
        st["error"] = "PRICE_AGENT_ADDRESS secret is not set"
        save_state(ctx, st)
        await finalize(ctx, rid)
        return
    await send_text(ctx, PRICE_AGENT_ADDRESS, json.dumps(
        {"request_id": rid, "crop": "tomato", "location": st["location"], "quantity_quintals": st["qty"]}))
    ctx.logger.info(f"[{rid}] price request sent")

def normalize_prices(raw) -> list:
    out = []
    for rec in raw or []:
        if not isinstance(rec, dict):
            continue
        name = rec.get("mandi") or rec.get("name")
        try:
            per_q = rec.get("price_per_quintal")
            per_q = float(per_q) if per_q is not None else float(rec["price_per_kg"]) * 100.0
        except (KeyError, TypeError, ValueError):
            continue
        if name and per_q > 0:
            out.append({"mandi": str(name), "price_per_quintal": per_q, "date": rec.get("observation_date", ""),
                        "source": rec.get("source", "")})
    return out

async def handle_agent_reply(ctx: Context, sender: str, text: str) -> None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        ctx.logger.warning(f"reply from {sender[:14]}... is not JSON; ignored: {text[:80]!r}")
        return
    rid = data.get("request_id") if isinstance(data, dict) else None
    st = load_state(ctx, rid) if rid else None
    if not st or st["done"]:
        ctx.logger.info(f"reply with unknown/finished request_id {rid!r} ignored")
        return
    if sender == PRICE_AGENT_ADDRESS:
        await on_price(ctx, st, data)
    elif sender == TRANSPORT_AGENT_ADDRESS:
        await on_transport(ctx, st, data)
    elif sender == WEATHER_AGENT_ADDRESS:
        await on_weather(ctx, st, data)

async def on_price(ctx: Context, st: dict, data: dict) -> None:
    rid = st["rid"]
    if st["stage"] != "price":
        return
    prices = normalize_prices(data.get("prices"))
    ctx.logger.info(f"[{rid}] price reply: status={data.get('status')} mandis={len(prices)} source={data.get('source')}")
    if not prices:
        st["error"] = data.get("message") or "price agent returned no prices"
        save_state(ctx, st)
        await finalize(ctx, rid)
        return
    st["prices"], st["price_source"] = prices, str(data.get("source"))
    if data.get("source") != "AGMARKNET":
        st["warnings"].append("Prices are ILLUSTRATIVE SAMPLE data, not live market prices.")
    elif data.get("status") == "partial":
        st["warnings"].append("Only some mandis reported prices.")
    st["stage"], st["fanout_at"] = "fanout", time.time()

    origin = LOCATION_COORDS[st["location"]]
    dests = []
    for p in prices:
        c = mandi_coord(p["mandi"])
        dests.append({"name": p["mandi"], "latitude": c[0], "longitude": c[1],
                      "distance_km": round(haversine_km(origin, c) * 1.3, 1)})  # fallback if live routing fails
    if TRANSPORT_AGENT_ADDRESS:
        await send_text(ctx, TRANSPORT_AGENT_ADDRESS, json.dumps({
            "request_id": rid, "origin": {"name": st["location"].title(), "latitude": origin[0], "longitude": origin[1]},
            "destinations": dests, "quantity_quintals": st["qty"]}))
        ctx.logger.info(f"[{rid}] transport request sent ({len(dests)} destinations)")
    else:
        st["transport_in"] = True
        st["warnings"].append("TRANSPORT_AGENT_ADDRESS secret not set; transport costs are estimates.")
    if WEATHER_AGENT_ADDRESS:
        await send_text(ctx, WEATHER_AGENT_ADDRESS, json.dumps({
            "request_id": rid, "crop_type": "tomato", "crop_condition": st.get("condition", "not specified"),
            "harvest_status": "unknown", "location": st["location"].title()}))
        ctx.logger.info(f"[{rid}] weather request sent")
    else:
        st["weather_in"] = True
        st["warnings"].append("WEATHER_AGENT_ADDRESS secret not set; assumed standard spoilage.")
    save_state(ctx, st)
    await maybe_finish(ctx, rid)

async def on_transport(ctx: Context, st: dict, data: dict) -> None:
    rid = st["rid"]
    if st["stage"] != "fanout" or st["transport_in"]:
        return
    results = data.get("results") if isinstance(data.get("results"), list) else []
    usable = {str(r.get("mandi", "")).lower(): r for r in results
              if isinstance(r, dict) and r.get("estimated_total_cost") is not None}
    ctx.logger.info(f"[{rid}] transport reply: status={data.get('status')} usable={len(usable)}")
    st["transport_in"] = True
    if usable:
        st["transport"] = usable
    else:
        st["warnings"].append("Transport estimate unavailable (" + str(data.get("message") or data.get("status")) +
                              "); transport costs are rough estimates.")
    save_state(ctx, st)
    await maybe_finish(ctx, rid)

async def on_weather(ctx: Context, st: dict, data: dict) -> None:
    rid = st["rid"]
    if st["stage"] != "fanout" or st["weather_in"]:
        return
    ctx.logger.info(f"[{rid}] weather reply: status={data.get('status')}")
    st["weather_in"] = True
    risk = (data.get("spoilage_risk") or {}) if isinstance(data.get("spoilage_risk"), dict) else {}
    level = str(risk.get("level", "")).upper()
    if data.get("status") == "success" and level in RISK_TO_SPOILAGE:
        cw = data.get("current_weather") if isinstance(data.get("current_weather"), dict) else {}
        max_rain, rain_day = None, None
        for day in (data.get("forecast_next_3_days") or []):
            pr = day.get("rain_probability_percent") if isinstance(day, dict) else None
            if isinstance(pr, (int, float)):
                max_rain = pr if max_rain is None else max(max_rain, pr)
                if pr >= 70 and rain_day is None:
                    rain_day = day.get("date")
        st["weather"] = {"level": level, "fraction": RISK_TO_SPOILAGE[level],
                         "advice": str(data.get("farmer_advice") or ""),
                         "temp": cw.get("temperature_c"), "humidity": cw.get("humidity_percent"),
                         "max_rain": max_rain, "rain_day": rain_day,
                         "actions": [str(x) for x in (risk.get("recommended_actions") or [])][:3]}
    else:
        st["warnings"].append("Weather data unavailable; assumed standard spoilage of "
                              f"{DEFAULT_SPOILAGE * 100:g}%.")
    save_state(ctx, st)
    await maybe_finish(ctx, rid)

async def maybe_finish(ctx: Context, rid: str) -> None:
    st = load_state(ctx, rid)
    if st and not st["done"] and st["transport_in"] and st["weather_in"]:
        await finalize(ctx, rid)

def short_date(d) -> str:
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%d %b")
    except ValueError:
        return str(d)

def compute_ranking(st: dict) -> list:
    qty = st["qty"]
    spoil = st["weather"]["fraction"] if st["weather"] else DEFAULT_SPOILAGE
    rows = []
    for p in st["prices"]:
        gross = p["price_per_quintal"] * qty
        loss = gross * spoil
        after = gross - loss
        t = (st["transport"] or {}).get(p["mandi"].lower())
        if t:
            tcost, dist, est = float(t["estimated_total_cost"]), t.get("distance_km"), False
        else:
            tcost, dist, est = DEFAULT_TRANSPORT_PER_QUINTAL * qty, None, True
        comm = after * COMMISSION_PCT
        rows.append({"mandi": p["mandi"], "price": p["price_per_quintal"], "transport": tcost,
                     "distance_km": dist, "estimated": est, "spoilage": spoil, "spoilage_loss": loss,
                     "commission": comm, "net": after - tcost - comm, "date": p.get("date", ""),
                     "source": p.get("source", "")})
    rows.sort(key=lambda r: r["net"], reverse=True)
    return rows

def timing_decision(st: dict):
    """returns (headline, reasons): sell today / within 1-2 days / no rush, from weather + tomato condition"""
    w, cond = st["weather"], st.get("condition")
    reasons = []
    if cond == "damaged":
        head = "SELL TODAY"
        reasons.append("Some tomatoes are already damaged or overripe, so they will lose value fast. Do not store them.")
    elif not w:
        head = "SELL AS SOON AS YOU CAN"
        reasons.append("Weather data was not available. Tomatoes spoil quickly, so do not wait.")
    elif w["level"] == "HIGH":
        head = "SELL TODAY"
        reasons.append("Spoilage risk is HIGH with this weather. Storing tomatoes now can cost you money.")
    elif w["level"] == "MODERATE" or cond == "ripe":
        head = "SELL WITHIN 1-2 DAYS"
        reasons.append("Spoilage risk is moderate or the tomatoes are already ripe. Sell soon and store carefully until then.")
    else:
        head = "NO RUSH - SELL WITHIN 2-3 DAYS"
        reasons.append("Spoilage risk is LOW. You can wait a day or two if needed, but I cannot predict price changes, so do not wait long.")
    if w and w.get("rain_day"):
        reasons.append(f"Heavy rain is likely on {short_date(w['rain_day'])}. Send the load before it, or use covered transport.")
    elif w and w.get("max_rain") is not None and w["max_rain"] >= 40:
        reasons.append("Some rain is possible in the next 3 days. Check the sky before you travel and cover the load.")
    return head, reasons

def format_report(st: dict, rows: list) -> str:
    best = rows[0]
    w = st["weather"]
    head, reasons = timing_decision(st)
    cond = CONDITION_LABELS.get(st.get("condition"), "not specified")
    lines = ["KISAN SAHAYAK - TOMATO REPORT",
             f"{st['qty']:g} quintal | {st['location'].title()} | condition: {cond}", "",
             "FINAL DECISION",
             f"Sell at: {best['mandi']}",
             f"When:    {head}",
             f"You take home: about {inr(best['net'])} (after transport, spoilage and commission)", ""]
    lines += [f"- {r}" for r in reasons]
    if best["source"] != "AGMARKNET":
        lines.append(f"- The price for {best['mandi']} is a SAMPLE estimate, not a live price. "
                     "Call the mandi to confirm before you travel.")
    if len(rows) > 1 and best["net"] > 0 and (best["net"] - rows[1]["net"]) / best["net"] < 0.03:
        lines.append(f"- Close call: {rows[1]['mandi']} is only {inr(best['net'] - rows[1]['net'])} behind. "
                     "Choose whichever is easier to reach.")

    lines += ["", "COMPARE MANDIS (money in your hand)"]
    for i, r in enumerate(rows[:3], 1):
        dist = f", {r['distance_km']:g} km" if r["distance_km"] is not None else ""
        est = " (estimated)" if r["estimated"] else ""
        star = "  <- best" if i == 1 else ""
        lines.append(f"{i}. {r['mandi']}: {inr(r['net'])}{star}")
        lines.append(f"   price {inr(r['price'])}/quintal, transport {inr(r['transport'])}{est}{dist}")

    lines += ["", "WEATHER AND SPOILAGE"]
    if w:
        now = []
        if w.get("temp") is not None:
            now.append(f"{w['temp']:g}\u00b0C")
        if w.get("humidity") is not None:
            now.append(f"{w['humidity']:g}% humidity")
        lines.append("Now: " + ", ".join(now) if now else "Now: not available")
        if w.get("max_rain") is not None:
            lines.append(f"Rain chance (next 3 days): up to {w['max_rain']:g}%")
        lines.append(f"Spoilage risk: {w['level']}. I assumed about {best['spoilage'] * 100:g}% loss, "
                     f"roughly {inr(best['spoilage_loss'])} on this load.")
    else:
        lines.append(f"Weather data was not available. I assumed {DEFAULT_SPOILAGE * 100:g}% spoilage.")

    tips = list(w["actions"]) if w else []
    for tip in ("Keep tomatoes in shade, in ventilated crates. Do not stack them high.",
                "Separate damaged or soft tomatoes before loading."):
        if tip not in tips:
            tips.append(tip)
    lines += ["", "UNTIL YOU SELL"] + [f"- {t}" for t in tips[:4]]

    lines += ["", f"Assumptions: commission {COMMISSION_PCT * 100:g}% of sale value; spoilage from the weather risk level."]
    if st["warnings"]:
        lines += ["", "Please note:"] + [f"- {x}" for x in st["warnings"]]
    return "\n".join(lines)

async def finalize(ctx: Context, rid: str) -> None:
    st = load_state(ctx, rid)
    if not st or st["done"]:
        return
    st["done"] = True            # saved BEFORE any await so a request is never answered twice
    save_state(ctx, st)
    set_pending(ctx, [i for i in get_pending(ctx) if i != rid])
    if st["error"] or not st["prices"]:
        ctx.logger.warning(f"[{rid}] failed: {st['error']}")
        text = f"Sorry, I could not get mandi prices right now ({st['error']}). Please try again in a few minutes."
    else:
        text = format_report(st, compute_ranking(st))
    text += "\n\nWant to try a different quantity or place? Just tell me, for example \"what if I take 60 quintals?\""
    await send_text(ctx, st["farmer"], text)
    conv = load_conv(ctx, st["farmer"])
    if conv.get("rid") == rid:
        conv["step"] = "idle"
        save_conv(ctx, st["farmer"], conv)
    ctx.logger.info(f"[{rid}] final report sent to farmer")

@chat_protocol.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    """ONE handler for every ChatMessage: replies from our agents vs messages from farmers."""
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = "\n".join(i.text for i in msg.content if isinstance(i, TextContent)).strip()
    if not text:
        return
    try:
        if sender in AGENT_ADDRESSES:
            await handle_agent_reply(ctx, sender, text)
        else:
            await handle_farmer(ctx, sender, text)
    except Exception:
        ctx.logger.exception("unhandled error")
        if sender not in AGENT_ADDRESSES:
            await send_text(ctx, sender, "Sorry, something went wrong on my side. Please try again.")

@chat_protocol.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass

@agent.on_interval(period=5.0)
async def check_timeouts(ctx: Context):
    now = time.time()
    for rid in list(get_pending(ctx)):
        st = load_state(ctx, rid)
        if not st or st["done"]:
            set_pending(ctx, [i for i in get_pending(ctx) if i != rid])
        elif st["stage"] == "price" and now - st["created"] > AGENT_TIMEOUT_S:
            st["error"] = "price agent did not answer in time"
            save_state(ctx, st)
            await finalize(ctx, rid)
        elif st["stage"] == "fanout" and now - st["fanout_at"] > AGENT_TIMEOUT_S:
            if not st["transport_in"]:
                st["warnings"].append("Transport agent did not answer in time; transport costs are estimates.")
            if not st["weather_in"]:
                st["warnings"].append("Weather agent did not answer in time; assumed standard spoilage.")
            save_state(ctx, st)
            await finalize(ctx, rid)

@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info(f"Orchestrator address: {ctx.agent.address}")
    for name, addr in (("price", PRICE_AGENT_ADDRESS), ("transport", TRANSPORT_AGENT_ADDRESS),
                       ("weather", WEATHER_AGENT_ADDRESS)):
        ctx.logger.info(f"{name:9s} agent address: {addr or 'MISSING - add it in the Secrets tab'}")
    ctx.logger.info("ASI1 parsing: " + ("on" if ASI_ONE_API_KEY else "off (rule-based)"))

agent.include(chat_protocol, publish_manifest=True)