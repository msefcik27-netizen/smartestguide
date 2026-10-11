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

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

# Stránkování Apaleo API. Certifikační podmínka Apaleo zní doslova „Your integration
# uses pageSize=200" (apaleo.dev → Certify your app), a 200 je zároveň jejich maximum.
# Dřív tu bylo 500 (properties, units, unit-groups) a 100 (services) — pětistovka se
# stejně ořízla na 200 a stovka znamenala zbytečně malé stránky.
# POZOR: nikde se nestránkuje, bere se jen první stránka. U property s víc než
# 200 jednotkami se tedy zbytek neuvidí — platilo to i před touhle změnou, jen to
# nebylo vidět. Až bude na pořadu dne velký hotel, dodělat průchod přes `nextPageToken`.
_PAGE_SIZE = 200

# Smí osobní údaje hosta z PMS (jméno z primaryGuest) do promptu jazykového modelu?
# Dnes je aplikace v Apaleu nastavená na PII „Omit all", takže primaryGuest chodí
# prázdný a tenhle gate nic nemění. Jakmile se PII zapne (nutné pro ověřovací kód
# e-mailem u zápisu do rezervace — viz APALEO_ZAPIS_DO_REZERVACE_NAVRH.md), začalo
# by jméno téct do promptu samo, bez jediné změny v kódu. Rozhodnutí z 9. 10. 2026:
# PII slouží VÝHRADNĚ k ověření hosta, do modelu nevstupuje. Zapnout jen vědomě.
_PII_TO_PROMPT = False

# ── Normalizovaný model pobytu ────────────────────────────────────────────────

@dataclass
class Stay:
    room: str                       # číslo/název pokoje (unit name)
    reservation_id: str = ""        # id rezervace v PMS — adresuje zápisové akce (book-service, amend)
    guest_name: str = ""            # jméno primárního hosta (do promptu jen při _PII_TO_PROMPT)
    arrival: str = ""               # ISO datum příjezdu (YYYY-MM-DD)
    arrival_time: str = ""          # čas příjezdu z rezervace (HH:MM), "" = neznámo
    departure: str = ""             # ISO datum odjezdu (YYYY-MM-DD)
    departure_time: str = ""        # čas check-outu z rezervace (HH:MM), "" = neznámo
    nights: int = 0
    adults: int = 0
    children: int = 0
    children_ages: list = field(default_factory=list)  # věky dětí — amend je chce zpět, jinak by je zahodil
    arrival_iso: str = ""           # příjezd přesně tak, jak ho vrátil PMS (datum+čas+zóna) — pro amend
    departure_iso: str = ""         # odjezd přesně z PMS — pro amend a per-reservation offers
    rate_plan: str = ""             # název balíčku/sazby (např. "Wellness balíček")
    rate_plan_id: str = ""          # id sazby — pro service-offers (upsell služeb na míru pobytu)
    unit_group: str = ""            # typ pokoje (unit group name) — pro nabídku prodloužení
    unit_group_id: str = ""         # id typu pokoje — porovnávání nabídek podle id, ne jména
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
    if _PII_TO_PROMPT and stay.guest_name: lines.append(f"- Host: {stay.guest_name}")
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
    if isinstance(price, (int, float)):
        price = f"{price:g}"   # 97, ne 97.0 (Alex jinak říká „97.0 GBP")
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
    # Hotel může mít zapnutý zápis prodloužení (karta s tlačítkem, blok OBJEDNÁNÍ V CHATU) — pak se
    # prodlužuje tlačítkem. Dřív tu stálo „potvrzuje VÝHRADNĚ recepce" a Alex posílal hosta na recepci
    # i tam, kde šlo prodloužit v chatu (nález 11. 10. 2026).
    lines.append("DŮLEŽITÉ: Toto je informativní cena. Pokud máš v promptu blok OBJEDNÁNÍ V CHATU s prodloužením, "
                 "host ho potvrdí tlačítkem pod tvou odpovědí — řiď se tím blokem. Pokud ho nemáš, prodloužení "
                 "v chatu potvrdit nejde: vyzvi hosta, ať se obrátí na recepci. Ty sám NIC nerezervuješ.")
    return "\n".join(lines)

def format_services_block(services: list, nights: int = 0) -> str:
    """Blok SLUŽBY HOTELU do promptu — nabídka z PMS (service-offers na míru pobytu).
    nights = počet nocí pobytu → umožní přesný popisek periodicity („za osobu a noc"
    vs. „jednorázově") místo Alexova domýšlení."""
    if not services:
        return ""
    _UNIT_LBL = {"Person": "za osobu", "Room": "za pokoj"}
    lines = ["SLUŽBY HOTELU K DOKOUPENÍ (živá nabídka z hotelového systému, na míru pobytu hosta):"]
    for s in services[:12]:
        row = f"- {s.get('name','')}"
        cur = s.get("currency", "")
        if s.get("price") is not None:
            row += f" — {s['price']} {cur}"
            u = _UNIT_LBL.get(s.get("pricing_unit") or "")
            if u and not s.get("price_is_total"):
                row += f" {u}"
            # Periodicita PŘESNĚ z čísel (nedomýšlet!): součet = jednotka × noci → „a noc";
            # součet = jednotka → jednorázově; jinak periodicitu vůbec neuvádět.
            _tot, _p = s.get("price_total"), s.get("price")
            if not s.get("price_is_total") and _tot is not None and _p is not None:
                try:
                    if nights and abs(float(_tot) - float(_p) * nights) < 0.05:
                        row += " a noc"
                    elif abs(float(_tot) - float(_p)) < 0.005:
                        row += " (jednorázově)"
                except (TypeError, ValueError):
                    pass
            if s.get("price_is_total"):
                row += " CELKEM za celý pobyt hosta"
            elif _tot is not None and _tot != s.get("price"):
                row += f" (celkem za celý pobyt hosta: {_tot} {cur})"
        if s.get("description"):
            row += f" ({s['description'][:120]})"
        lines.append(row)
    lines.append("Tyto služby přirozeně nabídni, když se hodí k tématu (snídaně, wellness, pozdní check-out…) — "
                 "jako přátelský tip concierge, ne jako reklamu. Ceny uváděj PŘESNĚ vč. rozlišení "
                 "jednotkové ceny a součtu za pobyt — nic nedopočítávej. "
                 "DŮLEŽITÉ: Ty sám NIC neobjednáváš a nerezervuješ — NIKDY se neptej, zda to má host "
                 "objednat (žádné 'Would you like to book it?'). Objednání vyřizuje VÝHRADNĚ recepce — "
                 "odpověď zakonči odkazem na recepci (osobně/telefon/WhatsApp).")
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
        reservation_id=res.get("id") or "",
        guest_name=name,
        arrival=arrival,
        arrival_time=arrival_time,
        departure=departure,
        departure_time=departure_time,
        nights=nights,
        adults=res.get("adults") or 0,
        children=len(res.get("childrenAges") or []),
        children_ages=[int(a) for a in (res.get("childrenAges") or []) if isinstance(a, (int, float))],
        arrival_iso=arrival_raw,
        departure_iso=departure_raw,
        rate_plan=((res.get("ratePlan") or {}).get("name") or ""),
        rate_plan_id=((res.get("ratePlan") or {}).get("id") or ""),
        unit_group=((res.get("unitGroup") or {}).get("name") or ""),
        unit_group_id=((res.get("unitGroup") or {}).get("id") or ""),
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

def _retry_after(r) -> float:
    """Kolik vteřin počkat po 429 (hlavička Retry-After), nejvýš 3 — host čeká na odpověď v chatu."""
    try:
        return max(0.2, min(float(r.headers.get("Retry-After") or 1), 3.0))
    except (TypeError, ValueError):
        return 1.0

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
        if r.status_code == 429:
            # Rate limit (apaleo.dev/guides/api/rate-limiting): počkat podle Retry-After a zkusit jednou znovu
            await asyncio.sleep(_retry_after(r))
            r = await client.get(f"{_APALEO_API}{path}",
                                 headers={"Authorization": f"Bearer {token}"}, params=params)
    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return None
    if r.status_code == 204:
        return {}   # platný dotaz, prázdný výsledek (Apaleo vrací 204 No Content)
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
    d = await _apaleo_get(hotel, "/inventory/v1/properties", {"pageSize": _PAGE_SIZE}, quiet=True)
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

async def apaleo_get_property(hotel: dict, property_id: str) -> Optional[dict]:
    """Detail property (bez zvláštního scope): adresa a země — pro předvyplnění
    profilu u skupinové registrace. Vrací {address, city, country} nebo None."""
    d = await _apaleo_get(hotel, f"/inventory/v1/properties/{property_id}",
                          {"languages": "all"}, quiet=True)
    if not d:
        return None
    loc = d.get("location") or {}
    addr = ", ".join(str(x) for x in (loc.get("addressLine1"), loc.get("postalCode"), loc.get("city")) if x)
    def _lang_pick(v):
        if isinstance(v, dict):
            return str(v.get("en") or next(iter(v.values()), "") or "")
        return str(v or "")
    return {"address": addr, "city": str(loc.get("city") or ""),
            "country": (loc.get("countryCode") or "").upper(),
            "description": _lang_pick(d.get("description")).strip(),
            "currency": str(d.get("currencyCode") or "")}

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
                                {"propertyId": pid, "pageSize": _PAGE_SIZE}, quiet=True)
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
                             {"propertyId": pid, "unitGroupTypes": "BedRoom", "pageSize": _PAGE_SIZE}, quiet=True)
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
    # Typy pokojů (unit groups BedRoom): název, kapacita, počet pokojů — do profilu
    room_types = []
    if ug_d:
        _per_group_count = {}
        for u in units_d.get("units") or []:
            _gid = (u.get("unitGroup") or {}).get("id") or u.get("unitGroupId")
            _per_group_count[_gid] = _per_group_count.get(_gid, 0) + 1
        for g in ug_d.get("unitGroups") or []:
            _nm = g.get("name")
            if isinstance(_nm, dict):
                _nm = _nm.get("en") or next(iter(_nm.values()), "")
            if _nm:
                room_types.append({"name": str(_nm), "maxPersons": int(g.get("maxPersons") or 0),
                                   "units": _per_group_count.get(g.get("id"), 0)})
    data = {"property_id": pid, "units": units, "unit_count": len(units),
            "beds": beds, "checkin_time": checkin, "checkout_time": checkout,
            "room_types": room_types}
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

async def apaleo_service_offers(hotel: dict, stay: "Stay") -> Optional[list]:
    """Upsell na míru POBYTU hosta: služby nabízené k jeho sazbě a termínu
    (GET /booking/v1/service-offers — ratePlanId + arrival/departure + adults).
    Pozn.: /availability/v1/services vrací jen služby s kapacitním omezením (204 u
    běžných služeb — zjištěno 1. 8. na sandboxu), proto upsell jede tudy.
    Cache 1 h per (hotel, sazba, příjezd). Vrací [{name, description, price, currency}] / None."""
    pid = (hotel.get("pms_property_id") or "").strip()
    rid = (getattr(stay, "rate_plan_id", "") or "").strip()
    if not (pid and rid and stay.arrival and stay.departure):
        return None
    ck = ((hotel.get("id") or ""), rid, stay.arrival)
    now = time.time()
    c = _services_cache.get(ck)
    if c and c["expires"] > now:
        return c["data"]
    d = await _apaleo_get(hotel, "/booking/v1/service-offers",
                          {"ratePlanId": rid, "arrival": stay.arrival,
                           "departure": stay.departure,
                           "adults": max(1, stay.adults or 1)}, quiet=True)
    if d is None:
        _services_cache[ck] = {"data": None, "expires": now + 600}
        return None
    out, seen = [], set()
    for item in d.get("services") or []:
        svc = item.get("service") if isinstance(item.get("service"), dict) else item
        name = str(svc.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        # Jednotková cena (defaultGrossPrice služby) vs. SOUČET za celý pobyt (totalAmount)
        # — totalAmount je např. 105 EUR = 15 EUR × 7 nocí; bez popisku by mátl hosty.
        _unit_obj = svc.get("defaultGrossPrice") or {}
        _total_obj = item.get("totalAmount") or item.get("price") or {}
        unit_price = _unit_obj.get("amount", _unit_obj.get("grossAmount"))
        total_price = _total_obj.get("grossAmount", _total_obj.get("amount"))
        desc = str(svc.get("description") or "").strip()
        out.append({"name": name, "description": desc[:200],
                    "price": unit_price if unit_price is not None else total_price,
                    "price_is_total": unit_price is None and total_price is not None,
                    "price_total": total_price,
                    "pricing_unit": str(svc.get("pricingUnit") or ""),
                    "currency": (_unit_obj.get("currency") or _total_obj.get("currency") or "")})
        if len(out) >= 12:
            break
    if not out:
        logging.warning("Apaleo service-offers: OK, ale 0 služeb — klíče: %s | ukázka: %s",
                        list(d.keys())[:8], str(d)[:500])
    else:
        logging.warning("Apaleo service-offers: načteno %d služeb (%s)", len(out),
                        ", ".join(s["name"] for s in out[:5]))
    _services_cache[ck] = {"data": out, "expires": now + (3600 if out else 600)}
    return out

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
                           "pageSize": _PAGE_SIZE}, quiet=True)
    if d is None:
        d = await _apaleo_get(hotel, "/availability/v1/services",
                              {"propertyId": pid, "from": d_from + "T00:00:00Z",
                               "to": d_to + "T00:00:00Z", "pageSize": _PAGE_SIZE}, quiet=True)
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
    params = {"propertyIds": property_id, "status": "InHouse", "pageSize": _PAGE_SIZE}
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

_catalogue_cache: dict = {}   # hotel_id -> {"data": {...}, "expires": epoch}

async def apaleo_service_catalogue(hotel: dict) -> Optional[dict]:
    """Katalog služeb property BEZ propojeného pobytu — pro allowlist v portálu.
    Cesta jen přes offers.read (máme): /booking/v1/offers (zítra → pozítří, 2 dospělí)
    dá rate plany property, /booking/v1/service-offers per rate plan dá služby.
    Vrací {"currency": str, "services": [{id, name, unit_price, currency, pricing_unit}]};
    cache 1 h per hotel. None = chyba/bez PMS."""
    pid = (hotel.get("pms_property_id") or "").strip()
    if not pid:
        return None
    ck = hotel.get("id") or pid
    now = time.time()
    c = _catalogue_cache.get(ck)
    if c and c["expires"] > now:
        return c["data"]
    from datetime import date, timedelta
    arr = date.today() + timedelta(days=1)
    dep = arr + timedelta(days=1)
    d = await _apaleo_get(hotel, "/booking/v1/offers",
                          {"propertyId": pid, "arrival": arr.isoformat(), "departure": dep.isoformat(),
                           "adults": 2, "timeSliceTemplate": "OverNight"}, quiet=True)
    if d is None:
        return None
    rate_plans, currency = [], ""
    for o in d.get("offers") or []:
        rp = ((o.get("ratePlan") or {}).get("id") or "").strip()
        if rp and rp not in rate_plans:
            rate_plans.append(rp)
        currency = currency or ((o.get("totalGrossAmount") or {}).get("currency") or "")
    services: dict = {}
    for rp in rate_plans[:4]:
        s = await _apaleo_get(hotel, "/booking/v1/service-offers",
                              {"ratePlanId": rp, "arrival": arr.isoformat(), "departure": dep.isoformat(), "adults": 2},
                              quiet=True)
        for item in (s or {}).get("services") or []:
            svc = item.get("service") if isinstance(item.get("service"), dict) else item
            sid = str(svc.get("id") or "").strip()
            if not sid or sid in services:
                continue
            unit = svc.get("defaultGrossPrice") or {}
            services[sid] = {"id": sid, "name": str(svc.get("name") or "").strip(), "unit_price": unit.get("amount"),
                             "currency": unit.get("currency") or currency, "pricing_unit": str(svc.get("pricingUnit") or "")}
            currency = currency or unit.get("currency") or ""
    data = {"currency": currency, "services": sorted(services.values(), key=lambda x: x["name"].lower())}
    _catalogue_cache[ck] = {"data": data, "expires": now + 3600}
    return data

# ── Zápis do rezervace (reservations.manage) ──────────────────────────────────
# Požadavek Apalea po certifikačním callu (9. 10. 2026): Alex má umět PŘIDAT SLUŽBU
# a PRODLOUŽIT POBYT, ne jen o nich informovat. Návrh a pravidla:
# APALEO_ZAPIS_DO_REZERVACE_NAVRH.md. Tvar těl a odpovědí je 1:1 podle
# api.apaleo.com/swagger/booking-v1/swagger.json (staženo 10. 10. 2026).
#
# Zásady téhle vrstvy:
#   - Čtení pro zápis jde přes PER-RESERVATION endpointy (/reservations/{id}/…), které
#     vrací přesně to, co se posílá do book-service / amend (serviceId, timeSlices).
#     Dnešní property-level čtení (apaleo_extension_offer, apaleo_service_offers) se
#     NEMĚNÍ — jsou ověřená certifikací a Alexovy odpovědi na nich stojí.
#   - Zápis NIKDY nenastavuje _pms_fail: selhaný zápis ≠ nedostupné PMS, nesmí spustit
#     alert „PMS selhává" ani vypnout personalizaci.
#   - Normalizéry (_norm_*) a skladba těla (_build_amend_body) jsou čisté funkce —
#     testují se na vzorcích ze swaggeru bez sítě.
#   - Ochrana proti dvojímu zápisu: před book-service se přečte seznam už
#     zabookovaných služeb; stejná služba se stejnými daty se znovu neposílá.

async def _apaleo_send(hotel: dict, method: str, path: str, params: dict = None, body: dict = None):
    """PUT/DELETE na Apaleo API s tokenem hotelu. Vrací (status, data):
    204 → (204, {}), 200 → (200, json), chyba → (status, text[:300]), bez tokenu → (0, 'token')."""
    token = await _apaleo_access_token(hotel)
    if not token:
        return 0, "token"
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.request(method, f"{_APALEO_API}{path}",
                                     headers={"Authorization": f"Bearer {token}"},
                                     params=params or None, json=body)
            if r.status_code == 429:
                # 429 = požadavek se nezpracoval → po Retry-After ho lze bezpečně poslat znovu
                await asyncio.sleep(_retry_after(r))
                r = await client.request(method, f"{_APALEO_API}{path}",
                                         headers={"Authorization": f"Bearer {token}"},
                                         params=params or None, json=body)
    except httpx.TimeoutException:
        # Zápis nemá idempotency key: po timeoutu se NEVÍ, jestli proběhl → -1, volající stav ověří čtením
        logging.warning("Apaleo %s %s: timeout (výsledek neznámý)", method, path)
        return -1, "timeout"
    if r.status_code == 204:
        return 204, {}
    if r.status_code == 200:
        try:
            return 200, r.json()
        except Exception:
            return 200, {}
    if r.status_code in (401, 403):
        _drop_connect_token(hotel)
    logging.warning("Apaleo %s %s selhal: %s %s", method, path, r.status_code, r.text[:300])
    return r.status_code, r.text[:300]

def _rid(stay: "Stay") -> str:
    return (getattr(stay, "reservation_id", "") or "").strip()

def _norm_service_offers(d: dict) -> list:
    """ServiceOffersModel → [{service_id, code, name, description, pricing_unit, unit_price,
    currency, count, available_count, total, dates:[{date, count, gross, mandatory, default}]}].
    Zachovává ID a data — to je rozdíl proti apaleo_service_offers (jen k zobrazení)."""
    out = []
    for item in (d or {}).get("services") or []:
        svc = item.get("service") or {}
        sid = str(svc.get("id") or "").strip()
        if not sid:
            continue
        unit = svc.get("defaultGrossPrice") or {}
        total = item.get("totalAmount") or {}
        dates = []
        for x in item.get("dates") or []:
            amt = x.get("amount") or {}
            dates.append({"date": str(x.get("serviceDate") or "")[:10],
                          "count": int(x.get("count") or item.get("count") or 1),
                          "gross": amt.get("grossAmount"),
                          "mandatory": bool(x.get("isMandatory")),
                          "default": bool(x.get("isDefaultDate"))})
        out.append({"service_id": sid, "code": str(svc.get("code") or ""),
                    "name": str(svc.get("name") or "").strip(),
                    "description": str(svc.get("description") or "").strip()[:300],
                    "pricing_unit": str(svc.get("pricingUnit") or ""),
                    "unit_price": unit.get("amount"),
                    "currency": unit.get("currency") or total.get("currency") or "",
                    "count": int(item.get("count") or 1),
                    "available_count": item.get("availableCount"),
                    "total": total.get("grossAmount"),
                    "dates": dates,
                    "validation": [m.get("code") for m in (item.get("validationMessages") or []) if m.get("code")]})
    return out

def _norm_booked_services(d: dict) -> list:
    """ReservationServiceListModel → [{service_id, name, total, currency, dates:[{date, count, gross, mandatory}]}]."""
    out = []
    for item in (d or {}).get("services") or []:
        svc = item.get("service") or {}
        total = item.get("totalAmount") or {}
        out.append({"service_id": str(svc.get("id") or ""), "name": str(svc.get("name") or ""),
                    "total": total.get("grossAmount"), "currency": total.get("currency") or "",
                    "dates": [{"date": str(x.get("serviceDate") or "")[:10], "count": int(x.get("count") or 1),
                               "gross": (x.get("amount") or {}).get("grossAmount"),
                               "mandatory": bool(x.get("isMandatory"))} for x in item.get("dates") or []]})
    return out

def _norm_stay_offers(d: dict, stay: "Stay") -> list:
    """ReservationStayOffersModel → nabídky seřazené tak, že PRVNÍ je ta, která se má
    použít: stejná sazba jako má host (rate_plan_id) > stejný typ pokoje > nejlevnější.
    Každá nese timeSlices s ratePlanId — přesně to, co chce amend. Cena doslova z API."""
    offers = []
    for o in (d or {}).get("offers") or []:
        tg = o.get("totalGrossAmount") or {}
        slices = []
        for ts in o.get("timeSlices") or []:
            rp = ts.get("ratePlan") or {}
            ug = ts.get("unitGroup") or {}
            tsg = ts.get("totalGrossAmount") or {}
            slices.append({"from": str(ts.get("from") or ""), "to": str(ts.get("to") or ""),
                           "rate_plan_id": str(rp.get("id") or ""), "rate_plan": str(rp.get("name") or ""),
                           "unit_group_id": str(ug.get("id") or ""), "unit_group": str(ug.get("name") or ""),
                           "gross": tsg.get("amount"), "currency": tsg.get("currency") or ""})
        if not slices or tg.get("amount") is None:
            continue
        cf = o.get("cancellationFee") or {}
        validation = [m.get("code") for m in (o.get("validationMessages") or []) if m.get("code")]
        # Rozhodují jen NOVĚ PŘIDANÉ noci (mimo dnešní rozsah pobytu) — stávající noci jsou,
        # jaké jsou (requote=false je nechá být), a klidně už smíšené. Nové noci musí být ve
        # STEJNÉM TYPU POKOJE jako rezervace: naostro 10. 10. 2026 amend s novými nocemi
        # v jiném typu přestěhoval hosta (Family room → Single). Taková nabídka = UnitGroupChange,
        # řadí se na konec a chat ji nenabízí. Sazba nové noci jen ovlivňuje pořadí.
        cur_arr = (getattr(stay, "arrival", "") or "")[:10]
        cur_dep = (getattr(stay, "departure", "") or "")[:10]
        new_slices = [ts for ts in slices
                      if (cur_dep and ts["from"][:10] >= cur_dep) or (cur_arr and ts["to"][:10] <= cur_arr)] or slices
        stay_ug_id = (getattr(stay, "unit_group_id", "") or "").strip()
        stay_ug = (getattr(stay, "unit_group", "") or "").strip()
        if any((stay_ug_id and ts["unit_group_id"] != stay_ug_id) or
               (not stay_ug_id and stay_ug and ts["unit_group"] != stay_ug) for ts in new_slices):
            validation.append("UnitGroupChange")
        lead = new_slices[0]
        # Městská taxa a poplatky nejsou v ceně nocí — Apaleo je připisuje NAVÍC (swagger: fees „will be
        # added on top of the TotalGrossAmount"). Taxa má rozpad po dnech → jen přidané noci; poplatky
        # rozpad nemají (platí za celý pobyt), proto jen jménem, bez částky.
        tax_sum, tax_names = 0.0, []
        for ct in o.get("cityTaxes") or []:
            for it in ct.get("dates") or []:
                if cur_dep and str(it.get("serviceDate") or "")[:10] >= cur_dep and (it.get("amount") or {}).get("grossAmount") is not None:
                    tax_sum += float(it["amount"]["grossAmount"])
                    if ct.get("name") and ct["name"] not in tax_names:
                        tax_names.append(str(ct["name"]))
        fee_names = [str(f.get("name") or f.get("code") or "") for f in o.get("fees") or [] if f.get("name") or f.get("code")]
        offers.append({"arrival": str(o.get("arrival") or ""), "departure": str(o.get("departure") or ""),
                       "total": tg.get("amount"), "currency": tg.get("currency") or "",
                       "available_units": o.get("availableUnits"),
                       "rate_plan_id": lead["rate_plan_id"], "rate_plan": lead["rate_plan"],
                       "unit_group_id": lead["unit_group_id"], "unit_group": lead["unit_group"],
                       "new_nights": len(new_slices),
                       "new_total": sum(float(ts["gross"]) for ts in new_slices if ts.get("gross") is not None),
                       "time_slices": slices,
                       "new_city_tax": round(tax_sum, 2), "city_tax_name": ", ".join(tax_names), "fee_names": fee_names,
                       "cancellation_fee": {"name": str(cf.get("name") or ""), "due": str(cf.get("dueDateTime") or ""),
                                            "amount": (cf.get("fee") or {}).get("amount"),
                                            "currency": (cf.get("fee") or {}).get("currency") or ""},
                       "validation": validation})
    cur_rp = (getattr(stay, "rate_plan_id", "") or "").strip()
    cur_ug_id = (getattr(stay, "unit_group_id", "") or "").strip()
    cur_ug = (getattr(stay, "unit_group", "") or "").strip()
    def _amount(o):
        try:
            return float(o["total"])
        except (TypeError, ValueError):
            return float("inf")
    def _same_ug(o):
        if cur_ug_id:
            return o["unit_group_id"] == cur_ug_id
        return bool(cur_ug) and o["unit_group"] == cur_ug
    # Pořadí: platné > STEJNÝ TYP POKOJE (host při prodloužení zůstává ve svém pokoji — má
    # přednost před sazbou) > stejná sazba > nejlevnější. Neplatné/smíšené vždy na konec.
    offers.sort(key=lambda o: (1 if o["validation"] else 0,
                               0 if _same_ug(o) else 1,
                               0 if cur_rp and o["rate_plan_id"] == cur_rp else 1,
                               _amount(o)))
    return offers

def _build_amend_body(stay: "Stay", offer: dict) -> dict:
    """DesiredStayDetailsModel pro PUT amend. Příjezd zůstává PŘESNĚ jak je v PMS
    (arrival_iso — jinak by se posunul čas), odjezd z nabídky, dospělí a věky dětí
    z rezervace (bez childrenAges by amend děti zahodil), timeSlices 1:1 z nabídky
    včetně ceny — Apaleo tak zapíše to, co host viděl, ne přeceněnou variantu."""
    body = {"arrival": stay.arrival_iso or stay.arrival,
            "departure": offer["departure"],
            "adults": max(1, int(stay.adults or 1)),
            "requote": False,
            "timeSlices": [{"ratePlanId": ts["rate_plan_id"],
                            **({"totalGrossAmount": {"amount": ts["gross"], "currency": ts["currency"]}}
                               if ts.get("gross") is not None and ts.get("currency") else {})}
                           for ts in offer["time_slices"]]}
    if stay.children_ages:
        body["childrenAges"] = list(stay.children_ages)
    return body

async def apaleo_reservation_service_offers(hotel: dict, stay: "Stay") -> Optional[list]:
    """GET /booking/v1/reservations/{id}/service-offers (scope offers.read — máme už dnes).
    Nabídka služeb k TÉHLE rezervaci včetně serviceId a výchozích dat → vstup pro book-service."""
    rid = _rid(stay)
    if not rid:
        return None
    d = await _apaleo_get(hotel, f"/booking/v1/reservations/{rid}/service-offers", {}, quiet=True)
    return None if d is None else _norm_service_offers(d)

async def apaleo_booked_services(hotel: dict, stay: "Stay") -> Optional[list]:
    """GET /booking/v1/reservations/{id}/services (scope reservations.read) — co už má host
    na rezervaci. Slouží ochraně proti dvojímu zápisu a nabídce „zrušit"."""
    rid = _rid(stay)
    if not rid:
        return None
    d = await _apaleo_get(hotel, f"/booking/v1/reservations/{rid}/services", {}, quiet=True)
    return None if d is None else _norm_booked_services(d)

async def apaleo_book_service(hotel: dict, stay: "Stay", service_id: str, count: int = 1,
                              dates: list = None, idempotent: bool = True) -> dict:
    """PUT /booking/v1/reservation-actions/{id}/book-service (scope reservations.manage).
    dates = [{"date": "YYYY-MM-DD", "count": n}] nebo None (= výchozí data služby).
    Vrací {ok, status, already, error}. already=True = stejná služba se stejnými daty už
    na rezervaci je → NEPOSÍLÁ se znovu (síť si požadavek ráda zopakuje)."""
    rid = _rid(stay)
    sid = (service_id or "").strip()
    if not (rid and sid):
        return {"ok": False, "status": 0, "already": False, "error": "chybí id rezervace nebo služby"}
    want_dates = sorted({x["date"] for x in (dates or []) if x.get("date")})
    if idempotent:
        booked = await apaleo_booked_services(hotel, stay)
        for b in booked or []:
            if b["service_id"] != sid:
                continue
            have_dates = sorted({x["date"] for x in b["dates"]})
            if not want_dates or want_dates == have_dates or set(want_dates) <= set(have_dates):
                return {"ok": True, "status": 200, "already": True, "error": ""}
    body = {"serviceId": sid, "count": max(1, int(count or 1))}
    if dates:
        body["dates"] = [{"serviceDate": x["date"], "count": max(1, int(x.get("count") or 1))}
                         for x in dates if x.get("date")]
    status, data = await _apaleo_send(hotel, "PUT", f"/booking/v1/reservation-actions/{rid}/book-service", body=body)
    ok = status == 204
    logging.warning("Apaleo book-service %s service=%s → %s", rid, sid, status)
    return {"ok": ok, "status": status, "already": False, "error": "" if ok else str(data)}

async def apaleo_remove_service(hotel: dict, stay: "Stay", service_id: str) -> dict:
    """DELETE /booking/v1/reservations/{id}/services?serviceId= (scope reservations.manage).
    Undo přidané služby. Podmínky, kdy Apaleo odmítne (422), se ověřují na dev účtu."""
    rid = _rid(stay)
    sid = (service_id or "").strip()
    if not (rid and sid):
        return {"ok": False, "status": 0, "error": "chybí id rezervace nebo služby"}
    status, data = await _apaleo_send(hotel, "DELETE", f"/booking/v1/reservations/{rid}/services",
                                      params={"serviceId": sid})
    ok = status == 204
    logging.warning("Apaleo remove-service %s service=%s → %s", rid, sid, status)
    return {"ok": ok, "status": status, "error": "" if ok else str(data)}

async def apaleo_reservation_extension_offers(hotel: dict, stay: "Stay", nights: int = 1) -> Optional[list]:
    """GET /booking/v1/reservations/{id}/offers?departure=… (scope offers.read — máme).
    Nabídky prodloužení o N nocí PRO TUTO rezervaci: celý pobyt od původního příjezdu
    po nový odjezd, s timeSlices → vstup pro amend. requote=false = stávající noci
    za původní cenu, přeceňují se jen přidané. První v seznamu = doporučená (viz _norm_stay_offers)."""
    rid = _rid(stay)
    if not (rid and stay.departure):
        return None
    try:
        from datetime import date, timedelta
        new_dep = date.fromisoformat(stay.departure) + timedelta(days=max(1, min(int(nights or 1), 7)))
    except Exception:
        return None
    params = {"departure": new_dep.isoformat(), "adults": max(1, int(stay.adults or 1)), "requote": "false"}
    if stay.children_ages:
        params["childrenAges"] = list(stay.children_ages)
    d = await _apaleo_get(hotel, f"/booking/v1/reservations/{rid}/offers", params, quiet=True)
    if d is None:
        return None
    if d == {}:
        return []   # 204 = žádná kapacita
    return _norm_stay_offers(d, stay)

async def apaleo_room_free_for_extension(hotel: dict, stay: "Stay", new_departure: str) -> Optional[bool]:
    """Je hostův KONKRÉTNÍ pokoj volný i na přidané noci? GET /availability/v1/reservations/{id}/units
    (scope availability.read — máme) za období původní odjezd → nový odjezd. True = host zůstane
    ve svém pokoji, False = pokoj je obsazený (Apaleo by při prodloužení přiřadilo jiný = stěhování),
    None = nejde zjistit (chyba, neznámý pokoj) → nic netvrdit."""
    rid = _rid(stay)
    cur = (stay.room or "").strip().lower()
    d_from = stay.departure_iso or stay.departure
    if not (rid and cur and d_from and new_departure):
        return None
    params = {"from": d_from, "to": new_departure, "pageSize": 500}
    if getattr(stay, "unit_group_id", ""):
        params["unitGroupId"] = stay.unit_group_id
    d = await _apaleo_get(hotel, f"/availability/v1/reservations/{rid}/units", params, quiet=True)
    if d is None:
        return None
    return any(((u.get("name") or "").strip().lower() == cur) for u in d.get("units") or [])

async def apaleo_extension_precheck(hotel: dict, stay: "Stay") -> Optional[dict]:
    """Smí se u rezervace měnit odjezd? GET /booking/v1/reservations/{id}?expand=actions → akce
    AmendDeparture (Apaleo ji zakáže např. u zavřeného/zavíraného hlavního účtu nebo ve stavu mimo
    Confirmed/InHouse). Vrací {allowed, reasons} nebo None (nejde zjistit → rozhodne až amend)."""
    rid = _rid(stay)
    if not rid:
        return None
    d = await _apaleo_get(hotel, f"/booking/v1/reservations/{rid}", {"expand": "actions"}, quiet=True)
    if not d:
        return None
    act = next((a for a in d.get("actions") or [] if a.get("action") == "AmendDeparture"), None)
    if act is None:
        return None
    return {"allowed": bool(act.get("isAllowed")),
            "reasons": [str(x.get("code") or "") for x in act.get("reasons") or [] if x.get("code")]}

async def apaleo_extension_check(hotel: dict, stay: "Stay") -> Optional[dict]:
    """Stav rezervace PO prodloužení: GET /booking/v1/reservations/{id}?expand=timeSlices
    (scope reservations.read). Apaleo při prodloužení s obsazeným pokojem zápis neodmítne —
    přiřadí na přidané noci JINÝ pokoj a označí stěhování (Apaleo Training Kit: Reservation
    Management). Vrací {departure, moved, units}: units = pokoje přidaných nocí (od původního
    odjezdu), moved = některá přidaná noc není v hostově pokoji (jiný pokoj / nepřiřazeno)."""
    rid = _rid(stay)
    if not rid:
        return None
    d = await _apaleo_get(hotel, f"/booking/v1/reservations/{rid}", {"expand": "timeSlices"}, quiet=True)
    if not d:
        return None
    old_dep = (stay.departure or "")[:10]
    added = [ts for ts in d.get("timeSlices") or [] if old_dep and str(ts.get("from") or "")[:10] >= old_dep]
    units = [((ts.get("unit") or {}).get("name") or "").strip() for ts in added]
    cur = (stay.room or "").strip().lower()
    # Apaleo stěhování samo hlásí validační zprávou AutoUnitAssignment/UnitMoved. Ta ale může být
    # i z dřívějška, proto rozhodují pokoje přidaných nocí; zpráva jen když noci pokoj nenesou.
    flagged = any(m.get("code") == "UnitMoved" or m.get("category") == "AutoUnitAssignment"
                  for m in d.get("validationMessages") or [])
    moved = bool(cur) and any(u.lower() != cur for u in units) if any(units) else flagged
    return {"departure": str(d.get("departure") or "")[:10], "units": units, "moved": moved, "flagged": flagged}

async def apaleo_amend_stay(hotel: dict, stay: "Stay", offer: dict) -> dict:
    """PUT /booking/v1/reservation-actions/{id}/amend (scope reservations.manage).
    offer = položka z apaleo_reservation_extension_offers. Vrací {ok, status, error}.
    Pozor (swagger + apaleo.dev): u InHouse rezervace jdou měnit jen BUDOUCÍ time slices
    — prodloužení odjezdu je budoucí, projde; zkrácení/posun příjezdu ne."""
    rid = _rid(stay)
    if not (rid and offer and offer.get("time_slices") and offer.get("departure")):
        return {"ok": False, "status": 0, "error": "chybí id rezervace nebo nabídka"}
    if offer.get("validation"):
        return {"ok": False, "status": 0, "error": "nabídka není platná: " + ", ".join(offer["validation"])}
    body = _build_amend_body(stay, offer)
    status, data = await _apaleo_send(hotel, "PUT", f"/booking/v1/reservation-actions/{rid}/amend", body=body)
    ok = status == 204
    logging.warning("Apaleo amend %s → %s → %s", rid, offer.get("departure"), status)
    return {"ok": ok, "status": status, "error": "" if ok else str(data)}
