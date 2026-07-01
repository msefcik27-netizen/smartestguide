# SmartestGuide — lehký smoke test (jen dostupnost, BEZ vedlejších efektů)
"""
Rychlá kontrola, že produkce žije. Pouze READ-ONLY endpointy:
- žádné vytváření/mazání hotelů
- žádné volání Anthropic (chat)
- žádné odesílání e-mailů (Brevo)

Vhodné pro častý monitoring (každé 4 h). Plný E2E (test_flow.py) běží po deployi.

Spuštění:
  pip install requests
  python smoke_test.py
"""

import requests
import sys
import time

BASE = "https://smartestguide-production.up.railway.app"

GREEN = "\033[92m"; RED = "\033[91m"; RESET = "\033[0m"; BOLD = "\033[1m"

passed = []
failed = []

def ok(name, detail=""):
    passed.append(name)
    print(f"  {GREEN}OK{RESET} {name}" + (f" — {detail}" if detail else ""))

def fail(name, detail=""):
    failed.append(name)
    print(f"  {RED}FAIL{RESET} {name}" + (f" — {detail}" if detail else ""))

def get_retry(url, **kwargs):
    """GET s krátkým retry při 502/timeoutu (produkce může restartovat)."""
    last = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=15, **kwargs)
            if r.status_code != 502:
                return r
            last = r
        except requests.exceptions.RequestException as e:
            last = e
        time.sleep(5)
    if isinstance(last, Exception):
        raise last
    return last

def check_status(path, label, expected=(200,), check_json_key=None):
    try:
        r = get_retry(f"{BASE}{path}")
        if r.status_code not in expected:
            fail(label, f"status {r.status_code} (čekáno {expected})")
            return
        if check_json_key:
            try:
                v = r.json().get(check_json_key)
            except Exception:
                fail(label, "odpověď není JSON")
                return
            if v in (None, "", []):
                fail(label, f"chybí/prázdné '{check_json_key}'")
                return
            ok(label, f"{check_json_key}={v}")
        else:
            ok(label)
    except Exception as e:
        fail(label, str(e))


if __name__ == "__main__":
    print(f"\n{BOLD}SmartestGuide — Smoke test{RESET}")
    print(f"URL: {BASE}\n")

    # Kritický: verze (potvrzuje, že app běží a vrací data)
    check_status("/api/version", "/api/version", check_json_key="version")

    # Veřejné stránky
    check_status("/", "Admin panel (/)")
    check_status("/landing", "Landing page")
    check_status("/sw.js", "Service worker")
    check_status("/privacy", "Privacy (EN)")
    check_status("/privacy?lang=cs", "Privacy (CZ)")
    check_status("/terms", "Terms (EN)")

    # Read-only API
    check_status("/api/settings", "/api/settings")
    check_status("/api/pricing?beds=50", "/api/pricing", check_json_key="monthly_eur")
    check_status("/api/invoices", "/api/invoices")

    # Stripe webhook existuje (GET → 405 Method Not Allowed)
    check_status("/api/stripe/webhook", "/api/stripe/webhook (405)", expected=(405, 200))

    total = len(passed) + len(failed)
    print(f"\n{BOLD}Výsledek: {len(passed)}/{total} OK, {len(failed)} selhalo{RESET}")
    if failed:
        print(f"{RED}Selhalo:{RESET} " + ", ".join(failed))
    sys.exit(1 if failed else 0)
