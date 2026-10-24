"""
Kisan Sahayak - Orchestrator Agent (replaces agents/orchestrator.py)

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
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import requests
from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

load_dotenv()

HOSTED = os.getenv("AGENTVERSE_HOSTED", "false").lower() == "true"
USE_MAILBOX = os.getenv("USE_MAILBOX", "false").lower() == "true"
SEED = os.getenv("ORCHESTRATOR_SEED", "").strip()
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

if HOSTED:
    agent = Agent()
else:
    if not SEED or SEED.startswith(("PASTE_", "replace_")):
        raise RuntimeError("Set a valid ORCHESTRATOR_SEED in the root .env file.")
    if USE_MAILBOX:
        agent = Agent(name="kisan-sahayak-orchestrator", seed=SEED, port=8000, mailbox=True)
    else:
        agent = Agent(name="kisan-sahayak-orchestrator", seed=SEED, port=8000,
                      endpoint=["http://127.0.0.1:8000/submit"])

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

def load_partial(ctx: Context, user: str) -> dict:
    raw = ctx.storage.get(f"partial:{user}")
    data = json.loads(raw) if raw else {}
    return data if data and time.time() - data.get("ts", 0) < 600 else {}

def save_partial(ctx: Context, user: str, data: dict) -> None:
    ctx.storage.set(f"partial:{user}", json.dumps({**data, "ts": time.time()}))

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

def validate_request(p: dict):
    """returns (clean_request | None, error_text | None, keep_partial)"""
    crop = p.get("crop")
    if crop and crop != "tomato":
        return None, "Kisan Sahayak currently supports tomato only.", False
    missing = []
    if not crop:
        missing.append("crop (tomato)")
    loc = p.get("location")
    if not loc:
        missing.append("location (Bengaluru, Kolar, Ramanagara or Channapatna)")
    q = p.get("quantity_quintals")
    try:
        q = float(q) if q is not None else None
    except (TypeError, ValueError):
        q = None
    if q is None or isinstance(q, bool) or not math.isfinite(q) or not 0 < q <= 100000:
        missing.append("quantity in quintals")
    if missing:
        return None, "I need a little more information. Please tell me your " + ", ".join(missing) + ".", True
    return {"crop": "tomato", "location": loc, "quantity_quintals": q}, None, False

WELCOME = ("Namaste! I am Kisan Sahayak. Tell me your crop, quantity and location, for example: "
           "\"I have 40 quintal tomatoes in Kolar, where should I sell?\"")

async def handle_farmer(ctx: Context, sender: str, text: str) -> None:
    parsed = await asyncio.to_thread(parse_farmer_message, text)
    partial = load_partial(ctx, sender)
    merged = {k: parsed.get(k) or partial.get(k) for k in ("crop", "location", "quantity_quintals")}
    if not any(merged.values()):
        await send_text(ctx, sender, WELCOME)
        return
    clean, err, keep = validate_request(merged)
    if err:
        save_partial(ctx, sender, merged if keep else {})
        await send_text(ctx, sender, err)
        return
    save_partial(ctx, sender, {})
    await start_request(ctx, sender, clean)

async def start_request(ctx: Context, farmer: str, req: dict) -> None:
    rid = uuid4().hex[:8]
    st = {"rid": rid, "farmer": farmer, "created": time.time(), "crop": req["crop"],
          "location": req["location"], "qty": req["quantity_quintals"], "stage": "price", "done": False,
          "prices": None, "price_source": "", "transport": None, "weather": None,
          "transport_in": False, "weather_in": False, "warnings": [], "fanout_at": 0.0, "error": ""}
    save_state(ctx, st)
    set_pending(ctx, get_pending(ctx) + [rid])
    ctx.logger.info(f"[{rid}] new request: {st['qty']:g} quintal tomato at {st['location']}")
    await send_text(ctx, farmer, f"Checking prices, weather and transport for {st['qty']:g} quintal "
                                 f"tomato near {st['location'].title()}. This takes up to {AGENT_TIMEOUT_S:.0f} seconds...")
    if not PRICE_AGENT_ADDRESS:
        st["error"] = "PRICE_AGENT_ADDRESS is not set in .env"
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
            out.append({"mandi": str(name), "price_per_quintal": per_q, "date": rec.get("observation_date", "")})
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
        st["warnings"].append("TRANSPORT_AGENT_ADDRESS not set; transport costs are estimates.")
    if WEATHER_AGENT_ADDRESS:
        await send_text(ctx, WEATHER_AGENT_ADDRESS, json.dumps({
            "request_id": rid, "crop_type": "tomato", "crop_condition": "not specified",
            "harvest_status": "unknown", "location": st["location"].title()}))
        ctx.logger.info(f"[{rid}] weather request sent")
    else:
        st["weather_in"] = True
        st["warnings"].append("WEATHER_AGENT_ADDRESS not set; assumed standard spoilage.")
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
        st["weather"] = {"level": level, "fraction": RISK_TO_SPOILAGE[level],
                         "advice": str(data.get("farmer_advice") or "")}
    else:
        st["warnings"].append("Weather data unavailable; assumed standard spoilage of "
                              f"{DEFAULT_SPOILAGE * 100:g}%.")
    save_state(ctx, st)
    await maybe_finish(ctx, rid)

async def maybe_finish(ctx: Context, rid: str) -> None:
    st = load_state(ctx, rid)
    if st and not st["done"] and st["transport_in"] and st["weather_in"]:
        await finalize(ctx, rid)

def compute_ranking(st: dict) -> list:
    qty = st["qty"]
    spoil = st["weather"]["fraction"] if st["weather"] else DEFAULT_SPOILAGE
    rows = []
    for p in st["prices"]:
        gross = p["price_per_quintal"] * qty
        after = gross * (1 - spoil)
        t = (st["transport"] or {}).get(p["mandi"].lower())
        if t:
            tcost, dist, est = float(t["estimated_total_cost"]), t.get("distance_km"), False
        else:
            tcost, dist, est = DEFAULT_TRANSPORT_PER_QUINTAL * qty, None, True
        comm = after * COMMISSION_PCT
        rows.append({"mandi": p["mandi"], "price": p["price_per_quintal"], "transport": tcost,
                     "distance_km": dist, "estimated": est, "spoilage": spoil, "commission": comm,
                     "net": after - tcost - comm, "date": p.get("date", "")})
    rows.sort(key=lambda r: r["net"], reverse=True)
    return rows

def format_report(st: dict, rows: list) -> str:
    best = rows[0]
    lines = [f"KISAN SAHAYAK - TOMATO, {st['qty']:g} quintal from {st['location'].title()}", "",
             f"Best option: {best['mandi']}. You would take home about {inr(best['net'])}.", "", "Options:"]
    for i, r in enumerate(rows[:3], 1):
        dist = f", {r['distance_km']:g} km" if r["distance_km"] is not None else ""
        est = " (estimated)" if r["estimated"] else ""
        lines.append(f"{i}. {r['mandi']}: price {inr(r['price'])}/quintal, transport {inr(r['transport'])}{est}{dist}, "
                     f"spoilage {r['spoilage'] * 100:g}%, net in hand {inr(r['net'])}")
    if st["weather"]:
        lines += ["", f"Weather spoilage risk: {st['weather']['level']}. {st['weather']['advice']}".strip()]
    lines += ["", f"Assumptions: commission {COMMISSION_PCT * 100:g}% of sale value; spoilage from weather risk level."]
    if st["warnings"]:
        lines += ["", "Please note:"] + [f"- {w}" for w in st["warnings"]]
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
    await send_text(ctx, st["farmer"], text)
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
        ctx.logger.info(f"{name:9s} agent address: {'set' if addr else 'MISSING - set it in .env'}")
    ctx.logger.info("ASI1 parsing: " + ("on" if ASI_ONE_API_KEY and not ASI_ONE_API_KEY.startswith(('PASTE_', 'replace_')) else "off (rule-based)"))

agent.include(chat_protocol, publish_manifest=True)

if __name__ == "__main__":
    agent.run()