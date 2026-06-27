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
            r = requests.get(f"{BASE}{path}", timeout=10)
            if r.status_code == 200:
                ok(label)
            else:
                fail(label, f"status {r.status_code}")
        except Exception as e:
            fail(label, str(e))

    # /api/version
    section("1b. Verze aplikace")
    try:
        r = requests.get(f"{BASE}/api/version", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("commit"):
            ok("/api/version", f"commit: {d['commit'][:7]}")
        else:
            fail("/api/version", f"status {r.status_code}")
    except Exception as e:
        fail("/api/version", str(e))

    # Kontrola JS syntax
    section("1c. Kontrola JS v HTML souborech")
    for path, label in [
        ("/", "Admin panel JS"),
        ("/hotel?token=x", "Hotel portál JS"),
        ("/landing", "Landing page JS"),
    ]:
        try:
            r = requests.get(f"{BASE}{path}", timeout=10)
            content = r.text
            errors = []
            # Počítej reálné HTML script tagy
            # Odstraň JS stringy z obsahu před hledáním
            stripped = re.sub(r"'[^']*'", "''", content)
            stripped = re.sub(r'"[^"]*"', '""', stripped)
            real_open  = len(re.findall(r'<script(?:\s[^>]*)?>\s', stripped))
            real_close = stripped.count('</script>')
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
            if d.get("has_api_key"):
                ok("Anthropic API klíč nastaven")
            else:
                fail("Anthropic API klíč CHYBÍ")
            if d.get("has_stripe_key"):
                ok("Stripe klíč nastaven")
            else:
                fail("Stripe klíč CHYBÍ")
            # Zkontroluj pricing typy
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
            }, timeout=10),
            "WhatsApp čísla (více oddělení)",
            lambda r: r.status_code == 200 and r.json().get("hotel", {}).get("whatsapp_wellness") == "+420111222334"
        ),
    ]:
        try:
            r = req_fn()
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

    # QR kód
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/qr", timeout=15)
        d = r.json()
        if r.status_code == 200 and d.get("qr_base64"):
            url = d.get("guest_url", "")
            if "localhost" in url:
                fail("QR kód URL obsahuje localhost!", url)
            else:
                ok("QR kód generován", url[:60])
        else:
            fail("QR kód", f"status {r.status_code}")
    except Exception as e:
        fail("QR kód", str(e))

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

    # Leták PDF
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/flyer", timeout=30)
        if r.status_code == 200 and "pdf" in r.headers.get("content-type", ""):
            ok("Leták PDF", f"{len(r.content)} bytes")
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

    # Portal update – více WA čísel
    try:
        r = requests.patch(f"{BASE}/api/hotel-portal/update?token={token}", json={
            "star_rating": 3,
            "checkin_time": "15:00",
            "whatsapp_wellness": "+420999888777",
            "whatsapp_restaurant": "+420999888778"
        }, timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("hotel", {}).get("star_rating") == 3:
            ok("Portal update – star_rating + WA čísla uložena")
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
        r = requests.get(f"{BASE}/guest/{hotel_id}", timeout=10)
        if r.status_code == 200 and "SmartestGuide" in r.text:
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
        if r.status_code == 200:
            d = r.json()
            if d.get("reply"):
                ok("POST /api/guest/chat", f"odpověď: {d['reply'][:60]}…")
            else:
                fail("POST /api/guest/chat", "prázdná odpověď")
        else:
            fail("POST /api/guest/chat", f"status {r.status_code}")
    except Exception as e:
        fail("POST /api/guest/chat", str(e))

# ─────────────────────────────────────────────
# 6. Ceník — dynamicky z API
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
            r = requests.get(f"{BASE}/api/pricing?beds={beds}", timeout=10)
            d = r.json()
            if r.status_code == 200 and d.get("monthly_eur") == expected:
                ok(f"{beds} lůžek → {expected} EUR")
            else:
                fail(f"{beds} lůžek", f"očekáváno {expected}, dostáno {d.get('monthly_eur')}")
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

    # Company settings
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

    # Generuj fakturu
    inv_id = None
    try:
        r = requests.post(f"{BASE}/api/hotels/{hotel_id}/invoices/generate", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("invoice", {}).get("id"):
            inv_id = d["invoice"]["id"]
            num = d["invoice"].get("invoice_number", "")
            ok("Generování faktury", num)
        else:
            fail("Generování faktury", str(d)[:100])
    except Exception as e:
        fail("Generování faktury", str(e))

    # Seznam faktur
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

    # Změna stavu
    try:
        r = requests.patch(f"{BASE}/api/invoices/{inv_id}/status?status=paid", timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("invoice", {}).get("status") == "paid":
            ok("PATCH /api/invoices/{id}/status", "→ paid")
        else:
            fail("PATCH /api/invoices/{id}/status", str(d)[:80])
    except Exception as e:
        fail("PATCH /api/invoices/{id}/status", str(e))

    # PDF faktury
    try:
        r = requests.get(f"{BASE}/api/invoices/{inv_id}/pdf", timeout=30)
        if r.status_code == 200 and "pdf" in r.headers.get("content-type", ""):
            ok("GET /api/invoices/{id}/pdf", f"{len(r.content)} bytes")
        else:
            detail = ""
            try:
                detail = r.json().get("detail", r.text[:150])
            except:
                detail = r.text[:150]
            fail("GET /api/invoices/{id}/pdf", f"status {r.status_code} — {detail}")
    except Exception as e:
        fail("GET /api/invoices/{id}/pdf", str(e))

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

    # CZ/EN přepínač
    try:
        r = requests.get(f"{BASE}/privacy", timeout=10)
        if "lang=cs" in r.text and "lang=en" in r.text:
            ok("Privacy — CZ/EN přepínač přítomen")
        else:
            fail("Privacy — CZ/EN přepínač chybí")
    except Exception as e:
        fail("Privacy CZ/EN přepínač", str(e))

# ─────────────────────────────────────────────
# 9. Widget
# ─────────────────────────────────────────────
def test_widget(hotel_id):
    section("9. Widget.js")
    try:
        r = requests.get(f"{BASE}/widget.js?hotel_id={hotel_id}", timeout=10)
        if r.status_code == 200 and "SmartestGuide" in r.text:
            ok("GET /widget.js", f"{len(r.content)} bytes")
        else:
            fail("GET /widget.js", f"status {r.status_code}")
    except Exception as e:
        fail("GET /widget.js", str(e))

# ─────────────────────────────────────────────
# 10. Úklid
# ─────────────────────────────────────────────
def cleanup(hotel_id):
    section("10. Úklid")
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
    test_widget(hotel_id) if hotel_id else skip("Widget test", "hotel chybí")

    if hotel_id:
        cleanup(hotel_id)

    summary()
    sys.exit(1 if failed else 0)
