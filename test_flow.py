"""
SmartestGuide – Automatický E2E test
Testuje celý flow na Railway produkci.

Spuštění:
  pip install requests
  python test_flow.py
"""

import requests
import sys

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

def test_basic():
    section("1. Základní dostupnost stránek")
    for path, label in [("/", "Admin panel"), ("/landing", "Landing page"), ("/hotel?token=x", "Hotel portál"), ("/sw.js", "Service Worker")]:
        try:
            r = requests.get(f"{BASE}{path}", timeout=10)
            if r.status_code == 200:
                ok(label)
            else:
                fail(label, f"status {r.status_code}")
        except Exception as e:
            fail(label, str(e))

    # Kontrola JS syntax – hledáme nekompletní bloky v HTML
    section("1b. Kontrola JS v HTML souborech")
    import re
    for path, label in [("/", "Admin panel JS"), ("/hotel?token=x", "Hotel portál JS"), ("/landing", "Landing page JS")]:
        try:
            r = requests.get(f"{BASE}{path}", timeout=10)
            content = r.text
            errors = []
            # Počet script tagů musí sedět – ignoruj escaped varianty v JS stringech
            open_tags = len(re.findall(r'<script(?:\s[^>]*)?>(?!.*\\\/script>)', content))
            close_tags = len(re.findall(r'<\/script>', content))
            # Přesnější: spočítej jen reálné HTML script tagy (ne ty v JS stringech s \/)
            real_open = len(re.findall(r'<script(?:\s[^>]*)?>\s', content))
            real_close = len(re.findall(r'\s*<\/script>', content))
            if real_open != real_close:
                errors.append(f"Nekompletní script tag ({real_open} open vs {real_close} close)")
            # Extrahuj JS bloky a hledej extra závorky
            scripts = re.findall(r'<script[^>]*>(.*?)</script>', content, re.DOTALL)
            for script in scripts:
                # Počet { musí přibližně odpovídat počtu }
                open_br = script.count('{')
                close_br = script.count('}')
                if abs(open_br - close_br) > 10:
                    errors.append(f"Nevyvážené závorky v JS ({{ {open_br} vs }} {close_br})")
                    break
            if errors:
                fail(label, ", ".join(errors))
            else:
                ok(label, "závorky v pořádku")
        except Exception as e:
            fail(label, str(e))

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
                fail("Anthropic API klíč CHYBÍ – scraping nefunguje")
            if d.get("has_stripe_key"):
                ok("Stripe klíč nastaven")
            else:
                fail("Stripe klíč CHYBÍ – platby nefungují")
        else:
            fail("GET /api/settings", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/settings", str(e))

def test_hotels():
    section("3. Hotely CRUD + portál")
    hotel_id = None
    token = None

    # Vytvoř
    try:
        r = requests.post(f"{BASE}/api/hotels", json={"name":"E2E Test Hotel","url":"https://example.com","bed_count":50,"email":"test@test.com"}, timeout=10)
        d = r.json()
        if r.status_code == 200 and d.get("hotel",{}).get("id"):
            hotel_id = d["hotel"]["id"]
            ok("Vytvoření hotelu", f"ID: {hotel_id[:8]}…")
        else:
            fail("Vytvoření hotelu", str(d))
            return None, None
    except Exception as e:
        fail("Vytvoření hotelu", str(e))
        return None, None

    # Načti
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}", timeout=10)
        d = r.json()
        ok("Načtení hotelu") if r.status_code==200 else fail("Načtení hotelu", f"status {r.status_code}")
    except Exception as e:
        fail("Načtení hotelu", str(e))

    # Aktualizuj
    try:
        r = requests.patch(f"{BASE}/api/hotels/{hotel_id}", json={"star_rating":4,"checkin_time":"14:00"}, timeout=10)
        d = r.json()
        if r.status_code==200 and d.get("hotel",{}).get("star_rating")==4:
            ok("Aktualizace hotelu (star_rating)")
        else:
            fail("Aktualizace hotelu", f"star_rating={d.get('hotel',{}).get('star_rating')}")
    except Exception as e:
        fail("Aktualizace hotelu", str(e))

    # Completeness
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/completeness", timeout=10)
        d = r.json()
        if r.status_code==200 and "score" in d:
            ok("Completeness skóre", f"{d['score']}%")
        else:
            fail("Completeness", f"status {r.status_code}")
    except Exception as e:
        fail("Completeness", str(e))

    # QR kód
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/qr", timeout=15)
        d = r.json()
        if r.status_code==200 and d.get("qr_base64"):
            url = d.get("guest_url","")
            if "localhost" in url:
                fail("QR kód URL obsahuje localhost!", url)
            else:
                ok("QR kód generován", url)
        else:
            fail("QR kód", f"status {r.status_code}")
    except Exception as e:
        fail("QR kód", str(e))

    # Portal link
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/portal-link", timeout=10)
        d = r.json()
        if r.status_code==200 and d.get("portal_url"):
            url = d["portal_url"]
            token = d.get("token")
            if "localhost" in url:
                fail("Portal link URL obsahuje localhost!", url)
            else:
                ok("Portal link", url[:60]+"…")
        else:
            fail("Portal link", f"status {r.status_code}")
    except Exception as e:
        fail("Portal link", str(e))

    # Leták PDF
    try:
        r = requests.get(f"{BASE}/api/hotels/{hotel_id}/flyer", timeout=30)
        if r.status_code==200 and "pdf" in r.headers.get("content-type",""):
            ok("Leták PDF", f"{len(r.content)} bytes")
        else:
            fail("Leták PDF", f"status {r.status_code}, content-type: {r.headers.get('content-type')}")
    except Exception as e:
        fail("Leták PDF", str(e))

    return hotel_id, token

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

    # Portal update – star_rating
    try:
        r = requests.patch(f"{BASE}/api/hotel-portal/update?token={token}", json={"star_rating":3,"checkin_time":"15:00"}, timeout=10)
        d = r.json()
        if r.status_code==200 and d.get("hotel",{}).get("star_rating")==3:
            ok("Portal update – star_rating uložen")
        else:
            fail("Portal update – star_rating", f"vráceno: {d.get('hotel',{}).get('star_rating')}")
    except Exception as e:
        fail("Portal update", str(e))

def test_guest(hotel_id):
    section("5. Guest API")

    # Aktivuj hotel
    try:
        requests.post(f"{BASE}/api/hotels/{hotel_id}/subscription?active=true", timeout=10)
    except:
        pass

    # Guest data
    try:
        r = requests.get(f"{BASE}/api/guest/{hotel_id}", timeout=10)
        d = r.json()
        if r.status_code==200 and d.get("hotel"):
            ok("GET /api/guest/{id}", d["hotel"].get("name",""))
        else:
            fail("GET /api/guest/{id}", f"status {r.status_code}")
    except Exception as e:
        fail("GET /api/guest/{id}", str(e))

    # Guest HTML
    try:
        r = requests.get(f"{BASE}/guest/{hotel_id}", timeout=10)
        if r.status_code==200 and "SmartestGuide" in r.text:
            ok("GET /guest/{id} HTML")
        else:
            fail("GET /guest/{id} HTML", f"status {r.status_code}")
    except Exception as e:
        fail("GET /guest/{id} HTML", str(e))

def test_pricing():
    section("6. Ceník")
    cases = [(50,300),(100,300),(150,450),(200,600)]
    for beds, expected in cases:
        try:
            r = requests.get(f"{BASE}/api/pricing?beds={beds}", timeout=10)
            d = r.json()
            if r.status_code==200 and d.get("monthly_eur")==expected:
                ok(f"{beds} lůžek → {expected} EUR")
            else:
                fail(f"{beds} lůžek", f"očekáváno {expected}, dostáno {d.get('monthly_eur')}")
        except Exception as e:
            fail(f"Ceník {beds} lůžek", str(e))

def cleanup(hotel_id):
    section("7. Úklid")
    try:
        r = requests.delete(f"{BASE}/api/hotels/{hotel_id}", timeout=10)
        if r.status_code==200:
            ok("Testovací hotel smazán")
        else:
            fail("Mazání hotelu", f"status {r.status_code}")
    except Exception as e:
        fail("Mazání hotelu", str(e))

def summary():
    total = len(passed)+len(failed)+len(skipped)
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
    if hotel_id:
        cleanup(hotel_id)
    summary()
    sys.exit(1 if failed else 0)
