"""
SmartestGuide – vše v jednom souboru
Spusť: python -m uvicorn app:app --reload
Nebo použij SPUSTIT.bat
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import os, json, uuid, httpx, asyncio, re, base64, hmac, hashlib, socket
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv

load_dotenv()

# Absolutní cesta ke složce s app.py
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_local_ip() -> str:
    """Zjistí lokální IP adresu počítače v síti."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"

def get_base_url() -> str:
    # 1. Pokud je nastaven ngrok_url v nastaveni, pouzij ho
    s = db_get_settings()
    ngrok = s.get("ngrok_url", "").strip().rstrip("/")
    if ngrok and ngrok.startswith("https://"):
        return ngrok
    # 2. Zkus autodetekovat ngrok z lokalniho API
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=1) as r:
            data = json.loads(r.read())
            for t in data.get("tunnels", []):
                url = t.get("public_url", "")
                if url.startswith("https://"):
                    # Uloz do nastaveni pro pristi pouziti
                    db_save_settings({"ngrok_url": url})
                    return url
    except Exception:
        pass
    # 3. Fallback na lokalni IP
    ip = get_local_ip()
    return f"http://{ip}:8000" 

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
DB_PATH = os.path.join(BASE_DIR, "data.json")

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
    room_types: Optional[str] = None
    language_note: Optional[str] = None
    fitness_info: Optional[str] = None
    pool_info: Optional[str] = None
    spa_info: Optional[str] = None
    room_service_hours: Optional[str] = None
    laundry_info: Optional[str] = None
    bike_rental: Optional[str] = None
    transfer_info: Optional[str] = None
    congress_info: Optional[str] = None
    pets_policy: Optional[str] = None
    accessibility: Optional[str] = None
    breakfast_type: Optional[str] = None
    bar_info: Optional[str] = None
    dietary_options: Optional[str] = None
    minibar: Optional[str] = None
    menu_urls: Optional[List[str]] = None
    custom_fields: Optional[List[dict]] = None
    whatsapp_reception: Optional[str] = None
    whatsapp_wellness: Optional[str] = None
    whatsapp_restaurant: Optional[str] = None
    menu_urls: Optional[List[str]] = None
    custom_fields: Optional[List[dict]] = None
    whatsapp_reception: Optional[str] = None
    whatsapp_wellness: Optional[str] = None
    whatsapp_restaurant: Optional[str] = None
    nearby_places_maps: Optional[List[dict]] = None
    country: Optional[str] = None
    continent: Optional[str] = None
    extra_info: Optional[str] = None
    subscription_active: Optional[bool] = None
    stripe_customer_id: Optional[str] = None
    scraped_pages: Optional[List[str]] = None

# ─────────────────────────────────────────────
# Nastavení API klíče
# ─────────────────────────────────────────────
class CompanySettingsRequest(BaseModel):
    company_name: Optional[str] = None
    company_address: Optional[str] = None
    company_city: Optional[str] = None
    company_ico: Optional[str] = None
    company_dic: Optional[str] = None
    company_email: Optional[str] = None
    company_phone: Optional[str] = None
    company_bank: Optional[str] = None
    company_iban: Optional[str] = None

@app.post("/api/settings/company")
def save_company_settings(req: CompanySettingsRequest):
    db_save_settings(req.model_dump(exclude_none=True))
    return {"status": "ok"}

@app.get("/api/settings/company")
def get_company_settings():
    s = db_get_settings()
    return {
        "company_name": s.get("company_name", "SmartestGuide s.r.o."),
        "company_address": s.get("company_address", ""),
        "company_city": s.get("company_city", "Praha, Česká republika"),
        "company_ico": s.get("company_ico", ""),
        "company_dic": s.get("company_dic", ""),
        "company_email": s.get("company_email", "support@smartestguide.com"),
        "company_phone": s.get("company_phone", ""),
        "company_bank": s.get("company_bank", ""),
        "company_iban": s.get("company_iban", ""),
    }

@app.get("/api/settings/pricing")
def get_pricing_settings():
    s = db_get_settings()
    return {
        "base_price": s.get("price_base", 300),
        "price_per_bed": s.get("price_per_bed", 3),
        "free_beds": s.get("price_free_beds", 100),
    }

class PricingSettingsRequest(BaseModel):
    base_price: int
    price_per_bed: int
    free_beds: int

@app.post("/api/settings/pricing")
def save_pricing_settings(req: PricingSettingsRequest):
    if req.base_price < 1 or req.price_per_bed < 0 or req.free_beds < 1:
        raise HTTPException(400, "Neplatné hodnoty")
    db_save_settings({
        "price_base": req.base_price,
        "price_per_bed": req.price_per_bed,
        "price_free_beds": req.free_beds,
    })
    return {"status": "ok"}

@app.get("/api/info")
def get_info():
    """Základní info o serveru – IP adresa pro přístup z mobilu."""
    ip = get_local_ip()
    return {
        "local_ip": ip,
        "base_url": get_base_url(),
        "guest_example": f"{get_base_url()}/guest/HOTEL_ID",
        "admin_url": f"http://{ip}:8000",
    }

@app.get("/manifest.json")
def serve_manifest():
    path = os.path.join(BASE_DIR, "manifest.json")
    if os.path.exists(path):
        with open(path) as f:
            return JSONResponse(json.load(f))
    return JSONResponse({"name": "SmartestGuide", "short_name": "Concierge", "display": "standalone", "theme_color": "#6c63ff", "background_color": "#0d0f1a", "start_url": "/"})

@app.get("/sw.js")
def serve_sw():
    path = os.path.join(BASE_DIR, "sw.js")
    if os.path.exists(path):
        with open(path) as f:
            return HTMLResponse(content=f.read(), media_type="application/javascript")
    return HTMLResponse(content="", media_type="application/javascript")

@app.get("/icon-192.png")
@app.get("/icon-512.png")
def serve_icon():
    # Vrati prazdny PNG placeholder pokud neni ikona
    import base64
    # 1x1 fialovy pixel jako placeholder
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    return HTMLResponse(content=png, media_type="image/png")

@app.get("/landing", response_class=HTMLResponse)
def serve_landing():
    html_path = os.path.join(BASE_DIR, "landing.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    html_path = os.path.join(BASE_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/hotel", response_class=HTMLResponse)
def serve_hotel_portal():
    html_path = os.path.join(BASE_DIR, "hotel.html")
    import logging
    logging.warning(f"SERVING HOTEL PORTAL from: {html_path}")
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    logging.warning(f"hotel.html size: {len(content)} chars, title: {content[:200]}")
    return content

@app.get("/debug-hotel")
def debug_hotel():
    html_path = os.path.join(BASE_DIR, "hotel.html")
    index_path = os.path.join(BASE_DIR, "index.html")
    return {
        "BASE_DIR": BASE_DIR,
        "hotel_html_exists": os.path.exists(html_path),
        "hotel_html_size": os.path.getsize(html_path) if os.path.exists(html_path) else 0,
        "index_html_size": os.path.getsize(index_path) if os.path.exists(index_path) else 0,
        "hotel_html_title": open(html_path).read()[:100] if os.path.exists(html_path) else "NOT FOUND",
    }

@app.get("/api/settings")
def get_settings():
    s = db_get_settings()
    return {
        "has_api_key": bool(s.get("anthropic_api_key")),
        "api_key_preview": ("sk-ant-..." + s["anthropic_api_key"][-6:]) if s.get("anthropic_api_key") else None,
        "has_stripe_key": bool(s.get("stripe_secret_key")),
        "stripe_payment_link": s.get("stripe_payment_link", "https://buy.stripe.com/test_7sY6oA2sr7Zo3jq6zGa3u01"),
        "stripe_key_preview": ("sk_test_..." + s["stripe_secret_key"][-6:]) if s.get("stripe_secret_key") else None,
        "ngrok_url": s.get("ngrok_url", ""),
    }

class StripeSettingsRequest(BaseModel):
    stripe_secret_key: str
    stripe_payment_link: str
    stripe_webhook_secret: Optional[str] = None

class NgrokRequest(BaseModel):
    ngrok_url: str

@app.post("/api/settings/ngrok")
def save_ngrok_url(req: NgrokRequest):
    url = req.ngrok_url.strip().rstrip("/")
    db_save_settings({"ngrok_url": url})
    return {"status": "ok", "ngrok_url": url}

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
Pokud informaci nenajdeš, použij null. Nikdy nevymýšlej data.
DULEZITE: Hvezdicove hodnoceni (star_rating) hlehej vsude - v nazvu hotelu (napr "Hotel ***", "4* Hotel"), v textu ("ctyrh hvezdickovy", "four-star", "****"), v meta tazich, v BookingDotCom odkazech. Pokud vidis v nazvu nebo textu symboly *, hviezdicky, hvezdy, stars - spocitej je a dej jako cislo 1-5."""

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
        "origin": data.model_dump().get("origin") or "manual",
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
    room_types: Optional[str] = None
    language_note: Optional[str] = None
    fitness_info: Optional[str] = None
    pool_info: Optional[str] = None
    spa_info: Optional[str] = None
    room_service_hours: Optional[str] = None
    laundry_info: Optional[str] = None
    bike_rental: Optional[str] = None
    transfer_info: Optional[str] = None
    congress_info: Optional[str] = None
    pets_policy: Optional[str] = None
    accessibility: Optional[str] = None
    breakfast_type: Optional[str] = None
    bar_info: Optional[str] = None
    dietary_options: Optional[str] = None
    minibar: Optional[str] = None
    menu_urls: Optional[List[str]] = None
    custom_fields: Optional[List[dict]] = None
    whatsapp_reception: Optional[str] = None
    whatsapp_wellness: Optional[str] = None
    whatsapp_restaurant: Optional[str] = None
    menu_urls: Optional[List[str]] = None
    custom_fields: Optional[List[dict]] = None
    whatsapp_reception: Optional[str] = None
    whatsapp_wellness: Optional[str] = None
    whatsapp_restaurant: Optional[str] = None
    nearby_places_maps: Optional[List[dict]] = None

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

@app.post("/api/hotels/{hotel_id}/generate-token")
def generate_token(hotel_id: str):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    if not db["hotels"][hotel_id].get("hotel_token"):
        db["hotels"][hotel_id]["hotel_token"] = str(uuid.uuid4()).replace("-", "")
        db_save(db)
    token = db["hotels"][hotel_id]["hotel_token"]
    return {"status": "ok", "token": token, "portal_url": f"{get_base_url()}/hotel?token={token}"}

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
# QR kód
# ─────────────────────────────────────────────
@app.get("/api/hotels/{hotel_id}/qr")
def generate_qr(hotel_id: str):
    try:
        import qrcode
        from io import BytesIO
    except ImportError:
        raise HTTPException(500, "Nainstaluj qrcode: pip install qrcode[pil]")

    db = db_load()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")

    guest_url = f"{get_base_url()}/guest/{hotel_id}"
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
    s = db_get_settings()
    base = int(s.get("price_base", 300))
    per_bed = int(s.get("price_per_bed", 3))
    free_beds = int(s.get("price_free_beds", 100))
    price = calc_price_eur(beds, s)
    return {
        "beds": beds,
        "monthly_eur": price,
        "quarterly_eur": price * 3,
        "base_price": base,
        "price_per_bed": per_bed,
        "free_beds": free_beds,
        "note": "Zaváděcí cena – při objednání v prvních 3 měsících zůstane zachována"
    }

# ─────────────────────────────────────────────
# Stripe – platby a webhook
# ─────────────────────────────────────────────
from fastapi import Request

@app.get("/api/stripe/checkout/{hotel_id}")
def stripe_checkout(hotel_id: str):
    """Vrátí Stripe payment link s prefilled hotel_id v metadata přes client_reference_id."""
    db = db_load()
    s = db_get_settings()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")
    payment_link = s.get("stripe_payment_link", "https://buy.stripe.com/test_7sY6oA2sr7Zo3jq6zGa3u01")
    # Přidej client_reference_id = hotel_id pro identifikaci po platbě
    sep = "&" if "?" in payment_link else "?"
    url = f"{payment_link}{sep}client_reference_id={hotel_id}"
    return {"status": "ok", "checkout_url": url, "hotel_name": hotel.get("name", "")}

def _create_invoice_for_hotel(h: dict, subscription_id: str, event_type: str):
    """Vygeneruje fakturu pro hotel po platbě."""
    now = datetime.utcnow()
    inv_number = f"SG-{now.strftime('%Y%m')}-{str(uuid.uuid4())[:6].upper()}"
    beds = h.get("bed_count", 0) or 0
    amount_eur = calc_price_eur(beds)
    currency_code, currency_symbol = get_hotel_currency(h)
    amount_local = convert_eur(amount_eur, currency_code)
    invoice = {
        "id": str(uuid.uuid4()),
        "invoice_number": inv_number,
        "hotel_id": h["id"],
        "hotel_name": h.get("name", ""),
        "hotel_address": h.get("address", ""),
        "hotel_email": h.get("email", ""),
        "beds": beds,
        "amount_eur": amount_eur,
        "amount_local": amount_local,
        "currency_code": currency_code,
        "currency_symbol": currency_symbol,
        "period_from": now.strftime("%Y-%m-01"),
        "period_to": now.strftime(f"%Y-%m-{now.day:02d}"),
        "created_at": now.isoformat(),
        "status": "paid",
        "paid_at": now.isoformat(),
        "stripe_invoice_id": subscription_id,
        "stripe_event_type": event_type,
    }
    db = db_load()
    if "invoices" not in db:
        db["invoices"] = {}
    db["invoices"][invoice["id"]] = invoice
    db_save(db)

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

        import logging
        logging.warning(f"WEBHOOK DEBUG: type={event_type}, hotel_id={hotel_id}, customer={customer_id}")
        logging.warning(f"WEBHOOK OBJ KEYS: {list(obj.keys())}")
        logging.warning(f"WEBHOOK client_ref: {obj.get('client_reference_id')}")

        # Zkus dekódovat registrační data z client_reference_id
        reg_data = None
        if hotel_id and len(hotel_id) > 36:
            # Vypadá jako base64 payload z landing page
            try:
                import base64
                decoded = base64.b64decode(hotel_id + "==").decode("utf-8")
                reg_data = json.loads(decoded)
                hotel_id = None  # Není to existující hotel_id
            except Exception:
                pass

        db = db_load()

        if hotel_id and hotel_id in db["hotels"]:
            # Existující hotel - aktivuj předplatné
            h = db["hotels"][hotel_id]
            h["subscription_active"] = True
            h["stripe_customer_id"] = customer_id
            h["stripe_subscription_id"] = subscription_id
            h["subscription_start"] = datetime.utcnow().isoformat()
            h["updated_at"] = datetime.utcnow().isoformat()
            if not h.get("subscription_paid_beds") and h.get("bed_count"):
                h["subscription_paid_beds"] = h["bed_count"]
            db_save(db)
            # Vygeneruj fakturu
            _create_invoice_for_hotel(h, subscription_id, event_type)

        elif reg_data:
            # Nová registrace z landing page - scraping + vytvoření hotelu
            import asyncio as _asyncio
            settings = db_get_settings()
            api_key = settings.get("anthropic_api_key", "")
            hotel_name = reg_data.get("n", "Hotel")
            hotel_url  = reg_data.get("u", "")
            hotel_email = reg_data.get("e", "")
            hotel_beds  = int(reg_data.get("b", 0) or 0)
            hotel_country = reg_data.get("c", "")
            hotel_phone = reg_data.get("p", "")
            hotel_source = reg_data.get("s", "")

            # Scraping
            scraped = {}
            if api_key and hotel_url:
                try:
                    scraped = _asyncio.get_event_loop().run_until_complete(
                        scrape_hotel_data(hotel_url, api_key)
                    )
                except Exception:
                    scraped = {}

            hid = str(uuid.uuid4())
            now2 = datetime.utcnow()
            hotel_token = str(uuid.uuid4()).replace("-", "")

            new_hotel = {
                "id": hid,
                "created_at": now2.isoformat(),
                "updated_at": now2.isoformat(),
                "qr_code_id": str(uuid.uuid4()),
                "hotel_token": hotel_token,
                "subscription_active": True,
                "subscription_start": now2.isoformat(),
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "origin": "automatic",
                "origin_source": hotel_source,
                "registration_email": hotel_email,
                "registration_phone": hotel_phone,
                "name": scraped.get("name") or hotel_name,
                "email": hotel_email,
                "phone": hotel_phone,
                "bed_count": hotel_beds,
                "subscription_paid_beds": hotel_beds,
                "country": hotel_country or scraped.get("country"),
                **{k: v for k, v in scraped.items()
                   if v and k not in ("name", "bed_count", "country", "source_url")},
            }

            db2 = db_load()
            db2["hotels"][hid] = new_hotel
            db_save(db2)

            # Vygeneruj fakturu
            _create_invoice_for_hotel(new_hotel, subscription_id, event_type)

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

# ─────────────────────────────────────────────
# Registrace z landing page (po platbě)
# ─────────────────────────────────────────────
class LandingRegistration(BaseModel):
    name: str
    url: str
    email: str
    beds: int
    country: Optional[str] = None
    phone: Optional[str] = None
    source: Optional[str] = None

@app.post("/api/register")
async def register_from_landing(req: LandingRegistration):
    """Zpracuje registraci z landing page — scraping + vytvoření hotelu."""
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(500, "API klic neni nastaven")

    # Scraping webu
    try:
        scraped = await scrape_hotel_data(req.url, api_key)
    except Exception:
        # Fallback - vytvoř hotel jen ze zadaných dat
        scraped = {"name": req.name, "url": req.url, "source_url": req.url}

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
        "origin": "automatic",
        "origin_source": req.source or "",
        "registration_email": req.email,
        "registration_phone": req.phone or "",
        "name": req.name,
        "email": req.email,
        "bed_count": req.beds,
        "country": req.country or scraped.get("country"),
        **{k: v for k, v in scraped.items() if v and k not in ("name", "bed_count", "country")},
    }

    db["hotels"][hid] = hotel
    db_save(db)

    portal_url = f"{get_base_url()}/hotel?token={hotel_token}"
    return {
        "status": "ok",
        "hotel_id": hid,
        "portal_url": portal_url,
        "hotel_token": hotel_token,
    }

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


# ─────────────────────────────────────────────
# Jazykové helpery
# ─────────────────────────────────────────────
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
def download_flyer(hotel_id: str, format: str = "a4"):
    """Format: a4 (default), a5, rollup"""
    db = db_load()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")
    pdf_bytes = generate_flyer_pdf(hotel, get_base_url(), format=format)
    import unicodedata, re
    raw_name = hotel.get("name","hotel")
    safe_name = unicodedata.normalize("NFKD", raw_name).encode("ascii","ignore").decode("ascii")
    safe_name = re.sub(r"[^a-zA-Z0-9-]", "-", safe_name).strip("-") or "hotel"
    fname = f"letak-{format}-{safe_name}.pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="' + fname + '"'}
    )

def generate_flyer_pdf(hotel: dict, base_url: str, format: str = 'a4') -> bytes:
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase import pdfmetrics
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics import renderPDF

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
    # Výběr formátu
    if format == 'a5':
        from reportlab.lib.pagesizes import A5
        W, H = A5
    elif format == 'rollup':
        # Roll-up banner: 85cm x 200cm
        W, H = 85*mm*2.8346, 200*mm*2.8346  # přibližně 85x200cm v points
        # Limitujeme na rozumnou velikost pro PDF
        W, H = 595, 1400  # cca A4 šířka, 2x výška
    else:
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

    free_t, free_en = T2("ZDARMA pro hosty", "FREE for guests")
    banner_y = qr_y - 20*mm
    cv.setFillColor(colors.HexColor("#0d3d2a"))
    cv.roundRect(RIGHT_X, banner_y-6*mm, RIGHT_W, 18*mm, 3*mm, fill=1, stroke=0)
    cv.setStrokeColor(TEAL)
    cv.setLineWidth(1.2)
    cv.roundRect(RIGHT_X, banner_y-6*mm, RIGHT_W, 18*mm, 3*mm, fill=0, stroke=1)
    cv.setFont(FONTB, 14)
    cv.setFillColor(TEAL)
    cv.drawCentredString(RIGHT_X + RIGHT_W/2, banner_y+2*mm, free_t)
    if free_en:
        cv.setFont(FONT, 9)
        cv.setFillColor(colors.HexColor("#4daa88"))
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
    html_path = os.path.join(BASE_DIR, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/guest/{hotel_id}", response_class=HTMLResponse)
def serve_guest(hotel_id: str):
    html_path = os.path.join(BASE_DIR, "guest.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/guest/{hotel_id}")
def get_guest_hotel(hotel_id: str):
    """Vrátí veřejná data hotelu pro hosta – bez interních dat."""
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    if not h.get("subscription_active"):
        raise HTTPException(403, "Hotel nema aktivni predplatne")
    # Jen veřejná pole
    public = {
        "id": h["id"],
        "name": h.get("name"),
        "description": h.get("description"),
        "address": h.get("address"),
        "phone": h.get("phone"),
        "email": h.get("email"),
        "checkin_time": h.get("checkin_time"),
        "checkout_time": h.get("checkout_time"),
        "breakfast_hours": h.get("breakfast_hours"),
        "lunch_hours": h.get("lunch_hours"),
        "dinner_hours": h.get("dinner_hours"),
        "amenities": h.get("amenities", []),
        "nearby_places": h.get("nearby_places", []),
        "restaurant_name": h.get("restaurant_name"),
        "wellness_info": h.get("wellness_info"),
        "parking_info": h.get("parking_info"),
        "whatsapp_number": h.get("whatsapp_number"),
        "star_rating": h.get("star_rating"),
        "country": h.get("country"),
        "extra_info": h.get("extra_info"),
        "languages": h.get("languages", []),
    }
    return {"status": "ok", "hotel": public}

class MenuTranslateRequest(BaseModel):
    hotel_id: str
    image_base64: str
    language: str = "en"
    guest_name: Optional[str] = None

@app.post("/api/guest/translate-menu")
async def translate_menu(req: MenuTranslateRequest):
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(500, "API klic neni nastaven")

    db = db_load()
    h = db["hotels"].get(req.hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")

    lang_names = {
        "cs":"Czech","sk":"Slovak","en":"English","de":"German",
        "fr":"French","it":"Italian","es":"Spanish","pl":"Polish",
        "hu":"Hungarian","ru":"Russian","uk":"Ukrainian",
    }
    lang = lang_names.get(req.language, "English")

    system = f"You are Alex, a hotel concierge at {h.get('name','hotel')}. Translate and explain the menu items in the photo. Respond in {lang}. Be friendly and helpful. Format the translation clearly."

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
                "max_tokens": 1000,
                "system": system,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": req.image_base64,
                            }
                        },
                        {
                            "type": "text",
                            "text": f"Please translate this menu into {lang} and briefly explain each dish."
                        }
                    ]
                }]
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            raise HTTPException(500, f"AI error: {r.status_code}")
        data = r.json()

    reply = data["content"][0]["text"].strip()
    return {"status": "ok", "reply": reply}

class GuestChatRequest(BaseModel):
    hotel_id: str
    message: str
    language: str = "cs"
    guest_name: Optional[str] = None
    history: Optional[List[dict]] = None
    auto_lang: Optional[bool] = False

@app.post("/api/guest/chat")
async def guest_chat(req: GuestChatRequest):
    """AI chat pro hosta – odpovídá v jeho jazyce s daty hotelu."""
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(500, "API klic neni nastaven")

    db = db_load()
    h = db["hotels"].get(req.hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    if not h.get("subscription_active"):
        raise HTTPException(403, "Hotel nema aktivni predplatne")

    # Sestav kontext hotelu
    hotel_context = f"""
Název hotelu: {h.get('name', 'Hotel')}
Adresa: {h.get('address', 'neuvedeno')}
Popis: {h.get('description', '')}
Telefon recepce: {h.get('phone', 'neuvedeno')}
Email: {h.get('email', 'neuvedeno')}
WhatsApp: {h.get('whatsapp_number', 'neuvedeno')}
Check-in: {h.get('checkin_time', 'neuvedeno')}
Check-out: {h.get('checkout_time', 'neuvedeno')}
Snídaně: {h.get('breakfast_hours', 'neuvedeno')}
Oběd: {h.get('lunch_hours', 'neuvedeno')}
Večeře: {h.get('dinner_hours', 'neuvedeno')}
Restaurace: {h.get('restaurant_name', 'neuvedeno')}
Typ snidane: {h.get('breakfast_type', 'neuvedeno')}
Bar: {h.get('bar_info', 'neuvedeno')}
Mini bar: {h.get('minibar', 'neuvedeno')}
Vegetarianska/vegan jidla: {h.get('dietary_options', 'neuvedeno')}
Wellness/Spa: {h.get('wellness_info', 'neuvedeno')}
Fitness/Gym: {h.get('fitness_info', 'neuvedeno')}
Bazen: {h.get('pool_info', 'neuvedeno')}
Spa masaze: {h.get('spa_info', 'neuvedeno')}
Parkování: {h.get('parking_info', 'neuvedeno')}
Room service: {h.get('room_service_hours', 'neuvedeno')}
Pradlna: {h.get('laundry_info', 'neuvedeno')}
Transfer/Shuttle: {h.get('transfer_info', 'neuvedeno')}
Pujcovna kol: {h.get('bike_rental', 'neuvedeno')}
Domaci mazlicci: {h.get('pets_policy', 'neuvedeno')}
Bezbarierovy pristup: {h.get('accessibility', 'neuvedeno')}
WhatsApp recepce: {h.get('whatsapp_reception') or h.get('whatsapp_number', 'neuvedeno')}
WhatsApp wellness: {h.get('whatsapp_wellness', 'neuvedeno')}
WhatsApp restaurace: {h.get('whatsapp_restaurant', 'neuvedeno')}
Menu URL: {', '.join(h.get('menu_urls') or []) or 'neuvedeno'}
Vlastni informace hotelu: {'; '.join([f.get('label','')+'='+f.get('value','') for f in (h.get('custom_fields') or []) if f.get('value')]) or 'zadne'}
Vybavení: {', '.join(h.get('amenities', []))}
Místa v okolí: {', '.join(h.get('nearby_places', []))}
Hvězdičky: {h.get('star_rating', 'neuvedeno')}
Další info: {h.get('extra_info', '')}
"""

    guest_name = req.guest_name or "host"

    lang_names = {
        "cs": "češtině", "sk": "slovenštině", "en": "English",
        "de": "Deutsch", "fr": "français", "it": "italiano",
        "es": "español", "pl": "polštině", "hu": "maďarštině",
        "ru": "ruštině", "zh": "čínštině", "ja": "japonštině",
        "ar": "arabštině", "uk": "ukrajinštině",
    }
    lang_label = lang_names.get(req.language, req.language)

    system = f"""You are Alex, a friendly and professional AI concierge of hotel {h.get('name', '')}.
The guest's name is {guest_name}.

CRITICAL LANGUAGE RULE - THIS IS THE MOST IMPORTANT INSTRUCTION:
You MUST ALWAYS respond in EXACTLY the same language as the guest's message.
- If the guest writes in German → respond ONLY in German
- If the guest writes in English → respond ONLY in English  
- If the guest writes in Czech → respond ONLY in Czech
- If the guest writes in French → respond ONLY in French
- NEVER switch to a different language
- NEVER respond in Czech if the guest wrote in another language
- The configured language is {lang_label} but ALWAYS follow the actual language of the message
- Even if you don't know the answer, respond in the SAME language as the question

You are polite, helpful and knowledgeable like a local guide.
Keep responses brief (2-4 sentences), natural and friendly.
If asked something you don't know, say so honestly in the guest's language.
Never invent information.

Jsi zdvořilý, nápomocný a máš znalosti místního průvodce.
Odpovědi jsou stručné (2-4 věty), přirozené a přátelské.
Pokud se tě ptají na něco co nevíš, řekni to upřímně.
Nikdy nevymýšlej informace. Pouze sdílej data která máš k dispozici.

POCASI A FUNKCE APLIKACE:
- Pro aktualni pocasi host klikne na tlacitko v horni liste aplikace nebo napise "pocasi"/"weather"
- Nikdy nerekni ze nevís jake je pocasi - vzdy navrhni pouzit tlacitko pocasi
- Preklad menu: host muze vyfotit jidelni listek a ty ho prelozis

INFORMACE O HOTELU:
{hotel_context}"""

    # Sestavení historie
    messages = []
    if req.history:
        for msg in req.history[-10:]:  # max 10 zpráv historie
            if msg.get("role") in ("user", "assistant"):
                messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({"role": "user", "content": req.message})

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
                "max_tokens": 500,
                "system": system,
                "messages": messages,
            },
            timeout=30.0,
        )
        if r.status_code != 200:
            raise HTTPException(500, f"AI chyba: {r.status_code}")
        data = r.json()

    reply = data["content"][0]["text"].strip()

    # Ulož analytics
    try:
        db2 = db_load()
        if "analytics" not in db2:
            db2["analytics"] = {}
        hid = req.hotel_id
        if hid not in db2["analytics"]:
            db2["analytics"][hid] = {"total": 0, "topics": {}}
        db2["analytics"][hid]["total"] += 1
        db2["analytics"][hid]["last_chat"] = datetime.utcnow().isoformat()
        # Jednoduché topic detection
        msg_lower = req.message.lower()
        topics = {
            "checkin": ["check-in","check in","příjezd","arrival","checkin"],
            "checkout": ["check-out","check out","odjezd","departure","checkout"],
            "breakfast": ["snídaně","breakfast","snidane","raňajky","frühstück"],
            "parking": ["parkování","parking","parken","parkování"],
            "wellness": ["wellness","spa","bazén","pool","fitness","gym"],
            "restaurant": ["restaurace","restaurant","oběd","večeře","dinner","lunch"],
            "weather": ["počasí","weather","wetter","météo"],
            "nearby": ["okolí","nearby","okolie","attraction","tip"],
            "contact": ["telefon","phone","kontakt","contact","email","whatsapp"],
        }
        for topic, keywords in topics.items():
            if any(kw in msg_lower for kw in keywords):
                db2["analytics"][hid]["topics"][topic] = db2["analytics"][hid]["topics"].get(topic, 0) + 1
        db_save(db2)
    except Exception:
        pass

    return {"status": "ok", "reply": reply}

# Vrátí portal link pro existující hotel (+ vygeneruje token pokud chybí)
@app.get("/api/hotels/{hotel_id}/portal-link")
def get_portal_link(hotel_id: str):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    if not db["hotels"][hotel_id].get("hotel_token"):
        db["hotels"][hotel_id]["hotel_token"] = str(uuid.uuid4()).replace("-", "")
        db_save(db)
    token = db["hotels"][hotel_id]["hotel_token"]
    return {"status": "ok", "token": token, "portal_url": f"{get_base_url()}/hotel?token={token}"}



# ─────────────────────────────────────────────
# PDF generátor faktury
# ─────────────────────────────────────────────
from fastapi.responses import StreamingResponse
from io import BytesIO

def generate_invoice_pdf_bytes(inv: dict, company: dict) -> bytes:
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase import pdfmetrics
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT
    from datetime import datetime as dt

    # Arial z Windows, fallback Liberation, fallback Helvetica
    FONT = 'Helvetica'
    FONTB = 'Helvetica-Bold'
    for rp, bp in [
        ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
        (os.path.join(BASE_DIR,'fonts','LiberationSans-Regular.ttf'),
         os.path.join(BASE_DIR,'fonts','LiberationSans-Bold.ttf')),
    ]:
        try:
            if os.path.exists(rp) and os.path.exists(bp):
                pdfmetrics.registerFont(TTFont('SG', rp))
                pdfmetrics.registerFont(TTFont('SG-Bold', bp))
                FONT, FONTB = 'SG', 'SG-Bold'
                break
        except Exception:
            pass

    BLACK = colors.black
    GRAY  = colors.HexColor('#555555')
    LGRAY = colors.HexColor('#f0f0f0')

    def s(text, bold=False, size=10, color=None, align=TA_LEFT):
        return Paragraph(str(text), ParagraphStyle('_',
            fontName=FONTB if bold else FONT,
            fontSize=size, textColor=color or BLACK,
            alignment=align, leading=size*1.4))

    def fmt(d):
        if not d: return ''
        try: return dt.fromisoformat(str(d)).strftime('%d. %m. %Y')
        except: return str(d)[:10]

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=20*mm, bottomMargin=20*mm)
    co = company
    story = []

    # Hlavicka
    story.append(Table([
        [s('SmartestGuide', bold=True, size=18),
         s('FAKTURA / INVOICE', bold=True, size=16, align=TA_RIGHT)],
        [s(inv.get('invoice_number',''), size=9, color=GRAY),
         s(inv.get('invoice_number',''), size=9, color=GRAY, align=TA_RIGHT)],
    ], colWidths=[90*mm, 80*mm]))
    story.append(Spacer(1, 4*mm))

    # Cara
    story.append(Table([['']], colWidths=[170*mm],
        style=[('LINEABOVE',(0,0),(-1,-1),1,BLACK),('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]))
    story.append(Spacer(1, 6*mm))

    # Dodavatel / Odberatel
    sup = [s('DODAVATEL / SUPPLIER', size=8, color=GRAY)]
    sup.append(s(co.get('company_name','SmartestGuide s.r.o.'), bold=True, size=10))
    for l in filter(None,[co.get('company_address',''), co.get('company_city',''), 
        ('ICO: '+co['company_ico']) if co.get('company_ico') else '',
        ('DIC: '+co['company_dic']) if co.get('company_dic') else '',
        co.get('company_email',''), co.get('company_phone','')]):
        sup.append(s(l, size=9, color=GRAY))

    cli = [s('ODBERATEL / CLIENT', size=8, color=GRAY)]
    cli.append(s(inv.get('hotel_name',''), bold=True, size=10))
    for l in filter(None,[inv.get('hotel_address',''), inv.get('hotel_email','')]):
        cli.append(s(l, size=9, color=GRAY))

    story.append(Table([[sup, cli]], colWidths=[85*mm, 85*mm],
        style=[('VALIGN',(0,0),(-1,-1),'TOP')]))
    story.append(Spacer(1, 8*mm))

    # Detaily
    rows = [
        ('Cislo faktury / Invoice no.',        inv.get('invoice_number','')),
        ('Datum vystaveni / Issue date',        fmt(inv.get('created_at',''))),
        ('Fakturacni obdobi / Billing period',  inv.get('period_from','') + ' - ' + inv.get('period_to','')),
        ('Pocet luzek / Number of beds',        str(inv.get('beds',''))),
    ]
    if inv.get('paid_at'):
        rows.append(('Datum uhrady / Payment date', fmt(inv.get('paid_at',''))))
    rows.append(('Sluzba / Service', 'SmartestGuide - mesicni predplatne / Monthly subscription'))
    rows.append(('Castka EUR / Amount EUR', str(inv.get('amount_eur',0)) + ' EUR'))

    tbl_data = [[s(r, size=9, color=GRAY), s(v, bold=True, size=9, align=TA_RIGHT)] for r,v in rows]
    tbl = Table(tbl_data, colWidths=[110*mm, 60*mm])
    tbl.setStyle(TableStyle([
        ('ROWBACKGROUNDS',(0,0),(-1,-1),[colors.white, LGRAY]),
        ('TOPPADDING',(0,0),(-1,-1),5), ('BOTTOMPADDING',(0,0),(-1,-1),5),
        ('LEFTPADDING',(0,0),(-1,-1),6), ('RIGHTPADDING',(0,0),(-1,-1),6),
        ('LINEBELOW',(0,-1),(-1,-1),0.5,colors.HexColor('#cccccc')),
        ('LINEABOVE',(0,0),(-1,0),0.5,colors.HexColor('#cccccc')),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 8*mm))

    # Celkova castka - jednoducha
    amt_val = str(inv.get('amount_local', inv.get('amount_eur',0)))
    amt_sym = inv.get('currency_symbol', inv.get('currency_code','EUR'))
    amt_sub = '(' + str(inv.get('amount_eur',0)) + ' EUR)' if inv.get('currency_code','EUR') != 'EUR' else ''

    amt_tbl = Table([
        [s('CELKEM K UHRADE / TOTAL DUE', size=9, color=GRAY),
         s(amt_val + ' ' + amt_sym + ('  ' + amt_sub if amt_sub else ''), bold=True, size=16, align=TA_RIGHT)]
    ], colWidths=[80*mm, 90*mm])
    amt_tbl.setStyle(TableStyle([
        ('BOX',(0,0),(-1,-1),0.5,BLACK),
        ('TOPPADDING',(0,0),(-1,-1),10), ('BOTTOMPADDING',(0,0),(-1,-1),10),
        ('LEFTPADDING',(0,0),(-1,-1),8), ('RIGHTPADDING',(0,0),(-1,-1),8),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
    ]))
    story.append(amt_tbl)

    # Platebni udaje
    if co.get('company_bank') or co.get('company_iban'):
        story.append(Spacer(1, 6*mm))
        story.append(s('Platebni udaje / Payment details', size=8, color=GRAY))
        story.append(Spacer(1, 2*mm))
        if co.get('company_bank'):
            story.append(s('C. uctu / Account: ' + co['company_bank'], size=9))
        if co.get('company_iban'):
            story.append(s('IBAN: ' + co['company_iban'], size=9))

    # Footer
    story.append(Spacer(1, 10*mm))
    story.append(Table([['']], colWidths=[170*mm],
        style=[('LINEABOVE',(0,0),(-1,-1),0.5,GRAY),('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]))
    story.append(Spacer(1, 3*mm))
    story.append(s('Faktura vystavena systemem SmartestGuide  |  Dekujeme za spolupraci / Thank you  |  ' +
        co.get('company_email','support@smartestguide.com'), size=7, color=GRAY, align=TA_CENTER))

    doc.build(story)
    return buf.getvalue()



@app.get("/api/invoices/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: str, token: Optional[str] = None):
    """Stáhne PDF fakturu přímo — bez pop-upu."""
    db = db_load()
    inv = db.get("invoices", {}).get(invoice_id)
    if not inv:
        raise HTTPException(404, "Faktura nenalezena")
    if token:
        h = find_hotel_by_token(token)
        if not h or h["id"] != inv.get("hotel_id"):
            raise HTTPException(403, "Pristup odepren")
    s = db_get_settings()
    company = {k: s.get(k,'') for k in ['company_name','company_address','company_city','company_ico','company_dic','company_email','company_phone','company_bank','company_iban']}
    if not company['company_name']: company['company_name'] = 'SmartestGuide s.r.o.'
    if not company['company_city']: company['company_city'] = 'Praha, Česká republika'
    if not company['company_email']: company['company_email'] = 'support@smartestguide.com'

    pdf_bytes = generate_invoice_pdf_bytes(inv, company)
    filename = "faktura-" + inv.get("invoice_number","SG") + ".pdf"
    return StreamingResponse(
        BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="' + filename + '"'}
    )

# ─────────────────────────────────────────────
# Měny podle zemí
# ─────────────────────────────────────────────
COUNTRY_CURRENCY = {
    "CZ": ("CZK", "Kč"), "SK": ("EUR", "€"), "AT": ("EUR", "€"),
    "DE": ("EUR", "€"), "FR": ("EUR", "€"), "IT": ("EUR", "€"),
    "ES": ("EUR", "€"), "HR": ("EUR", "€"), "GR": ("EUR", "€"),
    "PL": ("PLN", "zł"), "HU": ("HUF", "Ft"), "PT": ("EUR", "€"),
    "GB": ("GBP", "£"), "US": ("USD", "$"), "CH": ("CHF", "Fr"),
    "NO": ("NOK", "kr"), "SE": ("SEK", "kr"), "RO": ("RON", "lei"),
    "TR": ("TRY", "₺"), "AE": ("AED", "د.إ"), "JP": ("JPY", "¥"),
}

EUR_TO = {
    "CZK": 25.0, "PLN": 4.3, "HUF": 390.0, "GBP": 0.86,
    "USD": 1.08, "CHF": 0.96, "NOK": 11.5, "SEK": 11.2,
    "RON": 5.0, "TRY": 35.0, "AED": 3.97, "JPY": 162.0,
}

def get_hotel_currency(hotel: dict) -> tuple:
    country = (hotel.get("country") or "").upper()
    return COUNTRY_CURRENCY.get(country, ("EUR", "€"))

def convert_eur(amount_eur: float, currency_code: str) -> float:
    if currency_code == "EUR":
        return amount_eur
    rate = EUR_TO.get(currency_code, 1.0)
    return round(amount_eur * rate, 2)

def calc_price_eur(beds: int, settings: dict = None) -> int:
    if settings is None:
        settings = db_get_settings()
    base = int(settings.get("price_base", 300))
    per_bed = int(settings.get("price_per_bed", 3))
    free_beds = int(settings.get("price_free_beds", 100))
    if not beds or beds <= 0:
        return base
    # Pokud má hotel locked-in cenu, použij ji
    return base if beds <= free_beds else base + (beds - free_beds) * per_bed

# ─────────────────────────────────────────────
# Fakturace – endpointy
# ─────────────────────────────────────────────

@app.get("/api/hotels/{hotel_id}/invoices")
def get_hotel_invoices(hotel_id: str):
    """Admin – seznam faktur hotelu."""
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    invoices = db.get("invoices", {})
    hotel_invoices = [v for v in invoices.values() if v.get("hotel_id") == hotel_id]
    hotel_invoices.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"status": "ok", "invoices": hotel_invoices}

@app.get("/api/invoices")
def get_all_invoices():
    """Admin – všechny faktury přehled."""
    db = db_load()
    invoices = list(db.get("invoices", {}).values())
    invoices.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    # Přidej jméno hotelu
    for inv in invoices:
        h = db["hotels"].get(inv.get("hotel_id", ""), {})
        inv["hotel_name"] = h.get("name", "–")
    return {"status": "ok", "invoices": invoices}

@app.post("/api/hotels/{hotel_id}/invoices/generate")
def generate_invoice(hotel_id: str):
    """Admin – manuálně vygeneruj fakturu pro hotel."""
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")

    currency_code, currency_symbol = get_hotel_currency(h)
    beds = h.get("bed_count", 0) or 0
    amount_eur = calc_price_eur(beds)
    amount_local = convert_eur(amount_eur, currency_code)

    now = datetime.utcnow()
    inv_number = f"SG-{now.strftime('%Y%m')}-{str(uuid.uuid4())[:6].upper()}"

    invoice = {
        "id": str(uuid.uuid4()),
        "invoice_number": inv_number,
        "hotel_id": hotel_id,
        "hotel_name": h.get("name", ""),
        "hotel_address": h.get("address", ""),
        "hotel_email": h.get("email", ""),
        "beds": beds,
        "amount_eur": amount_eur,
        "amount_local": amount_local,
        "currency_code": currency_code,
        "currency_symbol": currency_symbol,
        "period_from": now.strftime("%Y-%m-01"),
        "period_to": now.strftime(f"%Y-%m-{now.day:02d}"),
        "created_at": now.isoformat(),
        "status": "issued",  # issued | paid | cancelled
        "stripe_invoice_id": h.get("stripe_subscription_id", ""),
    }

    if "invoices" not in db:
        db["invoices"] = {}
    db["invoices"][invoice["id"]] = invoice
    db_save(db)
    return {"status": "ok", "invoice": invoice}

@app.patch("/api/invoices/{invoice_id}/status")
def update_invoice_status(invoice_id: str, status: str):
    """Admin – změň stav faktury (issued/paid/cancelled)."""
    db = db_load()
    if invoice_id not in db.get("invoices", {}):
        raise HTTPException(404, "Faktura nenalezena")
    db["invoices"][invoice_id]["status"] = status
    db["invoices"][invoice_id]["updated_at"] = datetime.utcnow().isoformat()
    if status == "paid":
        db["invoices"][invoice_id]["paid_at"] = datetime.utcnow().isoformat()
    db_save(db)
    return {"status": "ok", "invoice": db["invoices"][invoice_id]}

@app.get("/api/hotel-portal/invoices")
def portal_invoices(token: str):
    """Hotel portál – vlastní faktury."""
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    db = db_load()
    invoices = [v for v in db.get("invoices", {}).values() if v.get("hotel_id") == h["id"]]
    invoices.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"status": "ok", "invoices": invoices}

@app.get("/api/invoices/{invoice_id}/pdf-data")
def get_invoice_pdf_data(invoice_id: str, token: Optional[str] = None):
    """Vrátí data faktury pro PDF generování v browseru."""
    db = db_load()
    inv = db.get("invoices", {}).get(invoice_id)
    if not inv:
        raise HTTPException(404, "Faktura nenalezena")
    # Ověř token pokud zadán (hotelový přístup)
    if token:
        h = find_hotel_by_token(token)
        if not h or h["id"] != inv.get("hotel_id"):
            raise HTTPException(403, "Pristup odepren")
    s = db_get_settings()
    company = {
        "company_name": s.get("company_name", "SmartestGuide s.r.o."),
        "company_address": s.get("company_address", ""),
        "company_city": s.get("company_city", "Praha, Česká republika"),
        "company_ico": s.get("company_ico", ""),
        "company_dic": s.get("company_dic", ""),
        "company_email": s.get("company_email", "support@smartestguide.com"),
        "company_phone": s.get("company_phone", ""),
        "company_bank": s.get("company_bank", ""),
        "company_iban": s.get("company_iban", ""),
    }
    return {"status": "ok", "invoice": inv, "company": company}
