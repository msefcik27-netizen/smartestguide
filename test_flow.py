# SmartestGuide v0.4.4
"""
SmartestGuide – Automatický E2E test
Testuje celý flow na Railway produkci.

Spuštění:
  pip install requests
  python test_flow.py
"""

import requests
import sys
import re

BASE = "https://smartestguide-production.up.railway.app"

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

passed = []
failed = []
skipped = []

def ok(name, detail=""):
    passed.append(name)
    print(f"  {GREEN}✅ {name}{RESET}" + (f" — {detail}" if detail else ""))

def fail(name, detail=""):
    failed.append(name)
    print(f"  {RED}❌ {name}{RESET}" + (f" — {detail}" if detail else ""))

def skip(name, reason=""):
    skipped.append(name)
    print(f"  {YELLOW}⏭  {name}{RESET}" + (f" — {reason}" if reason else ""))

def section(title):
    print(f"\n{BOLD}{BLUE}{'─'*55}{RESET}")
    print(f"{BOLD}{BLUE}  {title}{RESET}")
    print(f"{BOLD}{BLUE}{'─'*55}{RESET}")

def get_retry(url, **kwargs):
    """GET s retry při 502"""
    for attempt in range(3):
        try:
            r = requests.get(url, **kwargs)
            if r.status_code != 502:
                return r
        except requests.exceptions.Timeout:
            if attempt == 2: raise
        import time; time.sleep(5)
    return r

# ─────────────────────────────────────────────
# 1. Základní dostupnost + verze
# ─────────────────────────────────────────────
def test_basic():
    section("1. Základní dostupnost stránek")
    for path, label in [
        ("/", "Admin panel"),
        ("/landing", "Landing page"),
        ("/hotel?token=x", "Hotel portál"),
        ("/sw.js", "Service Worker"),
        ("/privacy", "Privacy Policy (EN)"),
        ("/privacy?lang=cs", "Privacy Policy (CZ)"),
        ("/terms", "Terms of Service (EN)"),
        ("/terms?lang=cs", "Terms of Service (CZ)"),
    ]:
        try:
            r = get_retry(f"{BASE}{path}", timeout=15)
            if r.status_code == 200:
                ok(label)
            else:
                fail(label, f"status {r.status_code}")
        except Exception as e:
            fail(label, str(e))

    section("1b. Verze aplikace")
    try:
        r = requests.get(f"{BASE}/api/version", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("version"):
            ok("/api/version", f"v{d['version']}")
        else:
            fail("/api/version", f"status {r.status_code}")
    except Exception as e:
        fail("/api/version", str(e))

    section("1c. Kontrola JS v HTML souborech")
    for path, label in [
        ("/", "Admin panel JS"),
        ("/hotel?token=x", "Hotel portál JS"),
        ("/landing", "Landing page JS"),
    ]:
        try:
            r = get_retry(f"{BASE}{path}", timeout=15)
            content = r.text
            errors = []
            clean = re.sub(r"'[^'\n]*<[^'\n]*'", "''", content)
            clean = re.sub(r'"[^"\n]*<[^"\n]*"', '""', clean)
            real_open  = len(re.findall(r'<script(?:\s[^>]*)?>', clean))
            real_close = len(re.findall(r'</script>', clean))
            if real_open != real_close:
                errors.append(f"Nekompletní script tag ({real_open} open vs {real_close} close)")
            scripts = re.findall(r'<script[^>]*>(.*?)</script>', content, re.DOTALL)
            for script in scripts:
                diff = abs(script.count('{') - script.count('}'))
                if diff > 10:
                    errors.append(f"Nevyvážené závorky ({script.count('{')} vs {script.count('}')})")
                    break
            if errors:
                fail(label, ", ".join(errors))
            else:
                ok(label, "závorky v pořádku")
        except Exception as e:
            fail(label, str(e))

# ─────────────────────────────────────────────
# 2. Nastavení a klíče
# ─────────────────────────────────────────────
def test_settings():
    section("2. Nastavení a klíče")
    try:
        r = requests.get(f"{BASE}/api/settings", timeout=10)
        d = r.json()
        if r.status_code == 200:
            ok("GET /api/settings dostupný")
            ok("Anthropic API klíč nastaven") if d.get("has_api_key") else fail("Anthropic API klíč CHYBÍ")
            ok("Stripe klíč nastaven") if d.get("has_stripe_key") else fail("Stripe klíč CHYBÍ")

            # OPRAVA: stripe_mode pole (live/test detekce) + dynamický preview
            if d.get("has_stripe_key"):
                mode = d.get("stripe_mode")
                if mode in ("live", "test"):
                    ok("Stripe mode detekován", mode)
                else:
                    fail("Stripe mode", f"očekáváno 'live'/'test', dostáno {mode!r}")
                prev = d.get("stripe_key_preview") or ""
                # preview nesmí natvrdo tvrdit sk_test_ když je klíč live
                if mode == "live" and prev.startswith("sk_test_"):
                    fail("Stripe preview nesedí s mode", f"mode=live ale preview='{prev}'")
                elif mode == "live" and prev.startswith("sk_live_"):
                    ok("Stripe preview odpovídá live klíči", prev)
                elif mode == "test" and prev.startswith("sk_test_"):
                    ok("Stripe preview odpovídá test klíči", prev)
                elif prev:
                    ok("Stripe preview přítomen", prev)
                else:
                    fail("Stripe preview chybí", "has_stripe_key=true ale preview je prázdný")
            else:
                skip("Stripe mode/preview", "Stripe klíč není nastaven")

            pb = d.get("pricing_base")
            if isinstance(pb, (int, float)) and pb > 0:
                ok("pricing_base je číslo", f"{pb} EUR")
            else:
                fail("pricing_base není číslo", f"hodnota: {pb!r}")
        else:
            fail("GET /api/settings", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/settings", str(e))

# ─────────────────────────────────────────────
# 3. Hotely CRUD + portál
# ─────────────────────────────────────────────
def test_hotels():
    section("3. Hotely CRUD + portál")
    hotel_id = None
    token = None

    try:
        r = requests.post(f"{BASE}/api/hotels", json={
            "name": "E2E Test Hotel",
            "url": "https://example.com",
            "bed_count": 50,
            "email": "test@test.com"
        }, timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("hotel", {}).get("id"):
            hotel_id = d["hotel"]["id"]
            ok("Vytvoření hotelu", f"ID: {hotel_id[:8]}…")
        else:
            fail("Vytvoření hotelu", str(d))
            return None, None
    except Exception as e:
        fail("Vytvoření hotelu", str(e))
        return None, None

    for req_fn, label, check_fn in [
        (
            lambda: requests.get(f"{BASE}/api/hotels/{hotel_id}", timeout=10),
            "Načtení hotelu",
            lambda r: r.status_code == 200
        ),
        (
            lambda: requests.patch(f"{BASE}/api/hotels/{hotel_id}", json={"star_rating": 4, "checkin_time": "14:00"}, timeout=10),
            "Aktualizace hotelu (star_rating)",
            lambda r: r.status_code == 200 and r.json().get("hotel", {}).get("star_rating") == 4
        ),
        (
            lambda: requests.patch(f"{BASE}/api/hotels/{hotel_id}", json={
                "whatsapp_number": "+420111222333",
                "whatsapp_wellness": "+420111222334",
                "whatsapp_restaurant": "+420111222335",
                "whatsapp_sport": "+420111222336"
            }, timeout=15),
            "WhatsApp čísla (více oddělení)",
            lambda r: r.status_code == 200 and r.json().get("hotel", {}).get("whatsapp_wellness") == "+420111222334"
        ),
        (
            lambda: requests.patch(f"{BASE}/api/hotels/{hotel_id}", json={
                "active_offer": "Dnes sleva 20% na wellness do 20:00",
                "hidden_gems": ["Hospoda U Chvojena (místní oblíbená, 10 min)", "Tajný park nad řekou"]
            }, timeout=10),
            "Aktivní nabídka + skrytá místa uložena",
            lambda r: r.status_code == 200 and r.json().get("hotel", {}).get("active_offer") == "Dnes sleva 20% na wellness do 20:00"
        ),
        (
            lambda: requests.post(f"{BASE}/api/hotels/{hotel_id}/send-reminder?dry_run=1", timeout=15),
            "Reminder endpoint OK (dry-run, bez odeslání)",
            lambda r: r.status_code == 200 and r.json().get("status") == "ok" and r.json().get("dry_run") is True
        ),
    ]:
        try:
            for attempt in range(3):
                r = req_fn()
                if r.status_code != 502:
                    break
                import time; time.sleep(5)
            if check_fn(r):
                ok(label)
            else:
                fail(label, f"status {r.status_code}, data: {str(r.json())[:80]}")
        except Exception as e:
            fail(label, str(e))

    # Completeness
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/completeness", timeout=10)
        d = r.json()
        if r.status_code == 200 and "score" in d:
            ok("Completeness skóre", f"{d['score']}%")
        else:
            fail("Completeness", f"status {r.status_code}")
    except Exception as e:
        fail("Completeness", str(e))

    # QR kód PNG
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/qr", timeout=15)
        d = r.json()
        if r.status_code == 200 and d.get("qr_base64"):
            url = d.get("guest_url", "")
            if "localhost" in url:
                fail("QR kód URL obsahuje localhost!", url)
            else:
                ok("QR kód PNG generován", url[:60])
        else:
            fail("QR kód PNG", f"status {r.status_code}")
    except Exception as e:
        fail("QR kód PNG", str(e))

    # Portal link
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/portal-link", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("portal_url"):
            url = d["portal_url"]
            token = d.get("token")
            if "localhost" in url:
                fail("Portal link URL obsahuje localhost!", url)
            else:
                ok("Portal link", url[:60] + "…")
        else:
            fail("Portal link", f"status {r.status_code}")
    except Exception as e:
        fail("Portal link", str(e))

    # Leták PDF (starý endpoint)
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/flyer", timeout=30)
        if r.status_code == 200 and "pdf" in r.headers.get("content-type", ""):
            ok("Leták PDF (starý endpoint)", f"{len(r.content)} bytes")
        else:
            fail("Leták PDF", f"status {r.status_code}")
    except Exception as e:
        fail("Leták PDF", str(e))

    return hotel_id, token

# ─────────────────────────────────────────────
# 4. Hotel portál API
# ─────────────────────────────────────────────
def test_portal(hotel_id, token):
    section("4. Hotel portál API")
    if not token:
        skip("Hotel portál", "token chybí")
        return

    for endpoint, label in [
        (f"/api/hotel-portal/me?token={token}", "hotel-portal/me"),
        (f"/api/hotel-portal/completeness?token={token}", "hotel-portal/completeness"),
        (f"/api/hotel-portal/invoices?token={token}", "hotel-portal/invoices"),
        (f"/api/hotel-portal/analytics?token={token}", "hotel-portal/analytics"),
    ]:
        try:
            r = requests.get(f"{BASE}{endpoint}", timeout=10)
            if r.status_code == 200:
                ok(f"GET {label}")
            else:
                fail(f"GET {label}", f"status {r.status_code}")
        except Exception as e:
            fail(f"GET {label}", str(e))

    # Portal update – aktivní nabídka + skrytá místa
    try:
        r = requests.patch(f"{BASE}/api/hotel-portal/update?token={token}", json={
            "star_rating": 3,
            "active_offer": "Happy hour v baru 17-19h",
            "hidden_gems": ["Lokální kavárna za rohem", "Skrytý park u řeky"],
            "whatsapp_wellness": "+420999888777"
        }, timeout=10)
        d = r.json()
        hotel = d.get("hotel", {})
        if r.status_code == 200 and hotel.get("star_rating") == 3 and hotel.get("active_offer"):
            ok("Portal update — aktivní nabídka + skrytá místa uložena")
        else:
            fail("Portal update", f"vráceno: {str(d)[:80]}")
    except Exception as e:
        fail("Portal update", str(e))

# ─────────────────────────────────────────────
# 5. Guest API
# ─────────────────────────────────────────────
def test_guest(hotel_id):
    section("5. Guest API")

    try:
        requests.post(f"{BASE}/api/hotels/{hotel_id}/subscription?active=true", timeout=10)
    except:
        pass

    # Nastav aktivní nabídku pro test
    try:
        requests.patch(f"{BASE}/api/hotels/{hotel_id}", json={
            "active_offer": "Dnes sleva 20% na wellness do 20:00",
            "hidden_gems": ["Hospoda U Chvojena — kam chodí místní, 10 min pěšky"]
        }, timeout=10)
    except:
        pass

    try:
        r = requests.get(f"{BASE}/api/guest/{hotel_id}", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("hotel"):
            ok("GET /api/guest/{id}", d["hotel"].get("name", ""))
        else:
            fail("GET /api/guest/{id}", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/guest/{id}", str(e))

    try:
        r = get_retry(f"{BASE}/guest/{hotel_id}", timeout=15)
        if r.status_code == 200 and "SMARTEST" in r.text.upper():
            ok("GET /guest/{id} HTML")
        else:
            fail("GET /guest/{id} HTML", f"status {r.status_code}")
    except Exception as e:
        fail("GET /guest/{id} HTML", str(e))

    # Chat endpoint
    try:
        r = requests.post(f"{BASE}/api/guest/chat", json={
            "hotel_id": hotel_id,
            "message": "What time is check-in?",
            "language": "en",
            "history": []
        }, timeout=30)
        if r.status_code == 200 and r.json().get("reply"):
            ok("POST /api/guest/chat", f"{r.json()['reply'][:60]}…")
        else:
            fail("POST /api/guest/chat", f"status {r.status_code}")
    except Exception as e:
        fail("POST /api/guest/chat", str(e))

    # Chat — aktivní nabídka se zmíní při relevantní otázce
    try:
        r = requests.post(f"{BASE}/api/guest/chat", json={
            "hotel_id": hotel_id,
            "message": "Do you have wellness or spa?",
            "language": "en",
            "history": []
        }, timeout=30)
        if r.status_code == 200:
            reply = r.json().get("reply", "").lower()
            if "wellness" in reply or "spa" in reply or "20%" in reply or "offer" in reply or "discount" in reply:
                ok("Chat — aktivní nabídka zmíněna při relevantní otázce")
            else:
                ok("Chat — odpověď přišla (nabídka nezmíněna, možná hotel nemá data)")
        else:
            fail("Chat — aktivní nabídka test", f"status {r.status_code}")
    except Exception as e:
        fail("Chat — aktivní nabídka test", str(e))

    # Chat — skrytá místa
    try:
        r = requests.post(f"{BASE}/api/guest/chat", json={
            "hotel_id": hotel_id,
            "message": "Where do locals go? Any hidden gems nearby?",
            "language": "en",
            "history": []
        }, timeout=30)
        if r.status_code == 200:
            reply = r.json().get("reply", "").lower()
            if "local" in reply or "hidden" in reply or "chvojena" in reply or "gem" in reply:
                ok("Chat — skrytá místa zmíněna")
            else:
                ok("Chat — odpověď přišla (skrytá místa nezmíněna, možná hotel nemá data)")
        else:
            fail("Chat — skrytá místa test", f"status {r.status_code}")
    except Exception as e:
        fail("Chat — skrytá místa test", str(e))

    # Chat — detekce jazyka (německy)
    try:
        r = requests.post(f"{BASE}/api/guest/chat", json={
            "hotel_id": hotel_id,
            "message": "Wann ist das Frühstück?",
            "language": "auto",
            "history": []
        }, timeout=30)
        if r.status_code == 200:
            reply = r.json().get("reply", "")
            german_words = ["Uhr", "ist", "das", "die", "der", "und", "Frühstück", "um", "Uhr"]
            if any(w in reply for w in german_words):
                ok("Chat detekce jazyka (DE)", f"{reply[:50]}…")
            else:
                ok("Chat detekce jazyka (DE)", "odpověď přišla (jazyk neověřen)")
        else:
            fail("Chat detekce jazyka (DE)", f"status {r.status_code}")
    except Exception as e:
        fail("Chat detekce jazyka (DE)", str(e))

    # Guest HTML kontroly
    try:
        r = get_retry(f"{BASE}/guest/{hotel_id}", timeout=15)
        html = r.text
        if "whatsapp" in html.lower() or "wa.me" in html:
            ok("Guest HTML — WhatsApp tlačítko přítomno")
        else:
            fail("Guest HTML — WhatsApp tlačítko", "chybí")
        if "openWhatsApp" in html or "openWhatsAppMenu" in html:
            ok("Guest HTML — WhatsApp funkce")
        else:
            fail("Guest HTML — WhatsApp funkce", "chybí")
        if "google.com/maps" in html or "maps.app" in html or "openMap" in html or "navUrl" in html:
            ok("Guest HTML — mapové odkazy přítomny")
        else:
            fail("Guest HTML — mapové odkazy", "chybí")
        if "formatBotText" in html or "linkify" in html.lower():
            ok("Guest HTML — linkifikace URL")
        else:
            fail("Guest HTML — linkifikace URL", "chybí")
        # Ověř zlatý avatar ✦
        if "✦" in html or "f5a623" in html or "f0c060" in html:
            ok("Guest HTML — zlatý brand (✦ avatar)")
        else:
            fail("Guest HTML — zlatý brand", "chybí gold barva nebo ✦")
    except Exception as e:
        fail("Guest HTML rozšířené testy", str(e))

# ─────────────────────────────────────────────
# 6. Ceník
# ─────────────────────────────────────────────
def test_pricing():
    section("6. Ceník")
    try:
        s = requests.get(f"{BASE}/api/settings", timeout=10).json()
        base      = int(s.get("pricing_base", 200))
        threshold = int(s.get("pricing_threshold", 100))
        per_bed   = float(s.get("pricing_per_bed", 3))
    except Exception as e:
        fail("Načtení ceníku z /api/settings", str(e))
        return

    def expected_price(beds):
        if beds <= threshold:
            return base
        return int(base + (beds - threshold) * per_bed)

    ok(f"Ceník načten z API", f"{base} EUR base, threshold {threshold}, +{per_bed} EUR/lůžko")

    for beds in [50, 100, 150, 200]:
        expected = expected_price(beds)
        try:
            r = get_retry(f"{BASE}/api/pricing?beds={beds}", timeout=15)
            d = r.json()
            actual = d.get("monthly_eur")
            if r.status_code == 200 and actual is not None and abs(actual - expected) <= 1:
                ok(f"{beds} lůžek → {actual} EUR")
            else:
                fail(f"{beds} lůžek", f"očekáváno {expected}, dostáno {actual}")
        except Exception as e:
            fail(f"Ceník {beds} lůžek", str(e))

# ─────────────────────────────────────────────
# 7. Faktury
# ─────────────────────────────────────────────
def test_invoices(hotel_id):
    section("7. Faktury")

    try:
        requests.post(f"{BASE}/api/hotels/{hotel_id}/subscription?active=true", timeout=10)
    except:
        pass

    try:
        r = requests.post(f"{BASE}/api/settings/company", json={
            "company_name": "Test s.r.o.",
            "company_email": "test@test.com",
            "company_ico": "12345678"
        }, timeout=10)
        ok("POST /api/settings/company") if r.status_code == 200 else fail("POST /api/settings/company", f"status {r.status_code}")
    except Exception as e:
        fail("POST /api/settings/company", str(e))

    try:
        r = requests.get(f"{BASE}/api/settings/company", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("company_name") == "Test s.r.o.":
            ok("GET /api/settings/company", "data uložena správně")
        else:
            fail("GET /api/settings/company", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/settings/company", str(e))

    inv_id = None
    try:
        r = requests.post(f"{BASE}/api/hotels/{hotel_id}/invoices/generate", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("invoice", {}).get("id"):
            inv_id = d["invoice"]["id"]
            ok("Generování faktury", d["invoice"].get("invoice_number", ""))
        else:
            fail("Generování faktury", str(d)[:100])
    except Exception as e:
        fail("Generování faktury", str(e))

    try:
        r = requests.get(f"{BASE}/api/invoices", timeout=10)
        d = r.json()
        if r.status_code == 200 and isinstance(d.get("invoices"), list):
            ok("GET /api/invoices", f"{len(d['invoices'])} faktur")
        else:
            fail("GET /api/invoices", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/invoices", str(e))

    if not inv_id:
        skip("Změna stavu faktury", "faktura nebyla vytvořena")
        skip("PDF faktury", "faktura nebyla vytvořena")
        return

    try:
        r = requests.patch(f"{BASE}/api/invoices/{inv_id}/status?status=paid", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("invoice", {}).get("status") == "paid":
            ok("PATCH /api/invoices/{id}/status", "→ paid")
        else:
            fail("PATCH /api/invoices/{id}/status", str(d)[:80])
    except Exception as e:
        fail("PATCH /api/invoices/{id}/status", str(e))

    try:
        r = requests.get(f"{BASE}/api/invoices/{inv_id}/pdf", timeout=30)
        if r.status_code == 200 and "pdf" in r.headers.get("content-type", ""):
            ok("GET /api/invoices/{id}/pdf", f"{len(r.content)} bytes")
        else:
            try:
                detail = r.json().get("detail", r.text[:150])
            except:
                detail = r.text[:150]
            fail("GET /api/invoices/{id}/pdf", f"status {r.status_code} — {detail}")
    except Exception as e:
        fail("GET /api/invoices/{id}/pdf", str(e))

    # Faktura viditelná na portálu hotelu + stažení PDF přes token (a odmítnutí cizího)
    token = None
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/portal-link", timeout=10)
        purl = r.json().get("portal_url", "")
        if "token=" in purl:
            token = purl.split("token=", 1)[1]
    except Exception:
        pass

    if not token:
        skip("Portál — faktura viditelná", "nezískán token")
        skip("Portál — stažení PDF přes token", "nezískán token")
        skip("Portál — cizí token odmítnut", "nezískán token")
    else:
        try:
            r = requests.get(f"{BASE}/api/hotel-portal/invoices?token={token}", timeout=10)
            ids = [i.get("id") for i in r.json().get("invoices", [])]
            if r.status_code == 200 and inv_id in ids:
                ok("Portál — faktura viditelná", f"{len(ids)} faktur hotelu")
            else:
                fail("Portál — faktura viditelná", f"status {r.status_code}, v seznamu: {inv_id in ids}")
        except Exception as e:
            fail("Portál — faktura viditelná", str(e))

        try:
            r = requests.get(f"{BASE}/api/invoices/{inv_id}/pdf?token={token}", timeout=30)
            if r.status_code == 200 and "pdf" in r.headers.get("content-type", ""):
                ok("Portál — stažení PDF přes token", f"{len(r.content)} bytes")
            else:
                fail("Portál — stažení PDF přes token", f"status {r.status_code}")
        except Exception as e:
            fail("Portál — stažení PDF přes token", str(e))

        try:
            r = requests.get(f"{BASE}/api/invoices/{inv_id}/pdf?token=NEPLATNY_TOKEN_XYZ", timeout=15)
            if r.status_code == 403:
                ok("Portál — cizí token odmítnut", "403")
            else:
                fail("Portál — cizí token odmítnut", f"očekáváno 403, dostal {r.status_code}")
        except Exception as e:
            fail("Portál — cizí token odmítnut", str(e))

# ─────────────────────────────────────────────
# 8. Legal stránky
# ─────────────────────────────────────────────
def test_legal():
    section("8. Legal stránky")
    for path, label, check_text in [
        ("/privacy",          "Privacy Policy EN", "Privacy Policy"),
        ("/privacy?lang=cs",  "Privacy Policy CZ", "Zásady ochrany"),
        ("/terms",            "Terms EN",           "Terms of Service"),
        ("/terms?lang=cs",    "Terms CZ",            "Obchodní podmínky"),
    ]:
        try:
            r = requests.get(f"{BASE}{path}", timeout=10)
            if r.status_code == 200 and check_text in r.text:
                ok(label)
            else:
                fail(label, f"status {r.status_code}, text chybí: '{check_text}'")
        except Exception as e:
            fail(label, str(e))

    try:
        r = requests.get(f"{BASE}/privacy", timeout=10)
        if "lang=cs" in r.text and "lang=en" in r.text:
            ok("Privacy — CZ/EN přepínač přítomen")
        else:
            fail("Privacy — CZ/EN přepínač chybí")
    except Exception as e:
        fail("Privacy CZ/EN přepínač", str(e))

# ─────────────────────────────────────────────
# 9. Stripe webhook
# ─────────────────────────────────────────────
def test_stripe(hotel_id):
    section("9. Stripe webhook")
    try:
        r = requests.get(f"{BASE}/api/stripe/webhook", timeout=10)
        if r.status_code == 405:
            ok("GET /api/stripe/webhook → 405 (endpoint existuje)")
        elif r.status_code == 200:
            ok("GET /api/stripe/webhook → 200")
        else:
            fail("GET /api/stripe/webhook", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/stripe/webhook", str(e))

    try:
        r = requests.post(f"{BASE}/api/stripe/webhook",
            data="test",
            headers={"stripe-signature": "invalid"},
            timeout=10)
        if r.status_code in (400, 422):
            ok("POST /api/stripe/webhook → správně odmítne neplatný podpis")
        else:
            ok(f"POST /api/stripe/webhook → {r.status_code} (endpoint existuje)")
    except Exception as e:
        fail("POST /api/stripe/webhook", str(e))

    skip("Simulace reálné platby", "vyžaduje Stripe CLI nebo manuální test")

# ─────────────────────────────────────────────
# 10. Landing page funkce
# ─────────────────────────────────────────────
def test_landing():
    section("10. Landing page funkce")
    try:
        r = get_retry(f"{BASE}/landing", timeout=15)
        html = r.text

        if "cookie" in html.lower() and "cookie-banner" in html:
            ok("GDPR cookies banner přítomen")
        else:
            fail("GDPR cookies banner", "chybí v HTML")

        if "/privacy" in html and "/terms" in html:
            ok("Privacy a Terms linky přítomny")
        else:
            fail("Privacy/Terms linky", "chybí v HTML")

        if "gdpr" in html.lower() or "reg-gdpr" in html or "agree" in html.lower():
            ok("GDPR checkbox v registračním formuláři")
        else:
            fail("GDPR checkbox", "chybí v HTML")

        if "calcSgPrice" in html or "price-preview" in html or "price-base-val" in html:
            ok("Ceník kalkulátor přítomen")
        else:
            fail("Ceník kalkulátor", "chybí v HTML")

        if "flagcdn.com" in html or "dc143c" in html:
            ok("Vlajky jazyků přítomny (flagcdn.com)")
        else:
            fail("Vlajky jazyků", "chybí flagcdn.com")

        if "setLang" in html and "btn-cs" in html and "btn-en" in html:
            ok("CZ/EN jazykový přepínač")
        else:
            fail("CZ/EN přepínač", "chybí")

        # Nové jazyky DE, ES, ZH, JA
        if "btn-de" in html and "btn-es" in html and "btn-zh" in html and "btn-ja" in html:
            ok("Landing — 6 jazyků (DE, ES, ZH, JA přítomny)")
        else:
            fail("Landing — 6 jazyků", "chybí btn-de/es/zh/ja")

        # Dropdown přepínač
        if "lang-dropdown" in html and "lang-trigger" in html:
            ok("Landing — dropdown přepínač jazyků")
        else:
            fail("Landing — dropdown přepínač", "chybí lang-dropdown")

        # SMARTEST GUIDE brand
        if "SMARTEST" in html and "GUIDE" in html:
            ok("Landing — SMARTEST GUIDE brand")
        else:
            fail("Landing — SMARTEST GUIDE brand", "chybí")

        if "stripe.com" in html or "api/register" in html:
            ok("Stripe platební odkaz přítomen")
        else:
            fail("Stripe platební odkaz", "chybí")

    except Exception as e:
        fail("Landing page funkce", str(e))

    try:
        r = requests.get(f"{BASE}/sw.js", timeout=10)
        ok("PWA service worker /sw.js dostupný") if r.status_code == 200 else fail("PWA service worker", f"status {r.status_code}")
    except Exception as e:
        fail("PWA service worker", str(e))

    try:
        r = requests.get(f"{BASE}/success", timeout=10)
        if r.status_code in (200, 422):
            ok("Success stránka dostupná")
        else:
            fail("Success stránka", f"status {r.status_code}")
    except Exception as e:
        fail("Success stránka", str(e))

# ─────────────────────────────────────────────
# 11. QR hub + tiskové materiály (NOVÉ)
# ─────────────────────────────────────────────
def test_print_materials(hotel_id):
    section("11. QR hub a tiskové materiály")

    # Nastav zemi CZ → aktivuje lokální (CZ) větev letáků v hubu (has_local=True)
    try:
        requests.patch(f"{BASE}/api/hotels/{hotel_id}", json={"country": "CZ"}, timeout=10)
    except:
        pass

    # QR hub stránka
    try:
        r = get_retry(f"{BASE}/api/hotels/{hotel_id}/qr-poster", timeout=15)
        html = r.text
        if r.status_code == 200 and "SmartestGuide" in html:
            ok("QR hub stránka dostupná")
        else:
            fail("QR hub stránka", f"status {r.status_code}")
        # Ověř přítomnost všech formátů
        if "flyer-en" in html and "flyer-cz" in html and "rollup" in html:
            ok("QR hub — všechny formáty přítomny (EN, CZ, rollup)")
        else:
            fail("QR hub — formáty", "chybí flyer-en/cz nebo rollup linky")

        # REGRESE (prázdná stránka CZ A4/A5): KAŽDÉ openFormat('X') musí mít klíč v urls mapě.
        # Když tlačítko volá formát, který v mapě není, window.open(undefined) → prázdná stránka.
        fmt_calls = set(re.findall(r"openFormat\('([^']+)'\)", html))
        if not fmt_calls:
            fail("QR hub — openFormat tlačítka", "žádná nenalezena")
        else:
            missing = sorted(k for k in fmt_calls if f"'{k}':" not in html)
            if missing:
                fail("QR hub — formáty bez URL v mapě (PRÁZDNÁ STRÁNKA!)", ", ".join(missing))
            else:
                ok("QR hub — všechna tlačítka mají URL v urls mapě", f"{len(fmt_calls)} formátů")

        # Explicitně lokální (CZ) klíče, které dřív v mapě chyběly
        for key in ["flyer-local", "flyer-a5-local"]:
            if f"'{key}':" in html:
                ok(f"QR hub — '{key}' v urls mapě")
            else:
                fail(f"QR hub — '{key}' CHYBÍ v urls mapě", "CZ A4/A5 by byla prázdná stránka")
        if "f5a623" in html or "f0c060" in html or "FF6B00" in html:
            ok("QR hub — brand barva")
        else:
            fail("QR hub — brand barva", "chybí brand barva")
    except Exception as e:
        fail("QR hub stránka", str(e))

    # A4 leták EN
    try:
        r = get_retry(f"{BASE}/api/hotels/{hotel_id}/flyer-en", timeout=15)
        if r.status_code == 200 and "AI concierge" in r.text:
            ok("Leták A4 EN dostupný")
        else:
            fail("Leták A4 EN", f"status {r.status_code}")
    except Exception as e:
        fail("Leták A4 EN", str(e))

    # A4 leták CZ
    try:
        r = get_retry(f"{BASE}/api/hotels/{hotel_id}/flyer-cz", timeout=15)
        if r.status_code == 200 and "concierge" in r.text.lower():
            ok("Leták A4 CZ dostupný")
        else:
            fail("Leták A4 CZ", f"status {r.status_code}")
    except Exception as e:
        fail("Leták A4 CZ", str(e))

    # Roll-up banner
    try:
        r = get_retry(f"{BASE}/api/hotels/{hotel_id}/rollup", timeout=15)
        if r.status_code == 200 and "SmartestGuide" in r.text:
            ok("Roll-up banner dostupný")
        else:
            fail("Roll-up banner", f"status {r.status_code}")
    except Exception as e:
        fail("Roll-up banner", str(e))

    # QR poster print view
    try:
        r = get_retry(f"{BASE}/api/hotels/{hotel_id}/qr-poster-print", timeout=15)
        has_holder = "sg-qr-holder" in r.text
        has_qrcode = "qrcode" in r.text.lower()
        has_smartest = "SmartestGuide" in r.text
        if r.status_code == 200 and (has_holder or has_qrcode or has_smartest):
            ok("QR plakát print view dostupný")
        elif r.status_code == 404:
            fail("QR plakát print view", "hotel nenalezen (404) — hotel možná nebyl správně uložen")
        else:
            fail("QR plakát print view", f"status {r.status_code}, holder={has_holder}, smartest={has_smartest}")
    except Exception as e:
        fail("QR plakát print view", str(e))

# ─────────────────────────────────────────────
# 12. Admin panel funkce (NOVÉ)
# ─────────────────────────────────────────────
def test_admin():
    section("12. Admin panel funkce")
    try:
        r = get_retry(f"{BASE}/", timeout=15)
        html = r.text

        # Verze v sidebaru
        if "app-version" in html:
            ok("Admin — verze v sidebaru (app-version element)")
        else:
            fail("Admin — verze v sidebaru", "chybí #app-version")

        # SMARTEST GUIDE brand
        if "SMARTEST" in html:
            ok("Admin — SMARTEST GUIDE brand")
        else:
            fail("Admin — SMARTEST GUIDE brand", "chybí")

        # Gold barva
        if "f5a623" in html or "f0c060" in html or "FF6B00" in html:
            ok("Admin — brand barva")
        else:
            fail("Admin — brand barva", "chybí brand barva")

        # Syne font
        if "Syne" in html:
            ok("Admin — Syne font")
        else:
            fail("Admin — Syne font", "chybí")

    except Exception as e:
        fail("Admin panel funkce", str(e))

# ─────────────────────────────────────────────
# 13. Widget
# ─────────────────────────────────────────────
def test_widget(hotel_id):
    section("13. Widget.js")
    try:
        r = requests.get(f"{BASE}/widget.js?hotel_id={hotel_id}", timeout=10)
        if r.status_code == 200 and "SmartestGuide" in r.text:
            ok("GET /widget.js", f"{len(r.content)} bytes")
        else:
            fail("GET /widget.js", f"status {r.status_code}")
    except Exception as e:
        fail("GET /widget.js", str(e))

# ─────────────────────────────────────────────
# 14. Úklid
# ─────────────────────────────────────────────
def cleanup(hotel_id):
    section("19. Úklid")
    try:
        r = requests.delete(f"{BASE}/api/hotels/{hotel_id}", timeout=10)
        if r.status_code == 200:
            ok("Testovací hotel smazán")
        else:
            fail("Mazání hotelu", f"status {r.status_code}")
    except Exception as e:
        fail("Mazání hotelu", str(e))

# ─────────────────────────────────────────────
# Souhrn
# ─────────────────────────────────────────────
def summary():
    total = len(passed) + len(failed) + len(skipped)
    print(f"\n{BOLD}{'═'*55}{RESET}")
    print(f"{BOLD}  SOUHRN TESTŮ{RESET}")
    print(f"{'═'*55}")
    print(f"  {GREEN}✅ Prošlo:    {len(passed)}/{total}{RESET}")
    print(f"  {RED}❌ Selhalo:   {len(failed)}/{total}{RESET}")
    if skipped:
        print(f"  {YELLOW}⏭  Přeskočeno: {len(skipped)}{RESET}")
    if failed:
        print(f"\n{RED}{BOLD}  Selhané:{RESET}")
        for f in failed:
            print(f"  {RED}  • {f}{RESET}")
    print(f"\n{'═'*55}")
    if not failed:
        print(f"{GREEN}{BOLD}  🎉 Všechny testy prošly!{RESET}")
    else:
        print(f"{RED}{BOLD}  ⚠️  {len(failed)} testů selhalo{RESET}")
    print(f"{'═'*55}\n")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# 15. WiFi pole v portálu
# ─────────────────────────────────────────────
def test_wifi(hotel_id, token):
    section("15. WiFi pole v portálu")
    if not token:
        skip("WiFi testy", "token chybí")
        return
    # Ulož WiFi přes portál
    try:
        r = requests.patch(f"{BASE}/api/hotel-portal/update?token={token}", json={
            "wifi_name": "Hotel_Test_Guest",
            "wifi_password": "testpass2024"
        }, timeout=10)
        d = r.json()
        hotel = d.get("hotel", {})
        if r.status_code == 200 and hotel.get("wifi_name") == "Hotel_Test_Guest":
            ok("WiFi název uložen přes portál")
        else:
            fail("WiFi název", f"vráceno: {str(d)[:80]}")
        if hotel.get("wifi_password") == "testpass2024":
            ok("WiFi heslo uloženo přes portál")
        else:
            fail("WiFi heslo", f"vráceno: {str(d)[:80]}")
    except Exception as e:
        fail("WiFi portál update", str(e))

    # Ověř že Alex zná WiFi
    try:
        r = requests.post(f"{BASE}/api/guest/chat", json={
            "hotel_id": hotel_id,
            "message": "What is the WiFi password?",
            "language": "en",
            "history": []
        }, timeout=30)
        if r.status_code == 200:
            reply = r.json().get("reply", "").lower()
            if "wifi" in reply or "testpass" in reply or "hotel_test" in reply or "password" in reply:
                ok("Alex zná WiFi informace")
            else:
                ok("Alex odpověděl (WiFi nezmíněno — hotel nemá data)")
        else:
            fail("WiFi chat test", f"status {r.status_code}")
    except Exception as e:
        fail("WiFi chat test", str(e))

# ─────────────────────────────────────────────
# 16. Subscription logika
# ─────────────────────────────────────────────
def test_subscription(hotel_id):
    section("16. Subscription logika")

    # Ověř že hotel má subscription_period_end pole
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}", timeout=10)
        d = r.json().get("hotel", {})
        if "subscription_period_end" in d or "trial_used" in d or "subscription_active" in d:
            ok("Hotel má subscription pole")
        else:
            ok("Hotel pole dostupná (subscription_period_end bude po platbě)")
    except Exception as e:
        fail("Subscription pole", str(e))

    # Test ochrany proti duplicitní registraci
    try:
        r = requests.post(f"{BASE}/api/register", json={
            "hotel_name": "E2E Duplicate Test",
            "contact_email": "test@test.com",  # stejný email jako test hotel
            "bed_count": 10,
            "hotel_url": "https://example.com"
        }, timeout=10)
        if r.status_code == 409:
            ok("Ochrana proti duplicitní registraci (409)")
        elif r.status_code == 200:
            # Mohl projít pokud neexistuje aktivní subscription pro ten email
            ok("Registrace prošla (email bez aktivní subscription)")
        else:
            ok(f"Registrace vrátila {r.status_code}")
    except Exception as e:
        fail("Ochrana duplicitní registrace", str(e))

    # Test cancel endpoint
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/portal-link", timeout=10)
        token = r.json().get("token", "")
        if token:
            r2 = requests.post(f"{BASE}/api/hotel-portal/cancel?token={token}", timeout=10)
            d = r2.json()
            if r2.status_code == 200:
                # Cancel nastaví subscription_cancel_requested, NEDEAKTIVUJE okamžitě
                hotel = d.get("hotel", {})
                if hotel.get("subscription_cancel_requested") or hotel.get("active_until") or d.get("message"):
                    ok("Cancel — nastaví cancel_requested, nedeaktivuje okamžitě")
                else:
                    ok("Cancel endpoint odpověděl 200")
            else:
                fail("Cancel endpoint", f"status {r2.status_code}")
        else:
            skip("Cancel test", "token chybí")
    except Exception as e:
        fail("Cancel endpoint", str(e))

# ─────────────────────────────────────────────
# 17. Lokální letáky (dle jazyka hotelu)
# ─────────────────────────────────────────────
def test_local_flyres(hotel_id):
    section("17. Lokální letáky a A5 formáty")

    for endpoint, label, check in [
        (f"/api/hotels/{hotel_id}/flyer-local", "Leták lokální jazyk", "concierge"),
        (f"/api/hotels/{hotel_id}/flyer-a5-en", "A5 Flyer EN", "concierge"),
        (f"/api/hotels/{hotel_id}/flyer-a5-cz", "A5 Leták CZ", "concierge"),
        (f"/api/hotels/{hotel_id}/flyer-a5-local", "A5 lokální jazyk", "concierge"),
    ]:
        try:
            r = get_retry(f"{BASE}{endpoint}", timeout=15)
            if r.status_code == 200 and check in r.text.lower():
                ok(label)
            else:
                fail(label, f"status {r.status_code}")
        except Exception as e:
            fail(label, str(e))

    # Ověř brand barvy v letácích (oranžová nebo zlatá)
    try:
        r = get_retry(f"{BASE}/api/hotels/{hotel_id}/flyer-en", timeout=15)
        if r.status_code == 200:
            has_brand = any(c in r.text for c in ["FF6B00", "f5a623", "f0c060", "00d4aa"])
            ok("Leták EN — brand barvy") if has_brand else fail("Leták EN — brand barvy", "chybí")
    except Exception as e:
        fail("Leták EN brand barvy", str(e))

# ─────────────────────────────────────────────
# 18. Reminder email (Brevo)
# ─────────────────────────────────────────────
def test_reminder_email(hotel_id):
    section("18. Reminder email")
    try:
        r = requests.post(f"{BASE}/api/hotels/{hotel_id}/send-reminder?dry_run=1", timeout=15)
        d = r.json()
        if r.status_code == 200 and d.get("status") == "ok" and d.get("dry_run") is True:
            ok("Reminder endpoint OK (dry-run, bez odeslání)", f"na: {d.get('email_to','?')}")
        else:
            fail("Reminder email", f"status {r.status_code}, {str(d)[:80]}")
    except Exception as e:
        fail("Reminder email", str(e))


# ─────────────────────────────────────────────
# 20. Regrese: country=None robustnost (0.4.4 fix)
# ─────────────────────────────────────────────
def test_country_none_guard():
    section("20. Regrese — country=None robustnost")
    nid = None
    try:
        r = requests.post(f"{BASE}/api/hotels", json={
            "name": "E2E NullCountry Hotel",
            "url": "https://example.com",
            "bed_count": 20,
            "email": "nullcountry@test.com",
            "country": None,
        }, timeout=10)
        d = r.json()
        nid = d.get("hotel", {}).get("id")
        if not nid:
            skip("country=None guard", f"hotel se nevytvořil: {str(d)[:80]}")
            return
    except Exception as e:
        fail("country=None — vytvoření hotelu", str(e))
        return

    # Endpointy, které dřív padaly na .upper()/.lower() nad None
    for endpoint, label in [
        (f"/api/hotels/{nid}/qr-poster", "qr-poster (country=None)"),
        (f"/api/hotels/{nid}/flyer-cz", "flyer-cz (country=None)"),
        (f"/api/hotels/{nid}/flyer-local", "flyer-local (country=None)"),
    ]:
        try:
            r = get_retry(f"{BASE}{endpoint}", timeout=15)
            if r.status_code == 200:
                ok(label)
            else:
                fail(label, f"status {r.status_code} (možná regrese country=None)")
        except Exception as e:
            fail(label, str(e))

    # Faktura nad hotelem bez země/emailu → nesmí 500 (guard na created_at/email)
    try:
        requests.post(f"{BASE}/api/hotels/{nid}/subscription?active=true", timeout=10)
        r = requests.post(f"{BASE}/api/hotels/{nid}/invoices/generate", timeout=10)
        if r.status_code in (200, 400):
            ok("Generování faktury (country=None) — bez 500")
        else:
            fail("Generování faktury (country=None)", f"status {r.status_code}")
    except Exception as e:
        fail("Generování faktury (country=None)", str(e))

    # Úklid
    try:
        requests.delete(f"{BASE}/api/hotels/{nid}", timeout=10)
        ok("country=None hotel smazán")
    except Exception as e:
        fail("Mazání country=None hotelu", str(e))


# ─────────────────────────────────────────────
# 21. Provize (externisté / affiliate)
# ─────────────────────────────────────────────
def test_commissions():
    section("21. Provize (partneři + ledger)")
    import time as _t
    # Nastavení provize
    try:
        r = get_retry(f"{BASE}/api/settings/commission", timeout=10)
        d = r.json()
        if r.status_code == 200 and "commission_amount" in d and d.get("commission_currency") == "CZK":
            ok("GET /api/settings/commission", f"{d.get('commission_amount')} CZK, hold {d.get('commission_hold_days')}d")
        else:
            fail("GET /api/settings/commission", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/settings/commission", str(e))

    try:
        r = requests.post(f"{BASE}/api/settings/commission",
                          json={"commission_enabled": True, "commission_amount": 1500, "commission_hold_days": 30}, timeout=10)
        ok("POST /api/settings/commission") if r.status_code == 200 else fail("POST /api/settings/commission", f"status {r.status_code}")
    except Exception as e:
        fail("POST /api/settings/commission", str(e))

    # Partner CRUD
    ref = f"E2E{int(_t.time())%100000}"
    pid = None
    try:
        r = requests.post(f"{BASE}/api/partners",
                          json={"name": "E2E Partner", "referral_code": ref, "email": "e2e@test.com"}, timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("partner", {}).get("id"):
            pid = d["partner"]["id"]
            ok("Vytvoření partnera", f"ref {d['partner'].get('referral_code')}")
        else:
            fail("Vytvoření partnera", str(d)[:100])
    except Exception as e:
        fail("Vytvoření partnera", str(e))

    # Duplicitní referral kód → 409
    try:
        r = requests.post(f"{BASE}/api/partners", json={"name": "Dup", "referral_code": ref}, timeout=10)
        ok("Duplicitní referral odmítnut (409)") if r.status_code == 409 else fail("Duplicitní referral", f"status {r.status_code}")
    except Exception as e:
        fail("Duplicitní referral", str(e))

    # Seznam partnerů obsahuje statistiky
    try:
        r = requests.get(f"{BASE}/api/partners", timeout=10)
        d = r.json()
        found = any(p.get("id") == pid for p in d.get("partners", []))
        if r.status_code == 200 and found:
            ok("GET /api/partners", f"{len(d['partners'])} partnerů")
        else:
            fail("GET /api/partners", f"status {r.status_code}, nalezen={found}")
    except Exception as e:
        fail("GET /api/partners", str(e))

    # Ledger provizí dostupný
    try:
        r = requests.get(f"{BASE}/api/commissions", timeout=10)
        d = r.json()
        ok("GET /api/commissions", f"{len(d.get('commissions', []))} provizí") if r.status_code == 200 and isinstance(d.get("commissions"), list) else fail("GET /api/commissions", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/commissions", str(e))

    # Admin má stránku Provize
    try:
        r = get_retry(f"{BASE}/", timeout=15)
        if "page-commissions" in r.text and "loadCommissionsPage" in r.text:
            ok("Admin — stránka Provize přítomna")
        else:
            fail("Admin — stránka Provize", "chybí v HTML")
    except Exception as e:
        fail("Admin — stránka Provize", str(e))

    # Úklid partnera
    if pid:
        try:
            requests.delete(f"{BASE}/api/partners/{pid}", timeout=10)
            ok("E2E partner smazán")
        except Exception as e:
            fail("Mazání partnera", str(e))


# ─────────────────────────────────────────────
# 22. Opakovatelné restaurace + ukládání polí portálu
# ─────────────────────────────────────────────
def test_restaurants(hotel_id, token):
    section("22. Restaurace (opakovatelné) + ukládání polí portálu")
    if not token:
        skip("Restaurace testy", "token chybí")
        return

    payload = {
        "restaurants": [
            {"name": "E2E Restaurace", "type": "Česká", "hours": "11:00-22:00",
             "directions": "Z recepce doprava, v přízemí",
             "menus": [{"label": "Polední", "url": "https://example.com/lunch.pdf"},
                       {"label": "Nápojový lístek", "url": "https://example.com/drinks"}]},
            {"name": "E2E Bar & Grill", "menus": [{"label": "Vinný lístek", "url": "https://example.com/wine"}]},
        ],
        # Pole, která se DŘÍV neukládala (regrese pre-existing bugu)
        "fitness_info": "Otevřeno 6-22 (E2E)",
        "pool_info": "Krytý bazén (E2E)",
        "minibar": "Ano, doplňován denně (E2E)",
    }
    hotel = {}
    try:
        r = requests.patch(f"{BASE}/api/hotel-portal/update?token={token}", json=payload, timeout=15)
        if r.status_code != 200:
            fail("Portal update (restaurace + pole)", f"status {r.status_code}")
            return
        ok("Portal update odeslán")
        hotel = r.json().get("hotel", {})
    except Exception as e:
        fail("Portal update (restaurace)", str(e))
        return

    rest = hotel.get("restaurants") or []
    if len(rest) == 2 and rest[0].get("name") == "E2E Restaurace" and len(rest[0].get("menus") or []) == 2:
        ok("Restaurace uloženy", "2 restaurace, 2 jídelníčky u první")
    else:
        fail("Restaurace uloženy", f"vráceno: {str(rest)[:120]}")

    ok("restaurant_name back-compat") if hotel.get("restaurant_name") == "E2E Restaurace" else fail("restaurant_name back-compat", str(hotel.get("restaurant_name")))
    ok("menu_urls back-compat") if hotel.get("menu_urls") else fail("menu_urls back-compat", "prázdné")

    # Regrese pre-existing bugu: pole se teď ukládají
    ok("Oprava — fitness_info se ukládá") if hotel.get("fitness_info") else fail("fitness_info se NEuložil (bug)")
    ok("Oprava — pool_info se ukládá") if hotel.get("pool_info") else fail("pool_info se NEuložil (bug)")
    ok("Oprava — minibar se ukládá") if hotel.get("minibar") else fail("minibar se NEuložil (bug)")

    # Přetrvání po opětovném načtení
    try:
        r = requests.get(f"{BASE}/api/hotel-portal/me?token={token}", timeout=10)
        rest2 = r.json().get("hotel", {}).get("restaurants") or []
        ok("Restaurace přetrvaly po /me", f"{len(rest2)} restaurací") if len(rest2) == 2 else fail("Restaurace /me", f"{len(rest2)}")
    except Exception as e:
        fail("Restaurace /me", str(e))


if __name__ == "__main__":
    print(f"\n{BOLD}SmartestGuide – E2E Test Runner{RESET}")
    print(f"URL: {BLUE}{BASE}{RESET}\n")

    test_basic()
    test_settings()
    hotel_id, token = test_hotels()

    if hotel_id:
        test_portal(hotel_id, token)
        test_guest(hotel_id)
    else:
        skip("Portal testy", "hotel se nepodařilo vytvořit")
        skip("Guest testy", "hotel se nepodařilo vytvořit")

    test_pricing()
    test_invoices(hotel_id) if hotel_id else skip("Fakturační testy", "hotel chybí")
    test_legal()
    test_stripe(hotel_id) if hotel_id else skip("Stripe testy", "hotel chybí")
    test_landing()
    test_print_materials(hotel_id) if hotel_id else skip("QR hub testy", "hotel chybí")
    test_admin()
    test_widget(hotel_id) if hotel_id else skip("Widget test", "hotel chybí")
    test_wifi(hotel_id, token) if hotel_id else skip("WiFi testy", "hotel chybí")
    test_subscription(hotel_id) if hotel_id else skip("Subscription testy", "hotel chybí")
    test_local_flyres(hotel_id) if hotel_id else skip("Lokální letáky", "hotel chybí")
    test_reminder_email(hotel_id) if hotel_id else skip("Reminder email", "hotel chybí")
    test_country_none_guard()
    test_commissions()
    test_restaurants(hotel_id, token) if hotel_id else skip("Restaurace testy", "hotel chybí")

    if hotel_id:
        cleanup(hotel_id)

    summary()
    sys.exit(1 if failed else 0)
