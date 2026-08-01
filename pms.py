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
import re
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
    unit_group: str = ""            # typ pokoje (unit group name) — pro nabídku prodloužení
    status: str = ""                # InHouse / Confirmed / ...
    source: str = ""                # 'apaleo' | 'mews' | ...

def format_stay_block(stay: "Stay") -> str:
    """Blok do Alexova system promptu. Alex odpovídá jazykem hosta, blok je česky
    (stejně jako zbytek interních dat v promptu).
    POZN.: Údaje o účtu/útratě/zůstatku z PMS NEČTEME (scope = jen reservations.read).
    U jakéhokoli dotazu na účet/platby Alex vždy odkáže hosta na recepci."""
    lines = [
        "AKTUÁLNÍ POBYT HOSTA (z hotelového systému — používej pro personalizované odpovědi,",
        "ale NIKDY nesděluj údaje o pobytu, pokud se host nejdřív nezmíní, že bydlí na tomto pokoji):",
        f"- Pokoj: {stay.room}",
    ]
    if stay.guest_name: lines.append(f"- Host: {stay.guest_name}")
    if stay.arrival:    lines.append(f"- Příjezd: {stay.arrival}" + (f" od {stay.arrival_time}" if stay.arrival_time else ""))
    if stay.departure:  lines.append(f"- Odjezd (check-out): {stay.departure}" + (f" v {stay.departure_time}" if stay.departure_time else ""))
    lines.append("DŮLEŽITÉ: Časy z této rezervace mají PŘEDNOST před obecnými časy hotelu (check-in/check-out v profilu). Hostovi vždy říkej čas z jeho rezervace.")
    lines.append("PLATÍ TO I PRO OBECNÉ DOTAZY: když se host zeptá obecně (např. Jaké jsou časy check-in/check-out? / Kdy je check-out?) nebo klikne na rychlou volbu s časy, odpověz PŘEDEVŠÍM konkrétním časem z JEHO rezervace (např. Váš check-out je 8. 7. v 10:00); obecné časy hotelu můžeš doplnit nanejvýš jako vedlejší poznámku. Nikdy neodpovídej jen obecnými časy, když má host propojený pobyt.")
    if stay.nights:     lines.append(f"- Počet nocí: {stay.nights}")
    if stay.adults or stay.children:
        lines.append(f"- Osoby: {stay.adults} dosp." + (f" + {stay.children} děti" if stay.children else ""))
    if stay.rate_plan:  lines.append(f"- Balíček/sazba: {stay.rate_plan}")
    lines.append("POZN.: Údaje o účtu, útratě, zůstatku a platbách NEMÁŠ k dispozici a nesděluj je — u jakéhokoli dotazu na účet/platby odkaž hosta na recepci.")
    lines.append("Pokud jsi dříve v této konverzaci uvedl jiné časy (obecné časy hotelu), tyto údaje z rezervace je NAHRAZUJÍ — odpovídej podle rezervace a případný rozpor krátce vysvětli (obecný čas hotelu vs. čas ve vaší rezervaci).")
    lines.append("PRAVIDLO PŘESNOSTI PRO POBYT: Odpovídej VÝHRADNĚ z údajů uvedených výše. Pokud se host ptá na detail pobytu, který tu není (např. co přesně zahrnuje balíček, cena, platby, změna rezervace), NIKDY ho nedomýšlej — řekni, že tuto informaci nemáš, a odkaž na recepci. Změny rezervace NIKDY sám nepotvrzuj — potvrzuje je vždy recepce. VÝJIMKA: pokud máš níže blok PRODLOUŽENÍ POBYTU s živou dostupností a cenou, smíš tyto údaje hostovi sdělit (přesně, beze změny) — s dovětkem, že rezervaci prodloužení potvrdí recepce.")
    return "\n".join(lines)

def format_extension_block(ext: dict) -> str:
    """Blok PRODLOUŽENÍ POBYTU do promptu — z živých dat offers.read. Cena doslova z API."""
    if not ext:
        return ""
    if not ext.get("available"):
        return ("PRODLOUŽENÍ POBYTU (živá data z hotelového systému): Na termín "
                f"{ext.get('from','')} až {ext.get('to','')} NENÍ volná kapacita. "
                "Hostovi to řekni a doporuč obrátit se na recepci — někdy umí najít řešení "
                "(jiný typ pokoje, čekací listina).")
    price = ext.get("price")
    cur = ext.get("currency") or ""
    lines = [
        "PRODLOUŽENÍ POBYTU (živá data z hotelového systému — smíš je hostovi sdělit):",
        f"- Termín: {ext.get('from','')} → {ext.get('to','')} ({ext.get('nights',1)} noc/noci)",
        f"- Cena: {price} {cur} (uváděj PŘESNĚ tuto částku, nic nedopočítávej ani nezaokrouhluj)",
    ]
    if ext.get("unit_group"):
        lines.append(f"- Typ pokoje: {ext['unit_group']}"
                     + ("" if ext.get("same_room_type") else " (POZOR: jiný typ než hostův současný pokoj — zmiň to)"))
    if ext.get("rate_plan"):
        lines.append(f"- Sazba: {ext['rate_plan']}")
    lines.append("DŮLEŽITÉ: Toto je informativní nabídka. Rezervaci prodloužení potvrzuje VÝHRADNĚ recepce — "
                 "hosta vždy vyzvi, ať se pro potvrzení zastaví na recepci nebo zavolá. Ty sám NIC nerezervuješ.")
    return "\n".join(lines)

def format_services_block(services: list) -> str:
    """Blok SLUŽBY HOTELU do promptu — nabídka z PMS (availability.read)."""
    if not services:
        return ""
    lines = ["SLUŽBY HOTELU K DOKOUPENÍ (živá nabídka z hotelového systému):"]
    for s in services[:12]:
        row = f"- {s.get('name','')}"
        if s.get("price") is not None:
            row += f" — {s['price']} {s.get('currency','')}"
        if s.get("description"):
            row += f" ({s['description'][:120]})"
        lines.append(row)
    lines.append("Tyto služby přirozeně nabídni, když se hodí k tématu (snídaně, wellness, pozdní check-out…) — "
                 "jako přátelský tip concierge, ne jako reklamu. Ceny uváděj přesně. Objednání vyřídí recepce.")
    return "\n".join(lines)

# ── Tolerantní párování čísla pokoje ─────────────────────────────────────────
# Apaleo má pokoj např. jako "3.008" — host ale z kartičky přečte "3008" či "308".
# Přesná shoda by selhala, přestože pokoj i rezervace existují.

def _room_keys(name: str) -> set:
    """Vygeneruje množinu normalizovaných variant názvu pokoje pro volné porovnání.
    '3.008' → {'3.008', '3008', '308', '38'}: lowercase, rozpad na segmenty podle
    oddělovačů (tečka/mezera/pomlčka/podtržítko/lomítko), u číselných segmentů
    varianty s postupně odstraněnými vodicími nulami, slepení bez oddělovačů."""
    s = (name or "").strip().lower()
    if not s:
        return set()
    keys = {s}
    segments = [seg for seg in re.split(r"[.\s\-_/]+", s) if seg]
    if not segments:
        return keys
    variant_lists = []
    for seg in segments:
        variants = {seg}
        if seg.isdigit():
            t = seg
            while len(t) > 1 and t.startswith("0"):
                t = t[1:]
                variants.add(t)
        variant_lists.append(variants)
    combos = {""}
    for variants in variant_lists:
        combos = {c + v for c in combos for v in variants}
    keys |= combos
    return keys

def _room_match(unit_name: str, guest_room: str) -> bool:
    """Volné porovnání názvu pokoje (True = pravděpodobně tentýž pokoj)."""
    return bool(_room_keys(unit_name) & _room_keys(guest_room))

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
        hotel["_pms_fail"] = ("exception: " + str(e))[:120]  # monitoring: technické selhání
    return None

# ── Apaleo adapter ────────────────────────────────────────────────────────────
# Docs: https://apaleo.dev — scope: reservations.read (+ offline_access).
# Dva režimy získání access tokenu (viz _apaleo_get_stay):
#   1) Connect (Apaleo Store) — Authorization Code Grant + refresh_token s rotací
#      (apaleo_refresh_access_token). PRIMÁRNÍ režim pro připojené hotely = ten certifikovaný.
#   2) Custom app — per-hotel client_credentials (_apaleo_token), fallback pro
#      hotely s ručně zadanými vlastními Apaleo credentials.
# POZN.: přesné tvary odpovědí ověřit proti sandboxu (fáze testování).

_APALEO_TOKEN_URL = "https://identity.apaleo.com/connect/token"
_APALEO_API = "https://api.apaleo.com"
_token_cache: dict = {}          # client_id -> {"token": str, "expires": epoch}  (client_credentials režim)
_connect_token_cache: dict = {}  # hotel_id  -> {"token": str, "expires": epoch}  (Connect/OAuth režim)

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
        unit_group=((res.get("unitGroup") or {}).get("name") or ""),
        status=res.get("status") or "",
        source="apaleo",
    )

async def apaleo_refresh_access_token(client_id: str, client_secret: str, refresh_token: str):
    """Connect (OAuth) flow: vymění refresh token za nový access + refresh token.
    Vrací (access_token, new_refresh_token, expires_in_s) nebo (None, None, 0)."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.post(
            _APALEO_TOKEN_URL,
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
    if r.status_code != 200:
        logging.warning("Apaleo refresh token selhal: %s %s", r.status_code, r.text[:150])
        return None, None, 0
    d = r.json()
    return d.get("access_token"), d.get("refresh_token"), int(d.get("expires_in", 3600))

async def _apaleo_access_token(hotel: dict) -> Optional[str]:
    """Získá access token pro hotel — sdílené všemi Apaleo čteními.
    1) Connect (OAuth, Apaleo Store) s cache do vypršení; 2) fallback client_credentials."""
    token = None
    # 1) Connect (OAuth) režim — hotel připojený přes Apaleo Store / tlačítko v portálu
    if hotel.get("pms_refresh_token") and hotel.get("_apaleo_app_client_id"):
        cache_key = (hotel.get("id") or "") + ":" + (hotel.get("pms_property_id") or "")
        now = time.time()
        cached = _connect_token_cache.get(cache_key)
        if cached and cached["expires"] > now + 60:
            return cached["token"]
        token, new_rt, expires_in = await apaleo_refresh_access_token(
            hotel["_apaleo_app_client_id"], hotel.get("_apaleo_app_client_secret", ""),
            hotel["pms_refresh_token"])
        if token:
            _connect_token_cache[cache_key] = {"token": token, "expires": now + expires_in}
        if new_rt and new_rt != hotel.get("pms_refresh_token"):
            # rotace refresh tokenu — volající (app.py) ho po požadavku uloží
            hotel["_new_refresh_token"] = new_rt
    # 2) Custom app režim — ručně zadané client credentials per hotel
    if not token:
        client_id = (hotel.get("pms_client_id") or "").strip()
        client_secret = (hotel.get("pms_client_secret") or "").strip()
        if client_id and client_secret:
            token = await _apaleo_token(client_id, client_secret)
    return token

def _drop_connect_token(hotel: dict):
    """Zahodí cachovaný Connect access token (po 401/403 — token zneplatněn)."""
    _connect_token_cache.pop((hotel.get("id") or "") + ":" + (hotel.get("pms_property_id") or ""), None)

async def _apaleo_get(hotel: dict, path: str, params: dict, quiet: bool = False):
    """GET na Apaleo API s tokenem hotelu. Vrací JSON dict nebo None.
    quiet=True: 403 (chybějící scope u starého souhlasu) NEoznačuje _pms_fail —
    nesmí spouštět alert 'PMS selhává', jádro (rezervace) může fungovat dál."""
    token = await _apaleo_access_token(hotel)
    if not token:
        if not quiet:
            hotel["_pms_fail"] = "token"
        return None
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(f"{_APALEO_API}{path}",
                             headers={"Authorization": f"Bearer {token}"}, params=params)
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return None
    if r.status_code in (401, 403):
        _drop_connect_token(hotel)
    if not quiet:
        logging.warning("Apaleo GET %s selhal: %s %s", path, r.status_code, r.text[:150])
        hotel["_pms_fail"] = f"http {r.status_code}"
    else:
        # WARNING schválně (Railway INFO aplikace nezobrazuje); díky negativním cache
        # se to neopakuje častěji než 1× za ~10 min na hotel. _pms_fail se u quiet NEnastavuje.
        logging.warning("Apaleo GET %s (quiet) selhal: %s %s", path, r.status_code, r.text[:200])
    return None

# ── setup.read: properties, units, časy check-in/out ─────────────────────────

_setup_cache: dict = {}      # (hotel_id, property_id) -> {"data": dict, "expires": epoch}
_services_cache: dict = {}   # (hotel_id, property_id) -> {"data": list, "expires": epoch}

async def apaleo_list_properties(hotel: dict) -> Optional[list]:
    """Seznam properties účtu (pro dropdown v portálu). Bez zvláštního scope.
    Vrací [{code, name, status}] nebo None při selhání."""
    d = await _apaleo_get(hotel, "/inventory/v1/properties", {"pageSize": 500}, quiet=True)
    if not d:
        return None
    out = []
    for p in d.get("properties") or []:
        code = p.get("id") or p.get("code") or ""
        name = p.get("name") or ""
        if isinstance(name, dict):  # multijazyčné jméno → první hodnota
            name = next(iter(name.values()), "")
        if code:
            out.append({"code": code, "name": str(name), "status": p.get("status") or ""})
    return out

async def apaleo_get_setup(hotel: dict, property_id: str = "") -> Optional[dict]:
    """Konfigurace property (scope setup.read): seznam units, počet lůžek,
    check-in/out časy z time slice definitions. Cache 10 min. Vrací None při selhání
    (typicky starý souhlas bez setup.read) — volající musí umět jet bez toho."""
    pid = (property_id or hotel.get("pms_property_id") or "").strip()
    if not pid:
        return None
    ck = ((hotel.get("id") or ""), pid)
    now = time.time()
    c = _setup_cache.get(ck)
    if c and c["expires"] > now:
        return c["data"]
    units_d = await _apaleo_get(hotel, "/inventory/v1/units",
                                {"propertyId": pid, "pageSize": 500}, quiet=True)
    if units_d is None:
        # Negativní cache: starý souhlas bez setup.read by jinak zkoušel units
        # při každé zprávě hosta znovu (latence + zbytečná volání)
        _setup_cache[ck] = {"data": None, "expires": now + 600}
        return None
    units = []
    for u in units_d.get("units") or []:
        if u.get("name"):
            units.append({"id": u.get("id") or "", "name": str(u.get("name")),
                          "maxPersons": int(u.get("maxPersons") or 0)})
    # Lůžka: součet maxPersons unit-groups typu BedRoom (units × kapacita);
    # fallback: součet maxPersons přes units.
    beds = 0
    ug_d = await _apaleo_get(hotel, "/inventory/v1/unit-groups",
                             {"propertyId": pid, "unitGroupTypes": "BedRoom", "pageSize": 500}, quiet=True)
    if ug_d and (ug_d.get("unitGroups") or []):
        try:
            bed_ids = {g.get("id") for g in ug_d["unitGroups"]}
            per_group = {g.get("id"): int(g.get("maxPersons") or 0) for g in ug_d["unitGroups"]}
            for u in units_d.get("units") or []:
                gid = (u.get("unitGroup") or {}).get("id") or u.get("unitGroupId")
                if gid in bed_ids:
                    beds += int(u.get("maxPersons") or per_group.get(gid, 0) or 0)
        except Exception:
            beds = 0
    if not beds:
        beds = sum(u["maxPersons"] for u in units)
    # Check-in/out časy z time slice definitions (OverNight šablona)
    checkin, checkout = "", ""
    ts_d = await _apaleo_get(hotel, f"/settings/v1/properties/{pid}/time-slice-definitions",
                             {}, quiet=True)
    for ts in (ts_d or {}).get("timeSliceDefinitions") or []:
        if (ts.get("template") or "") in ("OverNight", ""):
            checkin = (ts.get("checkInTime") or "")[:5]    # "15:00:00" → "15:00"
            checkout = (ts.get("checkOutTime") or "")[:5]
            if checkin or checkout:
                break
    data = {"property_id": pid, "units": units, "unit_count": len(units),
            "beds": beds, "checkin_time": checkin, "checkout_time": checkout}
    _setup_cache[ck] = {"data": data, "expires": now + 600}
    return data

async def apaleo_validate_property(hotel: dict, property_code: str) -> dict:
    """Validace property kódu při uložení: existuje v účtu? Vrací
    {valid: bool, reason: str, name: str}. Když seznam properties nejde načíst,
    vrací valid=True s reason='unverified' (nebránit uložení kvůli výpadku)."""
    code = (property_code or "").strip()
    if not code:
        return {"valid": False, "reason": "empty", "name": ""}
    props = await apaleo_list_properties(hotel)
    if props is None:
        return {"valid": True, "reason": "unverified", "name": ""}
    for p in props:
        if p["code"].strip().lower() == code.lower():
            return {"valid": True, "reason": "ok", "name": p["name"]}
    return {"valid": False, "reason": "not_found", "name": "",
            "available": [p["code"] for p in props][:20]}

# ── availability/offers.read: prodloužení pobytu + služby ────────────────────

async def apaleo_extension_offer(hotel: dict, stay: "Stay", nights: int = 1) -> Optional[dict]:
    """Živá nabídka prodloužení: od check-outu hosta o N nocí. Scope offers.read.
    Vrací {available, price, currency, rate_plan, unit_group, from, to, nights}
    nebo None (chyba/chybějící scope). Cena se NIKDY nedopočítává — jen doslova z API."""
    pid = (hotel.get("pms_property_id") or "").strip()
    if not (pid and stay and stay.departure):
        return None
    try:
        from datetime import date, timedelta
        d_from = date.fromisoformat(stay.departure)
        d_to = d_from + timedelta(days=max(1, min(nights, 7)))
    except Exception:
        return None
    d = await _apaleo_get(hotel, "/booking/v1/offers",
                          {"propertyId": pid, "arrival": d_from.isoformat(),
                           "departure": d_to.isoformat(),
                           "adults": max(1, stay.adults or 1),
                           "timeSliceTemplate": "OverNight"}, quiet=True)
    if d is None:
        return None
    offers = d.get("offers") or []
    if not offers:
        return {"available": False, "from": d_from.isoformat(), "to": d_to.isoformat(),
                "nights": (d_to - d_from).days}
    # Preferuj nabídku pro stejný typ pokoje (unit group) jako má host; jinak nejlevnější
    def _amount(o):
        tg = o.get("totalGrossAmount") or {}
        try:
            return float(tg.get("amount"))
        except (TypeError, ValueError):
            return float("inf")
    same_group = [o for o in offers
                  if stay.unit_group and ((o.get("unitGroup") or {}).get("name") or "") == stay.unit_group]
    pick = min(same_group or offers, key=_amount)
    tg = pick.get("totalGrossAmount") or {}
    if tg.get("amount") is None:
        return None
    return {"available": True,
            "price": tg.get("amount"), "currency": tg.get("currency") or "",
            "rate_plan": ((pick.get("ratePlan") or {}).get("name") or ""),
            "unit_group": ((pick.get("unitGroup") or {}).get("name") or ""),
            "same_room_type": bool(same_group),
            "from": d_from.isoformat(), "to": d_to.isoformat(),
            "nights": (d_to - d_from).days}

async def apaleo_available_services(hotel: dict) -> Optional[list]:
    """Dostupné služby hotelu (snídaně, wellness, late check-out…) — scope availability.read.
    Cache 1 h per hotel+property. Vrací [{name, description, price, currency}] nebo None."""
    pid = (hotel.get("pms_property_id") or "").strip()
    if not pid:
        return None
    ck = ((hotel.get("id") or ""), pid)
    now = time.time()
    c = _services_cache.get(ck)
    if c and c["expires"] > now:
        return c["data"]
    try:
        from datetime import date, timedelta
        d_from = date.today().isoformat()
        d_to = (date.today() + timedelta(days=1)).isoformat()
    except Exception:
        return None
    # API může vyžadovat datum, nebo plný date-time (ISO8601) — zkus obojí
    d = await _apaleo_get(hotel, "/availability/v1/services",
                          {"propertyId": pid, "from": d_from, "to": d_to,
                           "pageSize": 100}, quiet=True)
    if d is None:
        d = await _apaleo_get(hotel, "/availability/v1/services",
                              {"propertyId": pid, "from": d_from + "T00:00:00Z",
                               "to": d_to + "T00:00:00Z", "pageSize": 100}, quiet=True)
    if d is None:
        # Negativní cache — bez availability.read (starý souhlas) nezkoušet každou zprávu
        _services_cache[ck] = {"data": None, "expires": now + 600}
        return None
    out, seen = [], set()
    # Tvar odpovědi se liší dle verze — projdi timeSlices (položka může BÝT service záznam
    # s vnořeným "service", nebo obsahovat pole services[]) i ploché services[]
    buckets = []
    for ts in d.get("timeSlices") or []:
        if isinstance(ts.get("service"), dict):
            buckets.append(ts)
        buckets += ts.get("services") or []
    buckets += d.get("services") or []
    for item in buckets:
        svc = item.get("service") if isinstance(item.get("service"), dict) else item
        name = str(svc.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        price_obj = (svc.get("defaultGrossPrice") or item.get("price")
                     or item.get("grossAmount") or {})
        desc = str(svc.get("description") or "").strip()
        out.append({"name": name, "description": desc[:200],
                    "price": price_obj.get("amount"),
                    "currency": price_obj.get("currency") or ""})
        if len(out) >= 12:
            break
    if not out:
        # Diagnostika: API odpovědělo, ale nic jsme nevyparsovali → zaloguj tvar odpovědi
        # (WARNING, aby to bylo vidět v Railway; jakmile services pojedou, tahle větev zmlkne)
        logging.warning("Apaleo services: 200 OK, ale 0 služeb — klíče odpovědi: %s | ukázka: %s",
                        list(d.keys())[:8], str(d)[:500])
    else:
        logging.warning("Apaleo services: načteno %d služeb (%s)", len(out),
                        ", ".join(s["name"] for s in out[:5]))
    # Úspěch s výsledky drž 1 h; prázdný výsledek jen 10 min (ať oprava/nová služba naskočí dřív)
    _services_cache[ck] = {"data": out, "expires": now + (3600 if out else 600)}
    return out

async def _apaleo_get_stay(hotel: dict, room: str) -> Optional[Stay]:
    property_id = (hotel.get("pms_property_id") or "").strip()
    if not property_id:
        return None
    token = await _apaleo_access_token(hotel)
    if not token:
        hotel["_pms_fail"] = "token"   # monitoring: nepodařilo se získat access token
        return None
    # Server-side filtr dle unit (setup.read): pokoj → unit id(s) přes reálný seznam units.
    # Řeší velké properties (>200 InHouse rezervací = mimo první stránku) a umožní
    # rozlišit „pokoj neexistuje" od „pokoj je prázdný". Bez setup.read (starý souhlas)
    # tiše spadne na původní lokální filtrování.
    params = {"propertyIds": property_id, "status": "InHouse", "pageSize": 200}
    unit_ids = []
    try:
        setup = await apaleo_get_setup(hotel)
        if setup and setup.get("units"):
            exact = [u for u in setup["units"] if u["name"].strip().lower() == room.strip().lower()]
            loose = exact or [u for u in setup["units"] if _room_match(u["name"], room)]
            unit_ids = [u["id"] for u in loose if u.get("id")][:5]
            if not loose:
                hotel["_room_not_found"] = True  # pokoj v property neexistuje (překlep)
                return None
    except Exception as e:
        logging.info("Apaleo setup lookup přeskočen: %s", e)
    if unit_ids:
        params["unitIds"] = unit_ids
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(
            f"{_APALEO_API}/booking/v1/reservations",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
    if r.status_code != 200:
        if r.status_code in (401, 403):
            # token mezitím zneplatněn (např. hotel odpojil app) → zahodit z cache
            _drop_connect_token(hotel)
        logging.warning("Apaleo reservations selhal: %s %s", r.status_code, r.text[:150])
        hotel["_pms_fail"] = f"http {r.status_code}"   # monitoring: API vrátilo chybu
        return None
    hotel["_pms_fail"] = ""   # monitoring: spojení OK (i když pokoj třeba nemá rezervaci)
    reservations = r.json().get("reservations") or []
    room_l = room.strip().lower()
    # 1) přesná shoda má přednost (kdyby volné porovnání sedělo na víc pokojů)
    for res in reservations:
        unit_name = ((res.get("unit") or {}).get("name") or "").strip().lower()
        if unit_name == room_l:
            return _apaleo_normalize(res)
    # 2) tolerantní shoda — host vynechal tečku/oddělovače nebo vodicí nuly ("3008", "308" ~ "3.008")
    for res in reservations:
        unit_name = ((res.get("unit") or {}).get("name") or "")
        if unit_name and _room_match(unit_name, room):
            return _apaleo_normalize(res)
    return None
