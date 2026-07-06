# pms.py — abstrakční vrstva pro napojení na hotelové systémy (PMS)
# Fáze 1: Apaleo (sandbox zdarma). Další PMS = další adapter se stejným rozhraním.
#
# Návrh (viz PLAN_PMS_NAPOJENI.md):
#   Guest → Alex → [tato vrstva] → adapter (apaleo | mews | ...) → PMS API
#   Výstup je vždy NORMALIZOVANÝ model pobytu (Stay) — Alexův prompt na PMS nezávisí.
#
# Zásady:
#   - Graceful degradace: jakákoli chyba => None, Alex jede dál jako FAQ (nikdy nesmí spadnout chat).
#   - Minimalizace dat: tahá se jen aktuální pobyt pro daný pokoj, nic se neukládá do DB.
#   - Credentials per hotel (pms_client_id/secret v hotelu, jen pro admin — nikdy ke guestům).

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

# ── Normalizovaný model pobytu ────────────────────────────────────────────────

@dataclass
class Stay:
    room: str                       # číslo/název pokoje (unit name)
    guest_name: str = ""            # jméno primárního hosta
    arrival: str = ""               # ISO datum příjezdu (YYYY-MM-DD)
    arrival_time: str = ""          # čas příjezdu z rezervace (HH:MM), "" = neznámo
    departure: str = ""             # ISO datum odjezdu (YYYY-MM-DD)
    departure_time: str = ""        # čas check-outu z rezervace (HH:MM), "" = neznámo
    nights: int = 0
    adults: int = 0
    children: int = 0
    rate_plan: str = ""             # název balíčku/sazby (např. "Wellness balíček")
    balance: str = ""               # zůstatek na účtu, např. "123.50 EUR" ("" = neznámo)
    status: str = ""                # InHouse / Confirmed / ...
    source: str = ""                # 'apaleo' | 'mews' | ...

def format_stay_block(stay: "Stay", include_balance: bool = True) -> str:
    """Blok do Alexova system promptu. Alex odpovídá jazykem hosta, blok je česky
    (stejně jako zbytek interních dat v promptu).
    include_balance=False → znalostní ověření (pokoj+datum příjezdu v chatu):
    útratu/zůstatek nesdělujeme, jen přes QR z pokoje nebo recepci."""
    lines = [
        "AKTUÁLNÍ POBYT HOSTA (z hotelového systému — používej pro personalizované odpovědi,",
        "ale NIKDY nesděluj údaje o pobytu, pokud se host nejdřív nezmíní, že bydlí na tomto pokoji):",
        f"- Pokoj: {stay.room}",
    ]
    if stay.guest_name: lines.append(f"- Host: {stay.guest_name}")
    if stay.arrival:    lines.append(f"- Příjezd: {stay.arrival}" + (f" od {stay.arrival_time}" if stay.arrival_time else ""))
    if stay.departure:  lines.append(f"- Odjezd (check-out): {stay.departure}" + (f" v {stay.departure_time}" if stay.departure_time else ""))
    lines.append("DŮLEŽITÉ: Časy z této rezervace mají PŘEDNOST před obecnými časy hotelu (check-in/check-out v profilu). Hostovi vždy říkej čas z jeho rezervace.")
    if stay.nights:     lines.append(f"- Počet nocí: {stay.nights}")
    if stay.adults or stay.children:
        lines.append(f"- Osoby: {stay.adults} dosp." + (f" + {stay.children} děti" if stay.children else ""))
    if stay.rate_plan:  lines.append(f"- Balíček/sazba: {stay.rate_plan}")
    if include_balance and stay.balance:
        lines.append(f"- Zůstatek na účtu pokoje: {stay.balance} (u dotazů na účet doporuč ověření na recepci)")
    if not include_balance:
        lines.append("POZN.: Údaje o účtu/útratě v tomto režimu NEMÁŠ a nesděluj je — u dotazů na účet odkaž hosta na recepci nebo na QR kartičku na pokoji.")
    lines.append("Pokud jsi dříve v této konverzaci uvedl jiné časy (obecné časy hotelu), tyto údaje z rezervace je NAHRAZUJÍ — odpovídej podle rezervace a případný rozpor krátce vysvětli (obecný čas hotelu vs. čas ve vaší rezervaci).")
    lines.append("PRAVIDLO PŘESNOSTI PRO POBYT: Odpovídej VÝHRADNĚ z údajů uvedených výše. Pokud se host ptá na detail pobytu, který tu není (např. co přesně zahrnuje balíček, cena, platby, změna rezervace), NIKDY ho nedomýšlej — řekni, že tuto informaci nemáš, a odkaž na recepci. Změny rezervace (prodloužení, pozdní check-out) NIKDY nepotvrzuj — jen předej kontakt na recepci.")
    return "\n".join(lines)

# ── Dispatcher ────────────────────────────────────────────────────────────────

async def get_stay_for_room(hotel: dict, room: str) -> Optional[Stay]:
    """Najde aktuální pobyt pro daný pokoj podle PMS konfigurace hotelu.
    Vrací None, když PMS není nastavené, pokoj nemá rezervaci, nebo cokoli selže."""
    room = (room or "").strip()
    if not room:
        return None
    pms_type = (hotel.get("pms_type") or "").strip().lower()
    try:
        if pms_type == "apaleo":
            return await _apaleo_get_stay(hotel, room)
        # further adapters: elif pms_type == "mews": ...
    except Exception as e:
        logging.warning("PMS lookup selhal (%s, pokoj %s): %s", pms_type, room, e)
    return None

# ── Apaleo adapter ────────────────────────────────────────────────────────────
# Docs: https://apaleo.dev — OAuth2 client_credentials, scope reservations.read
# POZN.: přesné tvary odpovědí ověřit proti sandboxu (fáze testování).

_APALEO_TOKEN_URL = "https://identity.apaleo.com/connect/token"
_APALEO_API = "https://api.apaleo.com"
_token_cache: dict = {}   # client_id -> {"token": str, "expires": epoch}

async def _apaleo_token(client_id: str, client_secret: str) -> Optional[str]:
    now = time.time()
    cached = _token_cache.get(client_id)
    if cached and cached["expires"] > now + 30:
        return cached["token"]
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.post(
            _APALEO_TOKEN_URL,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials"},
        )
    if r.status_code != 200:
        logging.warning("Apaleo token selhal: %s %s", r.status_code, r.text[:150])
        return None
    d = r.json()
    token = d.get("access_token")
    _token_cache[client_id] = {"token": token, "expires": now + int(d.get("expires_in", 3600))}
    return token

def _apaleo_normalize(res: dict) -> Stay:
    guest = res.get("primaryGuest") or {}
    name = " ".join(x for x in (guest.get("firstName"), guest.get("lastName")) if x)
    bal = res.get("balance") or {}
    balance = f'{bal.get("amount")} {bal.get("currency")}' if bal.get("amount") is not None else ""
    arrival_raw = res.get("arrival") or ""
    departure_raw = res.get("departure") or ""
    arrival = arrival_raw[:10]
    departure = departure_raw[:10]
    # ISO "2026-07-08T10:00:00+02:00" → "10:00" (lokální čas property, jak ho vrací PMS)
    arrival_time = arrival_raw[11:16] if len(arrival_raw) >= 16 else ""
    departure_time = departure_raw[11:16] if len(departure_raw) >= 16 else ""
    nights = 0
    try:
        from datetime import date
        if arrival and departure:
            nights = (date.fromisoformat(departure) - date.fromisoformat(arrival)).days
    except Exception:
        pass
    return Stay(
        room=(res.get("unit") or {}).get("name", ""),
        guest_name=name,
        arrival=arrival,
        arrival_time=arrival_time,
        departure=departure,
        departure_time=departure_time,
        nights=nights,
        adults=res.get("adults") or 0,
        children=len(res.get("childrenAges") or []),
        rate_plan=((res.get("ratePlan") or {}).get("name") or ""),
        balance=balance,
        status=res.get("status") or "",
        source="apaleo",
    )

async def apaleo_refresh_access_token(client_id: str, client_secret: str, refresh_token: str):
    """Connect (OAuth) flow: vymění refresh token za nový access + refresh token.
    Vrací (access_token, new_refresh_token) nebo (None, None)."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.post(
            _APALEO_TOKEN_URL,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    if r.status_code != 200:
        logging.warning("Apaleo refresh token selhal: %s %s", r.status_code, r.text[:150])
        return None, None
    d = r.json()
    return d.get("access_token"), d.get("refresh_token")

async def _apaleo_get_stay(hotel: dict, room: str) -> Optional[Stay]:
    property_id = (hotel.get("pms_property_id") or "").strip()
    if not property_id:
        return None
    token = None
    # 1) Connect (OAuth) režim — hotel připojený přes Apaleo Store / tlačítko v portálu
    if hotel.get("pms_refresh_token") and hotel.get("_apaleo_app_client_id"):
        token, new_rt = await apaleo_refresh_access_token(
            hotel["_apaleo_app_client_id"], hotel.get("_apaleo_app_client_secret", ""),
            hotel["pms_refresh_token"])
        if new_rt and new_rt != hotel.get("pms_refresh_token"):
            # rotace refresh tokenu — volající (app.py) ho po požadavku uloží
            hotel["_new_refresh_token"] = new_rt
    # 2) Custom app režim — ručně zadané client credentials per hotel
    if not token:
        client_id = (hotel.get("pms_client_id") or "").strip()
        client_secret = (hotel.get("pms_client_secret") or "").strip()
        if not (client_id and client_secret):
            return None
        token = await _apaleo_token(client_id, client_secret)
    if not token:
        return None
    # Ubytovaní hosté (InHouse) pro danou property; pokoj filtrujeme lokálně dle unit.name
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(
            f"{_APALEO_API}/booking/v1/reservations",
            headers={"Authorization": f"Bearer {token}"},
            params={"propertyIds": property_id, "status": "InHouse", "pageSize": 200},
        )
    if r.status_code != 200:
        logging.warning("Apaleo reservations selhal: %s %s", r.status_code, r.text[:150])
        return None
    room_l = room.lower()
    for res in (r.json().get("reservations") or []):
        unit_name = ((res.get("unit") or {}).get("name") or "").strip().lower()
        if unit_name == room_l:
            return _apaleo_normalize(res)
    return None
