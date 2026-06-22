"""
SmartestGuide – vše v jednom souboru
Spusť: python -m uvicorn app:app --reload
Nebo použij SPUSTIT.bat
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import os, json, uuid, httpx, asyncio, re, base64, hmac, hashlib
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# Auto-načtení konfigurace z environment variables
# ─────────────────────────────────────────────
def init_settings_from_env():
    """Při startu načte klíče z env proměnných do DB pokud tam ještě nejsou."""
    updates = {}
    if os.getenv("ANTHROPIC_API_KEY") and os.getenv("ANTHROPIC_API_KEY").startswith("sk-ant-"):
        updates["anthropic_api_key"] = os.getenv("ANTHROPIC_API_KEY")
    if os.getenv("STRIPE_SECRET_KEY"):
        updates["stripe_secret_key"] = os.getenv("STRIPE_SECRET_KEY")
    if os.getenv("STRIPE_PAYMENT_LINK"):
        updates["stripe_payment_link"] = os.getenv("STRIPE_PAYMENT_LINK")
    if os.getenv("STRIPE_WEBHOOK_SECRET"):
        updates["stripe_webhook_secret"] = os.getenv("STRIPE_WEBHOOK_SECRET")
    if updates:
        db_save_settings(updates)

# ─────────────────────────────────────────────
# Aplikace
# ─────────────────────────────────────────────
app = FastAPI(title="SmartestGuide", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────
# Lokální JSON databáze
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DATA_PATH", os.path.join(os.path.dirname(__file__), "data.json"))

def db_load() -> dict:
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"hotels": {}, "settings": {}}

def db_save(data: dict):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def db_get_settings() -> dict:
    return db_load().get("settings", {})

def db_save_settings(s: dict):
    data = db_load()
    data["settings"] = {**data.get("settings", {}), **s}
    db_save(data)

# Načti nastavení z env proměnných při startu
init_settings_from_env()

# ─────────────────────────────────────────────
# Pydantic modely
# ─────────────────────────────────────────────
class ApiKeyRequest(BaseModel):
    api_key: str

class ScrapeRequest(BaseModel):
    url: str
    hotel_name: Optional[str] = None

class HotelData(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    source_url: Optional[str] = None
    description: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    bed_count: Optional[int] = None
    room_count: Optional[int] = None
    checkin_time: Optional[str] = None
    checkout_time: Optional[str] = None
    breakfast_hours: Optional[str] = None
    lunch_hours: Optional[str] = None
    dinner_hours: Optional[str] = None
    amenities: Optional[List[str]] = None
    nearby_places: Optional[List[str]] = None
    languages: Optional[List[str]] = None
    restaurant_name: Optional[str] = None
    wellness_info: Optional[str] = None
    parking_info: Optional[str] = None
    whatsapp_number: Optional[str] = None
    star_rating: Optional[int] = None
    country: Optional[str] = None
    continent: Optional[str] = None
    extra_info: Optional[str] = None
    subscription_active: Optional[bool] = None
    stripe_customer_id: Optional[str] = None
    scraped_pages: Optional[List[str]] = None

# ─────────────────────────────────────────────
# Nastavení API klíče
# ─────────────────────────────────────────────
@app.get("/api/settings")
def get_settings():
    s = db_get_settings()
    return {
        "has_api_key": bool(s.get("anthropic_api_key")),
        "api_key_preview": ("sk-ant-..." + s["anthropic_api_key"][-6:]) if s.get("anthropic_api_key") else None,
        "has_stripe_key": bool(s.get("stripe_secret_key")),
        "stripe_payment_link": s.get("stripe_payment_link", ""),
        "stripe_key_preview": ("sk_test_..." + s["stripe_secret_key"][-6:]) if s.get("stripe_secret_key") else None,
    }

class StripeSettingsRequest(BaseModel):
    stripe_secret_key: str
    stripe_payment_link: str
    stripe_webhook_secret: Optional[str] = None

@app.post("/api/settings/stripe")
def save_stripe_settings(req: StripeSettingsRequest):
    key = req.stripe_secret_key.strip()
    if not (key.startswith("sk_test_") or key.startswith("sk_live_")):
        raise HTTPException(400, "Neplatny Stripe klic")
    db_save_settings({
        "stripe_secret_key": key,
        "stripe_payment_link": req.stripe_payment_link.strip(),
        "stripe_webhook_secret": req.stripe_webhook_secret or "",
    })
    return {"status": "ok"}

@app.post("/api/settings/api-key")
def save_api_key(req: ApiKeyRequest):
    key = req.api_key.strip()
    if not key.startswith("sk-ant-"):
        raise HTTPException(400, "Neplatný API klíč – musí začínat 'sk-ant-'")
    db_save_settings({"anthropic_api_key": key})
    return {"status": "ok", "preview": "sk-ant-..." + key[-6:]}

@app.delete("/api/settings/api-key")
def delete_api_key():
    db_save_settings({"anthropic_api_key": ""})
    return {"status": "ok"}

# ─────────────────────────────────────────────
# Scraping
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "cs,en;q=0.9",
}
SUBPAGE_HINTS = [
    "/contact", "/kontakt", "/about", "/o-nas", "/restaurant", "/restaurace",
    "/wellness", "/spa", "/services", "/sluzby", "/rooms", "/pokoje",
    "/facilities", "/events", "/gallery",
]

def extract_text(html: str, max_chars: int = 4000) -> str:
    """Extrahuje čistý text z HTML, agresivně ořezává nepotřebný obsah."""
    soup = BeautifulSoup(html, "html.parser")
    # Odstraň vše nepotřebné
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav",
                     "meta", "link", "iframe", "img", "svg", "button", "form"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text(separator="\n").splitlines() if l.strip()]
    # Odfiltruj příliš krátké řádky (menu položky, čísla stránek atd.)
    lines = [l for l in lines if len(l) > 3]
    # Odstraň duplicitní řádky
    seen = set()
    unique = []
    for l in lines:
        if l not in seen:
            seen.add(l)
            unique.append(l)
    return "\n".join(unique)[:max_chars]

async def fetch_page(client, url: str) -> Optional[str]:
    try:
        r = await client.get(url, timeout=12.0, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "text/html" in ct:
            return r.text
    except Exception:
        pass
    return None

async def scrape_hotel_data(url: str, api_key: str) -> dict:
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
        main_html = await fetch_page(client, url)
        if not main_html:
            raise ValueError(f"Nepodařilo se stáhnout {url} – web možná blokuje boty nebo je nedostupný")

        # Hlavní stránka – max 4000 znaků
        main_text = extract_text(main_html, 4000)
        pages_text = [f"=== HLAVNI STRANKA ===\n{main_text}"]

        # Podstránky – zkusíme jen 5, každá max 1500 znaků
        async def try_sub(hint):
            sub_url = urljoin(base, hint)
            html = await fetch_page(client, sub_url)
            if html:
                t = extract_text(html, 1500)
                if len(t) > 100:  # ignoruj prázdné stránky
                    return f"=== {hint.upper()} ===\n{t}"
            return None

        results = await asyncio.gather(*[try_sub(h) for h in SUBPAGE_HINTS[:5]])
        pages_text += [r for r in results if r]

    # Celkový limit 8000 znaků – bezpečně pod limitem Claude API
    combined = "\n\n".join(pages_text)[:8000]

    # Systémový prompt zvlášť, user message jen s textem
    system_prompt = """Jsi expert na extrakci dat z hotelových webů.
Vždy odpovídáš POUZE validním JSON objektem, bez jakéhokoliv textu před nebo za ním.
Pokud informaci nenajdeš, použij null. Nikdy nevymýšlej data."""

    user_prompt = f"""Extrahuj informace o hotelu z tohoto textu webu a vrať JSON:

{{
  "name": "název hotelu",
  "url": "{url}",
  "description": "popis 2-3 věty v češtině",
  "address": "adresa nebo null",
  "phone": "telefon nebo null",
  "email": "email nebo null",
  "bed_count": číslo_nebo_null,
  "room_count": číslo_nebo_null,
  "checkin_time": "14:00 nebo null",
  "checkout_time": "11:00 nebo null",
  "breakfast_hours": "hodiny nebo null",
  "lunch_hours": "hodiny nebo null",
  "dinner_hours": "hodiny nebo null",
  "amenities": ["seznam vybaveni"],
  "nearby_places": ["mista v okoli"],
  "languages": ["jazyky webu"],
  "restaurant_name": "nazev nebo null",
  "wellness_info": "info nebo null",
  "parking_info": "info nebo null",
  "star_rating": číslo_nebo_null,
  "country": "ISO kod zeme napr CZ SK AT DE FR IT HR nebo null",
  "continent": "Europe/Asia/America/Africa/Oceania nebo null",
  "extra_info": "ostatni info nebo null"
}}

TEXT WEBU ({url}):
{combined}"""

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1500,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            },
            timeout=90.0,
        )

        # Detailní error handling
        if r.status_code == 401:
            raise ValueError("Neplatný API klíč – zkontroluj Nastavení")
        if r.status_code == 429:
            raise ValueError("Překročen limit API – počkej chvíli a zkus znovu")
        if r.status_code == 400:
            err_detail = ""
            try:
                err_detail = r.json().get("error", {}).get("message", "")
            except Exception:
                pass
            raise ValueError(f"Chyba požadavku na Claude API: {err_detail or r.text[:200]}")
        if r.status_code != 200:
            raise ValueError(f"Claude API vrátilo chybu {r.status_code}: {r.text[:200]}")

        data = r.json()

    raw = data["content"][0]["text"].strip()
    # Odstraň případné markdown backticky
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError(f"Claude nevrátil JSON. Odpověď: {raw[:200]}")

    result = json.loads(match.group())
    result["source_url"] = url
    result["scraped_pages"] = [url]
    return result

@app.post("/api/scrape")
async def scrape_endpoint(req: ScrapeRequest):
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(400, "Anthropic API klíč není nastaven. Jdi do Nastavení.")
    try:
        result = await scrape_hotel_data(req.url, api_key)
        return {"status": "ok", "data": result}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))

# ─────────────────────────────────────────────
# Hotely – CRUD
# ─────────────────────────────────────────────
@app.post("/api/hotels")
def create_hotel(data: HotelData):
    db = db_load()
    hid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    hotel_token = str(uuid.uuid4()).replace("-", "")
    hotel = {
        "id": hid,
        "created_at": now,
        "updated_at": now,
        "qr_code_id": str(uuid.uuid4()),
        "hotel_token": hotel_token,
        "subscription_active": False,
        **data.model_dump(exclude_none=True),
    }
    db["hotels"][hid] = hotel
    db_save(db)
    return {"status": "ok", "hotel": hotel}


# ─────────────────────────────────────────────
# Hotelový portál – token autentizace
# ─────────────────────────────────────────────
def find_hotel_by_token(token: str):
    db = db_load()
    for h in db["hotels"].values():
        if h.get("hotel_token") == token:
            return h
    return None

@app.get("/api/hotels/{hotel_id}/completeness")
def hotel_completeness(hotel_id: str):
    """Vrátí skóre vyplněnosti profilu hotelu."""
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")

    # Povinná pole (80% váha)
    required = [
        ("name", "Název hotelu"),
        ("address", "Adresa"),
        ("phone", "Telefon"),
        ("email", "E-mail"),
        ("bed_count", "Počet lůžek"),
        ("checkin_time", "Check-in čas"),
        ("checkout_time", "Check-out čas"),
        ("description", "Popis hotelu"),
    ]
    # Bonusová pole (20% váha)
    bonus = [
        ("breakfast_hours", "Hodiny snídaně"),
        ("dinner_hours", "Hodiny večeře"),
        ("restaurant_name", "Název restaurace"),
        ("parking_info", "Parkování"),
        ("wellness_info", "Wellness/Spa"),
        ("whatsapp_number", "WhatsApp"),
        ("nearby_places", "Místa v okolí"),
        ("amenities", "Vybavení"),
    ]

    filled_req = sum(1 for k, _ in required if h.get(k))
    filled_bon = sum(1 for k, _ in bonus if h.get(k))

    req_score = round((filled_req / len(required)) * 80)
    bon_score = round((filled_bon / len(bonus)) * 20)
    total = req_score + bon_score

    missing_req = [label for k, label in required if not h.get(k)]
    missing_bon = [label for k, label in bonus if not h.get(k)]

    return {
        "score": total,
        "required_score": req_score,
        "bonus_score": bon_score,
        "filled_required": filled_req,
        "total_required": len(required),
        "filled_bonus": filled_bon,
        "total_bonus": len(bonus),
        "missing": missing_req + missing_bon,
        "missing_required": missing_req,
        "missing_bonus": missing_bon,
    }

@app.get("/api/hotel-portal/completeness")
def portal_completeness(token: str):
    """Vrátí skóre vyplněnosti profilu pro hotel portál."""
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    return hotel_completeness(h["id"])

@app.get("/api/hotel-portal/me")
def hotel_portal_me(token: str):
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny pristupovy token")
    safe = {k: v for k, v in h.items() if k not in ("hotel_token",)}
    return {"status": "ok", "hotel": safe}

class HotelPortalUpdate(BaseModel):
    description: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    whatsapp_number: Optional[str] = None
    checkin_time: Optional[str] = None
    checkout_time: Optional[str] = None
    breakfast_hours: Optional[str] = None
    lunch_hours: Optional[str] = None
    dinner_hours: Optional[str] = None
    restaurant_name: Optional[str] = None
    wellness_info: Optional[str] = None
    parking_info: Optional[str] = None
    amenities: Optional[List[str]] = None
    nearby_places: Optional[List[str]] = None
    extra_info: Optional[str] = None
    bed_count: Optional[int] = None
    room_count: Optional[int] = None
    star_rating: Optional[int] = None
    address: Optional[str] = None
    country: Optional[str] = None

@app.patch("/api/hotel-portal/update")
def hotel_portal_update(token: str, data: HotelPortalUpdate):
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny pristupovy token")
    db = db_load()
    update_data = data.model_dump(exclude_none=True)
    current = db["hotels"][h["id"]]
    # Při prvním nastavení lůžek u aktivního předplatného ulož jako zaplacená lůžka
    new_beds = update_data.get("bed_count")
    paid_beds = current.get("subscription_paid_beds", 0)
    if current.get("subscription_active") and new_beds and not paid_beds:
        update_data["subscription_paid_beds"] = new_beds
    db["hotels"][h["id"]].update({
        **update_data,
        "updated_at": datetime.utcnow().isoformat(),
        "portal_last_edit": datetime.utcnow().isoformat(),
    })
    db_save(db)
    safe = {k: v for k, v in db["hotels"][h["id"]].items() if k != "hotel_token"}
    return {"status": "ok", "hotel": safe}

@app.post("/api/hotel-portal/cancel")
def hotel_portal_cancel(token: str):
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    db = db_load()
    db["hotels"][h["id"]]["subscription_active"] = False
    db["hotels"][h["id"]]["subscription_cancelled_at"] = datetime.utcnow().isoformat()
    db["hotels"][h["id"]]["updated_at"] = datetime.utcnow().isoformat()
    db_save(db)
    safe = {k: v for k, v in db["hotels"][h["id"]].items() if k != "hotel_token"}
    return {"status": "ok", "hotel": safe}

@app.get("/api/hotel-portal/invoices")
def hotel_portal_invoices(token: str):
    """Vrátí faktury pro hotel portál."""
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    db = db_load()
    invoices = [inv for inv in db.get("invoices", {}).values()
                if inv.get("hotel_id") == h["id"]]
    invoices.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"status": "ok", "invoices": invoices}

@app.get("/api/hotel-portal/analytics")
def hotel_portal_analytics(token: str):
    """Vrátí analytics pro hotel portál."""
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    db = db_load()
    analytics = db.get("analytics", {}).get(h["id"], {
        "total": 0,
        "topics": {},
        "last_chat": None
    })
    return {"status": "ok", "analytics": analytics}

@app.post("/api/hotels/{hotel_id}/generate-token")
def generate_token(hotel_id: str, request: Request):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    if not db["hotels"][hotel_id].get("hotel_token"):
        db["hotels"][hotel_id]["hotel_token"] = str(uuid.uuid4()).replace("-", "")
        db_save(db)
    token = db["hotels"][hotel_id]["hotel_token"]
    base = get_base_url(request)
    return {"status": "ok", "token": token, "portal_url": f"{base}/hotel?token={token}"}

@app.get("/api/hotels")
def list_hotels():
    db = db_load()
    hotels = sorted(db["hotels"].values(), key=lambda h: h.get("created_at", ""), reverse=True)
    return {"status": "ok", "hotels": hotels}

@app.get("/api/hotels/{hotel_id}")
def get_hotel(hotel_id: str):
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    return {"status": "ok", "hotel": h}

@app.patch("/api/hotels/{hotel_id}")
def update_hotel(hotel_id: str, data: HotelData):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    db["hotels"][hotel_id].update({
        **data.model_dump(exclude_none=True),
        "updated_at": datetime.utcnow().isoformat()
    })
    db_save(db)
    return {"status": "ok", "hotel": db["hotels"][hotel_id]}

@app.delete("/api/hotels/{hotel_id}")
def delete_hotel(hotel_id: str):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    del db["hotels"][hotel_id]
    db_save(db)
    return {"status": "ok"}

# ─────────────────────────────────────────────
# Helper – detekce base URL (lokál i Railway)
# ─────────────────────────────────────────────
def get_base_url(request: Request) -> str:
    base_url_env = os.getenv("BASE_URL", "").strip().rstrip("/")
    if base_url_env:
        return base_url_env
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", "localhost:8000"))
    return f"{scheme}://{host}"

# ─────────────────────────────────────────────
# QR kód
# ─────────────────────────────────────────────
@app.get("/api/hotels/{hotel_id}/qr")
def generate_qr(hotel_id: str, request: Request):
    try:
        import qrcode
        from io import BytesIO
    except ImportError:
        raise HTTPException(500, "Nainstaluj qrcode: pip install qrcode[pil]")

    db = db_load()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")

    base = get_base_url(request)
    guest_url = f"{base}/guest/{hotel_id}"
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=10, border=4)
    qr.add_data(guest_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1a2e", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return {"status": "ok", "qr_base64": base64.b64encode(buf.getvalue()).decode(), "guest_url": guest_url}

# ─────────────────────────────────────────────
# Ceník
# ─────────────────────────────────────────────
@app.get("/api/pricing")
def pricing(beds: int):
    if beds <= 0:
        raise HTTPException(400, "Počet lůžek musí být kladný")
    price = 300 if beds <= 100 else 300 + (beds - 100) * 3
    return {"beds": beds, "monthly_eur": price, "quarterly_eur": price * 3,
            "note": "Zaváděcí cena – při objednání v prvních 3 měsících zůstane zachována"}

# ─────────────────────────────────────────────
# Registrace z landing page + Stripe Checkout
# ─────────────────────────────────────────────
class RegistrationRequest(BaseModel):
    hotel_name: str
    hotel_url: Optional[str] = None
    contact_name: str
    contact_email: str
    contact_phone: Optional[str] = None
    bed_count: Optional[int] = None

@app.post("/api/register")
async def register_hotel(req: RegistrationRequest, request: Request):
    """
    Registrace hotelu z landing page:
    1. Zkontroluje duplicitní platbu podle emailu
    2. Vytvoří hotel v DB (neaktivní)
    3. Vytvoří Stripe Checkout Session s client_reference_id = hotel_id
    4. Vrátí URL pro přesměrování na platbu
    """
    s = db_get_settings()
    stripe_key = s.get("stripe_secret_key", "")
    if not stripe_key:
        raise HTTPException(400, "Stripe není nastaven")

    # Kontrola duplicitní platby – stejný email s aktivním předplatným
    db = db_load()
    existing = [h for h in db["hotels"].values()
                if h.get("email", "").lower() == req.contact_email.lower()
                and h.get("subscription_active")]
    if existing:
        h = existing[0]
        raise HTTPException(409, f"Tento e-mail již má aktivní předplatné pro hotel '{h.get('name', '')}'. Přihlaste se do portálu nebo kontaktujte podporu na info@smartestguide.com")

    # 1. Vytvoř hotel v DB
    db = db_load()
    hid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    hotel_token = str(uuid.uuid4()).replace("-", "")
    beds = req.bed_count or 0
    price = 300 if beds <= 100 else 300 + (beds - 100) * 3

    hotel = {
        "id": hid,
        "created_at": now,
        "updated_at": now,
        "qr_code_id": str(uuid.uuid4()),
        "hotel_token": hotel_token,
        "subscription_active": False,
        "origin": "automatic",
        "origin_source": "landing_page",
        "name": req.hotel_name,
        "url": req.hotel_url or "",
        "source_url": req.hotel_url or "",
        "email": req.contact_email,
        "phone": req.contact_phone or "",
        "bed_count": beds,
        "contact_name": req.contact_name,
    }
    db["hotels"][hid] = hotel
    db_save(db)

    # 2. Vytvoř Stripe Checkout Session přes API
    base = get_base_url(request)
    try:
        # Idempotency key – stejný email+hotel nemůže vytvořit dvě session
        import hashlib
        idempotency_key = hashlib.md5(f"{req.contact_email}:{req.hotel_name}:{req.hotel_url}".encode()).hexdigest()

        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                headers={
                    "Authorization": f"Bearer {stripe_key}",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Idempotency-Key": idempotency_key,
                },
                data={
                    "mode": "subscription",
                    "client_reference_id": hid,
                    "customer_email": req.contact_email,
                    "success_url": f"{base}/success?hotel_id={hid}",
                    "cancel_url": f"{base}/landing",
                    "line_items[0][price_data][currency]": "eur",
                    "line_items[0][price_data][product_data][name]": f"SmartestGuide – {req.hotel_name}",
                    "line_items[0][price_data][product_data][description]": f"AI concierge pro {beds} lůžek",
                    "line_items[0][price_data][recurring][interval]": "month",
                    "line_items[0][price_data][unit_amount]": str(price * 100),
                    "line_items[0][quantity]": "1",
                    "metadata[hotel_id]": hid,
                    "metadata[hotel_name]": req.hotel_name,
                },
                timeout=30.0,
            )
        if r.status_code != 200:
            raise ValueError(f"Stripe error: {r.text[:200]}")
        session = r.json()
        checkout_url = session.get("url")
        if not checkout_url:
            raise ValueError("Stripe nevrátil checkout URL")

        return {"status": "ok", "checkout_url": checkout_url, "hotel_id": hid}

    except Exception as e:
        # Pokud Stripe selže, smaž hotel z DB
        db = db_load()
        db["hotels"].pop(hid, None)
        db_save(db)
        raise HTTPException(500, f"Chyba při vytváření platby: {str(e)}")

# ─────────────────────────────────────────────
# Success page po platbě
# ─────────────────────────────────────────────
@app.get("/success", response_class=HTMLResponse)
def success_page(hotel_id: str = "", request: Request = None):
    # Načti portal token pro přesměrování
    portal_url = ""
    if hotel_id:
        db = db_load()
        h = db["hotels"].get(hotel_id)
        if h and h.get("hotel_token"):
            base = get_base_url(request) if request else ""
            portal_url = f"{base}/hotel?token={h['hotel_token']}"

    redirect_script = f"""
    <script>
      // Automaticky přesměruj na landing page po 5 sekundách
      var countdown = 5; var el = document.getElementById("countdown"); var interval = setInterval(function(){{ countdown--; el.textContent = countdown; if(countdown <= 0){{ clearInterval(interval); window.location.href = "/landing"; }} }}, 1000);
    </script>"""

    portal_btn = ''

    countdown_html = '<p style="font-size:13px;color:#7a7fa8;margin-top:8px">Automatické přesměrování za <span id="countdown">5</span> sekund…</p>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<title>Platba úspěšná – SmartestGuide</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet"/>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:Inter,sans-serif;background:#0d0f1a;color:#e8eaf6;display:flex;align-items:center;justify-content:center;min-height:100vh}}
  .box{{background:#1e2135;border:1px solid #2a2f4a;border-radius:20px;padding:52px 44px;text-align:center;max-width:500px;width:90%}}
  h1{{font-size:26px;margin-bottom:14px;color:#2ecc87;font-weight:700}}
  .steps{{text-align:left;background:#161828;border-radius:12px;padding:20px 24px;margin:24px 0;display:flex;flex-direction:column;gap:12px}}
  .step{{display:flex;align-items:center;gap:12px;font-size:14px;color:#9ba0c0}}
  .step .icon{{font-size:20px;width:28px;text-align:center;flex-shrink:0}}
  .step.done{{color:#2ecc87}}
  .step.active{{color:#e8eaf6;font-weight:600}}
  .step.pending{{color:#6b6f8e}}
</style>
</head>
<body>
<div class="box">
  <div style="font-size:60px;margin-bottom:20px">🎉</div>
  <h1>Platba proběhla úspěšně!</h1>
  <p style="color:#7a7fa8;font-size:15px;line-height:1.7;margin-bottom:20px">Váš hotel byl zaregistrován. Během několika minut vám zašleme email s přístupovým odkazem do hotelového portálu.</p>

  <div class="steps">
    <div class="step done"><span class="icon">✅</span> Registrace dokončena</div>
    <div class="step done"><span class="icon">✅</span> Platba přijata</div>
    <div class="step active"><span class="icon">⚡</span> Importujeme data z vašeho webu…</div>
    <div class="step pending"><span class="icon">📝</span> Doplňte údaje v portálu</div>
    <div class="step pending"><span class="icon">🖨️</span> Vytiskněte QR plakát</div>
    <div class="step pending"><span class="icon">🏨</span> Hosté začínají chatovat s Alexem</div>
  </div>

  {portal_btn}
  {countdown_html}
</div>
{redirect_script}
</body></html>"""

async def auto_scrape_after_payment(hotel_id: str, hotel_url: str):
    """Po úspěšné platbě automaticky naskenuje web hotelu a doplní data do DB."""
    try:
        s = db_get_settings()
        api_key = s.get("anthropic_api_key", "")
        if not api_key:
            return
        # Počkej 3 sekundy aby se DB stihla uložit
        await asyncio.sleep(3)
        scraped = await scrape_hotel_data(hotel_url, api_key)
        db = db_load()
        if hotel_id not in db["hotels"]:
            return
        # Doplň pouze pole která jsou prázdná (nepřepisuj existující data)
        for key, value in scraped.items():
            if key not in ("id", "created_at", "hotel_token", "subscription_active",
                          "stripe_customer_id", "stripe_subscription_id", "subscription_start"):
                if not db["hotels"][hotel_id].get(key) and value:
                    db["hotels"][hotel_id][key] = value
        db["hotels"][hotel_id]["scraping_done"] = True
        db["hotels"][hotel_id]["updated_at"] = datetime.utcnow().isoformat()
        db_save(db)
    except Exception:
        pass  # Scraping selhal – nevadí, hotel existuje bez dat

# ─────────────────────────────────────────────
# Stripe – platby a webhook
# ─────────────────────────────────────────────
@app.get("/api/stripe/checkout/{hotel_id}")
def stripe_checkout(hotel_id: str):
    """Vrátí Stripe payment link s prefilled hotel_id v metadata přes client_reference_id."""
    db = db_load()
    s = db_get_settings()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")
    payment_link = s.get("stripe_payment_link", "")
    if not payment_link:
        raise HTTPException(400, "Stripe payment link neni nastaven")
    # Přidej client_reference_id = hotel_id pro identifikaci po platbě
    sep = "&" if "?" in payment_link else "?"
    url = f"{payment_link}{sep}client_reference_id={hotel_id}"
    return {"status": "ok", "checkout_url": url, "hotel_name": hotel.get("name", "")}

@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Zpracuje Stripe webhook – aktivuje předplatné po úspěšné platbě."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    s = db_get_settings()
    webhook_secret = s.get("stripe_webhook_secret", "")

    # Ověř podpis pokud máme webhook secret
    if webhook_secret:
        try:
            # Stripe signature verification
            parts = {k: v for p in sig.split(",") for k, v in [p.split("=", 1)]}
            ts = parts.get("t", "")
            v1 = parts.get("v1", "")
            signed = f"{ts}.{payload.decode()}"
            mac = hmac.new(webhook_secret.encode(), signed.encode(), hashlib.sha256); expected = mac.hexdigest()
            if not hmac.compare_digest(expected, v1):
                raise HTTPException(400, "Neplatny podpis")
        except Exception:
            raise HTTPException(400, "Chyba overeni podpisu")

    try:
        event = json.loads(payload)
    except Exception:
        raise HTTPException(400, "Neplatny JSON")

    event_type = event.get("type", "")

    # checkout.session.completed – jednorázová platba nebo první platba subscription
    if event_type in ("checkout.session.completed", "invoice.payment_succeeded"):
        obj = event.get("data", {}).get("object", {})
        hotel_id = obj.get("client_reference_id") or obj.get("metadata", {}).get("hotel_id")
        customer_id = obj.get("customer", "")
        subscription_id = obj.get("subscription", "")

        if hotel_id:
            db = db_load()
            if hotel_id in db["hotels"]:
                db["hotels"][hotel_id]["subscription_active"] = True
                db["hotels"][hotel_id]["stripe_customer_id"] = customer_id
                db["hotels"][hotel_id]["stripe_subscription_id"] = subscription_id
                db["hotels"][hotel_id]["subscription_start"] = datetime.utcnow().isoformat()
                db["hotels"][hotel_id]["updated_at"] = datetime.utcnow().isoformat()
                db_save(db)

                # Spusť automatický scraping webu hotelu na pozadí
                hotel_url = db["hotels"][hotel_id].get("url") or db["hotels"][hotel_id].get("source_url")
                if hotel_url and event_type == "checkout.session.completed":
                    asyncio.create_task(auto_scrape_after_payment(hotel_id, hotel_url))

    # customer.subscription.deleted – zrušení předplatného
    elif event_type == "customer.subscription.deleted":
        obj = event.get("data", {}).get("object", {})
        customer_id = obj.get("customer", "")
        db = db_load()
        for hid, h in db["hotels"].items():
            if h.get("stripe_customer_id") == customer_id:
                db["hotels"][hid]["subscription_active"] = False
                db["hotels"][hid]["subscription_end"] = datetime.utcnow().isoformat()
                db["hotels"][hid]["updated_at"] = datetime.utcnow().isoformat()
                db_save(db)
                break

    return {"status": "ok"}

# Manuální aktivace/deaktivace z adminu
@app.post("/api/hotels/{hotel_id}/subscription")
def toggle_subscription(hotel_id: str, active: bool):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    db["hotels"][hotel_id]["subscription_active"] = active
    db["hotels"][hotel_id]["updated_at"] = datetime.utcnow().isoformat()
    # Při aktivaci nastav zaplacená lůžka = aktuální počet
    if active:
        beds = db["hotels"][hotel_id].get("bed_count", 0)
        db["hotels"][hotel_id]["subscription_paid_beds"] = beds
    db_save(db)
    return {"status": "ok", "subscription_active": active}

def hotel_is_english_only(hotel: dict) -> bool:
    """Vrátí True pokud hotel používá pouze angličtinu (bez překladu)."""
    langs = hotel.get("languages") or []
    if not langs:
        return False
    # Pokud jsou jazyky ['en'] nebo pouze angličtina
    langs_lower = [l.lower().strip() for l in langs]
    en_variants = {'en', 'english', 'anglicky', 'anglictina'}
    if len(langs_lower) == 1 and langs_lower[0] in en_variants:
        return True
    return False

def get_hotel_lang(hotel: dict) -> str:
    """Vrátí primární jazyk hotelu (výchozí cs)."""
    langs = hotel.get("languages") or []
    if not langs:
        return "cs"
    first = langs[0].lower().strip()
    lang_map = {
        "czech": "cs", "cestina": "cs", "cs": "cs",
        "english": "en", "en": "en",
        "german": "de", "de": "de", "nemcina": "de",
        "slovak": "sk", "sk": "sk",
        "french": "fr", "fr": "fr",
        "italian": "it", "it": "it",
        "spanish": "es", "es": "es",
        "polish": "pl", "pl": "pl",
    }
    return lang_map.get(first, "cs")


# ─────────────────────────────────────────────
# Embed widget
# ─────────────────────────────────────────────
@app.get("/widget.js")
def serve_widget(hotel_id: str, lang: str = "auto"):
    """JavaScript widget pro vložení na web hotelu."""
    base = get_base_url()
    guest_url = f"{base}/guest/{hotel_id}"
    js = f"""(function(){{
  var btn = document.createElement('div');
  btn.id = 'sg-widget-btn';
  btn.innerHTML = '<span style="font-size:22px">💬</span>';
  btn.style.cssText = 'position:fixed;bottom:24px;right:24px;width:56px;height:56px;border-radius:50%;background:linear-gradient(135deg,#6c63ff,#00d4aa);display:flex;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 4px 20px rgba(108,99,255,.5);z-index:9999;transition:transform .2s';
  btn.onmouseenter = function(){{ this.style.transform='scale(1.1)'; }};
  btn.onmouseleave = function(){{ this.style.transform='scale(1)'; }};
  btn.onclick = function(){{
    var w = window.open('{guest_url}','sg_concierge','width=420,height=700,right=24,bottom=24');
    if(!w) window.location.href = '{guest_url}';
  }};
  var tooltip = document.createElement('div');
  tooltip.innerHTML = 'Chat with our AI concierge';
  tooltip.style.cssText = 'position:fixed;bottom:88px;right:24px;background:#1a1d2e;color:#e8eaf6;font-size:13px;padding:8px 14px;border-radius:8px;white-space:nowrap;z-index:9999;opacity:0;transition:opacity .2s;pointer-events:none;font-family:sans-serif';
  btn.onmouseenter = function(){{ this.style.transform='scale(1.1)'; tooltip.style.opacity='1'; }};
  btn.onmouseleave = function(){{ this.style.transform='scale(1)'; tooltip.style.opacity='0'; }};
  document.body.appendChild(btn);
  document.body.appendChild(tooltip);
}})();"""
    return HTMLResponse(content=js, media_type="application/javascript")

# ─────────────────────────────────────────────
# Analytics
# ─────────────────────────────────────────────
@app.get("/api/hotels/{hotel_id}/analytics")
def get_analytics(hotel_id: str):
    db = db_load()
    if hotel_id not in db.get("hotels", {}):
        raise HTTPException(404, "Hotel nenalezen")
    analytics = db.get("analytics", {}).get(hotel_id, {"total": 0, "topics": {}})
    return {"status": "ok", "analytics": analytics}

@app.get("/api/hotel-portal/analytics")
def portal_analytics(token: str):
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    db = db_load()
    analytics = db.get("analytics", {}).get(h["id"], {"total": 0, "topics": {}})
    return {"status": "ok", "analytics": analytics}

# ─────────────────────────────────────────────
# Email reminder
# ─────────────────────────────────────────────
def hotel_profile_completeness(hotel: dict) -> dict:
    required = [
        "name", "address", "phone", "email",
        "checkin_time", "checkout_time", "breakfast_hours",
        "bed_count", "star_rating", "description",
    ]
    bonus = [
        "wellness_info", "parking_info", "restaurant_name",
        "nearby_places", "fitness_info", "pool_info",
        "whatsapp_number", "dinner_hours",
    ]
    filled_req = [f for f in required if hotel.get(f)]
    filled_bon = [f for f in bonus if hotel.get(f)]
    missing = [f for f in required if not hotel.get(f)]
    # Skóre: required = 80%, bonus = 20%
    req_score = int((len(filled_req) / len(required)) * 80)
    bon_score = min(20, int((len(filled_bon) / len(bonus)) * 20))
    score = req_score + bon_score
    return {
        "score": score,
        "filled_required": len(filled_req),
        "total_required": len(required),
        "missing": missing,
        "is_complete": score >= 80,
    }

@app.get("/api/hotels/{hotel_id}/completeness")
def get_completeness(hotel_id: str):
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    return {"status": "ok", **hotel_profile_completeness(h)}

@app.post("/api/hotels/{hotel_id}/send-reminder")
def send_reminder(hotel_id: str):
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    completeness = hotel_profile_completeness(h)
    portal_url = get_base_url() + "/hotel?token=" + h.get("hotel_token","")
    hotel_email = h.get("registration_email") or h.get("email", "")
    missing_labels = {
        "address": "Adresa hotelu", "phone": "Telefon recepce",
        "email": "Email", "checkin_time": "Check-in cas",
        "checkout_time": "Check-out cas", "breakfast_hours": "Hodiny snidane",
        "bed_count": "Pocet luzek", "star_rating": "Hvezdicky",
    }
    missing_list = [missing_labels.get(f, f) for f in completeness["missing"]]
    email_body = (
        "Predmet: Pripominka - doplnte informace o hotelu " + h.get("name","") + "\n\n"
        "Dobry den,\n\n"
        "vas hotel " + h.get("name","") + " ma aktivni predplatne SmartestGuide, "
        "ale profil je vyplnen pouze z " + str(completeness["score"]) + "%.\n\n"
        "Chybejici informace:\n" + "\n".join("- " + m for m in missing_list) + "\n\n"
        "Prihlaste se do portalu a doplnte chybejici informace:\n" + portal_url + "\n\n"
        "SmartestGuide\nsupport@smartestguide.com"
    )
    import logging
    logging.info("EMAIL REMINDER to %s: %s", hotel_email, email_body[:300])
    now = datetime.utcnow().isoformat()
    db["hotels"][hotel_id]["last_reminder_sent"] = now
    db["hotels"][hotel_id]["reminder_count"] = db["hotels"][hotel_id].get("reminder_count", 0) + 1
    db_save(db)
    return {
        "status": "ok",
        "email_to": hotel_email,
        "completeness": completeness,
        "portal_url": portal_url,
        "note": "Email pripraven - odesilani aktivni po nasazeni SendGrid"
    }


# ─────────────────────────────────────────────
# Leták PDF
# ─────────────────────────────────────────────
@app.get("/api/hotels/{hotel_id}/flyer")
def download_flyer(hotel_id: str, request: Request):
    from fastapi.responses import StreamingResponse
    from io import BytesIO
    import unicodedata, re
    db = db_load()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    pdf_bytes = generate_flyer_pdf(hotel, base)
    raw_name = hotel.get("name","hotel")
    safe_name = unicodedata.normalize("NFKD", raw_name).encode("ascii","ignore").decode("ascii")
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", safe_name).strip("-") or "hotel"
    fname = "letak-" + safe_name + ".pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="' + fname + '"'}
    )

def generate_flyer_pdf(hotel: dict, base_url: str) -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase import pdfmetrics
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics import renderPDF
    from io import BytesIO

    # Arial z Windows - plná podpora češtiny
    FONT, FONTB = "Helvetica", "Helvetica-Bold"
    for rp, bp in [
        ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
        ("C:/WINDOWS/Fonts/arial.ttf", "C:/WINDOWS/Fonts/arialbd.ttf"),
    ]:
        try:
            if os.path.exists(rp) and os.path.exists(bp):
                pdfmetrics.registerFont(TTFont("FL", rp))
                pdfmetrics.registerFont(TTFont("FLB", bp))
                FONT, FONTB = "FL", "FLB"
                break
        except Exception:
            pass
    if FONT == "Helvetica":
        # Fallback: Liberation Sans ze složky fonts/
        lib_r = os.path.join(BASE_DIR, "fonts", "LiberationSans-Regular.ttf")
        lib_b = os.path.join(BASE_DIR, "fonts", "LiberationSans-Bold.ttf")
        if os.path.exists(lib_r):
            try:
                pdfmetrics.registerFont(TTFont("FL", lib_r))
                pdfmetrics.registerFont(TTFont("FLB", lib_b))
                FONT, FONTB = "FL", "FLB"
            except Exception:
                pass

    en_only = hotel_is_english_only(hotel)
    W, H = A4

    PURPLE = colors.HexColor("#6c63ff")
    TEAL   = colors.HexColor("#00d4aa")
    DARK   = colors.HexColor("#0a0c14")
    DARK2  = colors.HexColor("#1a1040")
    WHITE  = colors.white
    LIGHT  = colors.HexColor("#e8eaf6")
    MUTED  = colors.HexColor("#7a7fa8")
    BORDER = colors.HexColor("#2a2f4a")

    hotel_name = hotel.get("name", "Hotel")
    hotel_loc  = (hotel.get("address","") or "").split(",")[0]
    qr_url = base_url + "/guest/" + hotel["id"]

    def T2(cz, en):
        if en_only:
            return (en, None)
        return (cz, en)

    benefits_data = [
        T2("Zeptejte se na cokoliv o hotelu", "Ask anything about the hotel"),
        T2("Check-in, check-out, snídaně",    "Check-in, check-out, breakfast"),
        T2("Doporučení míst v okolí",          "Recommendations nearby"),
        T2("Aktuální počasí",                  "Current weather"),
        T2("Kontakt na recepci",               "Contact reception"),
        T2("14 jazyků komunikace",             "14 languages available"),
    ]

    buf = BytesIO()
    cv = rl_canvas.Canvas(buf, pagesize=A4)

    cv.setFillColor(DARK)
    cv.rect(0, 0, W, H, fill=1, stroke=0)
    cv.setFillColor(DARK2)
    cv.rect(0, H-185*mm, W, 185*mm, fill=1, stroke=0)

    for r, a in [(50,.07),(70,.045),(90,.025)]:
        cv.setFillColorRGB(0.42,0.39,1.0,alpha=a)
        cv.circle(W/2, H-65*mm, r*mm, fill=1, stroke=0)

    cv.setFont(FONTB, 24)
    cv.setFillColor(WHITE)
    cv.drawCentredString(W/2, H-20*mm, hotel_name)
    if hotel_loc:
        cv.setFont(FONT, 10)
        cv.setFillColor(TEAL)
        cv.drawCentredString(W/2, H-28*mm, hotel_loc)

    cx, cy = W/2, H-63*mm
    cv.setFillColor(colors.HexColor("#1e1860"))
    cv.circle(cx, cy, 26*mm, fill=1, stroke=0)
    cv.setFillColor(PURPLE)
    cv.circle(cx, cy, 21*mm, fill=1, stroke=0)
    cv.setFillColor(colors.HexColor("#0099aa"))
    cv.circle(cx, cy, 16*mm, fill=1, stroke=0)
    cv.setFont(FONTB, 19)
    cv.setFillColor(WHITE)
    cv.drawCentredString(cx, cy-5*mm, "AI")
    cv.setStrokeColor(PURPLE)
    cv.setLineWidth(1.5)
    cv.circle(cx, cy, 29*mm, fill=0, stroke=1)
    cv.setFillColor(TEAL)
    cv.circle(cx+29*mm, cy, 2.3*mm, fill=1, stroke=0)

    headline, headline_en = T2("Váš osobní concierge", "Your personal concierge")
    sub, sub_en = T2("Dostupný 24/7 ve vašem jazyce", "Available 24/7 in your language")

    cv.setFont(FONTB, 22)
    cv.setFillColor(WHITE)
    cv.drawCentredString(W/2, H-103*mm, headline)
    y_off = 110
    if headline_en:
        cv.setFont(FONT, 11)
        cv.setFillColor(MUTED)
        cv.drawCentredString(W/2, H-110*mm, headline_en)
        y_off = 117
    cv.setFont(FONTB, 14)
    cv.setFillColor(TEAL)
    cv.drawCentredString(W/2, H-y_off*mm, sub)
    if sub_en:
        cv.setFont(FONT, 9)
        cv.setFillColor(MUTED)
        cv.drawCentredString(W/2, H-(y_off+7)*mm, sub_en)
        y_off += 7

    sep_y = H-(y_off+7)*mm
    cv.setStrokeColor(BORDER)
    cv.setLineWidth(0.5)
    cv.line(18*mm, sep_y, W-18*mm, sep_y)

    LEFT_X  = 14*mm
    RIGHT_W = 72*mm
    RIGHT_X = W - RIGHT_W - 12*mm
    CONTENT_Y = sep_y - 6*mm

    scan_t, scan_en = T2("Naskenujte QR kód:", "Scan QR code:")
    cv.setFont(FONTB, 11)
    cv.setFillColor(LIGHT)
    cv.drawString(LEFT_X, CONTENT_Y, scan_t)
    if scan_en:
        cv.setFont(FONT, 9)
        cv.setFillColor(MUTED)
        cv.drawString(LEFT_X, CONTENT_Y-6*mm, scan_en)

    by = CONTENT_Y - (14 if not en_only else 8)*mm
    for (main_t, transl) in benefits_data:
        cv.setFillColor(TEAL)
        cv.circle(LEFT_X+2*mm, by+1.5*mm, 1.6*mm, fill=1, stroke=0)
        cv.setFont(FONTB, 10)
        cv.setFillColor(LIGHT)
        cv.drawString(LEFT_X+6*mm, by+0.5*mm, main_t)
        if transl:
            cv.setFont(FONT, 8.5)
            cv.setFillColor(MUTED)
            cv.drawString(LEFT_X+6*mm, by-5*mm, transl)
        by -= (13 if not en_only else 10)*mm

    qr_size = 58*mm
    qr_x = RIGHT_X + (RIGHT_W - qr_size)/2
    qr_y = CONTENT_Y - qr_size - 4*mm

    cv.setFillColor(WHITE)
    cv.roundRect(qr_x-4*mm, qr_y-4*mm, qr_size+8*mm, qr_size+8*mm, 4*mm, fill=1, stroke=0)

    qr_w = QrCodeWidget(qr_url)
    bounds = qr_w.getBounds()
    scale = qr_size / max(bounds[2]-bounds[0], bounds[3]-bounds[1])
    d = Drawing(qr_size, qr_size, transform=[scale,0,0,scale,-bounds[0]*scale,-bounds[1]*scale])
    d.add(qr_w)
    renderPDF.draw(d, cv, qr_x, qr_y)

    free_t, free_en = T2("Pro hosty ZDARMA", "Free for guests")
    banner_y = qr_y - 20*mm
    cv.setFillColor(colors.HexColor("#1a1040"))
    cv.roundRect(RIGHT_X, banner_y-6*mm, RIGHT_W, 18*mm, 3*mm, fill=1, stroke=0)
    cv.setStrokeColor(PURPLE)
    cv.setLineWidth(1.2)
    cv.roundRect(RIGHT_X, banner_y-6*mm, RIGHT_W, 18*mm, 3*mm, fill=0, stroke=1)
    cv.setFont(FONTB, 14)
    cv.setFillColor(WHITE)
    cv.drawCentredString(RIGHT_X + RIGHT_W/2, banner_y+2*mm, free_t)
    if free_en:
        cv.setFont(FONT, 9)
        cv.setFillColor(colors.HexColor("#9ba0c0"))
        cv.drawCentredString(RIGHT_X + RIGHT_W/2, banner_y-4.5*mm, free_en)

    cv.setFillColor(PURPLE)
    cv.rect(0, 14*mm, W, 0.8*mm, fill=1, stroke=0)
    cv.setFillColor(TEAL)
    cv.rect(0, 13.2*mm, W, 0.4*mm, fill=1, stroke=0)
    cv.setFont(FONT, 7.5)
    cv.setFillColor(MUTED)
    cv.drawCentredString(W/2, 8*mm, "Powered by SmartestGuide  |  smartestguide.com")
    for i, xo in enumerate([-28,-18,-9,0,9,18,28]):
        cv.setFillColor(PURPLE if i%3==0 else BORDER)
        cv.circle(W/2+xo*mm, 3.5*mm, 0.9*mm, fill=1, stroke=0)

    cv.save()
    return buf.getvalue()


# ─────────────────────────────────────────────


# Frontend – servíruje HTML přímo z Pythonu
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
@app.get("/admin", response_class=HTMLResponse)
def serve_frontend():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/hotel", response_class=HTMLResponse)
def serve_hotel_portal():
    html_path = os.path.join(os.path.dirname(__file__), "hotel.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/landing", response_class=HTMLResponse)
def serve_landing():
    html_path = os.path.join(os.path.dirname(__file__), "landing.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/guest/{hotel_id}", response_class=HTMLResponse)
def serve_guest(hotel_id: str):
    html_path = os.path.join(os.path.dirname(__file__), "guest.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/sw.js")
def serve_sw():
    """Prázdný Service Worker – zabraňuje 404 chybě."""
    from fastapi.responses import Response
    return Response(content="// SmartestGuide SW", media_type="application/javascript")

# ─────────────────────────────────────────────
# Guest API
# ─────────────────────────────────────────────
@app.get("/api/guest/{hotel_id}")
def get_guest_hotel(hotel_id: str):
    """Vrátí veřejná data hotelu pro guest app."""
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    if not h.get("subscription_active"):
        raise HTTPException(403, "Hotel nemá aktivní předplatné")
    # Vrátí pouze veřejná data (bez tokenů a interních dat)
    public = {k: v for k, v in h.items() if k not in ("hotel_token", "stripe_customer_id", "stripe_subscription_id")}
    return {"status": "ok", "hotel": public}

class GuestChatRequest(BaseModel):
    hotel_id: str
    message: str
    language: Optional[str] = "en"
    guest_name: Optional[str] = None
    history: Optional[list] = None
    auto_lang: Optional[bool] = True

@app.post("/api/guest/chat")
async def guest_chat(req: GuestChatRequest):
    """AI chat pro hosta – používá Anthropic API."""
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(400, "AI není nakonfigurováno")

    db = db_load()
    h = db["hotels"].get(req.hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")

    # Sestav systémový prompt z dat hotelu
    hotel_info = f"""You are Alex, a friendly AI concierge for {h.get('name', 'this hotel')}.
Respond in language: {req.language}. Be helpful, concise and friendly.

Hotel information:
- Name: {h.get('name', 'N/A')}
- Address: {h.get('address', 'N/A')}
- Phone: {h.get('phone', 'N/A')}
- Email: {h.get('email', 'N/A')}
- Check-in: {h.get('checkin_time', 'N/A')}
- Check-out: {h.get('checkout_time', 'N/A')}
- Breakfast: {h.get('breakfast_hours', 'N/A')}
- Lunch: {h.get('lunch_hours', 'N/A')}
- Dinner: {h.get('dinner_hours', 'N/A')}
- Restaurant: {h.get('restaurant_name', 'N/A')}
- Parking: {h.get('parking_info', 'N/A')}
- Wellness: {h.get('wellness_info', 'N/A')}
- WhatsApp: {h.get('whatsapp_number', 'N/A')}
- Amenities: {', '.join(h.get('amenities', []))}
- Nearby: {', '.join(h.get('nearby_places', []))}
- Description: {h.get('description', 'N/A')}
- Extra info: {h.get('extra_info', 'N/A')}

Guest name: {req.guest_name or 'Guest'}
Always respond in {req.language} language."""

    messages = []
    for m in (req.history or []):
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500, "system": hotel_info, "messages": messages},
            timeout=30.0,
        )
    if r.status_code != 200:
        raise HTTPException(500, f"AI chyba: {r.text[:200]}")
    reply = r.json()["content"][0]["text"]
    return {"status": "ok", "reply": reply}

class TranslateMenuRequest(BaseModel):
    hotel_id: str
    image_base64: str
    language: Optional[str] = "en"
    guest_name: Optional[str] = None

@app.post("/api/guest/translate-menu")
async def translate_menu(req: TranslateMenuRequest):
    """Přeloží foto menu pomocí Claude Vision."""
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(400, "AI není nakonfigurováno")

    # Odstraň data URL prefix pokud je přítomen
    img_data = req.image_base64
    if "," in img_data:
        img_data = img_data.split(",", 1)[1]

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 800,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img_data}},
                        {"type": "text", "text": f"This is a menu photo. Please translate and describe all items you can see into {req.language} language. Format it nicely with dish names and descriptions."}
                    ]
                }]
            },
            timeout=30.0,
        )
    if r.status_code != 200:
        raise HTTPException(500, f"AI chyba: {r.text[:200]}")
    reply = r.json()["content"][0]["text"]
    return {"status": "ok", "reply": reply}


# Vrátí portal link pro existující hotel (+ vygeneruje token pokud chybí)
@app.get("/api/hotels/{hotel_id}/portal-link")
def get_portal_link(hotel_id: str, request: Request):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    if not db["hotels"][hotel_id].get("hotel_token"):
        db["hotels"][hotel_id]["hotel_token"] = str(uuid.uuid4()).replace("-", "")
        db_save(db)
    token = db["hotels"][hotel_id]["hotel_token"]
    base = get_base_url(request)
    return {"status": "ok", "token": token, "portal_url": f"{base}/hotel?token={token}"}
