# SmartestGuide v0.4.4
"""
SmartestGuide – vše v jednom souboru
Spusť: python -m uvicorn app:app --reload
Nebo použij SPUSTIT.bat
"""

from fastapi import FastAPI, HTTPException, Request, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List
import os, json, uuid, httpx, asyncio, re, base64, hmac, hashlib, logging, threading
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from dotenv import load_dotenv
import pms as pms_layer  # PMS napojení (Apaleo…) — viz pms.py, PLAN_PMS_NAPOJENI.md

load_dotenv()

# ─────────────────────────────────────────────
# Admin notifikace — jedna spolehlivá schránka pro interní kopie hotelových e-mailů.
# Soukromé osobní adresy (seznam/gmail) se používají VÝHRADNĚ pro zálohy a NIKDY
# nesmí vystupovat směrem k hotelu ani hostům.
# ─────────────────────────────────────────────
ADMIN_NOTIFY_EMAIL = os.getenv("ADMIN_NOTIFY_EMAIL", "admin@smartestguide.com").strip()

def _admin_notify_bcc(exclude: str = "") -> list:
    """BCC seznam pro interní admin kopie (neviditelné hotelu/hostům). Bez soukromých adres."""
    ex = (exclude or "").strip().lower()
    out, seen = [], set()
    for e in (ADMIN_NOTIFY_EMAIL, os.getenv("ADMIN_CC_EMAIL", "").strip()):
        e = (e or "").strip().lower()
        if e and e != ex and e not in seen:
            seen.add(e)
            out.append({"email": e})
    return out

# Reference na běžící background tasky — brání předčasnému GC. Bez toho se fire-and-forget
# task (create_task bez uložené reference) může „ztratit" dřív, než doběhne → e-mail se tiše neodešle.
_bg_tasks = set()
def _spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    def _done(task):
        _bg_tasks.discard(task)
        try:
            exc = task.exception()
        except Exception:
            exc = None
        if exc:
            logging.error("Background task SELHAL: %r", exc, exc_info=exc)
    t.add_done_callback(_done)
    return t

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
    # Apaleo Connect app (OAuth) — jedny credentials pro celý SmartestGuide (ne per hotel)
    if os.getenv("APALEO_CLIENT_ID"):
        updates["apaleo_client_id"] = os.getenv("APALEO_CLIENT_ID")
    if os.getenv("APALEO_CLIENT_SECRET"):
        updates["apaleo_client_secret"] = os.getenv("APALEO_CLIENT_SECRET")
    if updates:
        db_save_settings(updates)

# ─────────────────────────────────────────────
# Aplikace
# ─────────────────────────────────────────────
async def _reminder_background_loop():
    """Každých 6 h: reminder e-maily, deaktivace expirací a záloha dat."""
    await asyncio.sleep(60)
    while True:
        try:
            await _check_and_send_reminders()
            await _deactivate_expired_subscriptions()
            await _backup_data()
            await _send_monthly_reports_if_due()
        except Exception as e:
            logging.error("Background loop error: %s", e)
        await asyncio.sleep(6 * 60 * 60)

async def _deactivate_expired_subscriptions():
    """Deaktivuje hotely jejichž subscription_period_end uplynul."""
    db = db_load()
    now = datetime.utcnow()
    changed = False
    for hotel_id, h in db["hotels"].items():
        if not h.get("subscription_active"):
            continue
        period_end_str = h.get("subscription_period_end", "")
        if not period_end_str:
            continue
        try:
            period_end = datetime.fromisoformat(period_end_str)
        except Exception:
            continue
        if now > period_end:
            db["hotels"][hotel_id]["subscription_active"] = False
            db["hotels"][hotel_id]["subscription_deactivated_at"] = now.isoformat()
            changed = True
            logging.info("Auto-deactivated hotel %s (period ended %s)", hotel_id, period_end_str)
    if changed:
        db_save(db)

async def _backup_data():
    """Denní rotovaná záloha data.json (na volume) + týdenní off-site kopie e-mailem (kód + DB)."""
    import shutil, glob
    try:
        src = DB_PATH
        if _PG_ENABLED:
            # V Postgres režimu napiš čerstvý snímek z DB, ať je co zálohovat/poslat e-mailem
            try:
                os.makedirs(os.path.dirname(os.path.abspath(src)) or ".", exist_ok=True)
                with open(src, "w", encoding="utf-8") as f:
                    json.dump(db_load(), f, ensure_ascii=False, indent=2)
            except Exception:
                src = "/tmp/sg-db-snapshot.json"
                with open(src, "w", encoding="utf-8") as f:
                    json.dump(db_load(), f, ensure_ascii=False, indent=2)
        if not os.path.exists(src):
            return
        bdir = os.path.join(os.path.dirname(os.path.abspath(src)), "backups")
        os.makedirs(bdir, exist_ok=True)
        # Denní kopie (jedna za den)
        today = datetime.utcnow().strftime("%Y%m%d")
        daily = os.path.join(bdir, f"data-{today}.json")
        if not os.path.exists(daily):
            shutil.copy2(src, daily)
            logging.info("Denní záloha vytvořena: %s", daily)
        # Rotace — nech posledních 30 dní
        for old in sorted(glob.glob(os.path.join(bdir, "data-*.json")))[:-30]:
            try:
                os.remove(old)
            except Exception:
                pass
        # Týdenní off-site záloha e-mailem (přežije i ztrátu volume)
        marker = os.path.join(bdir, ".last_email")
        last = ""
        if os.path.exists(marker):
            try:
                last = open(marker).read().strip()
            except Exception:
                last = ""
        now = datetime.utcnow()
        try:
            last_dt = datetime.fromisoformat(last) if last else None
        except Exception:
            last_dt = None
        if last_dt is None or (now - last_dt).days >= 7:
            if await _email_backup(src):
                try:
                    open(marker, "w").write(now.isoformat())
                except Exception:
                    pass
    except Exception as e:
        logging.warning("Záloha selhala: %s", e)

def _redact_secrets(db: dict) -> dict:
    """Vrátí kopii DB bez citlivých klíčů — ať neputují e-mailem. Klíče se stejně
    obnovují z Railway env při startu aplikace, takže záloha zůstává plně použitelná."""
    import copy
    d = copy.deepcopy(db) if isinstance(db, dict) else {}
    s = d.get("settings")
    if isinstance(s, dict):
        for k in ("anthropic_api_key", "stripe_secret_key", "stripe_webhook_secret"):
            if s.get(k):
                s[k] = "<REDACTED — uloženo v Railway env>"
    return d

async def _email_backup(src) -> bool:
    """Pošle KOMPLETNÍ zálohu (kód + databáze) jako .zip na zálohovací e-maily (off-site)."""
    brevo_key = os.getenv("BREVO_API_KEY", "")
    # Zálohy chodí na obě adresy natvrdo (+ volitelně další přes BACKUP_EMAIL)
    recips = ["martin.1303@seznam.cz", "msefcik27@gmail.com"]
    extra = os.getenv("BACKUP_EMAIL", "").strip()
    if extra and extra not in recips:
        recips.append(extra)
    if not brevo_key:
        return False
    try:
        import io, zipfile
        stamp = datetime.utcnow().strftime("%Y-%m-%d")
        appdir = os.path.dirname(os.path.abspath(__file__))
        code_files = ["app.py", "index.html", "hotel.html", "guest.html", "landing.html", "logo.png"]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            # Databáze — živý snímek, bez citlivých klíčů
            z.writestr("database.json", json.dumps(_redact_secrets(db_load()), ensure_ascii=False, indent=2))
            # Kód aplikace (admin, portál, guest, landing, backend)
            for fn in code_files:
                p = os.path.join(appdir, fn)
                if os.path.exists(p):
                    z.write(p, arcname=f"code/{fn}")
        content_b64 = base64.b64encode(buf.getvalue()).decode()
        payload = {
            "sender": {"name": "SMARTEST GUIDE", "email": "admin@smartestguide.com"},
            "to": [{"email": e} for e in recips],
            "subject": f"SMARTEST GUIDE — týdenní záloha {stamp}",
            "htmlContent": (f"<p>Týdenní off-site záloha k {stamp}. V příloze .zip:</p>"
                            f"<ul><li><strong>database.json</strong> — živá data (hotely, faktury, provize, nastavení)</li>"
                            f"<li><strong>code/</strong> — app.py, index.html (admin), hotel.html (portál), guest.html, landing.html</li></ul>"
                            f"<p>Rozbal a ulož mimo Railway.</p>"
                            f"<p style='color:#888;font-size:12px'>Pozn.: citlivé klíče (Anthropic/Stripe) v záloze nejsou — jsou v Railway env a obnoví se automaticky při startu.</p>"),
            "textContent": f"Tydenni zaloha {stamp}: kod + databaze v .zip. Citlive klice nejsou (jsou v Railway env).",
            "attachment": [{"name": f"smartestguide-backup-{stamp}.zip", "content": content_b64}],
        }
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.brevo.com/v3/smtp/email", json=payload,
                headers={"api-key": brevo_key, "Content-Type": "application/json"}, timeout=60)
            if r.status_code in (200, 201):
                logging.info("Off-site záloha (kód+DB) odeslána -> %s", ", ".join(recips))
                return True
            logging.error("Brevo záloha CHYBA %s: %s", r.status_code, r.text[:200])
            return False
    except Exception as e:
        logging.warning("E-mail zálohy selhal: %s", e)
        return False

async def _check_and_send_reminders():
    """Zkontroluje všechny hotely a pošle reminder pokud splňují podmínky."""
    import httpx as _httpx
    brevo_key = os.getenv("BREVO_API_KEY", "")
    base_url = os.getenv("BASE_URL", "https://smartestguide-production.up.railway.app")
    if not brevo_key:
        return

    # POJISTKA: hromadné AUTOMATICKÉ rozesílání připomínek je VYPNUTÉ, dokud ho admin
    # výslovně nezapne (Nastavení → auto_reminders_enabled=true). Bez toho jdou
    # připomínky jen ručně z administrace = pod kontrolou / se souhlasem admina.
    if not db_get_settings().get("auto_reminders_enabled", False):
        logging.info("Auto-reminders vypnuté (auto_reminders_enabled=false) — přeskočeno.")
        return

    db = db_load()
    now = datetime.utcnow()
    sent_count = 0

    for hotel_id, h in db["hotels"].items():
        try:
            # Pouze aktivní hotely
            if not h.get("subscription_active"):
                continue

            # Přeskoč testovací (E2E) hotely — ať CI/testy nespamují reminder e-maily
            if (h.get("name") or "").strip().upper().startswith("E2E"):
                continue

            # Completeness skóre
            completeness = hotel_profile_completeness(h)
            score = completeness.get("score", 100)
            if score >= 80:
                continue  # Profil je kompletní, neposílej

            # Datum registrace / subscription_start
            reg_str = h.get("subscription_start") or h.get("created_at", "")
            if not reg_str:
                continue
            try:
                reg_date = datetime.fromisoformat(reg_str.replace("Z", ""))
            except Exception:
                continue

            days_since = (now - reg_date).days
            last_reminder_str = h.get("last_reminder_sent", "")
            last_reminder = datetime.fromisoformat(last_reminder_str.replace("Z", "")) if last_reminder_str else None
            reminder_count = h.get("reminder_count", 0)

            # Pravidla:
            # 1. Po 14 dnech — první reminder (pokud ještě nebyl poslán)
            # 2. Po 30 dnech — opakuj každých 30 dní dokud score < 80%
            should_send = False
            if days_since >= 14 and reminder_count == 0:
                should_send = True
            elif days_since >= 30:
                if last_reminder is None:
                    should_send = True
                elif (now - last_reminder).days >= 30:
                    should_send = True

            if not should_send:
                continue

            # Pošli reminder
            hotel_name = h.get("name", "Hotel")
            hotel_email = h.get("registration_email") or h.get("email", "")
            if not hotel_email:
                continue

            portal_url = f"{base_url}/portal?token={h.get('hotel_token','')}"
            missing_labels = {
                "address": "Adresa hotelu", "phone": "Telefon recepce",
                "email": "Email", "checkin_time": "Check-in čas",
                "checkout_time": "Check-out čas", "breakfast_hours": "Hodiny snídaně",
                "bed_count": "Počet lůžek", "star_rating": "Hvězdičky",
                "description": "Popis hotelu", "wifi_name": "WiFi síť",
                "wifi_password": "WiFi heslo", "pet_policy": "Pravidla pro mazlíčky",
            }
            missing_list = [missing_labels.get(f, f) for f in completeness.get("missing", [])]
            missing_html = "".join(f"<li>{m}</li>" for m in missing_list) if missing_list else ""

            subject = f"Připomínka: doplňte profil hotelu {hotel_name} ({score}%)"
            html_body = f"""
            <div style="font-family:Inter,sans-serif;max-width:600px;margin:0 auto;background:#1a1a1a;color:#f0ece0;padding:32px;border-radius:12px">
              <div style="font-weight:800;font-size:24px;color:#f0ece0;margin-bottom:4px">SMARTEST GUIDE<span style="width:7px;height:7px;border-radius:50%;background:#FF6B00;display:inline-block;margin-left:3px"></span></div>
              <div style="font-size:12px;color:#00d4aa;letter-spacing:.15em;text-transform:uppercase;margin-bottom:24px">AI Concierge for Hotels</div>
              <h2 style="color:#FF6B00;font-size:20px;margin-bottom:12px">Profil hotelu {hotel_name} je vyplněn z {score}%</h2>
              <p style="color:#9ba0c0;line-height:1.7">Dobrý den,<br><br>váš hotel <strong style="color:#f0ece0">{hotel_name}</strong> má aktivní předplatné SmartestGuide, ale profil není kompletní. Čím více informací Alex zná, tím lépe pomáhá hostům.</p>
              {"<p style='color:#9ba0c0'>Chybějící informace:</p><ul style='color:#f0ece0;line-height:2'>" + missing_html + "</ul>" if missing_list else ""}
              <a href="{portal_url}" style="display:inline-block;margin-top:20px;background:#FF6B00;color:#0a0b0f;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;font-size:15px">Přejít do portálu →</a>
              <p style="margin-top:32px;font-size:12px;color:#6b6f8e">SMARTEST GUIDE · support@smartestguide.com</p>
            </div>"""

            bcc_list = _admin_notify_bcc()

            payload = {
                "sender": {"name": "SMARTEST GUIDE", "email": "admin@smartestguide.com"},
                "to": [{"email": hotel_email, "name": hotel_name}],
                "bcc": bcc_list,
                "subject": subject,
                "htmlContent": html_body,
            }

            resp = _httpx.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                json=payload, timeout=15
            )
            if resp.status_code in (200, 201):
                db["hotels"][hotel_id]["last_reminder_sent"] = now.isoformat()
                db["hotels"][hotel_id]["reminder_count"] = reminder_count + 1
                db_save(db)
                sent_count += 1
                logging.info("Auto-reminder sent to %s (%s%%, day %s)", hotel_email, score, days_since)
            else:
                logging.warning("Auto-reminder Brevo error: %s %s", resp.status_code, resp.text[:200])

        except Exception as e:
            logging.error("Auto-reminder error for hotel %s: %s", hotel_id, e)

    if sent_count:
        logging.info("Auto-reminder loop: sent %s reminders", sent_count)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    # Firemní údaje dodavatele jsou na tvrdo v db_get_settings (přebijí data.json).
    task = asyncio.create_task(_reminder_background_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

app = FastAPI(title="SmartestGuide", version="0.2.0", lifespan=lifespan)

# Verze aplikace — zvyš při každém deployi
APP_VERSION = "0.5.25"
import time as _time
APP_START_TIME = _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime())

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cache-busting: HTML stránky (landing, admin, portál, guest) se nesmí kešovat,
# jinak prohlížeč po deployi servíruje starý JS (nutil by k Ctrl+Shift+R).
# JS bug z 0.5.4 se kvůli cache jevil „nasazený, ale pořád rozbitý".
@app.middleware("http")
async def _no_cache_html(request, call_next):
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ── Zabezpečení administrace ─────────────────────────────────
# Admin UI (shell) je veřejný, ale admin DATA/API jsou za heslem (cookie).
# Veřejné zůstává: guest, portál (token), registrace, webhook, QR/letáky, faktura PDF (token).
_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
_ADMIN_SALT = "sg-admin-v1"
_ADMIN_TOKEN = hashlib.sha256((_ADMIN_PASSWORD + _ADMIN_SALT).encode()).hexdigest() if _ADMIN_PASSWORD else ""
if not _ADMIN_PASSWORD:
    logging.warning("⚠️  ADMIN_PASSWORD není nastaveno — ADMINISTRACE NENÍ ZABEZPEČENA! Nastav env ADMIN_PASSWORD.")

# ── Rate limiting (in-memory) proti abuse a nákladům na Anthropic API ──
_RL_WINDOW = 60          # okno v sekundách
_RL_MAX_PER_MIN = 15     # max zpráv/min na IP (host)
_rl_hits = {}

def _client_ip(request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def _rate_limit_ok(key: str, max_hits: int = _RL_MAX_PER_MIN, window: int = _RL_WINDOW) -> bool:
    now = _time.time()
    lst = _rl_hits.get(key)
    if lst is None:
        if len(_rl_hits) > 20000:   # hrubá pojistka proti růstu paměti
            _rl_hits.clear()
        lst = []
        _rl_hits[key] = lst
    cutoff = now - window
    while lst and lst[0] < cutoff:
        lst.pop(0)
    if len(lst) >= max_hits:
        return False
    lst.append(now)
    return True

def _is_public_api(path: str) -> bool:
    if path in ("/api/version", "/api/pricing", "/api/pricing-config", "/api/contact"):
        return True
    for p in ("/api/guest/", "/api/hotel-portal/", "/api/app-icon/", "/api/docs/",
              "/api/ares/", "/api/stripe/", "/api/register", "/api/admin/",
              "/api/pms/apaleo/"):  # OAuth connect/callback — hlídá se hotel tokenem + state
        if path.startswith(p):
            return True
    if path.startswith("/api/hotels/"):
        for s in ("/qr", "/qr-poster", "/qr-poster-print", "/manifest.webmanifest",
                  "/rollup", "/flyer", "/flyer-en", "/flyer-cz", "/flyer-local",
                  "/flyer-a5-en", "/flyer-a5-cz", "/flyer-a5-local"):
            if path.endswith(s):
                return True
    if path.startswith("/api/invoices/") and path.endswith("/pdf"):
        return True  # faktura PDF si hlídá token/admin sama v endpointu
    return False

@app.middleware("http")
async def _admin_gate(request, call_next):
    if _ADMIN_TOKEN and request.method != "OPTIONS":
        p = request.url.path
        if p.startswith("/api/") and not _is_public_api(p):
            if request.cookies.get("sg_admin", "") != _ADMIN_TOKEN:
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Neautorizováno — přihlaste se do administrace."}, status_code=401)
    return await call_next(request)

@app.post("/api/admin/login")
async def admin_login(request: Request):
    from fastapi.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        body = {}
    pw = (body.get("password") or "").strip()
    if not _ADMIN_PASSWORD:
        return JSONResponse({"ok": True, "unprotected": True})
    if pw and hashlib.sha256((pw + _ADMIN_SALT).encode()).hexdigest() == _ADMIN_TOKEN:
        resp = JSONResponse({"ok": True})
        resp.set_cookie("sg_admin", _ADMIN_TOKEN, httponly=True, secure=True,
                        samesite="lax", max_age=60 * 60 * 24 * 30)
        return resp
    return JSONResponse({"detail": "Špatné heslo"}, status_code=401)

@app.post("/api/admin/logout")
async def admin_logout():
    from fastapi.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("sg_admin")
    return resp

@app.get("/api/admin/status")
async def admin_status(request: Request):
    return {"protected": bool(_ADMIN_TOKEN),
            "authed": (not _ADMIN_TOKEN) or request.cookies.get("sg_admin", "") == _ADMIN_TOKEN}

# ── Ochrana nebezpečných akcí (export/import DB, tvrdé mazání hotelů) ──
# Vyžaduje DRUHÉ heslo DANGER_PASSWORD navíc k admin přihlášení. Posílá se v hlavičce X-Danger-Key.
# Nastav ho v Railway u staging i produkce. Když není nastaveno, akce zůstává jen za admin loginem.
DANGER_PASSWORD = os.getenv("DANGER_PASSWORD", "").strip()

def _check_danger(request: Request):
    """Ověří druhé heslo pro destruktivní akce. Bez správného hesla vyhodí 403."""
    if not DANGER_PASSWORD:
        return  # nenakonfigurováno → spoléháme jen na admin gate
    key = request.headers.get("x-danger-key", "")
    if not (key and hmac.compare_digest(key, DANGER_PASSWORD)):
        raise HTTPException(403, "Neplatné heslo pro nebezpečnou akci.")

@app.get("/api/danger/status")
def danger_status():
    """Zjistí, jestli je vůbec nastavené heslo pro citlivé sekce (bez prozrazení hesla)."""
    return {"enabled": bool(DANGER_PASSWORD)}

@app.post("/api/danger/verify")
def danger_verify(request: Request):
    """Ověří heslo pro odemčení citlivých sekcí (Nastavení/Provize). Vrací 403 při chybě."""
    _check_danger(request)
    return {"ok": True}

# ── Migrace DB: export/import (chráněno admin_gate + DANGER_PASSWORD) ──
# Používá se při přesunu do jiného regionu (US → EU): export ze staré DB, import do nové.
@app.get("/api/db-export")
def admin_db_export(request: Request):
    _check_danger(request)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        db_load(),
        headers={"Content-Disposition": 'attachment; filename="smartestguide-db-export.json"'},
    )

@app.post("/api/db-import")
async def admin_db_import(request: Request):
    """POZOR: nahradí CELOU databázi nahraným JSONem. Jen pro migraci."""
    _check_danger(request)
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Neplatny JSON")
    if not isinstance(data, dict) or "hotels" not in data:
        raise HTTPException(400, "JSON musi obsahovat klic 'hotels' (kompletni export DB).")
    db_save(data)
    return {"status": "ok", "hotels": len(data.get("hotels", {})), "note": "DB nahrazena importem"}

# ─────────────────────────────────────────────
# Lokální JSON databáze
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DATA_PATH", os.path.join(os.path.dirname(__file__), "data.json"))

# ── Databáze ──────────────────────────────────
# Pokud je nastaven DATABASE_URL (Railway/Supabase/Neon Postgres), používáme Postgres:
#   celá DB je uložená jako jeden JSONB řádek (kv_store id=1). Zápisy jsou atomické
#   v transakci → žádná korupce při souběhu (na rozdíl od jednosouborového JSON).
#   Data se při prvním startu jednorázově zmigrují z data.json.
# Bez DATABASE_URL běží fallback na soubor (dev / než se Postgres nasadí).
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_PG_ENABLED = False
_pg_write_lock = threading.Lock()

def _pg_connect():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)

def _pg_init_once():
    """Jeden pokus: připojení, tabulka, jednorázová migrace z data.json. Vyhodí výjimku při chybě."""
    global _PG_ENABLED
    conn = _pg_connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS kv_store ("
                    "id INT PRIMARY KEY, data JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT now())")
        cur.execute("SELECT 1 FROM kv_store WHERE id=1")
        if cur.fetchone() is None:
            seed = {"hotels": {}, "settings": {}}
            try:
                if os.path.exists(DB_PATH):
                    with open(DB_PATH, "r", encoding="utf-8") as f:
                        seed = json.load(f)
                    logging.warning("Migrace: nacteno z data.json (hotels=%d)", len(seed.get("hotels", {})))
            except Exception as e:
                logging.warning("Migrace: cteni data.json selhalo: %s", e)
            from psycopg2.extras import Json
            cur.execute("INSERT INTO kv_store (id, data) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",
                        (Json(seed),))
            logging.warning("Postgres kv_store inicializovan (seed hotels=%d)", len(seed.get("hotels", {})))
    conn.close()
    _PG_ENABLED = True

def _pg_init():
    """Inicializuje Postgres s několika pokusy — privátní síť Railway může při startu chvíli chybět."""
    global _PG_ENABLED
    if not DATABASE_URL:
        logging.warning("Databaze: SOUBOROVY REZIM (DATABASE_URL nenastaven)")
        return
    import time as _t
    attempts = 8
    for i in range(1, attempts + 1):
        try:
            _pg_init_once()
            logging.warning("Databaze: POSTGRES AKTIVNI (pokus %d)", i)
            return
        except Exception as e:
            logging.warning("Postgres pripojeni pokus %d/%d selhalo: %s", i, attempts, e)
            if i < attempts:
                _t.sleep(3)
    _PG_ENABLED = False
    logging.error("Postgres init SELHAL po %d pokusech -> fallback na soubor", attempts)

def db_load() -> dict:
    if _PG_ENABLED:
        try:
            conn = _pg_connect()
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM kv_store WHERE id=1")
                row = cur.fetchone()
            conn.close()
            if row and row[0] is not None:
                return row[0]  # psycopg2 vraci JSONB primo jako dict
            return {"hotels": {}, "settings": {}}
        except Exception as e:
            logging.error("db_load Postgres chyba -> fallback soubor: %s", e)
    if os.path.exists(DB_PATH):
        with open(DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"hotels": {}, "settings": {}}

def db_save(data: dict):
    if _PG_ENABLED:
        try:
            from psycopg2.extras import Json
            with _pg_write_lock:
                conn = _pg_connect()
                conn.autocommit = False
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO kv_store (id, data, updated_at) VALUES (1, %s, now()) "
                        "ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
                        (Json(data),))
                conn.commit()
                conn.close()
            return
        except Exception as e:
            logging.error("db_save Postgres chyba -> fallback soubor: %s", e)
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# Firemní údaje dodavatele NA TVRDO — přebijí cokoli v data.json (reset při deployi
# na Railway). Bez diakritiky (faktura běží na Helvetice). Bank/IBAN se doplní později.
_COMPANY_HARDCODED = {
    "company_name": "Native Hotel Guide s.r.o.",   # registrovaný název dle ARES (IČO 23112905)
    "company_address": "Korunni 2569/108",
    "company_city": "101 00 Praha 10",
    "company_ico": "23112905",
    "company_dic": "",                              # neni platce DPH (potvrzeno v ARES)
    "company_email": "admin@smartestguide.com",
    "company_vat_payer": False,
    "company_bank": "1947110004/5500",             # Raiffeisenbank
    "company_iban": "CZ9855000000001947110004",
}

def db_get_settings() -> dict:
    # Firemní údaje se vždy vynutí na tvrdo přes to, co je uložené.
    return {**db_load().get("settings", {}), **_COMPANY_HARDCODED}

def db_save_settings(s: dict):
    data = db_load()
    data["settings"] = {**data.get("settings", {}), **s}
    db_save(data)

# ─────────────────────────────────────────────
# Slug hotelu — čitelná guest URL (/h/{slug}) místo UUID.
# Guest běží pod prefixem /h/, takže slug NIKDY nekoliduje s /admin, /portal apod.
# ─────────────────────────────────────────────
import unicodedata as _unicodedata

def _slugify(name: str) -> str:
    """Název hotelu -> URL-safe slug (bez diakritiky, malá písmena)."""
    s = _unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "hotel"

def _unique_slug(db: dict, base: str, exclude_id: str = "") -> str:
    """Zajistí unikátnost slugu napříč hotely (kolize -> -2, -3…)."""
    taken = {h.get("slug") for hid, h in db.get("hotels", {}).items()
             if hid != exclude_id and h.get("slug")}
    if base not in taken:
        return base
    i = 2
    while f"{base}-{i}" in taken:
        i += 1
    return f"{base}-{i}"

def _ensure_slug(db: dict, hotel_id: str, hotel: dict) -> str:
    """Doplní slug hotelu pokud chybí. Vrací slug. NEUKLÁDÁ (uloží volající)."""
    if hotel.get("slug"):
        return hotel["slug"]
    slug = _unique_slug(db, _slugify(hotel.get("name", "")), exclude_id=hotel_id)
    hotel["slug"] = slug
    return slug

def _resolve_hotel(db: dict, ident: str):
    """Najde (hotel_id, hotel) podle ID nebo slugu. Vrací (None, None) když nenalezen."""
    if not ident:
        return None, None
    h = db.get("hotels", {}).get(ident)
    if h:
        return ident, h
    for hid, hh in db.get("hotels", {}).items():
        if hh.get("slug") == ident:
            return hid, hh
    return None, None

def _guest_url(base: str, hotel_id: str, hotel: dict = None) -> str:
    """Veřejná guest URL — slug když existuje, jinak UUID fallback."""
    ident = (hotel or {}).get("slug") or hotel_id
    return f"{base.rstrip('/')}/h/{ident}"

def _backfill_slugs():
    """Doplní slug všem hotelům, kteří ho ještě nemají (jednorázově při startu)."""
    try:
        db = db_load()
        changed = False
        for hid, h in db.get("hotels", {}).items():
            if not h.get("slug"):
                _ensure_slug(db, hid, h)
                changed = True
        if changed:
            db_save(db)
            logging.warning("Slug backfill: doplněny slugy hotelů")
    except Exception as e:
        logging.error("Slug backfill selhal: %r", e)

# Inicializuj databázi (Postgres pokud DATABASE_URL, jinak soubor) — MUSÍ být před db_load()
_pg_init()

# Načti nastavení z env proměnných při startu
init_settings_from_env()

# Doplň slugy existujícím hotelům (čitelné guest URL)
_backfill_slugs()

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
    wifi_name: Optional[str] = None
    wifi_password: Optional[str] = None
    phone2: Optional[str] = None
    nav_pool: Optional[str] = None
    nav_wellness: Optional[str] = None
    nav_fitness: Optional[str] = None
    nav_restaurant: Optional[str] = None
    nav_bar: Optional[str] = None
    nav_parking: Optional[str] = None
    nav_arrival: Optional[str] = None
    nav_elevator: Optional[str] = None
    nav_conference: Optional[str] = None
    nav_other: Optional[str] = None
    nav_custom: Optional[list] = None
    whatsapp_number: Optional[str] = None
    whatsapp_wellness: Optional[str] = None
    whatsapp_restaurant: Optional[str] = None
    whatsapp_sport: Optional[str] = None
    star_rating: Optional[int] = None
    country: Optional[str] = None
    continent: Optional[str] = None
    pet_policy: Optional[str] = None     # pravidla pro mazlíčky (časté dotazy hostů)
    # PMS napojení (per hotel) — nastavuje jen admin; credentials NIKDY ke guestům
    pms_type: Optional[str] = None          # 'apaleo' | '' (vypnuto)
    pms_client_id: Optional[str] = None
    pms_client_secret: Optional[str] = None
    pms_property_id: Optional[str] = None
    ico: Optional[str] = None            # fakturační IČO hotelu (odběratel)
    dic: Optional[str] = None            # DIČ / VAT ID hotelu
    billing_name: Optional[str] = None   # právní/fakturační název (fallback name)
    extra_info: Optional[str] = None
    active_offer: Optional[str] = None
    hidden_gems: Optional[List[str]] = None
    restaurants: Optional[List[dict]] = None   # opakovatelné restaurace s jídelníčky
    subscription_active: Optional[bool] = None
    stripe_customer_id: Optional[str] = None
    scraped_pages: Optional[List[str]] = None
    menu_urls: Optional[List[str]] = None
    custom_fields: Optional[List[dict]] = None

# ─────────────────────────────────────────────
# Nastavení API klíče
# ─────────────────────────────────────────────
@app.get("/api/version")
def get_version():
    import subprocess
    commit = os.getenv("RAILWAY_GIT_COMMIT_SHA", "")
    if not commit:
        try:
            commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
        except Exception:
            commit = "unknown"
    return {"version": APP_VERSION, "commit": commit, "started": APP_START_TIME,
            "db": "postgres" if _PG_ENABLED else "file"}

@app.get("/api/settings")
def get_settings():
    s = db_get_settings()
    return {
        "has_api_key": bool(s.get("anthropic_api_key")),
        "api_key_preview": ("sk-ant-..." + s["anthropic_api_key"][-6:]) if s.get("anthropic_api_key") else None,
        "has_stripe_key": bool(s.get("stripe_secret_key")),
        "stripe_payment_link": s.get("stripe_payment_link", ""),
        "stripe_key_preview": ((s["stripe_secret_key"].split("_")[0] + "_" + s["stripe_secret_key"].split("_")[1] + "_..." + s["stripe_secret_key"][-6:]) if s.get("stripe_secret_key") and s["stripe_secret_key"].count("_") >= 2 else (s["stripe_secret_key"][:8] + "..." if s.get("stripe_secret_key") else None)),
        "stripe_mode": ("live" if s.get("stripe_secret_key","").startswith("sk_live_") else "test" if s.get("stripe_secret_key","").startswith("sk_test_") else None),
        "has_webhook_secret": bool(s.get("stripe_webhook_secret")),
        "has_brevo": bool(os.getenv("BREVO_API_KEY")),
        "pricing_base": s.get("pricing_base", 300),
        "pricing_threshold": s.get("pricing_threshold", 100),
        "pricing_per_bed": s.get("pricing_per_bed", 3),
    }

class PricingSettingsRequest(BaseModel):
    pricing_base: int = 300
    pricing_threshold: int = 100
    pricing_per_bed: float = 3.0

@app.post("/api/settings/pricing")
def save_pricing_settings(req: PricingSettingsRequest):
    if req.pricing_base < 1:
        raise HTTPException(400, "Základní cena musí být kladná")
    if req.pricing_threshold < 1:
        raise HTTPException(400, "Limit lůžek musí být kladný")
    db_save_settings({
        "pricing_base": req.pricing_base,
        "pricing_threshold": req.pricing_threshold,
        "pricing_per_bed": req.pricing_per_bed,
    })
    return {"status": "ok"}

@app.post("/api/pricing/apply-to-new")
def apply_pricing_to_new_hotels():
    """Aplikuje aktuální ceník na hotely které ještě nezaplatily (nemají subscription_price).
    Hotely s aktivním předplatným a subscription_price zůstávají nedotčeny."""
    db = db_load()
    s = db_get_settings()
    base      = int(s.get("pricing_base", 200))
    threshold = int(s.get("pricing_threshold", 100))
    per_bed   = float(s.get("pricing_per_bed", 3))
    updated = []
    skipped = []
    for hotel_id, hotel in db["hotels"].items():
        # Přeskočit hotely, které už mají zaplacenou cenu NEBO jsou označené jako zdarma
        if (hotel.get("subscription_price") and hotel.get("subscription_active")) or hotel.get("is_free"):
            skipped.append(hotel.get("name", hotel_id))
            continue
        # Aplikovat novou cenu na ostatní
        beds = hotel.get("bed_count", 0) or 0
        price = base if beds <= threshold else base + (beds - threshold) * per_bed
        db["hotels"][hotel_id]["subscription_price"] = round(price, 2)
        updated.append({"name": hotel.get("name", hotel_id), "price": round(price, 2)})
    db_save(db)
    return {
        "status": "ok",
        "updated": len(updated),
        "skipped": len(skipped),
        "details": updated,
        "message": f"Aktualizováno {len(updated)} hotelů, přeskočeno {len(skipped)} platících hotelů"
    }

class StripeSettingsRequest(BaseModel):
    stripe_secret_key: str
    stripe_payment_link: Optional[str] = None   # už se nepoužívá (reaktivace jede dynamicky) — necháno kvůli kompatibilitě
    stripe_webhook_secret: Optional[str] = None

@app.post("/api/settings/stripe")
def save_stripe_settings(req: StripeSettingsRequest, request: Request):
    _check_danger(request)
    key = req.stripe_secret_key.strip()
    if not (key.startswith("sk_test_") or key.startswith("sk_live_")):
        raise HTTPException(400, "Neplatny Stripe klic")
    db_save_settings({
        "stripe_secret_key": key,
        "stripe_payment_link": (req.stripe_payment_link or "").strip(),
        "stripe_webhook_secret": req.stripe_webhook_secret or "",
    })
    return {"status": "ok"}

@app.post("/api/settings/api-key")
def save_api_key(req: ApiKeyRequest, request: Request):
    _check_danger(request)
    key = req.api_key.strip()
    if not key.startswith("sk-ant-"):
        raise HTTPException(400, "Neplatný API klíč – musí začínat 'sk-ant-'")
    db_save_settings({"anthropic_api_key": key})
    return {"status": "ok", "preview": "sk-ant-..." + key[-6:]}

@app.delete("/api/settings/api-key")
def delete_api_key(request: Request):
    _check_danger(request)
    db_save_settings({"anthropic_api_key": ""})
    return {"status": "ok"}

# ─────────────────────────────────────────────
# Scraping
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "cs,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
SUBPAGE_HINTS = [
    "/contact", "/kontakt", "/about", "/o-nas",
    "/faq", "/informace", "/good-to-know", "/dobre-vedet",
    "/house-rules", "/domaci-rad", "/wifi",
    "/restaurant", "/restaurace", "/wellness", "/spa",
    "/services", "/sluzby", "/rooms", "/pokoje",
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
        r = await client.get(url, timeout=20.0, follow_redirects=True)
        ct = r.headers.get("content-type", "").lower()
        head = (r.text or "")[:2000].lower()
        # Tolerantnější: přijmi i weby bez přesného text/html v content-type,
        # pokud tělo vypadá jako HTML dokument.
        if r.status_code == 200 and ("html" in ct or "<html" in head or "<!doctype html" in head):
            return r.text
    except Exception:
        pass
    return None

async def scrape_hotel_data(url: str, api_key: str) -> dict:
    if not url.startswith("http"):
        url = "https://" + url

    async with httpx.AsyncClient(headers=HEADERS, timeout=20.0, follow_redirects=True) as client:
        # Zkus víc variant hlavní stránky — přehození www a http fallback (některé weby
        # jednu variantu blokují nebo přesměrovávají jinam).
        _p = urlparse(url)
        _alt = _p.netloc[4:] if _p.netloc.startswith("www.") else "www." + _p.netloc
        candidates = [url, f"{_p.scheme}://{_alt}{_p.path or ''}", f"http://{_p.netloc}{_p.path or ''}"]
        seen, main_html = set(), None
        for cu in candidates:
            if cu in seen:
                continue
            seen.add(cu)
            main_html = await fetch_page(client, cu)
            if main_html:
                url = cu
                break
        if not main_html:
            raise ValueError(f"Nepodařilo se stáhnout {url} – web možná blokuje boty, běží jen v JavaScriptu, nebo je nedostupný")

        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"

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

        results = await asyncio.gather(*[try_sub(h) for h in SUBPAGE_HINTS[:8]])
        pages_text += [r for r in results if r]

    # Celkový limit znaků – Haiku zvládne víc, lepší pokrytí praktických info (WiFi, FAQ, pravidla)
    combined = "\n\n".join(pages_text)[:12000]

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
  "wifi_name": "nazev WiFi site / SSID pokud je uveden, jinak null",
  "wifi_password": "heslo WiFi pouze pokud je verejne uvedeno na webu, jinak null",
  "pet_policy": "pravidla pro mazlicky / domaci zvirata nebo null",
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

_RESCRAPE_PROTECTED = {
    "id", "created_at", "qr_code_id", "hotel_token", "registered_bed_count",
    "subscription_active", "subscription_start", "subscription_period_end",
    "stripe_customer_id", "stripe_subscription_id", "ico", "dic", "billing_name",
}

@app.post("/api/hotels/{hotel_id}/rescrape")
async def rescrape_hotel(hotel_id: str):
    """Znovu stáhne data z webu hotelu a doplní POUZE prázdná pole (nepřepisuje ruční úpravy)."""
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(400, "Anthropic API klíč není nastaven.")
    db = db_load()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")
    url = hotel.get("url") or hotel.get("source_url")
    if not url:
        raise HTTPException(400, "Hotel nemá uvedený web (URL)")
    try:
        scraped = await scrape_hotel_data(url, api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    filled = []
    for k, v in (scraped or {}).items():
        if k in _RESCRAPE_PROTECTED:
            continue
        if v in (None, "", [], {}):
            continue
        if not hotel.get(k):  # doplň jen prázdné — ruční data zůstávají
            hotel[k] = v
            filled.append(k)
    hotel["updated_at"] = datetime.utcnow().isoformat()
    hotel["last_rescrape_at"] = datetime.utcnow().isoformat()
    db["hotels"][hotel_id] = hotel
    db_save(db)
    return {"status": "ok", "filled_fields": filled, "filled_count": len(filled)}

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
    _ensure_slug(db, hid, hotel)  # čitelná guest URL /h/{slug}
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
        ("wifi_name", "WiFi síť"),
        ("wifi_password", "WiFi heslo"),
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
        ("pet_policy", "Pravidla pro mazlíčky"),
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
    safe = {k: v for k, v in h.items() if k not in (
        "hotel_token", "pms_client_secret", "pms_refresh_token", "pms_oauth_state", "pms_oauth_state_at")}
    return {"status": "ok", "hotel": safe}

class HotelPortalUpdate(BaseModel):
    description: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    phone2: Optional[str] = None
    pms_property_id: Optional[str] = None   # kód property v PMS (hotel ho smí nastavit sám; credentials NE)
    nav_pool: Optional[str] = None
    nav_wellness: Optional[str] = None
    nav_fitness: Optional[str] = None
    nav_restaurant: Optional[str] = None
    nav_bar: Optional[str] = None
    nav_parking: Optional[str] = None
    nav_arrival: Optional[str] = None
    nav_elevator: Optional[str] = None
    nav_conference: Optional[str] = None
    nav_other: Optional[str] = None
    nav_custom: Optional[list] = None
    whatsapp_number: Optional[str] = None
    whatsapp_wellness: Optional[str] = None
    whatsapp_restaurant: Optional[str] = None
    whatsapp_sport: Optional[str] = None
    checkin_time: Optional[str] = None
    checkout_time: Optional[str] = None
    breakfast_hours: Optional[str] = None
    lunch_hours: Optional[str] = None
    dinner_hours: Optional[str] = None
    restaurant_name: Optional[str] = None
    wellness_info: Optional[str] = None
    wifi_name: Optional[str] = None
    wifi_password: Optional[str] = None
    parking_info: Optional[str] = None
    amenities: Optional[List[str]] = None
    nearby_places: Optional[List[str]] = None
    hidden_gems: Optional[List[str]] = None
    extra_info: Optional[str] = None
    active_offer: Optional[str] = None
    bed_count: Optional[int] = None
    room_count: Optional[int] = None
    star_rating: Optional[int] = None
    address: Optional[str] = None
    country: Optional[str] = None
    menu_urls: Optional[List[str]] = None
    custom_fields: Optional[List[dict]] = None

_PORTAL_PROTECTED = {
    "id", "hotel_token", "created_at", "qr_code_id", "registered_bed_count",
    "subscription_active", "subscription_start", "subscription_period_end",
    "subscription_paid_beds", "stripe_customer_id", "stripe_subscription_id",
    "acquired_by", "referral_code", "acquired_at", "trial_used", "trial_start",
    "is_free",
}

@app.patch("/api/hotel-portal/update")
def hotel_portal_update(token: str, data: dict = Body(default={})):
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny pristupovy token")
    db = db_load()
    # Přijmi libovolná pole hotelu (kromě chráněných) — ať se uloží vše z portálu
    update_data = {k: v for k, v in (data or {}).items() if k not in _PORTAL_PROTECTED}
    # Zpětná kompatibilita: odvoď restaurant_name / nav_restaurant / menu_urls z restaurants[]
    if "restaurants" in update_data:
        rests = update_data.get("restaurants") or []
        update_data["restaurant_name"] = ((rests[0].get("name") if rests else "") or None)
        update_data["nav_restaurant"] = ((rests[0].get("directions") if rests else "") or None)
        menus = [m.get("url") for r in rests for m in (r.get("menus") or []) if m.get("url")]
        update_data["menu_urls"] = menus or None
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
    now = datetime.utcnow()
    # Zrušení platí od konce aktuálního placeného období — NEDEAKTIVUJEME OKAMŽITĚ
    # Služba běží do subscription_period_end, poté se automaticky deaktivuje
    db["hotels"][h["id"]]["subscription_cancel_requested"] = True
    db["hotels"][h["id"]]["subscription_cancel_requested_at"] = now.isoformat()
    db["hotels"][h["id"]]["updated_at"] = now.isoformat()
    # Zruš v Stripe (zakáže automatické obnovení)
    stripe_sub_id = h.get("stripe_subscription_id", "")
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "")
    if stripe_sub_id and stripe_key:
        try:
            import httpx as _httpx
            _httpx.post(
                f"https://api.stripe.com/v1/subscriptions/{stripe_sub_id}",
                headers={"Authorization": f"Bearer {stripe_key}"},
                data={"cancel_at_period_end": "true"},
                timeout=10
            )
        except Exception as e:
            logging.warning("Stripe cancel error: %s", e)
    db_save(db)
    period_end = h.get("subscription_period_end", "")
    safe = {k: v for k, v in db["hotels"][h["id"]].items() if k != "hotel_token"}
    return {
        "status": "ok",
        "message": "Předplatné bude zrušeno na konci aktuálního období.",
        "active_until": period_end,
        "hotel": safe
    }

@app.get("/api/hotel-portal/invoices")
def hotel_portal_invoices(token: str):
    """Vrátí faktury pro hotel portál."""
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    db = db_load()
    # Portál hotelu ukazuje jen reálné faktury — trial (nulový) záznam je jen pro admin
    invoices = [inv for inv in db.get("invoices", {}).values()
                if inv.get("hotel_id") == h["id"] and inv.get("status") != "trial"]
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

@app.get("/api/hotel-portal/monthly-report")
def hotel_portal_monthly_report(token: str, month: str = ""):
    """Měsíční přehled pro hotelový portál (default: minulý měsíc + aktuální měsíc doposud)."""
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatny token")
    country = (h.get("country") or "").upper()
    lang = "cs" if country in ("CZ", "SK") else "en"
    lname = _LANG_NAMES.get(lang, _LANG_NAMES["en"])
    db = db_load()
    a = db.get("analytics", {}).get(h["id"], {}) or {}
    monthly = a.get("monthly", {}) or {}

    def summarize(mk: str) -> dict:
        cur = monthly.get(mk, {"count": 0, "flagged": 0, "langs": {}})
        prev = monthly.get(_month_key_shift(mk, -1), {"count": 0})
        count = int(cur.get("count", 0))
        prev_count = int(prev.get("count", 0))
        pct = round((count - prev_count) / prev_count * 100) if prev_count > 0 else None
        langs = cur.get("langs", {}) or {}
        top = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:3]
        saved_min = count * 2
        return {
            "month_key": mk,
            "month_label": _month_label(mk, lang),
            "count": count,
            "prev_count": prev_count,
            "trend_pct": pct,
            "visits": int(cur.get("visits", 0)),
            "visitors": len(cur.get("visit_devices") or {}),
            "flagged": int(cur.get("flagged", 0)),
            "top_langs": [{"code": c, "name": lname.get(c, c), "count": n} for c, n in top],
            "saved_minutes": saved_min,
            "saved_label": (f"~{round(saved_min/60,1)} h" if saved_min >= 60 else f"~{saved_min} min"),
        }

    report_key = month or _prev_month_key()
    current_key = datetime.utcnow().strftime("%Y-%m")
    comp = hotel_profile_completeness(h)
    return {
        "status": "ok",
        "lang": lang,
        "profile_score": comp.get("score", 0) if isinstance(comp, dict) else 0,
        "report": summarize(report_key),
        "current": summarize(current_key),
        "total_questions": int(a.get("total", 0)),
        "total_visits": int(a.get("visits_total", 0)),
        "available_months": sorted(monthly.keys(), reverse=True),
    }

@app.get("/api/hotels/{hotel_id}/monthly-report/preview", response_class=HTMLResponse)
def admin_monthly_report_preview(hotel_id: str, month: str = ""):
    """Náhled měsíčního reportu (admin) — vrátí HTML e-mailu."""
    rep = build_monthly_report(hotel_id, month or None)
    if not rep:
        raise HTTPException(404, "Hotel nenalezen")
    return HTMLResponse(rep["html"])

@app.post("/api/hotels/{hotel_id}/monthly-report/send")
async def admin_monthly_report_send(hotel_id: str, month: str = "", dry_run: bool = False):
    """Ruční odeslání měsíčního reportu hotelu (admin)."""
    return await send_monthly_report(hotel_id, month_key=(month or None), dry_run=dry_run)

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
    return {"status": "ok", "token": token, "portal_url": f"{base}/portal?token={token}"}

def _admin_safe_hotel(h: dict) -> dict:
    """Kopie hotelu bez PMS credentials — refresh tokeny nepatří ani do admin API.
    Místo hodnot vrací jen příznak, že secret existuje (pro placeholder v admin editu)."""
    out = dict(h)
    out["pms_client_secret_set"] = bool(h.get("pms_client_secret"))
    for k in ("pms_refresh_token", "pms_oauth_state", "pms_oauth_state_at", "pms_client_secret"):
        out.pop(k, None)
    return out

@app.get("/api/hotels")
def list_hotels():
    db = db_load()
    hotels = sorted(db["hotels"].values(), key=lambda h: h.get("created_at", ""), reverse=True)
    return {"status": "ok", "hotels": [_admin_safe_hotel(h) for h in hotels]}

@app.get("/api/hotels/{hotel_id}")
def get_hotel(hotel_id: str):
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    return {"status": "ok", "hotel": _admin_safe_hotel(h)}

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

@app.post("/api/hotels/{hotel_id}/free")
def set_hotel_free(hotel_id: str, data: dict = Body(default={})):
    """Označí/zruší hotel jako ZDARMA. Zdarma hotel má cenu 0 a přeskočí ho hromadné
    přecenění (apply-to-new), takže se cena nemůže omylem překlopit na standardní ceník."""
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    is_free = bool(data.get("is_free"))
    h["is_free"] = is_free
    if is_free:
        h["subscription_price"] = 0
    h["updated_at"] = datetime.utcnow().isoformat()
    db_save(db)
    return {"status": "ok", "is_free": is_free}

@app.delete("/api/hotels/{hotel_id}")
def delete_hotel(hotel_id: str, request: Request, hard: bool = False):
    """Produkce: aktivní hotel se NEMAŽE, jen deaktivuje/archivuje (subscription_active=false + archived).
    Tvrdé smazání (hard=1) je povolené pro TESTOVACÍ hotely NEBO už ARCHIVOVANÉ hotely
    (nejdřív deaktivovat, pak smazat) — vždy s heslem DANGER_PASSWORD.
    Aktivní reálný hotel nejde smazat jedním krokem. Faktury se nikdy nemažou."""
    if hard:
        _check_danger(request)  # tvrdé smazání vyžaduje druhé heslo
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    is_test = str(h.get("name", "")).startswith("E2E") or (h.get("url") in ("https://example.com", "http://example.com"))
    if hard and (is_test or h.get("archived")):
        del db["hotels"][hotel_id]
        db_save(db)
        return {"status": "ok", "deleted": True}
    if hard:
        # Aktivní reálný hotel nejde smazat napřímo — nejdřív ho deaktivuj.
        raise HTTPException(400, "Aktivní hotel nelze smazat natrvalo. Nejdřív ho deaktivuj, pak smaž.")
    # Deaktivace/archivace — data i historie zůstávají (reálný hotel nejde smazat).
    h["subscription_active"] = False
    h["archived"] = True
    h["archived_at"] = datetime.utcnow().isoformat()
    db["hotels"][hotel_id] = h
    db_save(db)
    return {"status": "ok", "archived": True}

def _is_test_hotel(h: dict) -> bool:
    """Testovací hotel = E2E jméno nebo example.com URL. Jen tyhle jde tvrdě smazat."""
    return (str(h.get("name", "")).startswith("E2E")
            or (h.get("url") in ("https://example.com", "http://example.com"))
            or (h.get("source_url") in ("https://example.com", "http://example.com")))

@app.post("/api/hotels/purge-test")
def purge_test_hotels(request: Request):
    """Úklid před startem: natvrdo smaže VŠECHNY testovací hotely (E2E / example.com).
    Reálných hotelů se NEDOTKNE. Faktury a provize zůstávají. Admin-gated + DANGER_PASSWORD."""
    _check_danger(request)
    db = db_load()
    victims = [(hid, h.get("name", "")) for hid, h in db["hotels"].items() if _is_test_hotel(h)]
    for hid, _ in victims:
        del db["hotels"][hid]
    if victims:
        db_save(db)
        logging.warning("Purge testovacích hotelů: smazáno %d (%s)", len(victims), [n for _, n in victims])
    return {"status": "ok", "deleted": len(victims), "names": [n for _, n in victims]}

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
def _generate_qr_png_branded(data: str, size: int = 400) -> bytes:
    """Generuje zlatý QR kód jako PNG pomocí Pillow — kreslí matici ručně + SG logo uprostřed."""
    import qrcode
    from PIL import Image, ImageDraw, ImageFont
    from io import BytesIO

    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=1, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)

    cell = size // n
    img_size = cell * n
    img = Image.new("RGB", (img_size, img_size), (10, 11, 15))
    draw = ImageDraw.Draw(img)

    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                x0, y0 = c * cell, r * cell
                draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=(255, 107, 0))

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

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
    guest_url = _guest_url(base, hotel_id, hotel)
    try:
        png_bytes = _generate_qr_png_branded(guest_url, size=400)
    except Exception as e:
        raise HTTPException(500, f"Chyba generování QR: {e}")
    buf = BytesIO()
    buf.write(png_bytes)
    return {"status": "ok", "qr_base64": base64.b64encode(png_bytes).decode(), "guest_url": guest_url}

# QR plakát hub — výběr formátu
# ─────────────────────────────────────────────
@app.get("/api/hotels/{hotel_id}/qr-poster")
def generate_qr_poster(hotel_id: str, request: Request):
    db = db_load()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")

    base = get_base_url(request)
    guest_url = _guest_url(base, hotel_id, hotel)
    hotel_name = hotel.get("name", "Hotel")
    hotel_token = hotel.get("hotel_token", "")
    portal_url = f"{base}/portal?token={hotel_token}" if hotel_token else ""
    portal_label = "Přejít do portálu hotelu" if (hotel.get("country") or "").upper() in ("CZ","SK") else "Go to hotel portal"
    country = (hotel.get("country") or "").upper()
    is_cs = country in ("CZ", "SK")
    primary_lang = "cz" if is_cs else "en"
    btn_print = "🖨️ Otevřít a tisknout" if is_cs else "🖨️ Open and print"
    hub_title = "Tiskové materiály" if is_cs else "Print materials"
    qr_poster_label = "QR Plakát · 800×800px" if is_cs else "QR Poster · 800×800px"
    qr_poster_desc = "Čtvercový plakát s QR kódem. Ideální pro recepci, výtah nebo restaurační stoly." if is_cs else "Square poster with QR code. Perfect for reception, elevator or restaurant tables."
    local_lang = get_hotel_local_lang(hotel)
    local_lang_name = get_flyer_lang_name(local_lang)
    has_local = local_lang != "en"

    # EN je vždy primární, lokální jazyk je sekundární
    flyer_primary_name = f"A4 Flyer · English"
    flyer_primary_desc = "English A4 flyer, print-ready."
    flyer_primary_url = "flyer-en"
    flyer_secondary_name = f"A4 Leták · {local_lang_name}" if has_local else "A4 Leták · Česky"
    flyer_secondary_desc = f"Leták v jazyce {local_lang_name} pro místní hosty." if has_local else "Český leták pro české hosty."
    flyer_secondary_url = "flyer-local" if has_local else "flyer-cz"
    rollup_desc = "Vysoký banner pro lobby, veletrh nebo konferenci." if is_cs else "Tall banner for lobby or trade show."

    # Vlajky jako inline SVG (stejné jako v designérových letácích)
    flags_svg = """
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="8" fill="#fff"/><rect y="8" width="24" height="8" fill="#D7141A"/><path d="M0 0 L12 8 L0 16 Z" fill="#11457E"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#012169"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#fff" stroke-width="3.2"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#C8102E" stroke-width="1.6"/><path d="M12 0 V16 M0 8 H24" stroke="#fff" stroke-width="5"/><path d="M12 0 V16 M0 8 H24" stroke="#C8102E" stroke-width="3"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#000"/><rect y="5.33" width="24" height="5.33" fill="#DD0000"/><rect y="10.66" width="24" height="5.34" fill="#FFCE00"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#002395"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#ED2939"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#009246"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#CE2B37"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#c60b1e"/><rect y="5.5" width="24" height="5" fill="#ffc400"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#fff"/><rect y="10.67" width="24" height="5.33" fill="#dc143c"/><rect width="8" height="16" fill="#dc143c"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#fff"/><rect x="8" width="8" height="16" fill="#0b4ea2"/><rect x="16" width="8" height="16" fill="#fff"/><rect y="6" width="24" height="4" fill="#ee1c25"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#477050"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#ce2939"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#003DA5"/><rect y="10.66" width="24" height="5.34" fill="#fff"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#EE1C25"/><rect x="4" y="3" width="6" height="10" fill="#FFFF00" rx="3"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#fff"/><circle cx="12" cy="8" r="5" fill="#BC002D"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#006C35"/><rect x="0" y="0" width="8" height="16" fill="#006C35"/><circle cx="8" cy="8" r="4" fill="#fff"/></svg></span>
      <span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#003DA5"/><rect y="10.66" width="24" height="5.34" fill="#CE1126"/></svg></span>"""
    flags_svg_small = """
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="8" fill="#fff"/><rect y="8" width="24" height="8" fill="#D7141A"/><path d="M0 0 L12 8 L0 16 Z" fill="#11457E"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#012169"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#fff" stroke-width="3.2"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#C8102E" stroke-width="1.6"/><path d="M12 0 V16 M0 8 H24" stroke="#fff" stroke-width="5"/><path d="M12 0 V16 M0 8 H24" stroke="#C8102E" stroke-width="3"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#000"/><rect y="5.33" width="24" height="5.33" fill="#DD0000"/><rect y="10.66" width="24" height="5.34" fill="#FFCE00"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#002395"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#ED2939"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#009246"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#CE2B37"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#c60b1e"/><rect y="5.5" width="24" height="5" fill="#ffc400"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#fff"/><rect y="10.67" width="24" height="5.33" fill="#dc143c"/><rect width="8" height="16" fill="#dc143c"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#fff"/><rect x="8" width="8" height="16" fill="#0b4ea2"/><rect x="16" width="8" height="16" fill="#fff"/><rect y="6" width="24" height="4" fill="#ee1c25"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#477050"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#ce2939"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#003DA5"/><rect y="10.66" width="24" height="5.34" fill="#fff"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#EE1C25"/><rect x="4" y="3" width="6" height="10" fill="#FFFF00" rx="3"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#fff"/><circle cx="12" cy="8" r="5" fill="#BC002D"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#006C35"/><rect x="0" y="0" width="8" height="16" fill="#006C35"/><circle cx="8" cy="8" r="4" fill="#fff"/></svg></span>
      <span style="width:14px;height:10px;border-radius:3px;overflow:hidden;display:inline-block;box-shadow:none"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#003DA5"/><rect y="10.66" width="24" height="5.34" fill="#CE1126"/></svg></span>"""

    # QR JS generátor (sdílený pro všechny formáty)
    qr_js = f"""
function drawQR(holderId, size){{
  var holder = document.getElementById(holderId);
  if(!holder) return;
  if(!window.qrcode){{ setTimeout(function(){{drawQR(holderId,size);}}, 120); return; }}
  var qr = window.qrcode(0,'H');
  qr.addData('{guest_url}');
  qr.make();
  var n = qr.getModuleCount();
  var S = size || 300;
  var cell = S/n;
  var rects='';
  for(var r=0;r<n;r++){{
    for(var c=0;c<n;c++){{
      if(qr.isDark(r,c)){{
        rects+='<rect x="'+(c*cell).toFixed(2)+'" y="'+(r*cell).toFixed(2)+'" width="'+(cell+0.4).toFixed(2)+'" height="'+(cell+0.4).toFixed(2)+'" fill="#FF6B00"/>';
      }}
    }}
  }}
  holder.innerHTML='<svg width="'+S+'" height="'+S+'" viewBox="0 0 '+S+' '+S+'" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg">'+rects+'</svg>';
}}"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tiskové materiály — {hotel_name}</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#faf9f5;font-family:'Inter',sans-serif;color:#1a1a1a;min-height:100vh}}
.topbar{{position:fixed;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#00d4aa,#00d4aa 60%,#FF6B00);z-index:200}}
.hub{{max-width:960px;margin:0 auto;padding:60px 24px 80px}}
.hub-header{{text-align:center;margin-bottom:48px}}
.hub-logo{{font-family:'Syne',sans-serif;font-weight:800;font-size:28px;color:#1a1a1a;display:inline-flex;align-items:center;gap:4px;margin-bottom:8px}}
.hub-dot{{width:9px;height:9px;border-radius:50%;background:#FF6B00;box-shadow:0 0 10px rgba(255,107,0,.8);margin-left:2px}}
.hub-hotel{{font-size:15px;color:#6b6b6b;margin-top:4px}}
.hub-title{{font-family:'Syne',sans-serif;font-size:22px;font-weight:700;color:#1a1a1a;margin-top:16px}}
.formats{{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;margin-top:0}}
.fmt-card{{background:#181920;border:1px solid #222330;border-radius:16px;overflow:hidden;display:flex;flex-direction:column}}
.fmt-card:hover{{border-color:#FF6B00;box-shadow:0 4px 16px rgba(255,107,0,.12)}}
.fmt-preview{{background:#e9eaee;padding:24px;display:flex;justify-content:center;align-items:center;min-height:200px;cursor:pointer;position:relative;overflow:hidden}}
.fmt-preview::before{{content:'';position:absolute;inset:0;background:radial-gradient(ellipse 80% 80% at 50% 50%,rgba(0,0,0,.04),transparent 70%);pointer-events:none}}
.fmt-preview>div{{box-shadow:0 6px 18px rgba(0,0,0,.20)}}
.fmt-info{{padding:20px}}
.fmt-name{{font-family:'Syne',sans-serif;font-weight:700;font-size:16px;color:#f0ece0;margin-bottom:4px}}
.fmt-desc{{font-size:13px;color:#6b6b6b;line-height:1.5;margin-bottom:16px}}
.fmt-btn{{display:block;width:100%;background:#FF6B00;color:#0a0b0f;border:none;border-radius:8px;padding:11px;font-family:'Inter',sans-serif;font-size:14px;font-weight:700;cursor:pointer;text-align:center;text-decoration:none;transition:opacity .15s}}
.fmt-btn:hover{{opacity:.88}}
/* Print styles */
@media print{{
  .topbar,.hub-header,.fmt-info,.formats,.fmt-preview:not(.printing){{display:none!important}}
  body{{background:#fff;padding:0}}
  .active-print{{display:block!important}}
  @page{{margin:0;size:auto}}
}}
</style>
</head>
<body>
<div class="topbar"></div>

<div class="hub">
  <div class="hub-header">
    <div class="hub-logo">SmartestGuide<span class="hub-dot"></span></div>
    <div class="hub-hotel">{hotel_name}</div>
    <div class="hub-title">{hub_title}</div>
    {f'<a href="{portal_url}" style="display:inline-flex;align-items:center;gap:6px;margin-top:12px;background:rgba(255,107,0,.1);border:1px solid rgba(255,107,0,.3);border-radius:8px;padding:7px 16px;font-size:13px;font-weight:600;color:#FF6B00;text-decoration:none;transition:opacity .15s" onmouseover="this.style.opacity=.8" onmouseout="this.style.opacity=1">⚙️ {portal_label}</a>' if portal_url else ''}
    <div style="margin-top:16px;font-size:12px;color:#1a1a1a;font-weight:700;margin-bottom:7px">🎨 Vyberte vzhled tiskovin:</div>
    <div style="display:inline-flex;background:rgba(0,0,0,.04);border:1px solid rgba(0,0,0,.15);border-radius:10px;padding:4px;gap:4px">
      <button id="theme-dark" onclick="setFlyerTheme('dark')" style="border:1px solid transparent;background:#FF6B00;color:#0a0b0f;border-radius:8px;padding:7px 16px;font-size:12px;font-weight:700;cursor:pointer">🌙 Tmavá</button>
      <button id="theme-light" onclick="setFlyerTheme('light')" style="border:1px solid rgba(255,107,0,.55);background:transparent;color:#1a1a1a;border-radius:8px;padding:7px 16px;font-size:12px;font-weight:700;cursor:pointer">📄 Print-friendly</button>
    </div>
    <div style="margin-top:8px;font-size:11px;color:#333333">🌙 Tmavá = ideální pro PDF &nbsp;·&nbsp; 📄 <b style="color:#d95700">Print-friendly</b> (světlá) šetří inkoust při tisku na papír</div>
  </div>

  <div class="formats">

    <!-- Roll-up -->
    <div class="fmt-card">
      <div class="fmt-preview" onclick="openFormat('rollup')">
        <div style="width:65px;height:156px;background:#1a1a1a;border:1px solid rgba(255,107,0,.3);border-radius:6px;padding:8px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:space-between">
          <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:7px;color:#FF6B00;line-height:1.2">Your<br>personal<br>AI<br>concierge</div>
          <div style="font-size:7px;color:#00d4aa;font-weight:600;letter-spacing:.05em">🌍 100+ LANG</div>
          <div id="qr-rollup" style="width:44px;height:44px"></div>
          <div style="font-size:6px;color:#00d4aa">smartestguide.com</div>
        </div>
      </div>
      <div class="fmt-info">
        <div class="fmt-name">Roll-up Banner · 850×2000mm</div>
        <div class="fmt-desc">{rollup_desc}</div>
        <button class="fmt-btn" onclick="openFormat('rollup')">{btn_print}</button>
      </div>
    </div>
    <!-- A4 Primární jazyk -->
    <div class="fmt-card">
      <div class="fmt-preview" onclick="openFormat('{flyer_primary_url}')">
        <div style="width:110px;min-height:155px;background:#1a1a1a;border:1px solid rgba(255,107,0,.3);border-radius:8px;padding:10px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:6px">
          <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:9px;color:#FF6B00;line-height:1.2">{'Váš osobní<br>AI concierge' if is_cs else 'Your personal<br>AI concierge'}</div>
          <div style="font-size:8px;color:#00d4aa;font-weight:600;letter-spacing:.05em">🌍 100+ LANGUAGES</div>
          <div id="qr-a4-primary" style="width:70px;height:70px"></div>
          <div style="font-size:7px;color:#00d4aa">smartestguide.com</div>
        </div>
      </div>
      <div class="fmt-info">
        <div class="fmt-name">{flyer_primary_name}</div>
        <div class="fmt-desc">{flyer_primary_desc}</div>
        <button class="fmt-btn" onclick="openFormat('{flyer_primary_url}')">{btn_print}</button>
      </div>
    </div>    <!-- A4 Sekundární jazyk -->
    <div class="fmt-card">
      <div class="fmt-preview" onclick="openFormat('{flyer_secondary_url}')">
        <div style="width:110px;min-height:155px;background:#1a1a1a;border:1px solid rgba(255,107,0,.3);border-radius:8px;padding:10px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:6px">
          <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:9px;color:#FF6B00;line-height:1.2">{'Your personal<br>AI concierge' if is_cs else 'Váš osobní<br>AI concierge'}</div>
          <div style="font-size:8px;color:#00d4aa;font-weight:600;letter-spacing:.05em">🌍 100+ LANGUAGES</div>
          <div id="qr-a4-secondary" style="width:70px;height:70px"></div>
          <div style="font-size:7px;color:#00d4aa">smartestguide.com</div>
        </div>
      </div>
      <div class="fmt-info">
        <div class="fmt-name">{flyer_secondary_name}</div>
        <div class="fmt-desc">{flyer_secondary_desc}</div>
        <button class="fmt-btn" onclick="openFormat('{flyer_secondary_url}')">{btn_print}</button>
      </div>
    </div>    <!-- QR Plakát -->
    <div class="fmt-card">
      <div class="fmt-preview" onclick="openFormat('qr-poster')">
        <div style="position:relative;padding:16px;background:#0c0d12;border:1px solid rgba(255,107,0,.4);border-radius:14px;display:inline-block">
          <div id="qr-thumb" style="width:160px;height:160px;display:block"></div>
          <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:40px;height:40px;border-radius:50%;background:#1a1a1a;border:2px solid #FF6B00;display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:14px;color:#FF6B00;z-index:10;pointer-events:none">SG</div>
        </div>
      </div>
      <div class="fmt-info">
        <div class="fmt-name">{qr_poster_label}</div>
        <div class="fmt-desc">{qr_poster_desc}</div>
        <button class="fmt-btn" onclick="openFormat('qr-poster')">{btn_print}</button>
      </div>
    </div>    <!-- A5 primární jazyk (EN) -->
    <div class="fmt-card">
      <div class="fmt-preview" onclick="openFormat('flyer-a5-en')">
        <div style="width:130px;height:92px;background:#1a1a1a;border:1px solid rgba(255,107,0,.3);border-radius:8px;padding:10px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:space-between">
          <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:9px;color:#FF6B00;line-height:1.2">Your AI concierge</div>
          <div style="font-size:8px;color:#00d4aa;font-weight:600">🌍 100+ LANGUAGES</div>
          <div id="qr-a5-primary" style="width:50px;height:50px"></div>
        </div>
      </div>
      <div class="fmt-info">
        <div class="fmt-name">A5 Flyer · English</div>
        <div class="fmt-desc">Compact A5 — perfect for rooms and tables.</div>
        <button class="fmt-btn" onclick="openFormat('flyer-a5-en')">{btn_print}</button>
      </div>
    </div>    <!-- A5 sekundární jazyk (lokální) -->
    <div class="fmt-card">
      <div class="fmt-preview" onclick="openFormat('flyer-a5-local')">
        <div style="width:130px;height:92px;background:#1a1a1a;border:1px solid rgba(255,107,0,.3);border-radius:8px;padding:10px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:space-between">
          <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:9px;color:#FF6B00;line-height:1.2">AI concierge</div>
          <div style="font-size:8px;color:#00d4aa;font-weight:600">🌍 100+ LANGUAGES</div>
          <div id="qr-a5-secondary" style="width:50px;height:50px"></div>
        </div>
      </div>
      <div class="fmt-info">
        <div class="fmt-name">A5 Leták · {local_lang_name}</div>
        <div class="fmt-desc">Kompaktní A5 v lokálním jazyce hotelu.</div>
        <button class="fmt-btn" onclick="openFormat('flyer-a5-local')">{btn_print}</button>
      </div>
    </div>
  </div>
</div>

<!-- Hidden print frames -->
<iframe id="print-frame" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;border:none;z-index:9999;background:#1a1a1a"></iframe>

<script>
{qr_js}

window.addEventListener('load', function(){{
  setTimeout(function(){{
    drawQR('qr-thumb', 160);
    drawQR('qr-a4-primary', 70);
    drawQR('qr-a4-secondary', 70);
    drawQR('qr-a5-primary', 50);
    drawQR('qr-a5-secondary', 50);
    drawQR('qr-rollup', 44);
  }}, 300);
}});

function openFormat(fmt){{
  var urls = {{
    'qr-poster': '/api/hotels/{hotel_id}/qr-poster-print',
    'flyer-en':  '/api/hotels/{hotel_id}/flyer-en',
    'flyer-cz':  '/api/hotels/{hotel_id}/flyer-cz',
    'flyer-local': '/api/hotels/{hotel_id}/flyer-local',
    'flyer-a5-en': '/api/hotels/{hotel_id}/flyer-a5-en',
    'flyer-a5-cz': '/api/hotels/{hotel_id}/flyer-a5-cz',
    'flyer-a5-local': '/api/hotels/{hotel_id}/flyer-a5-local',
    'rollup':    '/api/hotels/{hotel_id}/rollup'
  }};
  var u = urls[fmt];
  if(!u){{ console.error('Neznámý formát:', fmt); return; }}
  // Print-friendly téma platí pro VŠECHNY tiskoviny (leták, rollup, QR poster)
  if(window._flyerTheme==='light'){{
    u += (u.indexOf('?')>=0?'&':'?') + 'theme=light';
  }}
  window.open(u, '_blank');
}}
window._flyerTheme='dark';
function setFlyerTheme(t){{
  window._flyerTheme=t;
  var d=document.getElementById('theme-dark'), l=document.getElementById('theme-light');
  if(d){{ d.style.background = t==='dark'?'#FF6B00':'transparent'; d.style.color = t==='dark'?'#0a0b0f':'#1a1a1a'; d.style.borderColor = t==='dark'?'transparent':'rgba(255,107,0,.55)'; }}
  if(l){{ l.style.background = t==='light'?'#FF6B00':'transparent'; l.style.color = t==='light'?'#0a0b0f':'#1a1a1a'; l.style.borderColor = t==='light'?'transparent':'rgba(255,107,0,.55)'; }}
  // Přepni i pozadí náhledů (mini-plakátů) v kartách
  document.querySelectorAll('.fmt-preview>div').forEach(function(m){{ m.style.background = (t==='light')?'#ffffff':'#1a1a1a'; }});
}}
</script>
</body>
</html>"""

    return HTMLResponse(content=html)

# QR Plakát — print view
@app.get("/api/hotels/{hotel_id}/qr-poster-print")
def qr_poster_print(hotel_id: str, request: Request, theme: str = "dark"):
    db = db_load()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    guest_url = _guest_url(base, hotel_id, hotel)
    hotel_name = hotel.get("name", "Hotel")
    return HTMLResponse(content=_render_qr_poster(hotel_name, guest_url, theme=theme))

def _render_qr_poster(hotel_name: str, guest_url: str, theme: str = "dark") -> str:
    light = (theme == "light")
    c_page   = "#e9eaee" if light else "#1b1c22"
    c_bg     = "#ffffff" if light else "#1a1a1a"
    c_ink    = "#1a1a1a" if light else "#f0ece0"
    c_dim    = "#555555" if light else "#9ba0c0"
    c_teal   = "#0a9d86" if light else "#00d4aa"
    c_qrcard = "#ffffff" if light else "#0c0d12"
    c_qrfill = "#1a1a1a" if light else "#FF6B00"      # tmavý QR na bílé = nejspolehlivější sken/tisk
    c_badge  = "#ffffff" if light else "#1a1a1a"
    _tt = "dark" if light else "light"
    _tl = "🌙 Tmavá verze" if light else "☀️ Světlá (šetří inkoust)"
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js"></script>
<style>*{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}body{{margin:0;background:{c_page};display:flex;justify-content:center;padding:32px;font-family:'Inter',sans-serif}}
.btn{{position:fixed;top:16px;border:none;border-radius:8px;padding:10px 18px;font-weight:700;font-size:14px;cursor:pointer}}
.btn-print{{right:16px;background:#FF6B00;color:#0a0b0f}}
.btn-theme{{left:16px;background:#333;color:#fff}}
@media print{{.btn{{display:none}}body{{background:#fff;padding:0}}@page{{margin:0}}}}</style></head>
<body><button class="btn btn-print" onclick="window.print()">🖨️ Tisknout / PDF</button>
<button class="btn btn-theme" onclick="location.href='?theme={_tt}'">{_tl}</button>
<div style="position:relative;width:800px;height:800px;background:{c_bg};border:1px solid rgba(255,107,0,.35);border-radius:24px;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.5)">
  <div style="position:absolute;top:0;left:0;right:0;height:5px;background:linear-gradient(90deg,#00d4aa,#00d4aa 60%,#FF6B00)"></div>
  <div style="position:absolute;top:300px;left:50%;width:620px;height:620px;transform:translateX(-50%);border-radius:50%;background:radial-gradient(closest-side,rgba(255,107,0,.16),transparent 70%);pointer-events:none"></div>
  <div style="height:100%;display:flex;flex-direction:column;align-items:center;padding:58px 48px 48px;position:relative">
    <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:34px;color:{c_ink};display:flex;align-items:center;gap:4px">SmartestGuide<span style="width:10px;height:10px;border-radius:50%;background:#FF6B00;display:inline-block;margin-left:2px;box-shadow:0 0 14px rgba(255,107,0,.9)"></span></div>
    <div style="margin-top:10px;font-size:13px;font-weight:600;letter-spacing:.22em;text-transform:uppercase;color:{c_teal}">AI Concierge for Hotels</div>
    <div style="margin-top:26px;font-family:'Syne',sans-serif;font-weight:700;font-size:22px;color:{c_ink};text-align:center">{hotel_name}</div>
    <div style="position:relative;margin-top:22px;padding:22px;background:{c_qrcard};border:1px solid rgba(255,107,0,.4);border-radius:20px;box-shadow:0 0 40px rgba(255,107,0,.12)">
      <div id="qr" style="width:420px;height:420px;display:flex;align-items:center;justify-content:center"></div>
      <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:78px;height:78px;border-radius:50%;background:{c_badge};border:3px solid #FF6B00;display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:30px;color:#FF6B00;box-shadow:0 0 22px rgba(255,107,0,.5)">SG</div>
    </div>
    <div style="margin-top:30px;font-family:'Syne',sans-serif;font-weight:700;font-size:24px;color:{c_ink};text-align:center">Scan for your personal AI concierge</div>
    <div style="margin-top:10px;font-size:15px;color:{c_dim}">100+ languages · No app needed · 24/7</div>
    <div style="flex:1"></div>
    <div style="width:100%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,212,170,.5),transparent)"></div>
    <div style="margin-top:18px;font-size:14px;font-weight:600;color:{c_teal}">smartestguide.com</div>
  </div>
</div>
<script>
(function(){{function draw(){{var h=document.getElementById('qr');if(!h)return;if(!window.qrcode){{setTimeout(draw,120);return;}}
var qr=window.qrcode(0,'H');qr.addData('{guest_url}');qr.make();var n=qr.getModuleCount(),S=420,cell=S/n,r='';
for(var i=0;i<n;i++)for(var j=0;j<n;j++)if(qr.isDark(i,j))r+='<rect x="'+(j*cell).toFixed(2)+'" y="'+(i*cell).toFixed(2)+'" width="'+(cell+0.4).toFixed(2)+'" height="'+(cell+0.4).toFixed(2)+'" fill="{c_qrfill}"/>';
h.innerHTML='<svg width="'+S+'" height="'+S+'" viewBox="0 0 '+S+' '+S+'" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg">'+r+'</svg>';}}draw();}})();
</script></body></html>"""

# Mapování country → lokální jazyk letáku
COUNTRY_LANG_MAP = {
    "CZ": "cs", "SK": "cs",
    "DE": "de", "AT": "de", "CH": "de",
    "FR": "fr", "BE": "fr", "LU": "fr",
    "IT": "it",
    "ES": "es", "MX": "es", "AR": "es",
    "PL": "pl",
    "HU": "hu",
    "RU": "ru",
    "UA": "uk",
    "GB": "en", "US": "en", "AU": "en", "IE": "en",
}

def get_hotel_local_lang(hotel: dict) -> str:
    """Vrátí kód lokálního jazyka hotelu dle země. Fallback: en."""
    country = (hotel.get("country") or "").upper().strip()
    return COUNTRY_LANG_MAP.get(country, "en")

def get_flyer_lang_name(lang: str, in_lang: str = None) -> str:
    """Vrátí název jazyka pro zobrazení."""
    names = {
        "cs": "Česky", "de": "Deutsch", "fr": "Français",
        "it": "Italiano", "es": "Español", "pl": "Polski",
        "hu": "Magyar", "ru": "Русский", "uk": "Українська", "en": "English"
    }
    return names.get(lang, lang.upper())


# 16 reálných vlajek odpovídajících jazykům Alexe (každá právě jednou, žádné duplicity)
# Pořadí = pořadí jazyků v guest appce.
_FLAGS_16 = [
    ("cs", '<rect width="24" height="8" fill="#fff"/><rect y="8" width="24" height="8" fill="#D7141A"/><path d="M0 0 L12 8 L0 16 Z" fill="#11457E"/>'),
    ("sk", '<rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#0B4EA2"/><rect y="10.66" width="24" height="5.34" fill="#EE1C25"/>'),
    ("en", '<rect width="24" height="16" fill="#012169"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#fff" stroke-width="3.2"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#C8102E" stroke-width="1.6"/><path d="M12 0 V16 M0 8 H24" stroke="#fff" stroke-width="5"/><path d="M12 0 V16 M0 8 H24" stroke="#C8102E" stroke-width="3"/>'),
    ("de", '<rect width="24" height="5.33" fill="#000"/><rect y="5.33" width="24" height="5.33" fill="#DD0000"/><rect y="10.66" width="24" height="5.34" fill="#FFCE00"/>'),
    ("fr", '<rect width="8" height="16" fill="#002395"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#ED2939"/>'),
    ("it", '<rect width="8" height="16" fill="#009246"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#CE2B37"/>'),
    ("es", '<rect width="24" height="16" fill="#C60B1E"/><rect y="4" width="24" height="8" fill="#FFC400"/>'),
    ("pl", '<rect width="24" height="8" fill="#fff"/><rect y="8" width="24" height="8" fill="#DC143C"/>'),
    ("hu", '<rect width="24" height="5.33" fill="#CD2A3E"/><rect y="5.33" width="24" height="5.33" fill="#fff"/><rect y="10.66" width="24" height="5.34" fill="#436F4D"/>'),
    ("ru", '<rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#0039A6"/><rect y="10.66" width="24" height="5.34" fill="#D52B1E"/>'),
    ("uk", '<rect width="24" height="8" fill="#0057B7"/><rect y="8" width="24" height="8" fill="#FFD700"/>'),
    ("zh", '<rect width="24" height="16" fill="#DE2910"/><polygon points="5,2.6 5.9,5.2 8.6,5.2 6.4,6.9 7.3,9.6 5,7.9 2.7,9.6 3.6,6.9 1.4,5.2 4.1,5.2" fill="#FFDE00"/>'),
    ("nl", '<rect width="24" height="5.33" fill="#AE1C28"/><rect y="5.33" width="24" height="5.33" fill="#fff"/><rect y="10.66" width="24" height="5.34" fill="#21468B"/>'),
    ("pt", '<rect width="24" height="16" fill="#FF0000"/><rect width="9.6" height="16" fill="#006600"/><circle cx="9.6" cy="8" r="2.3" fill="#FFD700"/>'),
    ("ja", '<rect width="24" height="16" fill="#fff"/><circle cx="12" cy="8" r="4.8" fill="#BC002D"/>'),
    ("ko", '<rect width="24" height="16" fill="#fff"/><path d="M8.4 8 a3.6 3.6 0 0 1 7.2 0 z" fill="#CD2E3A"/><path d="M8.4 8 a3.6 3.6 0 0 0 7.2 0 z" fill="#0047A0"/>'),
]

def _flags_row(w: int = 30, h: int = 21, shadow: bool = True, light: bool = False) -> str:
    """Vrátí HTML řadu 16 reálných vlajek (bez duplicit). light=True ztmaví obrysy pro tisk na bílé."""
    outline = "rgba(0,0,0,.18)" if light else "rgba(255,255,255,.14)"
    sh = ";box-shadow:0 2px 5px rgba(0,0,0,.5)" if shadow else ""
    out = []
    for _code, inner in _FLAGS_16:
        out.append(
            f'<span style="width:{w}px;height:{h}px;border-radius:3px;overflow:hidden;'
            f'outline:1px solid {outline};outline-offset:-1px;display:inline-block{sh}">'
            f'<svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none">{inner}</svg></span>'
        )
    return "".join(out)


def _render_flyer(hotel_name: str, guest_url: str, lang: str = "en", size: str = "a4", theme: str = "dark") -> str:
    is_a5 = size == "a5"
    page_size = "A5" if is_a5 else "A4"
    page_w = "148mm" if is_a5 else "210mm"
    page_h = "210mm" if is_a5 else "297mm"
    h1_size = "32px" if is_a5 else "48px"
    qr_size = "140px" if is_a5 else "200px"
    pad = "28px 28px" if is_a5 else "48px 44px"

    # Barvy dle tématu — light = print-friendly (bílé pozadí, tmavý text, úspora inkoustu)
    light = (theme == "light")
    c_bg          = "#ffffff" if light else "#1a1a1a"
    c_ink         = "#1a1a1a" if light else "#f0ece0"   # logo, nadpisy
    c_sub         = "#444444" if light else "#cfcad0"   # subline
    c_dim         = "#666666" if light else "#9ba0c0"   # název hotelu, drobné
    c_feat        = "#222222" if light else "#e7e2d8"   # texty výhod
    c_teal        = "#0a9d86" if light else "#00d4aa"   # tyrkysové akcenty
    c_orange      = "#FF6B00"                             # brand oranžová (obě témata)
    c_qrcard_bg   = "#ffffff" if light else "#0c0d12"
    c_qrcard_bd   = "rgba(255,107,0,.55)" if light else "rgba(255,107,0,.4)"
    c_qr_fill     = "#1a1a1a" if light else "#FF6B00"   # tmavý QR na bílé = nejspolehlivější sken/tisk
    c_badge_bg    = "#ffffff" if light else "#1a1a1a"
    qr_glow       = "" if light else "box-shadow:0 0 30px rgba(255,107,0,.1)"
    badge_glow    = "" if light else ""
    flags_html = """<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="8" fill="#fff"/><rect y="8" width="24" height="8" fill="#D7141A"/><path d="M0 0 L12 8 L0 16 Z" fill="#11457E"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#012169"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#fff" stroke-width="3.2"/><path d="M0 0 L24 16 M24 0 L0 16" stroke="#C8102E" stroke-width="1.6"/><path d="M12 0 V16 M0 8 H24" stroke="#fff" stroke-width="5"/><path d="M12 0 V16 M0 8 H24" stroke="#C8102E" stroke-width="3"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#000"/><rect y="5.33" width="24" height="5.33" fill="#DD0000"/><rect y="10.66" width="24" height="5.34" fill="#FFCE00"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#002395"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#ED2939"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#009246"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#CE2B37"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#c60b1e"/><rect y="5.5" width="24" height="5" fill="#ffc400"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#fff"/><rect y="10.67" width="24" height="5.33" fill="#dc143c"/><rect width="8" height="16" fill="#dc143c"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#fff"/><rect x="8" width="8" height="16" fill="#0b4ea2"/><rect x="16" width="8" height="16" fill="#fff"/><rect y="6" width="24" height="4" fill="#ee1c25"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="8" height="16" fill="#477050"/><rect x="8" width="8" height="16" fill="#fff"/><rect x="16" width="8" height="16" fill="#ce2939"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#003DA5"/><rect y="10.66" width="24" height="5.34" fill="#fff"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#EE1C25"/><rect x="4" y="3" width="6" height="10" fill="#FFFF00" rx="3"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#fff"/><circle cx="12" cy="8" r="5" fill="#BC002D"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="16" fill="#006C35"/><circle cx="8" cy="8" r="4" fill="#fff"/></svg></span>
<span style="width:30px;height:21px;border-radius:3px;overflow:hidden;outline:1px solid rgba(255,255,255,.14);outline-offset:-1px;display:inline-block;box-shadow:0 2px 5px rgba(0,0,0,.5)"><svg viewBox="0 0 24 16" width="100%" height="100%" preserveAspectRatio="none"><rect width="24" height="5.33" fill="#fff"/><rect y="5.33" width="24" height="5.33" fill="#003DA5"/><rect y="10.66" width="24" height="5.34" fill="#CE1126"/></svg></span>"""

    # 16 reálných vlajek (bez duplicit, přesně dle jazyků Alexe), obrysy dle tématu
    flags_html = _flags_row(30, 21, shadow=True, light=light)

    check = f"""<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="{c_teal}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>"""

    if lang == "cs":
        headline = "Váš osobní<br>AI concierge"
        subline = f'Nechte <span style="color:#FF6B00;font-weight:600">Alexe</span> odpovědět na všechny vaše otázky — okamžitě, ve vašem jazyce, 24/7.'
        features = ["Snídaně a restaurace", "Tipy na výlety a skrytá místa", "Počasí a doprava", "Služby hotelu a WiFi", "K dispozici 24/7"]
        scan_text = "Naskenujte mě"
        no_app = "Bez instalace aplikace"
    elif lang == "de":
        headline = "Ihr persönlicher<br>AI-Concierge"
        subline = f'Lassen Sie <span style="color:#FF6B00;font-weight:600">Alex</span> alle Ihre Fragen beantworten — sofort, in Ihrer Sprache, 24/7.'
        features = ["Frühstück & Restaurant", "Ausflugstipps & versteckte Orte", "Wetter & Transport", "Hotelservices & WLAN", "Rund um die Uhr verfügbar"]
        scan_text = "Scannen Sie mich"
        no_app = "Keine App erforderlich"
    elif lang == "fr":
        headline = "Votre concierge<br>IA personnel"
        subline = f'Laissez <span style="color:#FF6B00;font-weight:600">Alex</span> répondre à toutes vos questions — instantanément, dans votre langue, 24/7.'
        features = ["Petit-déjeuner & restaurant", "Conseils locaux & lieux cachés", "Météo & transport", "Services hôtel & WiFi", "Disponible 24h/24"]
        scan_text = "Scannez-moi"
        no_app = "Sans installation d'app"
    elif lang == "it":
        headline = "Il vostro concierge<br>IA personale"
        subline = f'Lasciate che <span style="color:#FF6B00;font-weight:600">Alex</span> risponda a tutte le vostre domande — immediatamente, nella vostra lingua, 24/7.'
        features = ["Colazione & ristorante", "Consigli locali & luoghi nascosti", "Meteo & trasporti", "Servizi hotel & WiFi", "Disponibile 24/7"]
        scan_text = "Scansionami"
        no_app = "Nessuna app richiesta"
    elif lang == "es":
        headline = "Su concierge<br>IA personal"
        subline = f'Deje que <span style="color:#FF6B00;font-weight:600">Alex</span> responda todas sus preguntas — al instante, en su idioma, 24/7.'
        features = ["Desayuno & restaurante", "Consejos locales & lugares ocultos", "Tiempo & transporte", "Servicios hotel & WiFi", "Disponible 24/7"]
        scan_text = "Escanéame"
        no_app = "Sin instalación de app"
    elif lang == "pl":
        headline = "Twój osobisty<br>concierge AI"
        subline = f'Pozwól <span style="color:#FF6B00;font-weight:600">Alexowi</span> odpowiedzieć na wszystkie Twoje pytania — natychmiast, w Twoim języku, 24/7.'
        features = ["Śniadanie & restauracja", "Lokalne wskazówki & ukryte miejsca", "Pogoda & transport", "Usługi hotelowe & WiFi", "Dostępny 24/7"]
        scan_text = "Zeskanuj mnie"
        no_app = "Bez instalacji aplikacji"
    elif lang == "hu":
        headline = "Az Ön személyes<br>AI concierge-e"
        subline = f'Hagyja, hogy <span style="color:#FF6B00;font-weight:600">Alex</span> azonnal válaszoljon minden kérdésére — az Ön nyelvén, 24/7.'
        features = ["Reggeli és étterem", "Helyi tippek és rejtett helyek", "Időjárás és közlekedés", "Szállodai szolgáltatások és WiFi", "Elérhető 24/7"]
        scan_text = "Szkenneljen be"
        no_app = "Nincs szükség alkalmazásra"
    elif lang == "ru":
        headline = "Ваш персональный<br>AI-консьерж"
        subline = f'<span style="color:#FF6B00;font-weight:600">Алекс</span> ответит на все ваши вопросы — мгновенно, на вашем языке, 24/7.'
        features = ["Завтрак и ресторан", "Местные советы и скрытые места", "Погода и транспорт", "Услуги отеля и WiFi", "Доступен 24/7"]
        scan_text = "Отсканируйте меня"
        no_app = "Без установки приложения"
    elif lang == "uk":
        headline = "Ваш персональний<br>AI-консьєрж"
        subline = f'<span style="color:#FF6B00;font-weight:600">Алекс</span> відповість на всі ваші запитання — миттєво, вашою мовою, 24/7.'
        features = ["Сніданок і ресторан", "Місцеві поради та приховані місця", "Погода і транспорт", "Послуги готелю та WiFi", "Доступний 24/7"]
        scan_text = "Відскануйте мене"
        no_app = "Без встановлення додатку"
    else:
        headline = "Your personal<br>AI concierge"
        subline = f'Let <span style="color:#FF6B00;font-weight:600">Alex</span> answer all your questions — instantly, in your language, 24/7.'
        features = ["Breakfast times & restaurant info", "Local tips & hidden gems", "Weather & transport", "Hotel services & WiFi", "Available 24/7"]
        scan_text = "Scan me"
        no_app = "No app needed"

    feats_html = "".join('<div style="display:flex;gap:12px;align-items:center;font-size:16px;color:' + c_feat + '">' + check + feat + '</div>' for feat in features)

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js"></script>
<style>*{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{margin:0;background:#1b1c22;display:flex;justify-content:center;padding:32px;font-family:'Inter',sans-serif}}
.btn{{position:fixed;top:16px;border:none;border-radius:8px;padding:10px 18px;font-weight:700;font-size:14px;cursor:pointer}}
.btn-print{{right:16px;background:#FF6B00;color:#0a0b0f}}
.btn-theme{{left:16px;background:#333;color:#fff}}
@media print{{.btn{{display:none}}body{{background:#fff;padding:0}}@page{{size:{page_size};margin:0}}}}</style></head>
<body><button class="btn btn-print" onclick="window.print()">🖨️ Tisknout / PDF</button>
<button class="btn btn-theme" onclick="location.href='?theme={"dark" if light else "light"}'">{"🌙 Tmavá verze" if light else "☀️ Světlá (šetří inkoust)"}</button>
<div style="width:{page_w};min-height:{page_h};background:{c_bg};position:relative;overflow:hidden;padding:0">
  <div style="position:absolute;top:0;left:0;right:0;height:4px;background:linear-gradient(90deg,{c_teal},{c_teal} 60%,{c_orange})"></div>
  <div style="position:absolute;top:80px;left:50%;width:500px;height:500px;transform:translateX(-50%);border-radius:50%;background:radial-gradient(closest-side,rgba(255,107,0,.1),transparent 70%);pointer-events:none"></div>
  <div style="padding:{pad};display:flex;flex-direction:column;align-items:center;text-align:center;min-height:{page_h}">
    <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:26px;color:{c_ink};display:flex;align-items:center;gap:4px">SmartestGuide<span style="width:8px;height:8px;border-radius:50%;background:{c_orange};display:inline-block;margin-left:2px"></span></div>
    <div style="margin-top:6px;font-size:11px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:{c_teal}">AI Concierge for Hotels</div>
    <div style="margin-top:8px;font-size:13px;color:{c_dim}">{hotel_name}</div>
    <h1 style="font-family:'Syne',sans-serif;font-weight:800;font-size:{h1_size};line-height:1.05;letter-spacing:-.02em;margin:36px 0 0;color:{c_orange}">{headline}</h1>
    <p style="font-size:17px;line-height:1.6;color:{c_sub};max-width:480px;margin:18px 0 0">{subline}</p>
    <div style="display:flex;flex-wrap:wrap;justify-content:center;gap:6px;margin-top:28px;max-width:500px">{flags_html}</div>
    <div style="margin-top:32px;display:flex;flex-direction:column;gap:12px;align-items:flex-start;text-align:left;width:100%;max-width:420px">{feats_html}</div>
    <div style="margin-top:40px;position:relative;padding:18px;background:{c_qrcard_bg};border:1px solid {c_qrcard_bd};border-radius:16px;{qr_glow}">
      <div id="qr-flyer" style="width:{qr_size};height:{qr_size};display:flex;align-items:center;justify-content:center"></div>
      <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:44px;height:44px;border-radius:50%;background:{c_badge_bg};border:2px solid {c_orange};display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:16px;color:{c_orange}">SG</div>
    </div>
    <div style="margin-top:20px;font-family:'Syne',sans-serif;font-weight:700;font-size:20px;color:{c_ink}">{scan_text}</div>
    <div style="margin-top:6px;font-size:14px;color:{c_dim}">{no_app} · 100+ languages · 24/7</div>
    <div style="flex:1;min-height:32px"></div>
    <div style="width:100%;height:1px;background:linear-gradient(90deg,transparent,{c_teal},transparent);margin-top:32px;opacity:.5"></div>
    <div style="margin-top:14px;font-size:13px;font-weight:600;color:{c_teal}">smartestguide.com</div>
  </div>
</div>
<script>
(function(){{function draw(){{var h=document.getElementById('qr-flyer');if(!h)return;if(!window.qrcode){{setTimeout(draw,120);return;}}
var qr=window.qrcode(0,'H');qr.addData('{guest_url.replace("'", "%27")}');qr.make();var n=qr.getModuleCount(),S=200,cell=S/n,r='';
for(var i=0;i<n;i++)for(var j=0;j<n;j++)if(qr.isDark(i,j))r+='<rect x="'+(j*cell).toFixed(2)+'" y="'+(i*cell).toFixed(2)+'" width="'+(cell+0.4).toFixed(2)+'" height="'+(cell+0.4).toFixed(2)+'" fill="{c_qr_fill}"/>';
h.innerHTML='<svg width="'+S+'" height="'+S+'" viewBox="0 0 '+S+' '+S+'" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg">'+r+'</svg>';}}draw();}})();
</script></body></html>"""

def _render_rollup(hotel_name: str, guest_url: str, theme: str = "dark") -> str:
    light = (theme == "light")
    c_page   = "#e9eaee" if light else "#1b1c22"
    c_bg     = "#ffffff" if light else "#1a1a1a"
    c_ink    = "#1a1a1a" if light else "#f0ece0"
    c_sub    = "#333333" if light else "#cfcad0"
    c_dim    = "#555555" if light else "#9ba0c0"
    c_teal   = "#0a9d86" if light else "#00d4aa"
    c_qrcard = "#ffffff" if light else "#0c0d12"
    c_qrfill = "#1a1a1a" if light else "#FF6B00"
    c_badge  = "#ffffff" if light else "#1a1a1a"
    flags_html = _flags_row(28, 20, shadow=False, light=light)
    _tt = "dark" if light else "light"
    _tl = "🌙 Tmavá verze" if light else "☀️ Světlá (šetří inkoust)"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js"></script>
<style>*{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
body{{margin:0;background:{c_page};display:flex;justify-content:center;padding:32px;font-family:'Inter',sans-serif}}
.btn{{position:fixed;top:16px;border:none;border-radius:8px;padding:10px 18px;font-weight:700;font-size:14px;cursor:pointer}}
.btn-print{{right:16px;background:#FF6B00;color:#0a0b0f}}
.btn-theme{{left:16px;background:#333;color:#fff}}
@media print{{.btn{{display:none}}body{{background:#fff;padding:0}}@page{{size:85mm 200mm;margin:0}}}}</style></head>
<body><button class="btn btn-print" onclick="window.print()">🖨️ Tisknout / PDF</button>
<button class="btn btn-theme" onclick="location.href='?theme={_tt}'">{_tl}</button>
<div style="width:340px;background:{c_bg};position:relative;overflow:hidden;padding:0;border:1px solid rgba(255,107,0,.3);border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.6)">
  <div style="position:absolute;top:0;left:0;right:0;height:4px;background:linear-gradient(90deg,#00d4aa,#00d4aa 60%,#FF6B00)"></div>
  <div style="position:absolute;top:120px;left:50%;width:400px;height:400px;transform:translateX(-50%);border-radius:50%;background:radial-gradient(closest-side,rgba(255,107,0,.1),transparent 70%);pointer-events:none"></div>
  <div style="padding:36px 28px;display:flex;flex-direction:column;align-items:center;text-align:center">
    <div style="font-family:'Syne',sans-serif;font-weight:800;font-size:20px;color:{c_ink};display:flex;align-items:center;gap:3px">SmartestGuide<span style="width:7px;height:7px;border-radius:50%;background:#FF6B00;display:inline-block;margin-left:2px;box-shadow:0 0 8px rgba(255,107,0,.9)"></span></div>
    <div style="margin-top:5px;font-size:9px;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:{c_teal}">AI CONCIERGE FOR HOTELS</div>
    <div style="margin-top:6px;font-size:12px;color:{c_dim}">{hotel_name}</div>
    <h1 style="font-family:'Syne',sans-serif;font-weight:800;font-size:36px;line-height:1.05;letter-spacing:-.02em;margin:28px 0 0;color:#FF6B00">Your<br>personal<br>AI<br>concierge</h1>
    <p style="font-size:14px;line-height:1.6;color:{c_sub};margin:16px 0 0">Let <span style="color:#FF6B00;font-weight:600">Alex</span> answer every question — instantly, in your language, around the clock.</p>
    <div style="display:flex;flex-wrap:wrap;justify-content:center;gap:5px;margin-top:22px;max-width:300px">{flags_html}</div>
    <div style="margin-top:28px;position:relative;padding:14px;background:{c_qrcard};border:1px solid rgba(255,107,0,.4);border-radius:14px;box-shadow:0 0 24px rgba(255,107,0,.1)">
      <div id="qr-rollup" style="width:180px;height:180px;display:flex;align-items:center;justify-content:center"></div>
      <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:42px;height:42px;border-radius:50%;background:{c_badge};border:2px solid #FF6B00;display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:15px;color:#FF6B00">SG</div>
    </div>
    <div style="margin-top:18px;font-family:'Syne',sans-serif;font-weight:700;font-size:18px;color:{c_ink}">Scan me</div>
    <div style="margin-top:5px;font-size:13px;color:{c_dim}">100+ languages · No app needed · 24/7</div>
    <div style="width:100%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,212,170,.4),transparent);margin-top:32px"></div>
    <div style="margin-top:12px;font-size:12px;font-weight:600;color:{c_teal}">smartestguide.com</div>
  </div>
</div>
<script>
(function(){{function draw(){{var h=document.getElementById('qr-rollup');if(!h)return;if(!window.qrcode){{setTimeout(draw,120);return;}}
var qr=window.qrcode(0,'H');qr.addData('{guest_url}');qr.make();var n=qr.getModuleCount(),S=180,cell=S/n,r='';
for(var i=0;i<n;i++)for(var j=0;j<n;j++)if(qr.isDark(i,j))r+='<rect x="'+(j*cell).toFixed(2)+'" y="'+(i*cell).toFixed(2)+'" width="'+(cell+0.4).toFixed(2)+'" height="'+(cell+0.4).toFixed(2)+'" fill="{c_qrfill}"/>';
h.innerHTML='<svg width="'+S+'" height="'+S+'" viewBox="0 0 '+S+' '+S+'" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg">'+r+'</svg>';}}draw();}})();
</script></body></html>"""

def _flyer_theme(theme: str) -> str:
    return "light" if (theme or "").lower() in ("light", "print", "svetla", "světlá") else "dark"

@app.get("/api/hotels/{hotel_id}/flyer-en")
def flyer_en(hotel_id: str, request: Request, theme: str = "dark"):
    db = db_load(); hotel = db["hotels"].get(hotel_id)
    if not hotel: raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    return HTMLResponse(content=_render_flyer(hotel.get("name","Hotel"), _guest_url(base, hotel_id, hotel), "en", theme=_flyer_theme(theme)))

@app.get("/api/hotels/{hotel_id}/flyer-cz")
def flyer_cz(hotel_id: str, request: Request, theme: str = "dark"):
    db = db_load(); hotel = db["hotels"].get(hotel_id)
    if not hotel: raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    try:
        html = _render_flyer(hotel.get("name","Hotel"), _guest_url(base, hotel_id, hotel), "cs", theme=_flyer_theme(theme))
        if not html:
            return HTMLResponse(content="<h1>Render vrátil prázdný string</h1>", status_code=500)
        return HTMLResponse(content=html)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        logging.error("flyer-cz error: %s", err)
        return HTMLResponse(content=f"<pre>CHYBA:\n{err}</pre>", status_code=500)

@app.get("/api/hotels/{hotel_id}/flyer-local")
def flyer_local(hotel_id: str, request: Request, theme: str = "dark"):
    """Leták v lokálním jazyce hotelu."""
    db = db_load(); hotel = db["hotels"].get(hotel_id)
    if not hotel: raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    lang = get_hotel_local_lang(hotel)
    return HTMLResponse(content=_render_flyer(hotel.get("name","Hotel"), _guest_url(base, hotel_id, hotel), lang, theme=_flyer_theme(theme)))

@app.get("/api/hotels/{hotel_id}/flyer-a5-en")
def flyer_a5_en(hotel_id: str, request: Request, theme: str = "dark"):
    db = db_load(); hotel = db["hotels"].get(hotel_id)
    if not hotel: raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    return HTMLResponse(content=_render_flyer(hotel.get("name","Hotel"), _guest_url(base, hotel_id, hotel), "en", size="a5", theme=_flyer_theme(theme)))

@app.get("/api/hotels/{hotel_id}/flyer-a5-cz")
def flyer_a5_cz(hotel_id: str, request: Request, theme: str = "dark"):
    db = db_load(); hotel = db["hotels"].get(hotel_id)
    if not hotel: raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    return HTMLResponse(content=_render_flyer(hotel.get("name","Hotel"), _guest_url(base, hotel_id, hotel), "cs", size="a5", theme=_flyer_theme(theme)))

@app.get("/api/hotels/{hotel_id}/flyer-a5-local")
def flyer_a5_local(hotel_id: str, request: Request, theme: str = "dark"):
    """A5 leták v lokálním jazyce hotelu."""
    db = db_load(); hotel = db["hotels"].get(hotel_id)
    if not hotel: raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    lang = get_hotel_local_lang(hotel)
    return HTMLResponse(content=_render_flyer(hotel.get("name","Hotel"), _guest_url(base, hotel_id, hotel), lang, size="a5", theme=_flyer_theme(theme)))

@app.get("/api/hotels/{hotel_id}/rollup")
def rollup(hotel_id: str, request: Request, theme: str = "dark"):
    db = db_load(); hotel = db["hotels"].get(hotel_id)
    if not hotel: raise HTTPException(404, "Hotel nenalezen")
    base = get_base_url(request)
    return HTMLResponse(content=_render_rollup(hotel.get("name","Hotel"), _guest_url(base, hotel_id, hotel), theme=_flyer_theme(theme)))

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QR plakát — {hotel_name}</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@600;700;800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.js"></script>
<style>
  *{{box-sizing:border-box;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
  body{{margin:0;background:#1b1c22;font-family:'Inter',sans-serif;display:flex;justify-content:center;align-items:flex-start;padding:32px;min-height:100vh}}
  @media print{{body{{background:#fff;padding:0;display:block}} @page{{margin:0}}}}
  .print-btn{{position:fixed;top:20px;right:20px;background:#FF6B00;color:#0a0b0f;border:none;border-radius:10px;padding:12px 22px;font-family:'Inter',sans-serif;font-size:14px;font-weight:700;cursor:pointer;z-index:100;box-shadow:0 4px 16px rgba(255,107,0,.4)}}
  .print-btn:hover{{opacity:.88}}
  @media print{{.print-btn{{display:none}}}}
</style>
</head>
<body>
<button class="print-btn" onclick="window.print()">🖨️ Tisknout / Uložit PDF</button>
<div style="position:relative;width:800px;height:800px;background:#1a1a1a;border:1px solid rgba(255,107,0,.35);border-radius:24px;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.5)">
  <div style="position:absolute;top:0;left:0;right:0;height:5px;background:linear-gradient(90deg,#00d4aa,#00d4aa 60%,#FF6B00)"></div>
  <div style="position:absolute;top:300px;left:50%;width:620px;height:620px;transform:translateX(-50%);border-radius:50%;background:radial-gradient(closest-side,rgba(255,107,0,.16),transparent 70%);pointer-events:none"></div>
  <div style="position:relative;height:100%;display:flex;flex-direction:column;align-items:center;padding:58px 48px 48px">
    <div style="display:flex;align-items:center;gap:4px;font-family:'Syne',sans-serif;font-weight:800;font-size:34px;letter-spacing:-.02em;color:#f0ece0">SmartestGuide<span style="width:10px;height:10px;border-radius:50%;background:#FF6B00;display:inline-block;margin-left:2px;box-shadow:0 0 14px rgba(255,107,0,.9)"></span></div>
    <div style="margin-top:10px;font-size:13px;font-weight:600;letter-spacing:.22em;text-transform:uppercase;color:#00d4aa">AI Concierge for Hotels</div>
    <div style="margin-top:26px;font-family:'Syne',sans-serif;font-weight:700;font-size:22px;color:#f0ece0;text-align:center">{hotel_name}</div>
    <div style="position:relative;margin-top:22px;padding:22px;background:#0c0d12;border:1px solid rgba(255,107,0,.4);border-radius:20px;box-shadow:0 0 40px rgba(255,107,0,.12)">
      <div id="sg-qr-holder" style="width:420px;height:420px;display:flex;align-items:center;justify-content:center"></div>
      <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);width:78px;height:78px;border-radius:50%;background:#1a1a1a;border:3px solid #FF6B00;display:flex;align-items:center;justify-content:center;font-family:'Syne',sans-serif;font-weight:800;font-size:30px;color:#FF6B00;box-shadow:0 0 22px rgba(255,107,0,.5)">SG</div>
    </div>
    <div style="margin-top:30px;font-family:'Syne',sans-serif;font-weight:700;font-size:24px;color:#f0ece0;text-align:center">Scan for your personal AI concierge</div>
    <div style="margin-top:10px;font-size:15px;color:#9ba0c0;letter-spacing:.02em">100+ languages · No app needed · 24/7</div>
    <div style="flex:1"></div>
    <div style="width:100%;height:1px;background:linear-gradient(90deg,transparent,rgba(0,212,170,.5),transparent)"></div>
    <div style="margin-top:18px;font-size:14px;font-weight:600;color:#00d4aa;letter-spacing:.04em">smartestguide.com</div>
  </div>
</div>
<script>
(function(){{
  function draw(){{
    var holder = document.getElementById('sg-qr-holder');
    if(!holder) return;
    if(!window.qrcode){{ setTimeout(draw, 120); return; }}
    var url = '{guest_url}';
    var qr = window.qrcode(0, 'H');
    qr.addData(url);
    qr.make();
    var n = qr.getModuleCount();
    var S = 420;
    var cell = S / n;
    var rects = '';
    for(var r=0;r<n;r++){{
      for(var c=0;c<n;c++){{
        if(qr.isDark(r,c)){{
          rects += '<rect x="'+(c*cell).toFixed(2)+'" y="'+(r*cell).toFixed(2)+'" width="'+(cell+0.4).toFixed(2)+'" height="'+(cell+0.4).toFixed(2)+'" fill="#FF6B00"/>';
        }}
      }}
    }}
    holder.innerHTML = '<svg width="'+S+'" height="'+S+'" viewBox="0 0 '+S+' '+S+'" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg">'+rects+'</svg>';
  }}
  draw();
}})();
</script>
</body>
</html>"""

    return HTMLResponse(content=html)

# ─────────────────────────────────────────────
# Ceník
# ─────────────────────────────────────────────
@app.get("/api/pricing-config")
def pricing_config():
    """Veřejná ceníková konfigurace pro landing kalkulačku (bez citlivých údajů)."""
    s = db_get_settings()
    return {
        "pricing_base": s.get("pricing_base", 199),
        "pricing_threshold": s.get("pricing_threshold", 100),
        "pricing_per_bed": s.get("pricing_per_bed", 3),
    }

@app.get("/api/pricing")
def pricing(beds: int):
    if beds <= 0:
        raise HTTPException(400, "Počet lůžek musí být kladný")
    s = db_get_settings()
    base = s.get("pricing_base", 300)
    threshold = s.get("pricing_threshold", 100)
    per_bed = s.get("pricing_per_bed", 3)
    price = base if beds <= threshold else base + (beds - threshold) * per_bed
    price = int(price)
    return {"beds": beds, "monthly_eur": price, "yearly_eur": price * 12,
            "note": "Zaváděcí cena – měsíční předplatné, bez závazku (zrušíte kdykoli)"}

# ─────────────────────────────────────────────
# Kontaktní formulář z landing page ("Got a question?")
# ─────────────────────────────────────────────
CONTACT_TO_EMAIL = os.getenv("CONTACT_TO_EMAIL", "support@smartestguide.com").strip()

class ContactRequest(BaseModel):
    name: str
    email: str
    message: str

@app.post("/api/contact")
async def contact_form(req: ContactRequest, request: Request):
    """Odešle zprávu z landing kontaktního formuláře na support e-mail přes Brevo.
    Reply-To = e-mail odesílatele, ať lze rovnou odpovědět."""
    if not _rate_limit_ok(f"contact:{_client_ip(request)}", max_hits=5, window=3600):
        raise HTTPException(429, "Příliš mnoho zpráv. Zkuste to prosím později.")
    name = (req.name or "").strip()
    email = (req.email or "").strip()
    message = (req.message or "").strip()
    if not name or not email or not message:
        raise HTTPException(400, "Vyplňte jméno, e-mail i zprávu.")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(400, "Neplatný e-mail.")
    if len(message) > 5000:
        message = message[:5000]

    brevo_key = os.getenv("BREVO_API_KEY", "")
    if not brevo_key:
        logging.error("Kontaktní formulář: BREVO_API_KEY není nastaven — zpráva se neodešle")
        raise HTTPException(503, "E-mail momentálně nelze odeslat, zkuste to prosím později.")

    import html as _html
    safe_name = _html.escape(name)
    safe_email = _html.escape(email)
    safe_msg = _html.escape(message).replace("\n", "<br>")
    html_body = (
        '<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a2e">'
        '<div style="background:#ff6b00;padding:20px 32px;border-radius:12px 12px 0 0;color:#fff">'
        '<h2 style="margin:0;font-size:18px">📩 Nová zpráva z landing formuláře</h2></div>'
        '<div style="background:#f8f9ff;padding:32px;border-radius:0 0 12px 12px">'
        f'<p style="margin:0 0 8px"><strong>Jméno:</strong> {safe_name}</p>'
        f'<p style="margin:0 0 8px"><strong>E-mail:</strong> <a href="mailto:{safe_email}">{safe_email}</a></p>'
        f'<p style="margin:16px 0 4px"><strong>Zpráva:</strong></p>'
        f'<div style="background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:16px;line-height:1.6">{safe_msg}</div>'
        '</div></div>'
    )
    text_body = f"Nová zpráva z landing formuláře\n\nJméno: {name}\nE-mail: {email}\n\nZpráva:\n{message}"
    payload = {
        "sender": {"name": "SMARTEST GUIDE web", "email": "admin@smartestguide.com"},
        "to": [{"email": CONTACT_TO_EMAIL}],
        "replyTo": {"email": email, "name": name},
        "subject": f"Dotaz z webu — {name}",
        "htmlContent": html_body,
        "textContent": text_body,
    }
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.brevo.com/v3/smtp/email", json=payload,
                headers={"api-key": brevo_key, "Content-Type": "application/json"}, timeout=30)
        if r.status_code in (200, 201):
            logging.info(f"Kontaktní formulář OK -> {CONTACT_TO_EMAIL} (od {email})")
            return {"ok": True}
        logging.error(f"Brevo kontakt CHYBA {r.status_code}: {r.text[:300]}")
        raise HTTPException(502, "Zprávu se nepodařilo odeslat, zkuste to prosím znovu.")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Chyba při odesílání kontaktního e-mailu: {e}")
        raise HTTPException(502, "Zprávu se nepodařilo odeslat, zkuste to prosím znovu.")

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
    country: Optional[str] = None        # země hotelu (pro DPH/jazyk)
    ico: Optional[str] = None            # fakturační IČO (odběratel)
    dic: Optional[str] = None            # DIČ / VAT ID
    billing_name: Optional[str] = None   # právní/fakturační název
    ref: Optional[str] = None            # referral kód partnera (atribuce provize)
    src: Optional[str] = None            # akviziční kanál ("apaleo" = přišel z Apaleo Store — bez provize)

def _norm_ico(x: str) -> str:
    """IČO na porovnání — jen číslice."""
    return re.sub(r'\D', '', x or '')

def _norm_domain(url: str) -> str:
    """Doména webu na porovnání — bez schématu, www a cesty, malými písmeny."""
    u = (url or '').strip().lower()
    u = re.sub(r'^https?://', '', u)
    u = re.sub(r'^www\.', '', u)
    u = u.split('/')[0].split('?')[0].strip()
    return u

@app.post("/api/register")
async def register_hotel(req: RegistrationRequest, request: Request):
    """
    Registrace hotelu z landing page:
    1. Vytvoří hotel v DB (neaktivní)
    2. Vytvoří Stripe Checkout Session s client_reference_id = hotel_id
    3. Vrátí URL pro přesměrování na platbu
    """
    s = db_get_settings()
    stripe_key = s.get("stripe_secret_key", "")
    if not stripe_key:
        raise HTTPException(400, "Stripe není nastaven")

    # 1. Vytvoř hotel v DB
    db = db_load()

    # Ochrana proti duplicitní registraci — zkontroluj email.
    # Archivované (deaktivované) hotely NEBLOKUJÍ — po zrušení se zákazník může vrátit.
    email_lower = req.contact_email.lower().strip()
    existing = [(hid, h) for hid, h in db["hotels"].items()
                if not h.get("archived")
                and ((h.get("email") or "").lower().strip() == email_lower
                     or (h.get("registration_email") or "").lower().strip() == email_lower)]

    for hid_ex, h_ex in existing:
        if h_ex.get("subscription_active"):
            # Hotel s tímto emailem již má aktivní předplatné
            raise HTTPException(409, f"Hotel s emailem {req.contact_email} již má aktivní předplatné. Přihlaste se do portálu nebo kontaktujte podporu.")
        # "Registrace probíhá" blokuje jen při ČERSTVÉM rozdělaném platebním pokusu (skutečný
        # race po platbě, < 1 h). Starou zrušenou/neaktivní registraci klidně povolíme znovu.
        if h_ex.get("stripe_subscription_id"):
            try:
                created = datetime.fromisoformat(h_ex.get("created_at", ""))
                recent = (datetime.utcnow() - created).total_seconds() < 3600
            except Exception:
                recent = False
            if recent:
                raise HTTPException(409, f"Registrace s emailem {req.contact_email} již probíhá. Zkontrolujte svůj email.")

    # Načti aktuální ceník z DB
    pricing_base = int(s.get("pricing_base", 199))
    pricing_threshold = int(s.get("pricing_threshold", 100))
    pricing_per_bed = float(s.get("pricing_per_bed", 3))

    hid = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    hotel_token = str(uuid.uuid4()).replace("-", "")
    beds = req.bed_count or 0
    if beds <= pricing_threshold:
        price = pricing_base
    else:
        price = int(pricing_base + (beds - pricing_threshold) * pricing_per_bed)

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
        "registered_bed_count": beds,
        "contact_name": req.contact_name,
        "country": (req.country or "").upper().strip(),
        "ico": (req.ico or "").strip(),
        "dic": (req.dic or "").strip(),
        "billing_name": (req.billing_name or "").strip(),
    }
    # Atribuce provize — pouze přes platný referral kód partnera.
    # Výjimka: hotel z Apaleo Store (src=apaleo) NIKDY nezakládá provizi — lead přivedlo
    # Apaleo, ne partner (i kdyby v prohlížeči zůstal starý ref kód z dřívějška).
    _is_apaleo = (req.src or "").strip().lower() == "apaleo"
    _ref_partner = None if _is_apaleo else _partner_by_ref(db, req.ref)
    hotel["acquired_by"] = _ref_partner["id"] if _ref_partner else "auto"
    hotel["referral_code"] = _norm_ref(req.ref) if _ref_partner else ""
    hotel["acquisition_channel"] = "apaleo_store" if _is_apaleo else ("partner" if _ref_partner else "direct")
    hotel["acquired_at"] = now
    _ensure_slug(db, hid, hotel)  # čitelná guest URL /h/{slug}
    db["hotels"][hid] = hotel
    db_save(db)

    # 2. Vytvoř Stripe Checkout Session přes API
    base = get_base_url(request)
    try:
        async with httpx.AsyncClient() as client:
            # Ochrana proti opakovanému trialu — dedup podle E-MAILU, IČO i DOMÉNY webu.
            # Když už kdokoli s touhle identitou trial využil (i archivovaný hotel), další
            # trial nedáme — Stripe rovnou účtuje. Zabrání to opakování s jiným e-mailem.
            contact_email_lower = req.contact_email.lower().strip()
            req_ico = _norm_ico(req.ico)
            req_domain = _norm_domain(req.hotel_url)
            def _same_identity(h):
                if (h.get("email") or "").lower().strip() == contact_email_lower: return True
                if (h.get("registration_email") or "").lower().strip() == contact_email_lower: return True
                if req_ico and _norm_ico(h.get("ico")) == req_ico: return True
                if req_domain and _norm_domain(h.get("url") or h.get("source_url")) == req_domain: return True
                return False
            trial_already_used = any(
                h.get("trial_used", False) and _same_identity(h)
                for h in db["hotels"].values()
            )

            trial_params = {}
            if not trial_already_used:
                trial_params = {"subscription_data[trial_period_days]": "14"}

            r = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                headers={
                    "Authorization": f"Bearer {stripe_key}",
                    "Content-Type": "application/x-www-form-urlencoded",
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
                    "metadata[trial_used]": "0" if not trial_already_used else "1",
                    **trial_params,
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
            portal_url = f"{base}/portal?token={h['hotel_token']}"

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


def _build_invoice_pdf_bytes(inv: dict, s: dict) -> bytes:
    """Vygeneruje PDF faktury (standardní český daňový doklad, s diakritikou).
    Sdílené endpointem i e-mailem."""
    from io import BytesIO
    import os as _os
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Fonty s diakritikou (Liberation Sans ze složky aplikace); fallback Helvetica
    FN, FB = "Helvetica", "Helvetica-Bold"
    try:
        _b = _os.path.dirname(_os.path.abspath(__file__))
        if "LibSans" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("LibSans", _os.path.join(_b, "LiberationSans-Regular.ttf")))
            pdfmetrics.registerFont(TTFont("LibSans-Bold", _os.path.join(_b, "LiberationSans-Bold.ttf")))
        FN, FB = "LibSans", "LibSans-Bold"
    except Exception:
        pass

    W, H = A4
    buf = BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=A4)
    ORANGE = colors.HexColor("#FF6B00")
    INK = colors.HexColor("#1a1a1a")
    GREY = colors.HexColor("#666666")
    LINE = colors.HexColor("#dddddd")
    BOXBG = colors.HexColor("#f7f6f4")
    ML, MR = 18*mm, W - 18*mm

    cur = inv.get("currency_symbol", "€") or "€"
    def money(v):
        try:
            return f"{float(v):,.2f} {cur}"   # EN formát: 1,234.50 €
        except Exception:
            return f"{v} {cur}"

    net = inv.get("amount_net", inv.get("amount_eur", 0))
    vat_rate = inv.get("vat_rate", 0)
    vat_amount = inv.get("vat_amount", 0)
    total = inv.get("amount_total", inv.get("amount_local", net))

    # ── Hlavička ──
    c.setFillColor(ORANGE); c.rect(0, H - 6*mm, W, 6*mm, fill=1, stroke=0)
    c.setFillColor(INK); c.setFont(FB, 20); c.drawString(ML, H - 22*mm, "SMARTEST GUIDE")
    c.setFillColor(GREY); c.setFont(FN, 9); c.drawString(ML, H - 27*mm, "AI concierge for hotels")
    c.setFillColor(INK); c.setFont(FB, 22); c.drawRightString(MR, H - 20*mm, "INVOICE")
    c.setFillColor(INK); c.setFont(FB, 12); c.drawRightString(MR, H - 28*mm, "No. " + inv.get("invoice_number", ""))

    # ── Dodavatel / Odběratel ──
    box_top = H - 42*mm; box_h = 40*mm; box_w = (MR - ML - 6*mm) / 2
    def party(x, title, lines):
        c.setFillColor(BOXBG); c.roundRect(x, box_top - box_h, box_w, box_h, 3, fill=1, stroke=0)
        c.setFillColor(GREY); c.setFont(FB, 8); c.drawString(x + 5*mm, box_top - 7*mm, title.upper())
        yy = box_top - 13*mm; c.setFillColor(INK)
        for ln, bold in lines:
            if not ln:
                continue
            c.setFont(FB if bold else FN, 10 if bold else 9)
            c.drawString(x + 5*mm, yy, str(ln)[:48]); yy -= 5*mm

    sup = [
        (s.get("company_name", "SMARTEST GUIDE"), True),
        (s.get("company_address", ""), False),
        (s.get("company_city", ""), False),
        ((f"Reg. No.: {s.get('company_ico','')}" if s.get('company_ico') else ""), False),
        ((f"VAT ID: {s.get('company_dic','')}" if s.get('company_dic') else ""), False),
        ((f"E-mail: {s.get('company_email','')}" if s.get('company_email') else ""), False),
    ]
    cust = [
        (inv.get("hotel_billing_name") or inv.get("hotel_name", ""), True),
        (inv.get("hotel_address", ""), False),
        ((f"Reg. No.: {inv.get('hotel_ico','')}" if inv.get('hotel_ico') else ""), False),
        ((f"VAT ID: {inv.get('hotel_dic','')}" if inv.get('hotel_dic') else ""), False),
        ((f"Country: {inv.get('hotel_country','')}" if inv.get('hotel_country') else ""), False),
    ]
    party(ML, "Supplier", sup)
    party(ML + box_w + 6*mm, "Bill to", cust)

    # ── Meta řádek ──
    my = box_top - box_h - 10*mm
    meta = [
        ("Issue date", (inv.get("created_at") or "")[:10]),
        ("Due date", inv.get("due_date", "")),
        ("Payment", "Bank transfer"),
        ("Reference", inv.get("variable_symbol", "")),
    ]
    step = (MR - ML) / len(meta)
    for i, (k, v) in enumerate(meta):
        x = ML + i * step
        c.setFillColor(GREY); c.setFont(FN, 8); c.drawString(x, my, k)
        c.setFillColor(INK); c.setFont(FB, 10); c.drawString(x, my - 5*mm, str(v))

    # ── Položková tabulka ──
    ty = my - 14*mm
    c.setFillColor(INK); c.rect(ML, ty - 7*mm, MR - ML, 7*mm, fill=1, stroke=0)
    c.setFillColor(colors.white); c.setFont(FB, 8)
    c.drawString(ML + 3*mm, ty - 5*mm, "DESCRIPTION")
    c.drawRightString(MR - 62*mm, ty - 5*mm, "QTY")
    c.drawRightString(MR - 33*mm, ty - 5*mm, "UNIT PRICE")
    c.drawRightString(MR - 3*mm, ty - 5*mm, "TOTAL")
    ry = ty - 15*mm
    c.setFillColor(INK); c.setFont(FN, 10)
    c.drawString(ML + 3*mm, ry, "SMARTEST GUIDE subscription")
    c.setFillColor(GREY); c.setFont(FN, 8)
    c.drawString(ML + 3*mm, ry - 4.5*mm, f"Period {inv.get('period_from','')} - {inv.get('period_to','')} · {inv.get('beds','')} beds")
    c.setFillColor(INK); c.setFont(FN, 10)
    c.drawRightString(MR - 62*mm, ry, "1")
    c.drawRightString(MR - 33*mm, ry, money(net))
    c.drawRightString(MR - 3*mm, ry, money(net))
    c.setStrokeColor(LINE); c.setLineWidth(0.5); c.line(ML, ry - 8*mm, MR, ry - 8*mm)

    # ── Rekapitulace ──
    sy = ry - 16*mm; lx = ML + 105*mm
    def sumline(label, val, bold=False, big=False):
        nonlocal sy
        c.setFillColor(INK if bold else GREY); c.setFont(FB if bold else FN, 12 if big else 10)
        c.drawString(lx, sy, label)
        c.setFillColor(INK); c.setFont(FB if bold else FN, 13 if big else 10)
        c.drawRightString(MR, sy, val); sy -= 7*mm
    sumline("Subtotal (excl. VAT)", money(net))
    if vat_rate:
        sumline(f"VAT {vat_rate}%", money(vat_amount))
    c.setStrokeColor(LINE); c.line(lx, sy + 3*mm, MR, sy + 3*mm); sy -= 1*mm
    sumline("Total due", money(total), bold=True, big=True)

    # ── Platební údaje ──
    py = sy - 8*mm
    if s.get("company_bank") or s.get("company_iban"):
        c.setFillColor(GREY); c.setFont(FB, 8); c.drawString(ML, py, "PAYMENT DETAILS")
        c.setFillColor(INK); c.setFont(FN, 9); py -= 5*mm
        if s.get("company_bank"):
            c.drawString(ML, py, f"Account: {s.get('company_bank')}"); py -= 4.5*mm
        if s.get("company_iban"):
            c.drawString(ML, py, f"IBAN: {s.get('company_iban')}"); py -= 4.5*mm
        c.drawString(ML, py, f"Reference: {inv.get('variable_symbol','')}"); py -= 4.5*mm

    # ── Poznámka k DPH (mapa CZ → EN, dokud je faktura jen v EN) ──
    _note_map = {
        "Nejsme plátci DPH.": "Supplier is not a VAT payer — VAT not applicable.",
        "Přenesení daňové povinnosti / reverse charge — daň odvede zákazník.": "Reverse charge — VAT to be accounted for by the customer.",
        "Mimo EU — bez DPH (vývoz služby).": "Outside the EU — no VAT (export of services).",
    }
    _note = _note_map.get(str(inv.get("vat_note", "")).strip(), "")
    if not _note and (inv.get("vat_rate", 0) or 0) == 0:
        _note = "VAT not applicable."
    if _note:
        c.setFillColor(GREY); c.setFont(FN, 8)
        c.drawString(ML, py - 3*mm, _note[:110])

    # ── Patička ──
    c.setStrokeColor(LINE); c.line(ML, 18*mm, MR, 18*mm)
    c.setFillColor(GREY); c.setFont(FN, 8)
    c.drawString(ML, 13*mm, "Issued via SMARTEST GUIDE - AI concierge for hotels")
    c.drawRightString(MR, 13*mm, s.get("company_email", "admin@smartestguide.com"))

    c.save()
    return buf.getvalue()


async def send_invoice_email(hotel: dict, inv: dict, portal_url: str = ""):
    """Po zaplaceni posle hotelu fakturu (PDF v priloze) pres Brevo."""
    brevo_key = os.getenv("BREVO_API_KEY", "")
    hotel_email = (hotel.get("email") or "").strip()
    if not brevo_key or not hotel_email:
        logging.warning("Faktura e-mail preskocen (chybi BREVO_API_KEY nebo e-mail hotelu)")
        return
    s = db_get_settings()
    is_cs = (hotel.get("country") or "").upper() in ("CZ", "SK")
    hotel_name = hotel.get("name", "Hotel")
    num = inv.get("invoice_number", "")
    total = inv.get("amount_total", inv.get("amount_local", inv.get("amount_eur", 0)))
    portal_btn = (f'<div style="text-align:center;margin:24px 0"><a href="{portal_url}" '
                  f'style="display:inline-block;background:#FF6B00;color:#0a0b0f;text-decoration:none;'
                  f'padding:12px 28px;border-radius:8px;font-weight:700">'
                  f'{"Otevřít hotelový portál →" if is_cs else "Open hotel portal →"}</a></div>') if portal_url else ""
    header = ('<div style="background:#1a1a1a;padding:28px;text-align:center;border-radius:12px 12px 0 0;'
              'border-bottom:3px solid #FF6B00"><h1 style="color:#fff;margin:0;font-size:24px;letter-spacing:.5px">'
              'SMARTEST GUIDE</h1></div>')
    if is_cs:
        subject = f"Faktura {num} — SMARTEST GUIDE"
        body = (f'<h2 style="color:#1a1a2e;margin-bottom:8px">Děkujeme za platbu</h2>'
                f'<p style="color:#555;line-height:1.7">Vaše platba za předplatné proběhla úspěšně. '
                f'V příloze najdete fakturu <strong>{num}</strong> na částku <strong>{total} EUR</strong>.</p>'
                f'<p style="color:#555;line-height:1.7">Všechny faktury najdete také v hotelovém portálu '
                f'v sekci <strong>Faktury</strong>, kde je můžete kdykoli stáhnout.</p>{portal_btn}'
                f'<p style="color:#888;font-size:12px;text-align:center;margin-top:20px">Dotazy? '
                f'<a href="mailto:admin@smartestguide.com" style="color:#FF6B00">admin@smartestguide.com</a></p>')
        text_body = f"Dekujeme za platbu. Faktura {num} na {total} EUR je v priloze. Portal: {portal_url}"
    else:
        subject = f"Invoice {num} — SMARTEST GUIDE"
        body = (f'<h2 style="color:#1a1a2e;margin-bottom:8px">Thank you for your payment</h2>'
                f'<p style="color:#555;line-height:1.7">Your subscription payment was successful. '
                f'Please find invoice <strong>{num}</strong> for <strong>{total} EUR</strong> attached.</p>'
                f'<p style="color:#555;line-height:1.7">You can also find all invoices in your hotel portal '
                f'under <strong>Invoices</strong>, ready to download anytime.</p>{portal_btn}'
                f'<p style="color:#888;font-size:12px;text-align:center;margin-top:20px">Questions? '
                f'<a href="mailto:admin@smartestguide.com" style="color:#FF6B00">admin@smartestguide.com</a></p>')
        text_body = f"Thank you for your payment. Invoice {num} for {total} EUR is attached. Portal: {portal_url}"
    html_body = (f'<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a2e">{header}'
                 f'<div style="background:#f8f9ff;padding:32px;border-radius:0 0 12px 12px">{body}</div></div>')
    attachments = []
    try:
        pdf_bytes = _build_invoice_pdf_bytes(inv, s)
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        safe_num = (num or inv.get("id", "faktura")).replace("/", "-")
        attachments.append({"name": f"faktura-{safe_num}.pdf", "content": pdf_b64})
    except Exception as e:
        logging.error(f"Nepodarilo se vygenerovat PDF faktury pro e-mail: {e}")
    cc_email = s.get("cc_email", "")
    payload = {
        "sender": {"name": "SMARTEST GUIDE", "email": "admin@smartestguide.com"},
        "to": [{"email": hotel_email, "name": hotel_name}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
        "attachment": attachments,
    }
    # Brevo odmítá prázdné pole cc/bcc → přidáme jen když nejsou prázdné.
    if cc_email:
        payload["cc"] = [{"email": cc_email}]
    _bcc = _admin_notify_bcc(exclude=cc_email)
    if _bcc:
        payload["bcc"] = _bcc
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.brevo.com/v3/smtp/email", json=payload,
                headers={"api-key": brevo_key, "Content-Type": "application/json"}, timeout=30)
            if r.status_code in (200, 201):
                logging.info(f"Faktura e-mail OK -> {hotel_email}, {num}")
            else:
                logging.error(f"Brevo faktura CHYBA {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logging.error(f"Chyba pri odesilani faktury e-mailem: {e}")


async def send_onboarding_email(hotel_id: str, portal_url: str, hotel_name: str, hotel_email: str):
    """Posle onboarding email hotelu po uspesne platbe pres Brevo API.
    Jazyk emailu se urcuje dle zeme hotelu (CZ/SK = cestina, ostatni = anglictina).
    Prilohy: QR kod jako PNG a PDF instrukce pro IT.
    """
    brevo_key = os.getenv("BREVO_API_KEY", "")
    if not brevo_key:
        logging.warning("BREVO_API_KEY neni nastaven")
        return

    # Odvoď base_url z portal_url
    base_url = portal_url.split("/portal?")[0] if "/portal?" in portal_url else portal_url.rsplit("/", 1)[0]
    poster_url = f"{base_url}/api/hotels/{hotel_id}/qr-poster"
    if not hotel_email:
        logging.warning(f"Hotel {hotel_id} nema email")
        return

    # Zjisti jazyk emailu dle zeme hotelu
    db = db_load()
    hotel = db.get("hotels", {}).get(hotel_id, {})
    country = (hotel.get("country") or "").upper()
    base_url = os.getenv("BASE_URL", "https://smartestguide-production.up.railway.app")
    widget_code = f'<script src="{base_url}/widget.js?hotel_id={hotel_id}"></script>'
    local_lang = COUNTRY_LANG_MAP.get(country, "en")

    # Texty emailu dle jazyka hotelu
    email_texts = {
        "cs": {
            "subject": f"Vítejte ve SMARTEST GUIDE – {hotel_name} je připraven!",
            "greeting": f"Vítejte, {hotel_name}!",
            "subtitle": "AI concierge pro váš hotel",
            "intro": f"Váš hotel byl úspěšně zaregistrován a platba proběhla. Alex je připraven odpovídat hostům ve více než 100 jazycích 24 hodin denně.",
            "portal_btn_text": "Otevřít hotelový portál",
            "steps_title": "Co dělat jako první:",
            "steps": ["Přihlaste se do portálu a zkontrolujte informace o hotelu","Doplňte orientaci v hotelu (wellness, parkoviště, restaurace, bar)","Přidejte lokální tipy pro hosty","Stáhněte QR plakát k tisku (odkaz níže) a umístěte ho na recepci"],
            "help_text": "Potřebujete pomoc?",
            "qr_label": "QR plakát pro hosty", "qr_desc": "Otevřete odkaz níže, vytiskněte plakát nebo uložte jako PDF.", "qr_btn_text": "Otevřít QR plakát k tisku", "qr_attach_note": "QR kód je také přiložen jako PNG.",
            "it_title": "Jak přidat chat tlačítko na web hotelu", "it_intro": "Předejte instrukce vašemu IT oddělení:", "it_step1": "Otevřete zdrojový kód stránky.", "it_step2": "Vložte kód těsně před </body>:", "it_step3": "Po nasazení se zobrazí plovoucí chat tlačítko.", "it_note": "Funguje na všech zařízeních.",
        },
        "de": {
            "subject": f"Willkommen bei SMARTEST GUIDE - {hotel_name} ist bereit!",
            "greeting": f"Willkommen, {hotel_name}!",
            "subtitle": "KI-Concierge für Ihr Hotel",
            "intro": f"Ihr Hotel wurde erfolgreich registriert und die Zahlung bestätigt. Alex ist bereit, Ihren Gästen in über 100 Sprachen rund um die Uhr zu antworten.",
            "portal_btn_text": "Hotel-Portal öffnen",
            "steps_title": "Was zuerst tun:",
            "steps": ["Im Portal anmelden und Hotelinformationen überprüfen","Hotelnavigation hinzufügen (Wellness, Parkplatz, Restaurant, Bar)","Lokale Tipps für Gäste hinzufügen","QR-Poster herunterladen und an der Rezeption platzieren"],
            "help_text": "Brauchen Sie Hilfe?",
            "qr_label": "QR-Poster für Gäste", "qr_desc": "Öffnen Sie den Link, drucken Sie das Poster oder speichern Sie es als PDF.", "qr_btn_text": "QR-Poster zum Drucken öffnen", "qr_attach_note": "Der QR-Code ist auch als PNG beigefügt.",
            "it_title": "Chat-Button auf Ihrer Website hinzufügen", "it_intro": "Leiten Sie diese Anweisungen an Ihre IT-Abteilung weiter:", "it_step1": "Öffnen Sie den Quellcode Ihrer Website.", "it_step2": "Fügen Sie den Code vor dem </body>-Tag ein:", "it_step3": "Nach der Bereitstellung erscheint ein Chat-Button.", "it_note": "Funktioniert auf allen Geräten.",
        },
        "fr": {
            "subject": f"Bienvenue sur SMARTEST GUIDE - {hotel_name} est prêt!",
            "greeting": f"Bienvenue, {hotel_name}!",
            "subtitle": "Concierge IA pour votre hôtel",
            "intro": f"Votre hôtel a été enregistré avec succès et le paiement confirmé. Alex est prêt à répondre à vos clients en plus de 100 langues, 24h/24.",
            "portal_btn_text": "Ouvrir le portail hôtel",
            "steps_title": "Que faire en premier:",
            "steps": ["Se connecter au portail et vérifier les informations","Ajouter la navigation de l'hôtel (bien-être, parking, restaurant)","Ajouter des conseils locaux pour les clients","Télécharger l'affiche QR et la placer à la réception"],
            "help_text": "Besoin d'aide?",
            "qr_label": "Affiche QR pour les clients", "qr_desc": "Ouvrez le lien, imprimez l'affiche ou enregistrez en PDF.", "qr_btn_text": "Ouvrir l'affiche QR", "qr_attach_note": "Le QR code est également joint en PNG.",
            "it_title": "Ajouter le bouton de chat à votre site", "it_intro": "Transmettez ces instructions à votre équipe IT:", "it_step1": "Ouvrez le code source de votre site.", "it_step2": "Insérez le code avant la balise </body>:", "it_step3": "Après déploiement, un bouton de chat apparaîtra.", "it_note": "Fonctionne sur tous les appareils.",
        },
        "it": {
            "subject": f"Benvenuto su SMARTEST GUIDE - {hotel_name} è pronto!",
            "greeting": f"Benvenuto, {hotel_name}!",
            "subtitle": "Concierge IA per il vostro hotel",
            "intro": f"Il vostro hotel è stato registrato con successo e il pagamento confermato. Alex è pronto a rispondere ai vostri ospiti in oltre 100 lingue, 24/7.",
            "portal_btn_text": "Apri portale hotel",
            "steps_title": "Cosa fare prima:",
            "steps": ["Accedere al portale e verificare le informazioni","Aggiungere la navigazione dell'hotel (wellness, parcheggio, ristorante)","Aggiungere consigli locali per gli ospiti","Scaricare il poster QR e posizionarlo alla reception"],
            "help_text": "Hai bisogno di aiuto?",
            "qr_label": "Poster QR per gli ospiti", "qr_desc": "Apri il link, stampa il poster o salvalo come PDF.", "qr_btn_text": "Apri poster QR", "qr_attach_note": "Il QR code è allegato anche come PNG.",
            "it_title": "Aggiungere il pulsante chat al sito web", "it_intro": "Inoltrate queste istruzioni al vostro team IT:", "it_step1": "Aprite il codice sorgente del vostro sito.", "it_step2": "Inserite il codice prima del tag </body>:", "it_step3": "Dopo il deploy apparirà un pulsante chat.", "it_note": "Funziona su tutti i dispositivi.",
        },
        "es": {
            "subject": f"Bienvenido a SMARTEST GUIDE - {hotel_name} está listo!",
            "greeting": f"Bienvenido, {hotel_name}!",
            "subtitle": "Concierge IA para su hotel",
            "intro": f"Su hotel ha sido registrado con éxito y el pago confirmado. Alex está listo para responder a sus huéspedes en más de 100 idiomas, 24/7.",
            "portal_btn_text": "Abrir portal del hotel",
            "steps_title": "Qué hacer primero:",
            "steps": ["Iniciar sesión en el portal y revisar la información","Añadir navegación del hotel (spa, aparcamiento, restaurante)","Añadir consejos locales para los huéspedes","Descargar el cartel QR y colocarlo en recepción"],
            "help_text": "¿Necesita ayuda?",
            "qr_label": "Cartel QR para huéspedes", "qr_desc": "Abra el enlace, imprima el cartel o guárdelo como PDF.", "qr_btn_text": "Abrir cartel QR", "qr_attach_note": "El código QR también se adjunta como PNG.",
            "it_title": "Añadir botón de chat al sitio web", "it_intro": "Reenvíe estas instrucciones a su equipo de IT:", "it_step1": "Abra el código fuente de su sitio web.", "it_step2": "Inserte el código antes de la etiqueta </body>:", "it_step3": "Tras el despliegue aparecerá un botón de chat.", "it_note": "Funciona en todos los dispositivos.",
        },
    }

    # Použij lokální jazyk, fallback na EN
    t = email_texts.get(local_lang, None)
    is_cs = local_lang == "cs"

    if t:
        subject = t["subject"]
        greeting = t["greeting"]
        subtitle = t["subtitle"]
        intro = t["intro"]
        portal_btn_text = t["portal_btn_text"]
        steps_title = t["steps_title"]
        steps = t["steps"]
        help_text = t["help_text"]
        qr_label = t["qr_label"]
        qr_desc = t["qr_desc"]
        qr_btn_text = t["qr_btn_text"]
        qr_attach_note = t["qr_attach_note"]
        it_title = t["it_title"]
        it_intro = t["it_intro"]
        it_step1 = t["it_step1"]
        it_step2 = t["it_step2"]
        it_step3 = t["it_step3"]
        it_note = t["it_note"]
    else:
        # EN fallback (všechny ostatní jazyky)
        subject = f"Welcome to SMARTEST GUIDE - {hotel_name} is ready!"
        greeting = f"Welcome, {hotel_name}!"
        subtitle = "AI Concierge for your hotel"
        intro = f"Your hotel has been successfully registered and payment confirmed. Alex is ready to answer your guests in 100+ languages, 24/7."
        portal_btn_text = "Open hotel portal"
        steps_title = "What to do first:"
        steps = [
            "Log in to the portal and review your hotel information",
            "Add hotel navigation (wellness, parking, restaurant, bar)",
            "Add local tips for guests",
            "Download the QR poster for printing (link below) and place it at reception, in rooms or on restaurant tables",
        ]
        it_title = "How to add the chat button to your hotel website"
        it_intro = "Please forward the following instructions to your IT department or webmaster:"
        it_step1 = "Open the source code of your hotel website (or contact your IT team)."
        it_step2 = "Insert the following code just before the closing </body> tag:"
        it_step3 = "After saving and deploying, a floating chat button will appear on your website for guests."
        it_note = "The button works on all devices (mobile, tablet, desktop) and requires no additional configuration."
        help_text = "Need help?"
        qr_label = "QR poster for guests"
        qr_desc = "Open the link below, print the poster or save as PDF and place it at reception, in rooms or on restaurant tables."
        qr_btn_text = "Open QR poster for printing"
        qr_attach_note = "The QR code is also attached as PNG for direct use."

    steps_html = "".join(f"<li style='margin-bottom:8px'>{s}</li>" for s in steps)

    html_body = f"""<div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;color:#1a1a2e">
      <div style="background:#1a1a1a;padding:32px;text-align:center;border-radius:12px 12px 0 0;border-bottom:3px solid #FF6B00">
        <h1 style="color:#fff;margin:0;font-size:28px;letter-spacing:.5px">SMARTEST GUIDE</h1>
        <p style="color:rgba(255,255,255,.85);margin:8px 0 0">{subtitle}</p>
      </div>
      <div style="background:#f8f9ff;padding:32px;border-radius:0 0 12px 12px">
        <h2 style="color:#1a1a2e;margin-bottom:12px">{greeting}</h2>
        <p style="color:#555;line-height:1.7;margin-bottom:24px">{intro}</p>

        <div style="background:#fff;border:2px solid #00d4aa;border-radius:10px;padding:20px;margin-bottom:24px;text-align:center">
          <a href="{portal_url}" style="display:inline-block;background:#FF6B00;color:#0a0b0f;text-decoration:none;padding:14px 32px;border-radius:8px;font-weight:700;font-size:16px">{portal_btn_text} →</a>
        </div>

        <h3 style="color:#FF6B00;margin-bottom:12px">{steps_title}</h3>
        <ol style="color:#555;line-height:1.8;padding-left:20px;margin-bottom:24px">{steps_html}</ol>

        <div style="background:#1a1a1a;border:1px solid rgba(255,107,0,.4);border-radius:10px;padding:24px;margin-bottom:24px;text-align:center">
          <div style="font-size:11px;color:#00d4aa;text-transform:uppercase;letter-spacing:1.5px;font-weight:700;margin-bottom:12px">🖨️ {qr_label}</div>
          <p style="color:#9ba0c0;font-size:13px;line-height:1.6;margin-bottom:16px">{qr_desc}</p>
          <a href="{poster_url}" style="display:inline-block;background:#FF6B00;color:#0a0b0f;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;font-size:14px">🖨️ {qr_btn_text}</a>
          <p style="color:#555;font-size:11px;margin-top:12px">{qr_attach_note}</p>
        </div>

        <div style="background:#1a1a2e;border-radius:10px;padding:24px;margin-bottom:24px">
          <h3 style="color:#00d4aa;margin-bottom:8px">💻 {it_title}</h3>
          <p style="color:#b0b4cc;font-size:13px;line-height:1.7;margin-bottom:12px">{it_intro}</p>
          <p style="color:#b0b4cc;font-size:13px;margin-bottom:8px">1. {it_step1}</p>
          <p style="color:#b0b4cc;font-size:13px;margin-bottom:8px">2. {it_step2}</p>
          <div style="background:#0d1117;border-radius:6px;padding:12px;margin:10px 0;font-family:monospace;font-size:12px;color:#00d4aa;word-break:break-all">{widget_code}</div>
          <p style="color:#b0b4cc;font-size:13px;margin-bottom:4px">3. {it_step3}</p>
          <p style="color:#7a7fa8;font-size:12px;margin-top:8px;font-style:italic">{it_note}</p>
        </div>

        <hr style="border:none;border-top:1px solid #e0e0f0;margin:20px 0"/>
        <p style="color:#888;font-size:12px;text-align:center">
          {help_text} <a href="mailto:admin@smartestguide.com" style="color:#FF6B00">admin@smartestguide.com</a>
        </p>
      </div>
    </div>"""

    text_body = f"{greeting}\n\nPortal: {portal_url}\n\n{it_title}:\n{widget_code}\n\nPomoc: admin@smartestguide.com"

    # Vygeneruj QR kod jako PNG prilohu
    attachments = []
    try:
        _oid, _ohotel = _resolve_hotel(db_load(), hotel_id)
        guest_url = _guest_url(base_url, hotel_id, _ohotel)
        qr_bytes = _generate_qr_png_branded(guest_url, size=400)
        qr_b64 = base64.b64encode(qr_bytes).decode()
        attachments.append({
            "name": f"SMARTEST_GUIDE_QR_{hotel_name.replace(' ','_')}.png",
            "content": qr_b64,
        })
        logging.info(f"QR kod PNG vygenerovan pro {hotel_name}, velikost: {len(qr_b64)} znaku")
    except Exception as e:
        logging.warning(f"Nepodarilo se vygenerovat QR PNG: {e}")

    s = db_get_settings()
    cc_email = s.get("cc_email", "")

    payload = {
        "sender": {"name": "SMARTEST GUIDE", "email": "admin@smartestguide.com"},
        "to": [{"email": hotel_email, "name": hotel_name}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": text_body,
        "attachment": attachments,
    }
    # Brevo odmítá prázdné pole cc/bcc → přidáme jen když nejsou prázdné.
    if cc_email:
        payload["cc"] = [{"email": cc_email}]
    _bcc = _admin_notify_bcc(exclude=cc_email)
    if _bcc:
        payload["bcc"] = _bcc

    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.brevo.com/v3/smtp/email",
                json=payload,
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                timeout=30
            )
            if r.status_code in (200, 201):
                logging.warning(f"Onboarding email OK -> {hotel_email}, lang={'cs' if is_cs else 'en'}, prilohy={len(attachments)}")
            else:
                logging.error(f"Brevo API CHYBA {r.status_code}: {r.text[:300]}")
    except Exception as e:
        logging.error(f"Chyba pri odesilani emailu: {e}")

# ─────────────────────────────────────────────
# Měsíční report hotelu (retence — ukázat hodnotu)
# ─────────────────────────────────────────────
_LANG_NAMES = {
    "cs": {"en": "angličtina", "cs": "čeština", "de": "němčina", "sk": "slovenština",
           "pl": "polština", "fr": "francouzština", "es": "španělština", "it": "italština",
           "nl": "nizozemština", "pt": "portugalština", "ru": "ruština", "uk": "ukrajinština",
           "zh": "čínština", "ja": "japonština", "ko": "korejština", "ar": "arabština"},
    "en": {"en": "English", "cs": "Czech", "de": "German", "sk": "Slovak",
           "pl": "Polish", "fr": "French", "es": "Spanish", "it": "Italian",
           "nl": "Dutch", "pt": "Portuguese", "ru": "Russian", "uk": "Ukrainian",
           "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "ar": "Arabic"},
}

def _prev_month_key(ref: datetime = None) -> str:
    """Klíč předchozího kalendářního měsíce, formát YYYY-MM."""
    ref = ref or datetime.utcnow()
    first = ref.replace(day=1)
    prev = first - timedelta(days=1)
    return prev.strftime("%Y-%m")

def _month_key_shift(month_key: str, delta: int) -> str:
    """Posune YYYY-MM o delta měsíců (např. -1)."""
    y, m = int(month_key[:4]), int(month_key[5:7])
    idx = (y * 12 + (m - 1)) + delta
    return f"{idx // 12:04d}-{(idx % 12) + 1:02d}"

def _month_label(month_key: str, lang: str) -> str:
    y, m = int(month_key[:4]), int(month_key[5:7])
    names_cs = ["", "leden", "únor", "březen", "duben", "květen", "červen", "červenec",
                "srpen", "září", "říjen", "listopad", "prosinec"]
    names_en = ["", "January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]
    nm = names_cs if lang == "cs" else names_en
    return f"{nm[m]} {y}"

def build_monthly_report(hotel_id: str, month_key: str = None) -> dict:
    """Sestaví měsíční report hotelu. Vrací dict (subject, html, text, lang, has_data) nebo None."""
    db = db_load()
    hotel = db.get("hotels", {}).get(hotel_id)
    if not hotel:
        return None
    month_key = month_key or _prev_month_key()
    a = db.get("analytics", {}).get(hotel_id, {}) or {}
    monthly = a.get("monthly", {}) or {}
    cur = monthly.get(month_key, {"count": 0, "flagged": 0, "langs": {}})
    prev_key = _month_key_shift(month_key, -1)
    prev = monthly.get(prev_key, {"count": 0})
    count = int(cur.get("count", 0))
    flagged = int(cur.get("flagged", 0))
    prev_count = int(prev.get("count", 0))

    country = (hotel.get("country") or "").upper()
    lang = "cs" if country in ("CZ", "SK") else "en"
    hotel_name = hotel.get("name") or "hotel"
    base_url = os.getenv("BASE_URL", "https://smartestguide-production.up.railway.app").rstrip("/")
    token = hotel.get("hotel_token", "")
    portal_url = f"{base_url}/portal?token={token}" if token else base_url

    # Top 3 jazyky hostů
    langs = cur.get("langs", {}) or {}
    top_langs = sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:3]
    lname = _LANG_NAMES.get(lang, _LANG_NAMES["en"])
    top_langs_str = ", ".join(f"{lname.get(code, code)} ({n})" for code, n in top_langs) if top_langs else ("—")

    # Trend vs. minulý měsíc
    if prev_count > 0:
        pct = round((count - prev_count) / prev_count * 100)
        trend_cs = f"{'+' if pct >= 0 else ''}{pct} % oproti měsíci {_month_label(prev_key,'cs')}"
        trend_en = f"{'+' if pct >= 0 else ''}{pct}% vs. {_month_label(prev_key,'en')}"
    else:
        trend_cs = trend_en = ""

    comp = hotel_profile_completeness(hotel)
    score = comp.get("score", 0) if isinstance(comp, dict) else 0

    month_lbl = _month_label(month_key, lang)
    # Odhad ušetřeného času recepce (~2 min / dotaz)
    saved_min = count * 2
    saved_h = round(saved_min / 60, 1)

    if lang == "cs":
        subject = f"Měsíční přehled – {hotel_name} · {month_lbl}"
        trend_html = f'<p style="margin:2px 0 0;color:#16a34a;font-size:13px">{trend_cs}</p>' if trend_cs else ""
        flagged_html = ""
        if flagged > 0:
            flagged_html = (f'<tr><td style="padding:10px 0;border-top:1px solid #eee">'
                            f'<strong>{flagged}×</strong> Alex neměl dost informací k odpovědi. '
                            f'Doplňte profil hotelu, ať hosté dostanou odpověď vždy.</td></tr>')
        profile_html = ""
        if score < 100:
            profile_html = (f'<tr><td style="padding:10px 0;border-top:1px solid #eee">'
                            f'Profil hotelu je vyplněn na <strong>{score} %</strong>. '
                            f'Doplnění chybějících údajů zvýší kvalitu odpovědí.</td></tr>')
        intro = "tady je váš měsíční přehled, jak Alex sloužil vašim hostům."
        stat_label = "konverzací s hosty"
        langs_label = "Nejčastější jazyky hostů"
        saved_label = "Odhadem ušetřeno recepci"
        saved_val = f"~{saved_h} h" if saved_h >= 1 else f"~{saved_min} min"
        cta = "Otevřít hotelový portál"
        outro = "Alex odpovídá 24/7 ve více než 100 jazycích – bez zatížení recepce."
    else:
        subject = f"Monthly summary – {hotel_name} · {month_lbl}"
        trend_html = f'<p style="margin:2px 0 0;color:#16a34a;font-size:13px">{trend_en}</p>' if trend_en else ""
        flagged_html = ""
        if flagged > 0:
            flagged_html = (f'<tr><td style="padding:10px 0;border-top:1px solid #eee">'
                            f'<strong>{flagged}×</strong> Alex lacked enough info to answer. '
                            f'Complete your hotel profile so guests always get an answer.</td></tr>')
        profile_html = ""
        if score < 100:
            profile_html = (f'<tr><td style="padding:10px 0;border-top:1px solid #eee">'
                            f'Your hotel profile is <strong>{score}%</strong> complete. '
                            f'Filling the gaps improves answer quality.</td></tr>')
        intro = "here is your monthly summary of how Alex served your guests."
        stat_label = "guest conversations"
        langs_label = "Top guest languages"
        saved_label = "Estimated reception time saved"
        saved_val = f"~{saved_h} h" if saved_h >= 1 else f"~{saved_min} min"
        cta = "Open hotel portal"
        outro = "Alex answers 24/7 in 100+ languages — without burdening your reception."

    html = f"""<!DOCTYPE html><html><body style="margin:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;color:#1a1a1a">
<div style="max-width:560px;margin:0 auto;padding:24px">
  <div style="background:#1a1a1a;color:#fff;border-radius:14px 14px 0 0;padding:22px 24px">
    <div style="font-size:13px;letter-spacing:.5px;color:#ff8a3d">SMARTEST GUIDE</div>
    <div style="font-size:20px;font-weight:bold;margin-top:4px">{subject}</div>
  </div>
  <div style="background:#fff;border-radius:0 0 14px 14px;padding:24px">
    <p style="margin:0 0 16px">{hotel_name}, {intro}</p>
    <div style="background:#fafafa;border:1px solid #eee;border-radius:12px;padding:18px;text-align:center">
      <div style="font-size:40px;font-weight:bold;color:#ff6b00;line-height:1">{count}</div>
      <div style="color:#666;font-size:14px;margin-top:4px">{stat_label}</div>
      {trend_html}
    </div>
    <table style="width:100%;border-collapse:collapse;margin-top:14px;font-size:14px">
      <tr><td style="padding:10px 0;border-top:1px solid #eee;color:#666">{langs_label}</td>
          <td style="padding:10px 0;border-top:1px solid #eee;text-align:right"><strong>{top_langs_str}</strong></td></tr>
      <tr><td style="padding:10px 0;border-top:1px solid #eee;color:#666">{saved_label}</td>
          <td style="padding:10px 0;border-top:1px solid #eee;text-align:right"><strong>{saved_val}</strong></td></tr>
      {flagged_html}
      {profile_html}
    </table>
    <div style="text-align:center;margin:22px 0 6px">
      <a href="{portal_url}" style="display:inline-block;background:#ff6b00;color:#fff;text-decoration:none;padding:12px 26px;border-radius:10px;font-weight:bold">{cta}</a>
    </div>
    <p style="margin:16px 0 0;color:#888;font-size:12px;text-align:center">{outro}</p>
  </div>
</div></body></html>"""

    text = (f"{subject}\n\n{hotel_name}, {intro}\n\n"
            f"{count} {stat_label}"
            + (f" ({trend_cs if lang=='cs' else trend_en})" if (trend_cs if lang=='cs' else trend_en) else "")
            + f"\n{langs_label}: {top_langs_str}\n{saved_label}: {saved_val}\n\n"
            + (f"{flagged}x " + ("Alex nemel dost informaci.\n" if lang=='cs' else "Alex lacked info.\n") if flagged else "")
            + f"{cta}: {portal_url}\n\n{outro}")

    return {"subject": subject, "html": html, "text": text, "lang": lang,
            "has_data": count > 0, "count": count, "month_key": month_key}

async def send_monthly_report(hotel_id: str, month_key: str = None, dry_run: bool = False) -> dict:
    """Odešle měsíční report hotelu přes Brevo. dry_run=True jen sestaví, neodesílá."""
    rep = build_monthly_report(hotel_id, month_key)
    if not rep:
        return {"status": "error", "detail": "hotel nenalezen"}
    db = db_load()
    hotel = db.get("hotels", {}).get(hotel_id, {})
    hotel_email = (hotel.get("email") or "").strip()
    if not rep["has_data"]:
        return {"status": "skipped", "detail": "žádná aktivita v daném měsíci", "month_key": rep["month_key"]}
    if dry_run:
        return {"status": "dry_run", "subject": rep["subject"], "count": rep["count"], "month_key": rep["month_key"]}
    brevo_key = os.getenv("BREVO_API_KEY", "")
    if not brevo_key or not hotel_email:
        return {"status": "error", "detail": "chybí BREVO_API_KEY nebo e-mail hotelu"}
    payload = {
        "sender": {"name": "SMARTEST GUIDE", "email": "admin@smartestguide.com"},
        "to": [{"email": hotel_email, "name": hotel.get("name", "")}],
        "subject": rep["subject"],
        "htmlContent": rep["html"],
        "textContent": rep["text"],
    }
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post("https://api.brevo.com/v3/smtp/email", json=payload,
                headers={"api-key": brevo_key, "Content-Type": "application/json"}, timeout=30)
        if r.status_code in (200, 201):
            # Zaznamenej, že report za tento měsíc byl odeslán (dedup)
            db = db_load()
            a = db.setdefault("analytics", {}).setdefault(hotel_id, {})
            a["monthly_report_sent"] = rep["month_key"]
            db_save(db)
            logging.info("Měsíční report odeslán -> %s (%s)", hotel_email, rep["month_key"])
            return {"status": "sent", "month_key": rep["month_key"], "count": rep["count"]}
        logging.error("Brevo report CHYBA %s: %s", r.status_code, r.text[:200])
        return {"status": "error", "detail": f"Brevo {r.status_code}"}
    except Exception as e:
        logging.error("Report e-mail výjimka: %s", e)
        return {"status": "error", "detail": str(e)}

async def _send_monthly_reports_if_due():
    """Auto-odeslání měsíčních reportů (GATED settingem, default VYPNUTO).
    Odešle report za předchozí měsíc každému aktivnímu hotelu max. 1×."""
    if not db_get_settings().get("monthly_reports_enabled", False):
        return
    month_key = _prev_month_key()
    db = db_load()
    hotels = list(db.get("hotels", {}).items())
    analytics = db.get("analytics", {})
    for hotel_id, h in hotels:
        if not h.get("subscription_active"):
            continue
        a = analytics.get(hotel_id, {}) or {}
        if a.get("monthly_report_sent") == month_key:
            continue  # už odesláno
        try:
            await send_monthly_report(hotel_id, month_key=month_key)
        except Exception as e:
            logging.error("Auto-report hotel %s chyba: %s", hotel_id, e)

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
                if key == "bed_count" and value:
                    # Uložit scraped hodnotu zvlášť — nepřepisovat registrovanou hodnotu
                    db["hotels"][hotel_id]["scraped_bed_count"] = value
                    # Přepsat bed_count jen pokud hotel neuvedl žádnou hodnotu při registraci
                    if not db["hotels"][hotel_id].get("registered_bed_count"):
                        db["hotels"][hotel_id]["registered_bed_count"] = db["hotels"][hotel_id].get("bed_count") or 0
                    if not db["hotels"][hotel_id].get("bed_count"):
                        db["hotels"][hotel_id]["bed_count"] = value
                elif not db["hotels"][hotel_id].get(key) and value:
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
async def stripe_checkout(hotel_id: str, request: Request):
    """Vytvoří dynamickou Stripe Checkout Session (per-lůžko) pro reaktivaci/platbu z portálu.
    Nahrazuje statický payment link → vždy aktuální cena a žádný test/live link k údržbě."""
    db = db_load()
    s = db_get_settings()
    hotel = db["hotels"].get(hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel nenalezen")
    stripe_key = s.get("stripe_secret_key", "")
    if not stripe_key:
        raise HTTPException(400, "Stripe není nastaven")
    base = int(s.get("pricing_base", 199))
    threshold = int(s.get("pricing_threshold", 100))
    per_bed = float(s.get("pricing_per_bed", 3))
    beds = hotel.get("bed_count", 0) or 0
    price = base if beds <= threshold else int(base + (beds - threshold) * per_bed)
    base_url = get_base_url(request)
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.stripe.com/v1/checkout/sessions",
                headers={"Authorization": f"Bearer {stripe_key}",
                         "Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "mode": "subscription",
                    "client_reference_id": hotel_id,
                    "customer_email": hotel.get("email", ""),
                    "success_url": f"{base_url}/success?hotel_id={hotel_id}",
                    "cancel_url": f"{base_url}/portal?token={hotel.get('hotel_token','')}",
                    "line_items[0][price_data][currency]": "eur",
                    "line_items[0][price_data][product_data][name]": f"SmartestGuide – {hotel.get('name','hotel')}",
                    "line_items[0][price_data][product_data][description]": f"AI concierge pro {beds} lůžek",
                    "line_items[0][price_data][recurring][interval]": "month",
                    "line_items[0][price_data][unit_amount]": str(price * 100),
                    "line_items[0][quantity]": "1",
                    "metadata[hotel_id]": hotel_id,
                },
                timeout=30.0,
            )
        if r.status_code != 200:
            logging.error("Stripe checkout (portal) chyba %s: %s", r.status_code, r.text[:200])
            raise HTTPException(502, "Nepodařilo se vytvořit platbu")
        url = r.json().get("url")
        if not url:
            raise HTTPException(502, "Stripe nevrátil URL")
        return {"status": "ok", "checkout_url": url, "hotel_name": hotel.get("name", "")}
    except HTTPException:
        raise
    except Exception as e:
        logging.error("Stripe checkout (portal) výjimka: %s", e)
        raise HTTPException(502, "Chyba při vytváření platby")

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

        # Pro invoice.payment_succeeded najdi hotel dle customer_id pokud chybí hotel_id
        if not hotel_id and customer_id and event_type == "invoice.payment_succeeded":
            db_tmp = db_load()
            for hid, h in db_tmp["hotels"].items():
                if h.get("stripe_customer_id") == customer_id:
                    hotel_id = hid
                    break

        if hotel_id:
            db = db_load()
            if hotel_id in db["hotels"]:
                from datetime import timedelta
                now = datetime.utcnow()
                db["hotels"][hotel_id]["subscription_active"] = True
                db["hotels"][hotel_id]["stripe_customer_id"] = customer_id
                if subscription_id:
                    db["hotels"][hotel_id]["stripe_subscription_id"] = subscription_id
                db["hotels"][hotel_id]["updated_at"] = now.isoformat()

                if event_type == "checkout.session.completed":
                    # První platba — nastav trial a subscription_start
                    trial_used_meta = obj.get("metadata", {}).get("trial_used", "0")
                    db["hotels"][hotel_id]["subscription_start"] = now.isoformat()
                    if trial_used_meta == "0":
                        # Trial 14 dní zdarma, pak 30 dní placeno
                        db["hotels"][hotel_id]["trial_used"] = True
                        db["hotels"][hotel_id]["trial_start"] = now.isoformat()
                        db["hotels"][hotel_id]["subscription_period_end"] = (now + timedelta(days=44)).isoformat()
                        # Viditelný trial záznam v přehledu: €0, čeká se na první platbu (14. den)
                        try:
                            _first_pay = (now + timedelta(days=14)).date().isoformat()
                            _create_invoice_record(db, hotel_id, db["hotels"][hotel_id], trial=True, payment_due=_first_pay)
                            logging.info("Trial záznam vytvořen pro hotel %s (první platba %s)", hotel_id, _first_pay)
                        except Exception as e:
                            logging.warning("Trial záznam selhal pro hotel %s: %s", hotel_id, e)
                    else:
                        # Bez trialu — rovnou 30 dní
                        db["hotels"][hotel_id]["subscription_period_end"] = (now + timedelta(days=30)).isoformat()

                elif event_type == "invoice.payment_succeeded":
                    # Obnovení předplatného — prodloužit o 30 dní od konce aktuálního období
                    # Idempotency check — Stripe může poslat event 2x
                    invoice_id = obj.get("id", "")
                    last_invoice = db["hotels"][hotel_id].get("last_processed_invoice", "")
                    if invoice_id and invoice_id == last_invoice:
                        logging.info("Duplicate invoice event %s for hotel %s, skipping", invoice_id, hotel_id)
                        return {"status": "ok", "note": "duplicate"}
                    db["hotels"][hotel_id]["last_processed_invoice"] = invoice_id
                    current_end_str = db["hotels"][hotel_id].get("subscription_period_end", "")
                    try:
                        current_end = datetime.fromisoformat(current_end_str)
                        new_end = max(current_end, now) + timedelta(days=30)
                    except Exception:
                        new_end = now + timedelta(days=30)
                    db["hotels"][hotel_id]["subscription_period_end"] = new_end.isoformat()

                    # Faktura JEN když Stripe reálně strhl peníze (amount_paid > 0).
                    # Nulové trialové faktury (amount_paid=0) přeskoč — trial má vlastní
                    # záznam z checkoutu. Idempotenci hlídá last_processed_invoice výše.
                    amount_paid_cents = obj.get("amount_paid", 0) or 0
                    if amount_paid_cents > 0:
                        try:
                            inv = _create_invoice_record(db, hotel_id, db["hotels"][hotel_id], status="paid")
                            logging.info("Auto-faktura %s (PAID) vytvořena pro hotel %s", inv.get("invoice_number"), hotel_id)
                            # Pošli fakturu hotelu e-mailem (PDF v příloze)
                            _h = db["hotels"][hotel_id]
                            if not _h.get("hotel_token"):
                                import uuid as _uuid
                                _h["hotel_token"] = str(_uuid.uuid4()).replace("-", "")
                            _base = os.getenv("BASE_URL", "https://smartestguide-production.up.railway.app")
                            _purl = f"{_base}/portal?token={_h['hotel_token']}"
                            _spawn(send_invoice_email(_h, inv, _purl))
                        except Exception as e:
                            logging.warning("Auto-faktura selhala pro hotel %s: %s", hotel_id, e)

                        # Provize partnerovi až za PRVNÍ reálnou platbu (idempotentní)
                        try:
                            _create_commission_if_eligible(db, hotel_id)
                        except Exception as e:
                            logging.warning("Vytvoření provize selhalo pro hotel %s: %s", hotel_id, e)
                    else:
                        logging.info("invoice.payment_succeeded amount_paid=0 (trial) pro hotel %s — fakturu ani provizi negeneruji", hotel_id)

                db_save(db)

                # Po checkout.session.completed VŽDY: vygeneruj portal token + pošli onboarding
                # (NEZÁVISLE na tom, jestli má hotel vyplněný web — URL je nepovinná!).
                # Scraping webu jen když hotel web má.
                if event_type == "checkout.session.completed":
                    hotel_email = db["hotels"][hotel_id].get("email", "")
                    hotel_name = db["hotels"][hotel_id].get("name", "Hotel")
                    if not db["hotels"][hotel_id].get("hotel_token"):
                        import uuid as _uuid
                        db["hotels"][hotel_id]["hotel_token"] = str(_uuid.uuid4()).replace("-", "")
                        db_save(db)
                    token = db["hotels"][hotel_id]["hotel_token"]
                    base_url = os.getenv("BASE_URL", "https://smartestguide-production.up.railway.app")
                    portal_url = f"{base_url}/portal?token={token}"
                    # Onboarding NA POZADÍ — webhook musí Stripe odpovědět rychle. _spawn drží referenci (žádný GC).
                    logging.warning("Onboarding trigger -> hotel %s (%s)", hotel_id, hotel_email)
                    _spawn(send_onboarding_email(hotel_id, portal_url, hotel_name, hotel_email))
                    # Scraping webu jen když hotel web má
                    hotel_url = db["hotels"][hotel_id].get("url") or db["hotels"][hotel_id].get("source_url")
                    if hotel_url:
                        _spawn(auto_scrape_after_payment(hotel_id, hotel_url))

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
    # Při aktivaci nastav zaplacená lůžka = aktuální počet a zruš případnou archivaci (obnovení)
    if active:
        beds = db["hotels"][hotel_id].get("bed_count", 0)
        db["hotels"][hotel_id]["subscription_paid_beds"] = beds
        db["hotels"][hotel_id]["archived"] = False
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
def serve_widget(hotel_id: str, request: Request, lang: str = "auto"):
    """JavaScript widget pro vložení na web hotelu."""
    base = get_base_url(request)
    _wdb = db_load(); _whotel = _wdb["hotels"].get(hotel_id)
    guest_url = _guest_url(base, hotel_id, _whotel)
    js = f"""/* SmartestGuide Widget v1 */
(function(){{
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

@app.get("/api/analytics/overview")
def analytics_overview():
    """Souhrnná usage analytika napříč hotely — jen pro admina.
    POZOR: záměrně NENÍ pod /api/admin/ (ten prefix je veřejný kvůli loginu),
    takže tento endpoint chrání _admin_gate cookie."""
    db = db_load()
    hotels = db.get("hotels", {})
    analytics = db.get("analytics", {})
    now = datetime.utcnow()
    cur_key = now.strftime("%Y-%m")
    # Posledních 12 měsíců, od nejstaršího po aktuální
    month_keys = [_month_key_shift(cur_key, -i) for i in range(11, -1, -1)]

    months = {mk: {"count": 0, "flagged": 0, "devices": 0, "visits": 0, "visitors": 0} for mk in month_keys}
    langs_total = {}
    hotels_out = []
    grand_total = 0

    for hid, h in hotels.items():
        a = analytics.get(hid, {}) or {}
        monthly = a.get("monthly", {}) or {}
        for mk in month_keys:
            m = monthly.get(mk) or {}
            months[mk]["count"] += int(m.get("count", 0))
            months[mk]["flagged"] += int(m.get("flagged", 0))
            months[mk]["devices"] += len(m.get("devices") or {})
            months[mk]["visits"] += int(m.get("visits", 0))
            months[mk]["visitors"] += len(m.get("visit_devices") or {})
            for code, n in (m.get("langs") or {}).items():
                langs_total[code] = langs_total.get(code, 0) + int(n)
        cur = monthly.get(cur_key) or {}
        prev = monthly.get(_month_key_shift(cur_key, -1)) or {}
        cur_n, prev_n = int(cur.get("count", 0)), int(prev.get("count", 0))
        trend = round((cur_n - prev_n) / prev_n * 100) if prev_n > 0 else None
        top = sorted((cur.get("langs") or {}).items(), key=lambda kv: kv[1], reverse=True)[:3]
        last_chat = a.get("last_chat")
        silent_days = None
        if last_chat:
            try:
                silent_days = max(0, (now - datetime.fromisoformat(last_chat)).days)
            except Exception:
                pass
        total = int(a.get("total", 0))
        grand_total += total
        visits_total = int(a.get("visits_total", 0))
        hotels_out.append({
            "id": hid,
            "name": h.get("name") or hid,
            "active": bool(h.get("subscription_active")),
            "total": total,
            "month_count": cur_n,
            "month_devices": len(cur.get("devices") or {}),
            "month_visits": int(cur.get("visits", 0)),
            "month_visitors": len(cur.get("visit_devices") or {}),
            "visits_total": visits_total,
            "prev_count": prev_n,
            "trend_pct": trend,
            "month_flagged": int(cur.get("flagged", 0)),
            "flagged_total": int(a.get("flagged_count", 0)),
            "last_chat": last_chat,
            "silent_days": silent_days,
            "top_langs": [{"code": c, "count": n} for c, n in top],
        })

    hotels_out.sort(key=lambda x: (x["month_count"], x["total"]), reverse=True)
    cur_m = months[cur_key]
    prev_m = months[_month_key_shift(cur_key, -1)]
    trend_all = (round((cur_m["count"] - prev_m["count"]) / prev_m["count"] * 100)
                 if prev_m["count"] > 0 else None)
    top_langs = sorted(langs_total.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return {
        "status": "ok",
        "months": [{"key": mk, **months[mk]} for mk in month_keys],
        "summary": {
            "grand_total": grand_total,
            "grand_visits": sum(x["visits_total"] for x in hotels_out),
            "month_visits": cur_m["visits"],
            "month_visitors": cur_m["visitors"],
            "month_count": cur_m["count"],
            "prev_month_count": prev_m["count"],
            "trend_pct": trend_all,
            "month_flagged": cur_m["flagged"],
            "month_devices": cur_m["devices"],
            "hotels_with_usage": sum(1 for x in hotels_out if x["month_count"] > 0),
            "hotels_total": len(hotels_out),
        },
        "top_langs": [{"code": c, "count": n} for c, n in top_langs],
        "hotels": hotels_out,
    }

# Fráze naznačující, že Alexovi chyběla informace (heuristika napříč jazyky)
_LOW_INFO_MARKERS = [
    "i don't have", "i do not have", "i'm not sure", "i am not sure", "no information",
    "not sure", "nemám", "nemam", "bohužel nevím", "bohuzel nevim", "nevím", "nemáme tu informaci",
    "kontaktujte recepci", "contact the reception", "ask the reception", "ask at reception",
    "žádné informace", "zadne informace", "leider", "désolé je n'ai pas", "no tengo esa información",
]

def _log_guest_question(hotel_id: str, message: str, language: str, reply: str, device_id: str = ""):
    """Zaloguje dotaz hosta do analytiky + drží posledních 50 reálných dotazů (pro doplnění mezer)."""
    if not message:
        return
    db = db_load()
    analytics = db.setdefault("analytics", {})
    a = analytics.setdefault(hotel_id, {"total": 0, "topics": {}})
    a["total"] = a.get("total", 0) + 1
    now = datetime.utcnow()
    a["last_chat"] = now.isoformat()
    low = (reply or "").lower()
    flagged = any(m in low for m in _LOW_INFO_MARKERS)
    rq = a.setdefault("recent_questions", [])
    rq.insert(0, {
        "q": (message or "")[:300],
        "lang": language or "",
        "flagged": flagged,
        "at": now.isoformat(),
    })
    del rq[50:]
    if flagged:
        a["flagged_count"] = a.get("flagged_count", 0) + 1
    # Měsíční agregace (pro měsíční report hotelu) — count + jazyky + flagged
    month_key = now.strftime("%Y-%m")
    monthly = a.setdefault("monthly", {})
    m = monthly.setdefault(month_key, {"count": 0, "flagged": 0, "langs": {}})
    m["count"] = m.get("count", 0) + 1
    if flagged:
        m["flagged"] = m.get("flagged", 0) + 1
    if language:
        m.setdefault("langs", {})[language] = m["langs"].get(language, 0) + 1
    # Unikátní zařízení: ukládáme jen zkrácený hash (pseudonymizace), cap 2000/měsíc proti bobtnání JSONB
    if device_id:
        dh = hashlib.sha256(device_id.encode()).hexdigest()[:12]
        dv = m.setdefault("devices", {})
        if dh in dv or len(dv) < 2000:
            dv[dh] = dv.get(dh, 0) + 1
    # Nech jen posledních 18 měsíců, ať data.json nebobtná
    if len(monthly) > 18:
        for k in sorted(monthly.keys())[:-18]:
            monthly.pop(k, None)
    db_save(db)

# ─────────────────────────────────────────────
# Email reminder
# ─────────────────────────────────────────────
def hotel_profile_completeness(hotel: dict) -> dict:
    required = [
        "name", "address", "phone", "email",
        "checkin_time", "checkout_time", "breakfast_hours",
        "bed_count", "star_rating", "description",
        "wifi_name", "wifi_password",
    ]
    bonus = [
        "wellness_info", "parking_info", "restaurant_name",
        "nearby_places", "fitness_info", "pool_info",
        "whatsapp_number", "whatsapp_wellness", "dinner_hours",
        "pet_policy",
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
def send_reminder(hotel_id: str, request: Request, dry_run: bool = False):
    db = db_load()
    h = db["hotels"].get(hotel_id)
    if not hotel_id or not h:
        raise HTTPException(404, "Hotel nenalezen")
    completeness = hotel_profile_completeness(h)
    portal_url = get_base_url(request) + "/portal?token=" + h.get("hotel_token","")
    hotel_email = h.get("registration_email") or h.get("email", "")
    hotel_name = h.get("name", "Hotel")
    missing_labels = {
        "address": "Adresa hotelu", "phone": "Telefon recepce",
        "email": "Email", "checkin_time": "Check-in čas",
        "checkout_time": "Check-out čas", "breakfast_hours": "Hodiny snídaně",
        "bed_count": "Počet lůžek", "star_rating": "Hvězdičky",
    }
    missing_list = [missing_labels.get(f, f) for f in completeness["missing"]]
    score = completeness["score"]
    missing_html = "".join(f"<li>{m}</li>" for m in missing_list) if missing_list else "<li>Profil je kompletní!</li>"

    # Dry-run nebo testovací (E2E) hotel → NEODESÍLEJ reálný e-mail.
    # E2E test tak jen ověří, že endpoint funguje, a nespamuje inbox.
    if dry_run or (hotel_name or "").strip().upper().startswith("E2E"):
        return {"status": "ok", "dry_run": True, "email_to": hotel_email, "score": score,
                "note": "dry_run — e-mail neodeslán"}

    brevo_key = os.getenv("BREVO_API_KEY", "")
    if brevo_key and hotel_email:
        import httpx as _httpx
        import asyncio as _asyncio

        subject = f"Připomínka: doplňte profil hotelu {hotel_name}"
        html_body = f"""
        <div style="font-family:Inter,sans-serif;max-width:600px;margin:0 auto;background:#1a1a1a;color:#f0ece0;padding:32px;border-radius:12px">
          <div style="font-family:Syne,sans-serif;font-weight:800;font-size:24px;color:#f0ece0;margin-bottom:4px">SMARTEST GUIDE<span style="width:7px;height:7px;border-radius:50%;background:#FF6B00;display:inline-block;margin-left:3px"></span></div>
          <div style="font-size:12px;color:#00d4aa;letter-spacing:.15em;text-transform:uppercase;margin-bottom:24px">AI Concierge for Hotels</div>
          <h2 style="color:#FF6B00;font-size:20px;margin-bottom:12px">Profil hotelu {hotel_name} je vyplněn z {score}%</h2>
          <p style="color:#9ba0c0;line-height:1.7">Dobrý den,<br><br>váš hotel <strong style="color:#f0ece0">{hotel_name}</strong> má aktivní předplatné SmartestGuide, ale profil není kompletní. Čím více informací Alex zná, tím lépe pomáhá hostům.</p>
          {"<p style='color:#9ba0c0'>Chybějící informace:</p><ul style='color:#f0ece0;line-height:2'>" + missing_html + "</ul>" if missing_list else ""}
          <a href="{portal_url}" style="display:inline-block;margin-top:20px;background:#FF6B00;color:#0a0b0f;text-decoration:none;padding:12px 28px;border-radius:8px;font-weight:700;font-size:15px">Přejít do portálu →</a>
          <p style="margin-top:32px;font-size:12px;color:#6b6f8e">SMARTEST GUIDE · support@smartestguide.com</p>
        </div>"""

        payload = {
            "sender": {"name": "SMARTEST GUIDE", "email": "admin@smartestguide.com"},
            "to": [{"email": hotel_email, "name": hotel_name}],
            "bcc": _admin_notify_bcc(),
            "subject": subject,
            "htmlContent": html_body,
        }
        try:
            resp = _httpx.post(
                "https://api.brevo.com/v3/smtp/email",
                headers={"api-key": brevo_key, "Content-Type": "application/json"},
                json=payload, timeout=15
            )
            if resp.status_code not in (200, 201):
                logging.warning("Brevo reminder error: %s %s", resp.status_code, resp.text[:200])
        except Exception as e:
            logging.error("Brevo reminder exception: %s", e)

    now = datetime.utcnow().isoformat()
    db["hotels"][hotel_id]["last_reminder_sent"] = now
    db["hotels"][hotel_id]["reminder_count"] = db["hotels"][hotel_id].get("reminder_count", 0) + 1
    db_save(db)
    return {
        "status": "ok",
        "email_to": hotel_email,
        "completeness": completeness,
        "portal_url": portal_url,
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
        T2("100+ jazyků komunikace",             "100+ languages available"),
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
# TEST lišta — objeví se jen když je nastaveno SG_ENV=staging/test/dev.
# Na produkci (SG_ENV nenastaveno) se NEZOBRAZÍ. Chrání před záměnou staging × produkce.
SG_ENV = os.getenv("SG_ENV", "").strip().lower()

def _staging_banner(html: str) -> str:
    if SG_ENV not in ("staging", "test", "dev"):
        return html
    banner = (
        '<div style="position:fixed;top:0;left:0;right:0;height:30px;line-height:30px;'
        'background:#1e66f5;color:#fff;text-align:center;font:600 13px system-ui,-apple-system,sans-serif;'
        'z-index:2147483647;letter-spacing:.4px;box-shadow:0 1px 4px rgba(0,0,0,.25)">'
        '\U0001f9ea TESTOVACÍ PROSTŘEDÍ (STAGING) — změny se neprojeví v ostré verzi</div>'
        '<script>try{document.body.style.paddingTop='
        "((parseInt(getComputedStyle(document.body).paddingTop)||0)+30)+'px';}catch(e){}</script>"
    )
    # Vkládej k POSLEDNÍMU </body> (pravý závěr dokumentu). První výskyt může být
    # uvnitř JS template stringu (např. v index.html) — tam by banner rozbil skript.
    idx = html.rfind("</body>")
    if idx != -1:
        return html[:idx] + banner + html[idx:]
    return html + banner

# URL struktura:
#   /              → landing (marketing)
#   /admin         → náš admin (login-gated)
#   /portal?token= → hotelový portál  (/hotel = alias)
#   /h/{slug}      → guest chat        (/guest/{id} = alias)
@app.get("/", response_class=HTMLResponse)
@app.get("/landing", response_class=HTMLResponse)
def serve_landing():
    html_path = os.path.join(os.path.dirname(__file__), "landing.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return _staging_banner(f.read())

@app.get("/apaleo", response_class=HTMLResponse)
def serve_apaleo_landing():
    html_path = os.path.join(os.path.dirname(__file__), "apaleo.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return _staging_banner(f.read())

@app.get("/apaleo/guide.pdf")
def serve_apaleo_guide_pdf():
    """Onboarding guide (PDF) ke stažení — odkazovaný z /apaleo a z Apaleo Store profilu."""
    from fastapi.responses import FileResponse
    p = os.path.join(os.path.dirname(__file__), "SMARTESTGUIDE_Apaleo_Onboarding_Guide.pdf")
    if not os.path.exists(p):
        raise HTTPException(404, "Guide not found")
    return FileResponse(p, media_type="application/pdf",
                        filename="SMARTEST_GUIDE_Apaleo_Onboarding_Guide.pdf")

@app.get("/admin", response_class=HTMLResponse)
def serve_frontend():
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return _staging_banner(f.read())

@app.get("/portal", response_class=HTMLResponse)
@app.get("/hotel", response_class=HTMLResponse)
def serve_hotel_portal():
    html_path = os.path.join(os.path.dirname(__file__), "hotel.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return _staging_banner(f.read())

@app.get("/h/{slug}", response_class=HTMLResponse)
def serve_guest_slug(slug: str):
    html_path = os.path.join(os.path.dirname(__file__), "guest.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return _staging_banner(f.read())

@app.get("/guest/{hotel_id}", response_class=HTMLResponse)
def serve_guest(hotel_id: str):
    html_path = os.path.join(os.path.dirname(__file__), "guest.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return _staging_banner(f.read())

@app.get("/favicon.ico")
def favicon():
    """Brandovaná SG ikona v záložce prohlížeče — platí pro všechny stránky."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/api/app-icon/64")

@app.get("/sw.js")
def serve_sw():
    """Minimální service worker s fetch handlerem — splňuje kritérium instalace PWA."""
    from fastapi.responses import Response
    sw = (
        "self.addEventListener('install', e => self.skipWaiting());\n"
        "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));\n"
        "self.addEventListener('fetch', e => {});\n"
    )
    return Response(content=sw, media_type="application/javascript")

@app.get("/api/app-icon/{size}")
def app_icon(size: int):
    """Brandová ikona (logo) — favicon + PWA. Načítá logo.png (průhledné pozadí)."""
    from PIL import Image
    from io import BytesIO
    from fastapi.responses import Response
    size = max(16, min(int(size), 512))
    try:
        p = os.path.join(os.path.dirname(__file__), "logo.png")
        img = Image.open(p).convert("RGBA").resize((size, size), Image.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (size, size), (255, 107, 0, 255))  # fallback
    buf = BytesIO()
    img.save(buf, "PNG")
    return Response(content=buf.getvalue(), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})

@app.get("/api/hotels/{hotel_id}/manifest.webmanifest")
def hotel_manifest(hotel_id: str, request: Request):
    """Per-hotel PWA manifest — instalovaná ikona otevře přímo Alexe tohoto hotelu."""
    from fastapi.responses import JSONResponse
    db = db_load()
    _hid, hotel = _resolve_hotel(db, hotel_id)
    hotel = hotel or {}
    name = hotel.get("name") or "SmartestGuide"
    base = get_base_url(request)
    ident = hotel.get("slug") or _hid or hotel_id
    manifest = {
        "name": f"{name} — Concierge",
        "short_name": (name[:12] if name else "Concierge"),
        "description": "Váš osobní AI concierge — kdykoli po ruce",
        "start_url": f"/h/{ident}",
        "scope": f"/h/{ident}",
        "display": "standalone",
        "background_color": "#1a1a1a",
        "theme_color": "#1a1a1a",
        "orientation": "portrait",
        "icons": [
            {"src": f"{base}/api/app-icon/192", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": f"{base}/api/app-icon/512", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return JSONResponse(manifest, media_type="application/manifest+json")

# ─────────────────────────────────────────────
# Guest API
# ─────────────────────────────────────────────
@app.get("/api/guest/{hotel_id}")
def get_guest_hotel(hotel_id: str):
    """Vrátí veřejná data hotelu pro guest app. `hotel_id` může být ID i slug."""
    db = db_load()
    _hid, h = _resolve_hotel(db, hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")
    if not h.get("subscription_active"):
        raise HTTPException(403, "Hotel nemá aktivní předplatné")
    # Vrátí pouze veřejná data (bez tokenů, interních dat a PMS credentials)
    public = {k: v for k, v in h.items() if k not in (
        "hotel_token", "stripe_customer_id", "stripe_subscription_id",
        "pms_type", "pms_client_id", "pms_client_secret", "pms_property_id",
        "pms_refresh_token", "pms_oauth_state", "pms_oauth_state_at")}
    # Jen boolean — guest app podle něj nabídne „propojit pobyt" (žádné PMS detaily ven)
    public["pms_connected"] = bool(h.get("pms_type"))
    return {"status": "ok", "hotel": public}

class GuestVisitRequest(BaseModel):
    hotel_id: str
    device_id: Optional[str] = None

@app.post("/api/guest/visit")
def guest_visit(req: GuestVisitRequest, request: Request):
    """Beacon při otevření průvodce — počítá otevření a unikátní návštěvníky.
    Klient posílá max 1× denně (localStorage), server navíc rate-limituje IP.
    Vždy vrací ok (neprozrazuje existenci hotelu, chyby tiše ignoruje)."""
    if not _rate_limit_ok("visit:" + _client_ip(request), max_hits=10):
        return {"status": "ok"}
    try:
        db = db_load()
        _hid, h = _resolve_hotel(db, req.hotel_id)
        if not h:
            return {"status": "ok"}
        a = db.setdefault("analytics", {}).setdefault(_hid, {"total": 0, "topics": {}})
        now = datetime.utcnow()
        a["visits_total"] = a.get("visits_total", 0) + 1
        a["last_visit"] = now.isoformat()
        monthly = a.setdefault("monthly", {})
        m = monthly.setdefault(now.strftime("%Y-%m"), {"count": 0, "flagged": 0, "langs": {}})
        m["visits"] = m.get("visits", 0) + 1
        if req.device_id:
            dh = hashlib.sha256(req.device_id.encode()).hexdigest()[:12]
            dv = m.setdefault("visit_devices", {})
            if dh in dv or len(dv) < 5000:
                dv[dh] = dv.get(dh, 0) + 1
        db_save(db)
    except Exception as e:
        logging.warning("guest_visit chyba: %s", e)
    return {"status": "ok"}

class GuestChatRequest(BaseModel):
    hotel_id: str
    message: str
    language: Optional[str] = "en"
    guest_name: Optional[str] = None
    history: Optional[list] = None
    auto_lang: Optional[bool] = True
    device_id: Optional[str] = None  # anonymní ID zařízení (localStorage) pro počítání unikátních hostů
    room: Optional[str] = None       # číslo pokoje z QR (?room=214) — pro PMS lookup pobytu
    arrival: Optional[str] = None    # datum příjezdu zadané hostem (znalostní ověření, bez útraty)

def _norm_date(s: str) -> str:
    """Normalizuje datum na YYYY-MM-DD. Přijímá i DD.MM.YYYY / D.M.YYYY. Jinak vrací ''. """
    s = (s or "").strip()
    try:
        if "." in s:
            parts = [p.strip() for p in s.rstrip(".").split(".")]
            if len(parts) == 3:
                d, m, y = parts
                return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        from datetime import date as _d
        return _d.fromisoformat(s[:10]).isoformat()
    except Exception:
        return ""

async def _fetch_stay_for_hotel(h: dict, hid: str, room: str, settings: dict):
    """Načte pobyt z PMS vč. app-level credentials a uložení rotovaného refresh tokenu."""
    _ph = dict(h)  # kopie — do originálu nezasahujeme
    if h.get("pms_refresh_token"):  # Connect (OAuth) režim → app-level credentials
        _ph["_apaleo_app_client_id"] = settings.get("apaleo_client_id", "")
        _ph["_apaleo_app_client_secret"] = settings.get("apaleo_client_secret", "")
    stay = await pms_layer.get_stay_for_room(_ph, room)
    if _ph.get("_new_refresh_token"):
        try:
            _db2 = db_load()
            if hid in _db2["hotels"]:
                _db2["hotels"][hid]["pms_refresh_token"] = _ph["_new_refresh_token"]
                db_save(_db2)
        except Exception as e2:
            logging.warning("Uložení rotovaného refresh tokenu selhalo: %s", e2)
    return stay

class VerifyStayRequest(BaseModel):
    hotel_id: str
    room: str
    arrival: str

@app.post("/api/guest/verify-stay")
async def guest_verify_stay(req: VerifyStayRequest, request: Request):
    """Znalostní ověření pobytu: pokoj + datum příjezdu musí sedět s In-house rezervací.
    Vrací jen {verified: bool} — žádné údaje z rezervace neprozrazuje."""
    if not _rate_limit_ok("verify:" + _client_ip(request), max_hits=10):
        raise HTTPException(429, "Příliš mnoho pokusů. Zkuste to prosím za chvíli.")
    db = db_load()
    _hid, h = _resolve_hotel(db, req.hotel_id)
    if not h or not h.get("pms_type"):
        return {"status": "ok", "verified": False}
    arrival = _norm_date(req.arrival)
    if not arrival or not (req.room or "").strip():
        return {"status": "ok", "verified": False}
    try:
        settings = db_get_settings()
        stay = await _fetch_stay_for_hotel(h, _hid, req.room.strip(), settings)
        verified = bool(stay and stay.arrival == arrival)
    except Exception as e:
        logging.warning("Verify-stay selhal: %s", e)
        verified = False
    return {"status": "ok", "verified": verified}

@app.post("/api/guest/chat")
async def guest_chat(req: GuestChatRequest, request: Request):
    """AI chat pro hosta – používá Anthropic API."""
    # Rate limit na IP — brání spamu chatu a nekontrolovaným nákladům na API
    if not _rate_limit_ok("chat:" + _client_ip(request)):
        raise HTTPException(429, "Příliš mnoho dotazů. Zkuste to prosím za chvíli.")
    settings = db_get_settings()
    api_key = settings.get("anthropic_api_key", "")
    if not api_key:
        raise HTTPException(400, "AI není nakonfigurováno")

    db = db_load()
    _hid, h = _resolve_hotel(db, req.hotel_id)
    if not h:
        raise HTTPException(404, "Hotel nenalezen")

    # Mapa kódu jazyka na plný název
    LANG_NAMES = {
        "cs": "Czech", "sk": "Slovak", "en": "English", "de": "German",
        "fr": "French", "it": "Italian", "es": "Spanish", "pl": "Polish",
        "hu": "Hungarian", "ru": "Russian", "uk": "Ukrainian", "zh": "Chinese",
        "ja": "Japanese", "ko": "Korean", "nl": "Dutch", "pt": "Portuguese",
        "ar": "Arabic", "tr": "Turkish",
    }
    lang_name = LANG_NAMES.get(req.language, req.language)

    # Sestav systémový prompt z dat hotelu
    # Restaurace (opakovatelné) — sestav přehled pro Alexe
    _restos = h.get("restaurants") or []
    _rlines = []
    for _r in _restos:
        if not (_r.get("name") or _r.get("menus")):
            continue
        _head = "- " + (_r.get("name") or "Restaurace")
        if _r.get("type"): _head += f" ({_r['type']})"
        if _r.get("hours"): _head += f", otevřeno {_r['hours']}"
        _rlines.append(_head)
        if _r.get("dietary"): _rlines.append(f"    Dietní možnosti: {_r['dietary']}")
        if _r.get("directions"): _rlines.append(f"    Jak se tam dostat (krok za krokem): {_r['directions']}")
        for _m in (_r.get("menus") or []):
            if _m.get("url"):
                _rlines.append(f"    {_m.get('label') or 'Jídelníček'}: {_m['url']}")
    restaurants_block = ("RESTAURACE HOTELU (u konkrétní restaurace rovnou poraď, jak se k ní dostat; odkazy na jídelníčky sdílej jako plain URL, když se host ptá na jídlo/pití/menu):\n" + "\n".join(_rlines)) if _rlines else ""

    hotel_info = f"""You are Alex, a friendly AI concierge for {h.get('name', 'this hotel')}.

LANGUAGE RULE: Detect the language of the guest's message and always respond in that same language.
If you cannot detect the language, use {lang_name} ({req.language}) as default.
Never mix languages in a single response.
TRANSLATE HOTEL DATA: The hotel profile below may be written in a different language (often Czech).
Always TRANSLATE the information into the guest's language — never quote raw profile text in another
language. Example: profile says "Garáž v hotelu, 600 Kč/noc" and the guest writes English → answer
"Hotel garage, 600 CZK per night." Keep proper names (hotel name, restaurant names, street names,
place names) in their original form.

FORMATTING: Plain conversational text only. You may use **bold** for key facts and simple "-" bullet
lists. NEVER use Markdown headings (#, ##), tables, or code blocks — the chat does not render them.

BRAND NAMES (IMPORTANT): "SMARTEST GUIDE" and "Alex" are a brand and product name. NEVER translate or localise them into any language — always keep them exactly as "SMARTEST GUIDE" and "Alex", regardless of the language you are speaking. Do not write "Nejchytřejší průvodce", "Le guide le plus intelligent", or any translated form.

INPUT TOLERANCE (IMPORTANT): Guests often use voice dictation or type quickly, so words may be misspelled or phonetically garbled — possibly transcribed in the wrong language. If a word looks like a garbled, misheard or misspelled version of a common hotel topic, infer the most likely intended meaning and answer helpfully instead of saying you don't understand. For example: "Spicycarte"/"Spajzekarte" → German "Speisekarte" (menu / jídelní lístek); "checkout"/"chekaut" → check-out; "wai-fai"/"vайфай" → WiFi; "brekfast"/"frpštyk" → breakfast. Only ask the guest to rephrase if you genuinely cannot guess the intent. Never reply that you don't know a word like "Spicycarte" — recognise it as a misheard "Speisekarte" and give the menu info.

ACCURACY RULE (CRITICAL — never break this): Answer ONLY from the hotel information provided below. If a detail is missing, empty, marked "N/A" or "neuvedeno", do NOT guess or invent it. Instead say you don't have that specific information and offer to connect the guest with reception (use the reception phone or WhatsApp number if listed). Never make up prices, opening hours, room numbers, allergen or dietary details, policies, or availability. For safety-critical topics (allergens, medical, payments, prices) always recommend confirming directly with hotel staff. It is always better to admit you don't know than to state something that might be wrong.

FORMATTING RULES:
- When sharing a URL or link, always write the full URL as plain text starting with https:// on its own line. Never use markdown like [text](url). Example: https://www.hotel.cz/menu
- Use **bold** for important info like times, prices, names.
- Keep answers concise and friendly.
- When mentioning nearby places, attractions or restaurants, always use the EXACT name as listed in the hotel data (e.g. "Restaurace U Zlaté hvězdy" not just "a nearby restaurant"). This allows the guest to tap a map link.

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
- WiFi: {h.get('wifi_name', 'N/A')}{(' / Heslo: ' + h.get('wifi_password','')) if h.get('wifi_password') else ''}
- Wellness: {h.get('wellness_info', 'N/A')}
- Počet lůžek / kapacita: {h.get('bed_count', 'N/A')}
- Hvězdičky: {h.get('star_rating', 'N/A')}
- Domácí mazlíčci (pet policy): {h.get('pet_policy', 'N/A')}
- Bazén: {h.get('pool_info', 'N/A')}
- Fitness: {h.get('fitness_info', 'N/A')}
- Minibar: {h.get('minibar', 'N/A')}
- Bar: {h.get('bar', 'N/A')}
- WhatsApp Recepce: {h.get('whatsapp_number', 'N/A')}

ORIENTACE V HOTELU (krok za krokem):
- Bazén: {h.get('nav_pool', 'neuvedeno')}
- Wellness/Spa: {h.get('nav_wellness', 'neuvedeno')}
- Fitness: {h.get('nav_fitness', 'neuvedeno')}
- Restaurace: {h.get('nav_restaurant', 'neuvedeno')}
- Bar: {h.get('nav_bar', 'neuvedeno')}
- Parkování/Garáž: {h.get('nav_parking', 'neuvedeno')}
- Příjezd k hotelu: {h.get('nav_arrival', 'neuvedeno')}
- Výtah: {h.get('nav_elevator', 'neuvedeno')}
- Konferenční sály: {h.get('nav_conference', 'neuvedeno')}
- Další: {h.get('nav_other', 'neuvedeno')}
{chr(10).join([f"- {item.get('name','')}: {item.get('desc','')}" for item in (h.get('nav_custom') or []) if item.get('name')])}

Pokud host žádá cestu nebo navigaci, použij PŘESNÝ popis výše krok za krokem.
- WhatsApp Wellness: {h.get('whatsapp_wellness', 'N/A')}
- WhatsApp Restaurace: {h.get('whatsapp_restaurant', 'N/A')}
- WhatsApp Sport: {h.get('whatsapp_sport', 'N/A')}
- Amenities: {', '.join(h.get('amenities', []))}
- Turistická místa v okolí: {', '.join(h.get('nearby_places', []))}
- Skrytá místa (kam chodí místní, ne turisté): {', '.join(h.get('hidden_gems', []))}
INSTRUKCE K MÍSTŮM: Pokud host žádá tipy na okolí, doporučuj turistická místa přirozeně. Pokud se ptá na "místní tipy", "kam chodí místní", "co turisté neznají" nebo chce autentický zážitek — zdůrazni skrytá místa a uveď je jako insider tip: "tohle turisté obvykle neznají, ale místní to milují".
- Description: {h.get('description', 'N/A')}
- Extra info: {h.get('extra_info', 'N/A')}
{restaurants_block}
{chr(10).join([f"- {cf.get('label','Info')}: {cf.get('value','')}" for cf in h.get('custom_fields', []) if cf.get('value')]) if h.get('custom_fields') else ''}
{"\n⭐ AKTIVNÍ NABÍDKA HOTELU: " + h.get('active_offer') + "\nTuto nabídku VŽDY zmíň pokud se host ptá na téma které s ní souvisí (wellness, restaurace, bar, aktivity, služby apod.). Zmiň ji přirozeně jako součást odpovědi — ne jako reklamu, ale jako přátelský tip." if h.get('active_offer') else ''}

Guest name: {req.guest_name or 'Guest'}"""

    # PMS: když host přišel z QR pokoje a hotel má PMS nakonfigurované, přidej aktuální pobyt
    # jako SAMOSTATNÝ system blok (za kešovaným profilem hotelu — cache zůstane stabilní).
    # Jakákoli chyba => beze změny (graceful degradace, chat nikdy nesmí spadnout kvůli PMS).
    stay_block = None
    if req.room and h.get("pms_type"):
        try:
            stay = await _fetch_stay_for_hotel(h, _hid, req.room, settings)
            if req.arrival:
                # Znalostní režim (pokoj + datum příjezdu zadané v chatu): ověř shodu,
                # personalizace BEZ útraty/zůstatku. Neshoda => žádná data z rezervace.
                claimed = _norm_date(req.arrival)
                if stay and claimed and stay.arrival == claimed:
                    stay_block = pms_layer.format_stay_block(stay, include_balance=False)
                else:
                    stay_block = ("OVĚŘENÍ POBYTU SELHALO: Host zadal číslo pokoje a datum příjezdu, "
                                  "ale neodpovídají žádné aktuální rezervaci. Pokud se ptá na svůj pobyt, "
                                  "vysvětli, že se pobyt nepodařilo ověřit — ať zkontroluje číslo pokoje "
                                  "a datum příjezdu, nebo se obrátí na recepci. "
                                  "NIKDY nesděluj žádné údaje z rezervací.")
            elif stay:
                # QR režim (pokoj z kartičky na pokoji) — plná personalizace vč. účtu
                stay_block = pms_layer.format_stay_block(stay)
        except Exception as e:
            logging.warning("PMS injekce do promptu selhala: %s", e)
    elif h.get("pms_type") and not req.room:
        # Hotel má PMS, ale host bez propojeného pobytu — dej Alexovi vědět, jak hosta navést
        stay_block = ("PMS PROPOJENÍ: Hotel je připojen k hotelovému systému, ale tento host nemá "
                      "propojený pobyt. Pokud se ptá na SVŮJ pobyt (jeho check-out, rezervaci, účet), "
                      "odpověz z obecných údajů hotelu a přátelsky dodej, že odpovědi přímo ze své "
                      "rezervace získá propojením pobytu — tlačítkem pod chatem, kde zadá číslo pokoje "
                      "a datum příjezdu.")

    messages = []
    # Omez historii na posledních 10 zpráv (~5 výměn) — starší kontext jen zbytečně žere vstupní tokeny
    for m in (req.history or [])[-10:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": req.message})

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={
                "model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                # Prompt caching: statický profil hotelu (system) se kešuje → další zprávy v téže
                # konverzaci čtou tenhle vstup levněji. Kešování naskočí, když prefix dosáhne minima
                # (~1–2k tokenů); u bohatšího profilu ušetří nejvíc. Bez benefitu to neškodí.
                "system": ([{"type": "text", "text": hotel_info, "cache_control": {"type": "ephemeral"}}]
                           + ([{"type": "text", "text": stay_block}] if stay_block else [])),
                "messages": messages,
            },
            timeout=30.0,
        )
    if r.status_code != 200:
        raise HTTPException(500, f"AI chyba: {r.text[:200]}")
    reply = r.json()["content"][0]["text"]
    # Zaloguj reálný dotaz hosta (analytika + detekce mezer v informacích)
    try:
        _log_guest_question(_hid, req.message, req.language, reply, device_id=req.device_id or "")
    except Exception as e:
        logging.warning("Log dotazu hosta selhal: %s", e)
    return {"status": "ok", "reply": reply}

# ─────────────────────────────────────────────
# Apaleo Connect (OAuth) — jednoklikové připojení hotelu (Apaleo Store ready)
# Flow: portál → /connect?token=… → Apaleo authorize → /callback → uložení refresh tokenu
# ─────────────────────────────────────────────
_APALEO_AUTHORIZE_URL = "https://identity.apaleo.com/connect/authorize"
_APALEO_SCOPES = "offline_access reservations.read"

@app.get("/api/pms/apaleo/connect")
def apaleo_connect(token: str, request: Request):
    """Start OAuth: přesměruje hotel (přihlášený portál tokenem) na Apaleo souhlas."""
    from fastapi.responses import RedirectResponse
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatný přístupový token")
    s = db_get_settings()
    client_id = (s.get("apaleo_client_id") or "").strip()
    if not client_id:
        raise HTTPException(400, "Apaleo Connect není nakonfigurováno (APALEO_CLIENT_ID)")
    # CSRF ochrana: náhodný state uložený u hotelu (platnost 15 min)
    state = uuid.uuid4().hex
    db = db_load()
    hid = h.get("id")
    db["hotels"][hid]["pms_oauth_state"] = state
    db["hotels"][hid]["pms_oauth_state_at"] = datetime.utcnow().isoformat()
    db_save(db)
    redirect_uri = f"{get_base_url(request)}/api/pms/apaleo/callback"
    from urllib.parse import urlencode
    q = urlencode({"response_type": "code", "client_id": client_id,
                   "redirect_uri": redirect_uri, "scope": _APALEO_SCOPES, "state": state,
                   "prompt": "consent"})  # vynutí consent obrazovku i při opakovaném připojení
    return RedirectResponse(f"{_APALEO_AUTHORIZE_URL}?{q}")

@app.get("/api/pms/apaleo/callback")
async def apaleo_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Návrat z Apaleo: výměna kódu za tokeny, uložení k hotelu dle state."""
    def _page(title, body, ok=True, portal_url=""):
        color = "#2ecc87" if ok else "#ff4f6a"
        back = (f'<a href="{portal_url}" style="display:inline-block;margin-top:22px;background:#FF6B00;color:#0a0b0f;'
                f'text-decoration:none;padding:11px 24px;border-radius:9px;font-weight:700;font-size:14px">← Back to your portal</a>'
                if portal_url else
                '<p style="margin-top:22px;font-size:13px;color:#6b6f8e">You can close this window and return to your SMARTEST GUIDE portal.</p>')
        redirect = (f'<p style="margin-top:10px;font-size:12px;color:#6b6f8e">Returning automatically in <span id="cd">6</span> s…</p>'
                    f'<script>var c=6,e=document.getElementById("cd");setInterval(function(){{c--;if(e)e.textContent=c;'
                    f'if(c<=0)location.href="{portal_url}";}},1000);</script>' if (portal_url and ok) else "")
        return HTMLResponse(f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title></head>
<body style="font-family:sans-serif;background:#15161a;color:#e6e4df;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0">
<div style="max-width:460px;padding:36px;text-align:center;background:#1e1f25;border-radius:16px">
<div style="font-weight:800;font-size:15px;letter-spacing:.02em;margin-bottom:18px">SMARTEST GUIDE<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:#FF6B00;margin-left:3px"></span></div>
<div style="font-size:40px;margin-bottom:12px">{"✅" if ok else "⚠️"}</div>
<h2 style="color:{color};margin:0 0 10px">{title}</h2>
<p style="line-height:1.6;color:#a9adc1">{body}</p>
{back}
{redirect}
</div></body></html>""")
    if error:
        return _page("Connection failed", f"Apaleo returned an error: {error}. Please start the connection again from your portal.", ok=False)
    if not (code and state):
        return _page("Invalid request", "Missing authorization code or state. Please start the connection again from your portal.", ok=False)
    # Najdi hotel podle state (a zkontroluj stáří)
    db = db_load()
    hid, h = None, None
    for _id, _h in db["hotels"].items():
        if _h.get("pms_oauth_state") == state:
            hid, h = _id, _h
            break
    if not h:
        return _page("Invalid state", "Security check failed. Please start the connection again from your portal.", ok=False)
    portal_url = f"{get_base_url(request)}/portal?token={h.get('hotel_token','')}" if h.get("hotel_token") else ""
    try:
        started = datetime.fromisoformat(h.get("pms_oauth_state_at", "2000-01-01"))
        if (datetime.utcnow() - started).total_seconds() > 900:
            return _page("Session expired", "The connection took too long. Please start again from your portal.", ok=False, portal_url=portal_url)
    except Exception:
        pass
    s = db_get_settings()
    client_id = (s.get("apaleo_client_id") or "").strip()
    client_secret = (s.get("apaleo_client_secret") or "").strip()
    redirect_uri = f"{get_base_url(request)}/api/pms/apaleo/callback"
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post("https://identity.apaleo.com/connect/token",
            headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri})
    if r.status_code != 200:
        logging.warning("Apaleo callback token exchange selhal: %s %s", r.status_code, r.text[:200])
        return _page("Token exchange failed", "Apaleo did not accept the authorization code. Please try again, or contact support@smartestguide.com.", ok=False, portal_url=portal_url)
    tok = r.json()
    refresh_token = tok.get("refresh_token", "")
    if not refresh_token:
        return _page("Missing refresh token", "Apaleo did not grant offline access. Please contact support@smartestguide.com.", ok=False, portal_url=portal_url)
    db["hotels"][hid]["pms_type"] = "apaleo"
    db["hotels"][hid]["pms_refresh_token"] = refresh_token
    db["hotels"][hid].pop("pms_oauth_state", None)
    db["hotels"][hid].pop("pms_oauth_state_at", None)
    db_save(db)
    prop_note = ""
    if not h.get("pms_property_id"):
        prop_note = " One last step: enter your property code (e.g. BER) in the PMS section of your portal."
    logging.info("Apaleo Connect: hotel %s připojen.", hid)
    return _page("Apaleo connected", f"{h.get('name','Your hotel')} is now connected to SMARTEST GUIDE. Alex, the AI concierge, can answer your guests using their reservation details (check-out time, package, balance).{prop_note}", portal_url=portal_url)

@app.post("/api/pms/apaleo/disconnect")
def apaleo_disconnect(token: str):
    """Offboarding: hotel odpojí Apaleo z portálu — smažeme refresh token i vazbu.
    Žádná data hostů neuchováváme, takže odpojením končí veškerý přístup."""
    h = find_hotel_by_token(token)
    if not h:
        raise HTTPException(403, "Neplatný přístupový token")
    db = db_load()
    hid = h.get("id")
    for k in ("pms_refresh_token", "pms_type", "pms_oauth_state", "pms_oauth_state_at",
              "pms_client_id", "pms_client_secret"):
        db["hotels"][hid].pop(k, None)
    db_save(db)
    logging.info("Apaleo odpojeno: hotel %s", hid)
    return {"status": "ok", "disconnected": True}

class TranslateMenuRequest(BaseModel):
    hotel_id: str
    image_base64: str
    language: Optional[str] = "en"
    guest_name: Optional[str] = None

@app.post("/api/guest/translate-menu")
async def translate_menu(req: TranslateMenuRequest, request: Request):
    """Přeloží foto menu pomocí Claude Vision."""
    if not _rate_limit_ok("menu:" + _client_ip(request), max_hits=8):
        raise HTTPException(429, "Příliš mnoho požadavků. Zkuste to prosím za chvíli.")
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
    return {"status": "ok", "token": token, "portal_url": f"{base}/portal?token={token}"}

# ─────────────────────────────────────────────
# Privacy Policy & Terms of Service stránky
# ─────────────────────────────────────────────
@app.get("/privacy")
def privacy_policy(lang: str = "en"):
    if lang == "cs":
        return HTMLResponse(content=PRIVACY_CS)
    return HTMLResponse(content=PRIVACY_EN)

@app.get("/terms")
def terms_of_service(lang: str = "en"):
    if lang == "cs":
        return HTMLResponse(content=TERMS_CS)
    return HTMLResponse(content=TERMS_EN)

LEGAL_CSS = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#0f0f1a;color:#e0e0f0;line-height:1.8;padding:0}
  .topbar{background:#1a1a2e;border-bottom:1px solid rgba(255,255,255,.08);padding:16px 32px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10}
  .logo{font-weight:800;font-size:18px;color:#fff}.logo span{color:#00d4aa}
  .lang-switch{display:flex;gap:8px}
  .lang-switch a{color:#7a7fa8;font-size:13px;text-decoration:none;padding:4px 10px;border-radius:6px;border:1px solid rgba(255,255,255,.1)}
  .lang-switch a:hover,.lang-switch a.active{background:rgba(108,99,255,.2);color:#fff;border-color:rgba(108,99,255,.4)}
  .container{max-width:800px;margin:0 auto;padding:48px 32px}
  h1{font-size:28px;font-weight:800;color:#fff;margin-bottom:8px}
  .subtitle{color:#7a7fa8;font-size:14px;margin-bottom:40px}
  h2{font-size:16px;font-weight:700;color:#6c63ff;margin:32px 0 12px;text-transform:uppercase;letter-spacing:.5px}
  h3{font-size:14px;font-weight:700;color:#00d4aa;margin:20px 0 8px}
  p{color:#b0b4cc;font-size:14px;margin-bottom:12px}
  ul{color:#b0b4cc;font-size:14px;margin:8px 0 12px 20px}
  li{margin-bottom:6px}
  strong{color:#e0e0f0}
  .back{display:inline-flex;align-items:center;gap:6px;color:#7a7fa8;font-size:13px;text-decoration:none;margin-bottom:24px}
  .back:hover{color:#fff}
  footer{text-align:center;padding:32px;color:#7a7fa8;font-size:12px;border-top:1px solid rgba(255,255,255,.06);margin-top:48px}
</style>
"""

PRIVACY_CS = LEGAL_CSS + """
<div class="topbar">
  <div class="logo">Smartest<span>Guide</span></div>
  <div class="lang-switch">
    <a href="/privacy?lang=cs" class="active">🇨🇿 CZ</a>
    <a href="/privacy?lang=en">🇬🇧 EN</a>
  </div>
</div>
<div class="container">
  <a href="/landing" class="back">← Zpět na hlavní stránku</a>
  <h1>Zásady ochrany osobních údajů</h1>
  <div class="subtitle">Poskytovatele aplikace SmartestGuide · Účinnost od 1. 6. 2025</div>

  <h2>I. Úvodní ustanovení</h2>
  <h3>Správce osobních údajů</h3>
  <p><strong>Native Hotel Guide s.r.o.</strong><br>Korunní 2569/108, Vinohrady, 101 00 Praha 10<br>IČO: 231 12 905 · DIČ: CZ23112905</p>
  <p>Tyto Zásady popisují, jakým způsobem shromažďujeme, používáme, uchováváme a chráníme Vaše osobní údaje při užívání aplikace SmartestGuide, v souladu s GDPR (EU) 2016/679.</p>

  <h2>II. Jaké osobní údaje shromažďujeme</h2>
  <h3>Od hostů (koncových uživatelů)</h3>
  <ul>
    <li><strong>Křestní jméno</strong> — personalizace komunikace s avatarem</li>
    <li><strong>Pohlaví, věk</strong> — statistické účely a personalizace nabídek</li>
    <li><strong>E-mail</strong> — komunikace a marketingové účely (se souhlasem)</li>
    <li><strong>Telefonní číslo</strong> (nepovinné) — přímá komunikace nebo WhatsApp funkce</li>
    <li><strong>Datum ubytování</strong> — relevantní poskytování informací</li>
    <li><strong>Obsah komunikace s avatarem</strong> — zlepšování AI, zpravidla anonymizováno</li>
    <li><strong>Informace o zařízení, IP adresa</strong> — technická optimalizace a zabezpečení</li>
  </ul>
  <h3>Od hotelů (klientů)</h3>
  <ul>
    <li>Identifikační a kontaktní údaje, platební údaje, přístupové údaje k administraci</li>
  </ul>

  <h2>III. Právní základ zpracování</h2>
  <ul>
    <li><strong>Souhlas</strong> — pro většinu údajů od hostů (křestní jméno, e-mail, marketing)</li>
    <li><strong>Plnění smlouvy</strong> — pro fungování aplikace a smluvní vztah s hotelem</li>
    <li><strong>Oprávněný zájem</strong> — zlepšování aplikace, zabezpečení, statistiky</li>
    <li><strong>Právní povinnost</strong> — účetnictví, daňové povinnosti</li>
  </ul>

  <h2>IV. Sdílení osobních údajů</h2>
  <p>Vaše údaje sdílíme pouze s příslušným hotelem, smluvními zpracovateli (cloudové služby, platební brány, analytické nástroje) a orgány veřejné moci v případě zákonné povinnosti.</p>

  <h2>V. Přenos do třetích zemí</h2>
  <p>Údaje jsou primárně zpracovávány v EU/EHP. Případný přenos mimo EU/EHP probíhá v souladu s GDPR (standardní smluvní doložky, adekvátní rozhodnutí Komise).</p>

  <h2>VI. Doba uchovávání</h2>
  <ul>
    <li>Údaje o užívání aplikace: po dobu aktivity + 3 roky, poté anonymizace</li>
    <li>Marketingové kontakty: do odvolání souhlasu</li>
    <li>Fakturační údaje: 10 let dle daňových předpisů ČR</li>
  </ul>

  <h2>VII. Vaše práva</h2>
  <ul>
    <li>Právo na přístup, opravu, výmaz ("být zapomenut")</li>
    <li>Právo na omezení zpracování a přenositelnost dat</li>
    <li>Právo vznést námitku a odvolat souhlas kdykoli</li>
    <li>Právo podat stížnost u ÚOOÚ (Pplk. Sochora 27, Praha 7, posta@uoou.cz)</li>
  </ul>
  <p>Pro uplatnění práv nás kontaktujte e-mailem. Vaši žádost vyřídíme do 1 měsíce.</p>

  <h2>VIII. Zabezpečení</h2>
  <p>Používáme šifrování dat, řízení přístupu, pravidelné zálohy, firewally a bezpečnostní audity k ochraně Vašich osobních údajů.</p>

  <h2>IX. Změny Zásad</h2>
  <p>O podstatných změnách Vás budeme informovat prostřednictvím aplikace nebo webu. Doporučujeme tuto stránku pravidelně kontrolovat.</p>

  <footer>© 2025 Native Hotel Guide s.r.o. · SmartestGuide · Účinnost od 1. 6. 2025</footer>
</div>
"""

PRIVACY_EN = LEGAL_CSS + """
<div class="topbar">
  <div class="logo">Smartest<span>Guide</span></div>
  <div class="lang-switch">
    <a href="/privacy?lang=cs">🇨🇿 CZ</a>
    <a href="/privacy?lang=en" class="active">🇬🇧 EN</a>
  </div>
</div>
<div class="container">
  <a href="/landing" class="back">← Back to homepage</a>
  <h1>Privacy Policy</h1>
  <div class="subtitle">SmartestGuide application provider · Effective from 1 June 2025</div>

  <h2>I. Introduction</h2>
  <h3>Data Controller</h3>
  <p><strong>Native Hotel Guide s.r.o.</strong><br>Korunní 2569/108, Vinohrady, 101 00 Prague 10, Czech Republic<br>Company ID: 231 12 905 · VAT: CZ23112905</p>
  <p>This Privacy Policy describes how we collect, use, store and protect your personal data when you use the SmartestGuide application, in accordance with GDPR (EU) 2016/679.</p>

  <h2>II. What personal data we collect</h2>
  <h3>From guests (end users)</h3>
  <ul>
    <li><strong>First name</strong> — personalisation of avatar communication</li>
    <li><strong>Gender, age</strong> — statistical purposes and offer personalisation</li>
    <li><strong>Email address</strong> — communication and marketing (with consent)</li>
    <li><strong>Phone number</strong> (optional) — direct communication or WhatsApp features</li>
    <li><strong>Stay dates</strong> — providing relevant information</li>
    <li><strong>Avatar conversation content</strong> — AI improvement, typically anonymised</li>
    <li><strong>Device information, IP address</strong> — technical optimisation and security</li>
  </ul>
  <h3>From hotels (clients)</h3>
  <ul>
    <li>Identification and contact details, payment data, administration access credentials</li>
  </ul>

  <h2>III. Legal basis for processing</h2>
  <ul>
    <li><strong>Consent</strong> — for most guest data (first name, email, marketing)</li>
    <li><strong>Contract performance</strong> — for app functionality and hotel contract</li>
    <li><strong>Legitimate interest</strong> — app improvement, security, statistics</li>
    <li><strong>Legal obligation</strong> — accounting, tax compliance</li>
  </ul>

  <h2>IV. Sharing of personal data</h2>
  <p>Your data is shared only with the relevant hotel, contracted processors (cloud services, payment gateways, analytics tools) and public authorities when required by law.</p>

  <h2>V. International transfers</h2>
  <p>Data is primarily processed within the EU/EEA. Any transfer outside the EU/EEA is carried out in compliance with GDPR (standard contractual clauses, Commission adequacy decisions).</p>

  <h2>VI. Retention periods</h2>
  <ul>
    <li>App usage data: duration of use + 3 years, then anonymised</li>
    <li>Marketing contacts: until consent is withdrawn</li>
    <li>Billing data: 10 years under Czech tax regulations</li>
  </ul>

  <h2>VII. Your rights</h2>
  <ul>
    <li>Right of access, rectification, erasure ("right to be forgotten")</li>
    <li>Right to restriction of processing and data portability</li>
    <li>Right to object and to withdraw consent at any time</li>
    <li>Right to lodge a complaint with the Czech Data Protection Authority (ÚOOÚ)</li>
  </ul>
  <p>To exercise your rights, please contact us by email. We will respond within 1 month.</p>

  <h2>VIII. Security</h2>
  <p>We apply data encryption, access controls, regular backups, firewalls and security audits to protect your personal data.</p>

  <h2>IX. Changes to this Policy</h2>
  <p>We will notify you of material changes via the app or website. We recommend checking this page regularly.</p>

  <footer>© 2025 Native Hotel Guide s.r.o. · SmartestGuide · Effective from 1 June 2025</footer>
</div>
"""

TERMS_CS = LEGAL_CSS + """
<div class="topbar">
  <div class="logo">Smartest<span>Guide</span></div>
  <div class="lang-switch">
    <a href="/terms?lang=cs" class="active">🇨🇿 CZ</a>
    <a href="/terms?lang=en">🇬🇧 EN</a>
  </div>
</div>
<div class="container">
  <a href="/landing" class="back">← Zpět na hlavní stránku</a>
  <h1>Obchodní podmínky</h1>
  <div class="subtitle">SmartestGuide pro ubytovací zařízení · Účinnost od 1. 6. 2025</div>

  <h2>I. Úvodní ustanovení</h2>
  <p><strong>Poskytovatel:</strong> Native Hotel Guide s.r.o., Korunní 2569/108, Praha 10, IČO: 231 12 905</p>
  <p>Tyto podmínky upravují práva a povinnosti Poskytovatele a ubytovacího zařízení (Klient) při pronájmu aplikace SmartestGuide — AI concierge pro hosty hotelů.</p>

  <h2>II. Cena a platební podmínky</h2>
  <ul>
    <li>Měsíční poplatek: <strong>200 EUR</strong> pro hotely do 100 lůžek; nad 100 lůžek +3 EUR/lůžko/měsíc</li>
    <li>Platby probíhají automaticky kartou nebo převodem prostřednictvím platební brány</li>
    <li>Prvních <strong>14 dní zdarma</strong> — zkušební doba bez poplatku</li>
    <li>Zaváděcí cena je zachována po celou dobu nepřetržitého předplatného</li>
    <li>Žádné aktivační ani licenční poplatky</li>
  </ul>

  <h2>III. Práva a povinnosti Klienta</h2>
  <ul>
    <li>Klient je výhradně odpovědný za správnost a zákonnost obsahu vloženého do aplikace</li>
    <li>Obsah nesmí být nepravdivý, diskriminační, pornografický, násilný ani nezákonný</li>
    <li>Aplikaci nelze sublicencovat, dále distribuovat ani používat pro jiné objekty</li>
    <li>Klient zajistí ochranu přístupových údajů k administraci</li>
  </ul>

  <h2>IV. Práva a povinnosti Poskytovatele</h2>
  <ul>
    <li>Poskytovatel zajistí provoz aplikace na vlastním cloudu</li>
    <li>Poskytovatel může průběžně přidávat nové funkce (nové funkce mohou být zpoplatněny)</li>
    <li>Poskytovatel sbírá anonymizovaná data o používání pro zlepšování služby</li>
  </ul>

  <h2>V. Omezení odpovědnosti</h2>
  <p>Poskytovatel nenese odpovědnost za správnost obsahu vloženého Klientem, technické výpadky způsobené třetími stranami ani za ušlý zisk. Celková odpovědnost Poskytovatele je omezena na 3 měsíční poplatky zaplacené Klientem.</p>

  <h2>VI. Ukončení smlouvy</h2>
  <ul>
    <li><strong>Klient:</strong> výpovědní doba 1 měsíc (od prvního dne následujícího měsíce)</li>
    <li><strong>Poskytovatel:</strong> může okamžitě ukončit při závažném porušení podmínek nebo prodlení s platbou delším než 14 dní</li>
  </ul>

  <h2>VII. Podmínky pro hosty</h2>
  <p>Hosté užívají aplikaci zdarma pro osobní nekomerční účely. Veškerý obsah (informace o hotelu, doporučení) je spravován hotelem — Poskytovatel za správnost obsahu neručí. Pro kritické informace (alergeny, ceny) doporučujeme ověření přímo u personálu.</p>

  <h2>VIII. Závěrečná ustanovení</h2>
  <ul>
    <li>Podmínky se řídí právem České republiky</li>
    <li>Spory řešíme přednostně smírnou cestou; jinak příslušné soudy ČR</li>
    <li>O změnách podmínek informujeme e-mailem s předstihem 30 dní</li>
  </ul>

  <footer>© 2025 Native Hotel Guide s.r.o. · SmartestGuide · Účinnost od 1. 6. 2025</footer>
</div>
"""

TERMS_EN = LEGAL_CSS + """
<div class="topbar">
  <div class="logo">Smartest<span>Guide</span></div>
  <div class="lang-switch">
    <a href="/terms?lang=cs">🇨🇿 CZ</a>
    <a href="/terms?lang=en" class="active">🇬🇧 EN</a>
  </div>
</div>
<div class="container">
  <a href="/landing" class="back">← Back to homepage</a>
  <h1>Terms of Service</h1>
  <div class="subtitle">SmartestGuide for accommodation providers · Effective from 1 June 2025</div>

  <h2>I. Introduction</h2>
  <p><strong>Provider:</strong> Native Hotel Guide s.r.o., Korunní 2569/108, Prague 10, Czech Republic, Company ID: 231 12 905</p>
  <p>These Terms govern the rights and obligations of the Provider and the accommodation facility (Client) in connection with the rental and use of the SmartestGuide application — an AI concierge for hotel guests.</p>

  <h2>II. Pricing and payment</h2>
  <ul>
    <li>Monthly fee: <strong>€200</strong> for hotels up to 100 beds; above 100 beds +€3/bed/month</li>
    <li>Payments are processed automatically by card or bank transfer via the payment gateway</li>
    <li>First <strong>14 days free</strong> — trial period with no charge</li>
    <li>Introductory pricing is maintained for the duration of continuous subscription</li>
    <li>No activation or end-user licence fees</li>
  </ul>

  <h2>III. Client rights and obligations</h2>
  <ul>
    <li>The Client is solely responsible for the accuracy and legality of content uploaded to the app</li>
    <li>Content must not be false, discriminatory, pornographic, violent or unlawful</li>
    <li>The app may not be sublicensed, redistributed or used for other properties</li>
    <li>The Client must protect administration access credentials</li>
  </ul>

  <h2>IV. Provider rights and obligations</h2>
  <ul>
    <li>The Provider operates the application on its own cloud infrastructure</li>
    <li>The Provider may add new features over time (new features may be subject to additional charges)</li>
    <li>The Provider collects anonymised usage data to improve the service</li>
  </ul>

  <h2>V. Limitation of liability</h2>
  <p>The Provider is not liable for the accuracy of Client-uploaded content, technical outages caused by third parties, or loss of profit. Total Provider liability is limited to 3 monthly fees paid by the Client.</p>

  <h2>VI. Termination</h2>
  <ul>
    <li><strong>Client:</strong> 1-month notice period (from the first day of the following month)</li>
    <li><strong>Provider:</strong> may terminate immediately upon material breach or payment default exceeding 14 days</li>
  </ul>

  <h2>VII. Guest terms</h2>
  <p>Guests use the app free of charge for personal, non-commercial purposes. All content (hotel information, recommendations) is managed by the hotel — the Provider does not warrant the accuracy of this content. For critical information (allergens, prices), we recommend verifying directly with hotel staff.</p>

  <h2>VIII. General provisions</h2>
  <ul>
    <li>These Terms are governed by the laws of the Czech Republic</li>
    <li>Disputes are resolved preferably amicably; otherwise by the competent courts of the Czech Republic</li>
    <li>Changes to these Terms are communicated by email with 30 days' notice</li>
  </ul>

  <footer>© 2025 Native Hotel Guide s.r.o. · SmartestGuide · Effective from 1 June 2025</footer>
</div>
"""

# ─────────────────────────────────────────────
# Firemní údaje (dodavatel na faktuře)
# ─────────────────────────────────────────────
class CompanySettingsRequest(BaseModel):
    company_name: Optional[str] = None
    company_ico: Optional[str] = None
    company_dic: Optional[str] = None
    company_email: Optional[str] = None
    company_phone: Optional[str] = None
    company_address: Optional[str] = None
    company_city: Optional[str] = None
    company_bank: Optional[str] = None
    company_iban: Optional[str] = None
    cc_email: Optional[str] = None
    company_vat_payer: Optional[bool] = None   # jsme plátci DPH?
    company_vat_rate: Optional[float] = None   # základní sazba DPH (%), default 21

@app.get("/api/settings/company")
def get_company_settings():
    s = db_get_settings()
    out = {k: s.get(k, "") for k in ["company_name","company_ico","company_dic","company_email","company_phone","company_address","company_city","company_bank","company_iban","cc_email"]}
    out["company_vat_payer"] = bool(s.get("company_vat_payer", False))
    out["company_vat_rate"] = float(s.get("company_vat_rate", 21))
    return out

@app.post("/api/settings/company")
def save_company_settings(req: CompanySettingsRequest):
    db_save_settings(req.model_dump(exclude_none=True))
    return {"status": "ok"}

@app.get("/api/ares/{ico}")
async def ares_lookup(ico: str):
    """Dohledá firmu dle IČO v českém registru ARES (oficiální, zdarma).
    Vrací název, DIČ a adresu pro autofill fakturačních údajů."""
    ico = "".join(ch for ch in (ico or "") if ch.isdigit())
    if len(ico) != 8:
        raise HTTPException(400, "IČO musí mít 8 číslic")
    url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url, headers={"accept": "application/json"})
        if r.status_code == 404:
            raise HTTPException(404, "Subjekt s tímto IČO nebyl v ARES nalezen")
        if r.status_code != 200:
            raise HTTPException(502, f"ARES nedostupný ({r.status_code})")
        d = r.json()
        sidlo = d.get("sidlo", {}) or {}
        return {
            "ico": d.get("ico", ico),
            "name": d.get("obchodniJmeno", ""),
            "dic": d.get("dic", ""),
            "address": sidlo.get("textovaAdresa", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Chyba ARES: {str(e)}")

# ─────────────────────────────────────────────
# Provize (externisté / affiliate)
# ─────────────────────────────────────────────
def _norm_ref(code: str) -> str:
    return re.sub(r'[^A-Za-z0-9]', '', (code or '')).upper()

class CommissionSettingsRequest(BaseModel):
    commission_enabled: Optional[bool] = None
    commission_amount: Optional[float] = None      # CZK, fixní za získaný hotel
    commission_hold_days: Optional[int] = None

@app.get("/api/settings/commission")
def get_commission_settings():
    s = db_get_settings()
    return {
        "commission_enabled": bool(s.get("commission_enabled", False)),
        "commission_amount": float(s.get("commission_amount", 1500)),
        "commission_currency": "CZK",
        "commission_hold_days": int(s.get("commission_hold_days", 30)),
    }

@app.post("/api/settings/commission")
def save_commission_settings(req: CommissionSettingsRequest):
    db_save_settings(req.model_dump(exclude_none=True))
    return {"status": "ok"}

class PartnerRequest(BaseModel):
    name: str
    email: Optional[str] = None
    referral_code: str
    ico: Optional[str] = None
    active: Optional[bool] = True
    note: Optional[str] = None

def _partner_by_ref(db: dict, ref: str):
    ref = _norm_ref(ref)
    if not ref:
        return None
    for p in db.get("partners", {}).values():
        if _norm_ref(p.get("referral_code", "")) == ref and p.get("active", True):
            return p
    return None

@app.get("/api/partners")
def list_partners():
    db = db_load()
    partners = list(db.get("partners", {}).values())
    comms = db.get("commissions", {})
    hotels = db.get("hotels", {})
    for p in partners:
        pc = [c for c in comms.values() if c.get("partner_id") == p["id"]]
        p["_stats"] = {
            "hotels": sum(1 for h in hotels.values() if h.get("acquired_by") == p["id"]),
            "pending": sum(1 for c in pc if c.get("status") == "pending"),
            "approved": sum(1 for c in pc if c.get("status") == "approved"),
            "paid": sum(1 for c in pc if c.get("status") == "paid"),
            "owed_czk": round(sum(c.get("amount", 0) for c in pc if c.get("status") == "approved"), 2),
        }
    return {"status": "ok", "partners": partners}

@app.post("/api/partners")
def create_partner(req: PartnerRequest):
    db = db_load()
    partners = db.setdefault("partners", {})
    ref = _norm_ref(req.referral_code)
    if not ref:
        raise HTTPException(400, "Referral kód je povinný")
    for p in partners.values():
        if _norm_ref(p.get("referral_code", "")) == ref:
            raise HTTPException(409, f"Referral kód {ref} už existuje")
    pid = str(uuid.uuid4())
    partner = {
        "id": pid, "name": req.name, "email": (req.email or "").strip(),
        "referral_code": ref, "ico": (req.ico or "").strip(),
        "active": True if req.active is None else bool(req.active),
        "note": req.note or "", "created_at": datetime.utcnow().isoformat(),
    }
    partners[pid] = partner
    db_save(db)
    return {"status": "ok", "partner": partner}

@app.patch("/api/partners/{partner_id}")
def update_partner(partner_id: str, req: PartnerRequest):
    db = db_load()
    partners = db.setdefault("partners", {})
    if partner_id not in partners:
        raise HTTPException(404, "Partner nenalezen")
    ref = _norm_ref(req.referral_code)
    if not ref:
        raise HTTPException(400, "Referral kód je povinný")
    for pid2, p in partners.items():
        if pid2 != partner_id and _norm_ref(p.get("referral_code", "")) == ref:
            raise HTTPException(409, f"Referral kód {ref} už existuje")
    p = partners[partner_id]
    p.update({
        "name": req.name, "email": (req.email or "").strip(), "referral_code": ref,
        "ico": (req.ico or "").strip(),
        "active": True if req.active is None else bool(req.active), "note": req.note or "",
    })
    db_save(db)
    return {"status": "ok", "partner": p}

@app.delete("/api/partners/{partner_id}")
def delete_partner(partner_id: str, request: Request):
    _check_danger(request)  # trvalé smazání partnera vyžaduje druhé heslo
    db = db_load()
    if partner_id in db.get("partners", {}):
        db["partners"].pop(partner_id)
        db_save(db)
    return {"status": "ok"}

def _create_commission_if_eligible(db: dict, hotel_id: str):
    """Vytvoří provizi při první REÁLNÉ platbě, pokud hotel získal partner. Idempotentní."""
    hotel = db.get("hotels", {}).get(hotel_id)
    if not hotel:
        return
    pid = hotel.get("acquired_by")
    if not pid or pid == "auto":
        return
    s = db.get("settings", {})
    if not s.get("commission_enabled"):
        return
    comms = db.setdefault("commissions", {})
    if any(c.get("hotel_id") == hotel_id for c in comms.values()):
        return  # jedna provize na hotel (jednorázový model)
    amount = float(s.get("commission_amount", 1500))
    cid = str(uuid.uuid4())
    comms[cid] = {
        "id": cid, "partner_id": pid, "hotel_id": hotel_id,
        "hotel_name": hotel.get("name", ""), "amount": round(amount, 2), "currency": "CZK",
        "status": "pending", "created_at": datetime.utcnow().isoformat(),
        "approved_at": None, "paid_at": None, "payout_reference": "",
    }
    logging.info("Provize %s vytvořena pro hotel %s (partner %s, %s CZK)", cid, hotel_id, pid, amount)

def _refresh_commission_statuses(db: dict) -> bool:
    """Lazy schvalování: pending → approved po uplynutí hold_days, pokud hotel stále aktivní."""
    from datetime import timedelta
    s = db.get("settings", {})
    hold = int(s.get("commission_hold_days", 30))
    now = datetime.utcnow()
    changed = False
    for c in db.get("commissions", {}).values():
        if c.get("status") != "pending":
            continue
        try:
            created = datetime.fromisoformat(c.get("created_at", ""))
        except Exception:
            continue
        if now >= created + timedelta(days=hold):
            hotel = db.get("hotels", {}).get(c.get("hotel_id"), {})
            if hotel.get("subscription_active"):
                c["status"] = "approved"
                c["approved_at"] = now.isoformat()
                changed = True
    return changed

@app.get("/api/commissions")
def list_commissions():
    db = db_load()
    if _refresh_commission_statuses(db):
        db_save(db)
    partners = db.get("partners", {})
    comms = sorted(db.get("commissions", {}).values(), key=lambda c: c.get("created_at", ""), reverse=True)
    for c in comms:
        c["partner_name"] = partners.get(c.get("partner_id"), {}).get("name", "?")
    return {"status": "ok", "commissions": comms}

@app.get("/api/docs/commissions", response_class=HTMLResponse)
def docs_commissions():
    """Byznys dokumentace systému provizí — otevře se z adminu."""
    return """<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Provize pro externisty — jak to funguje</title>
<style>
:root{color-scheme:light dark}
body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:820px;margin:0 auto;padding:32px 20px 80px;line-height:1.7;color:#1a1a1a;background:#fff}
@media(prefers-color-scheme:dark){body{background:#15161a;color:#e6e4df}code{background:#2a2b31!important;color:#ffd7b0!important}.box{background:#1e1f25!important;border-color:#33343c!important}h1,h2{color:#fff!important}th{background:#23242b!important}}
h1{font-size:26px;border-bottom:3px solid #FF6B00;padding-bottom:10px}
h2{font-size:19px;margin-top:34px;color:#111}
h2 .n{display:inline-block;background:#FF6B00;color:#fff;width:26px;height:26px;line-height:26px;text-align:center;border-radius:50%;font-size:14px;margin-right:8px}
code{background:#f2efe9;padding:2px 6px;border-radius:5px;font-size:14px}
.box{background:#faf8f4;border:1px solid #e8e4dc;border-radius:10px;padding:14px 18px;margin:14px 0}
.tip{border-left:4px solid #00b894;padding-left:14px;color:#0a7a63}
table{border-collapse:collapse;width:100%;margin:12px 0}
th,td{border:1px solid #e8e4dc;padding:8px 10px;text-align:left;font-size:14px}
th{background:#faf8f4}
.flow{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0;font-size:14px;font-weight:600}
.flow span{background:#f2efe9;padding:6px 12px;border-radius:20px}
</style></head><body>
<h1>🤝 Provize pro externisty — jak to funguje</h1>
<p>Spolupracujeme s externisty (affiliate partnery), kteří přivádějí nové hotely. Když partner přivede hotel a ten <strong>skutečně zaplatí</strong>, náleží partnerovi provize. Tento systém všechno eviduje — kdo přivedl který hotel a co je/není vyplaceno. Peníze přes systém <strong>netečou</strong>, je to čistě evidence.</p>

<h2><span class="n">1</span>Partner (externista)</h2>
<p>Každého partnera založíš v adminu (sekce <em>Provize</em>). Potřebuješ: jméno, e-mail a <strong>IČO</strong> (partner ti bude fakturovat). Každý partner má svůj unikátní <strong>referral kód</strong>.</p>

<h2><span class="n">2</span>Referral kód — jak vzniká a k čemu je</h2>
<p>Referral kód je krátký unikátní identifikátor partnera, např. <code>JAN2026</code>. </p>
<ul>
<li><strong>Zadáváš ho ty</strong> při vytváření partnera (volíš něco zapamatovatelného — jméno + rok apod.).</li>
<li>Systém ho automaticky <strong>normalizuje</strong> — velká písmena, jen znaky A–Z a 0–9 (mezery a diakritika se odstraní), takže <code>jan 2026</code> → <code>JAN2026</code>.</li>
<li>Musí být <strong>unikátní</strong> — dva partneři nemůžou mít stejný kód (systém to hlídá).</li>
<li>Slouží k rozpoznání, <strong>který partner hotel přivedl</strong>.</li>
</ul>

<h2><span class="n">3</span>Referral link — jak funguje</h2>
<p>Z kódu se automaticky sestaví odkaz, který partner posílá hotelům:</p>
<div class="box"><code>https://www.smartestguide.com/landing?ref=JAN2026</code></div>
<p>Co se stane, když hotel přijde přes tento odkaz:</p>
<ol>
<li>Kód <code>ref=JAN2026</code> se <strong>uloží</strong> hotelu do prohlížeče (přežije i to, když odejde a vrátí se na stránku).</li>
<li>Když se hotel zaregistruje, kód se pošle na server a systém dohledá partnera → hotel dostane značku <strong>„získal partner Jan"</strong> (pole <code>acquired_by</code>).</li>
<li>Hotel bez platného kódu je veden jako <strong>„automatický"</strong> — bez nároku na provizi.</li>
</ol>
<p class="tip">💡 Tlačítko „Kopírovat link" u partnera v adminu zkopíruje přesně tento odkaz — partner ho jen pošle hotelu.</p>

<h2><span class="n">4</span>Kolik partner dostane</h2>
<p>V <strong>Nastavení provize</strong> zadáš jednu fixní částku v CZK, kterou dostane partner za každý přivedený hotel. Platí <strong>stejně pro všechny</strong> partnery.</p>

<h2><span class="n">5</span>Kdy provize vznikne</h2>
<p>Provize se vytvoří <strong>až po první SKUTEČNÉ platbě hotelu</strong> — tedy po skončení 14denního triálu, když hotel opravdu zaplatí. Za hotel, který jen prošel triálem a nezaplatil, provize <strong>nevzniká</strong>. Na jeden hotel připadá <strong>jedna</strong> provize.</p>

<h2><span class="n">6</span>Životní cyklus provize</h2>
<div class="flow"><span>⏳ pending</span> → <span>✅ approved</span> → <span>💸 paid</span></div>
<ul>
<li><strong>pending (čeká)</strong> — hotel poprvé zaplatil, běží <strong>zádržná lhůta</strong> (nastavitelná, např. 30 dní) jako ochrana proti hotelům, co hned zruší.</li>
<li><strong>approved (schváleno)</strong> — lhůta uplynula a hotel je stále aktivní → nárok potvrzen.</li>
<li><strong>paid (vyplaceno)</strong> — označíš ručně po zaplacení (viz níže).</li>
</ul>

<h2><span class="n">7</span>Výplata partnerovi</h2>
<ol>
<li>Vyplácejí se jen provize ve stavu <strong>approved</strong>.</li>
<li>Partner (OSVČ) ti vystaví <strong>fakturu</strong> na schválené provize.</li>
<li>Zaplatíš mu <strong>bankovním převodem</strong>.</li>
<li>V adminu klikneš u provize na <strong>„Vyplaceno"</strong> a zadáš číslo faktury / variabilní symbol.</li>
</ol>
<p class="tip">💡 <strong>Stripe se k výplatě nepoužívá</strong> — ten slouží k vybírání peněz od hotelů, ne k výplatě partnerům. Automatické výplaty (Stripe Connect) zvážíme až u více partnerů.</p>

<h2><span class="n">8</span>Daně</h2>
<p>Provize je zdanitelný příjem partnera; v ČR ji řeší vlastní fakturou (DPH/daňové dopady konzultuj s účetní). SmartestGuide pouze eviduje závazek a jeho úhradu.</p>
</body></html>"""

@app.post("/api/commissions/{commission_id}/pay")
def pay_commission(commission_id: str, reference: str = ""):
    db = db_load()
    comms = db.get("commissions", {})
    if commission_id not in comms:
        raise HTTPException(404, "Provize nenalezena")
    comms[commission_id]["status"] = "paid"
    comms[commission_id]["paid_at"] = datetime.utcnow().isoformat()
    comms[commission_id]["payout_reference"] = reference
    db_save(db)
    return {"status": "ok", "commission": comms[commission_id]}

# ─────────────────────────────────────────────
# Faktury
# ─────────────────────────────────────────────
@app.get("/api/invoices")
def list_invoices():
    db = db_load()
    invoices = list(db.get("invoices", {}).values())
    invoices.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"status": "ok", "invoices": invoices}

# Pozn.: v produkci faktury NEMAŽEME (účetní doklady). Testovací faktury
# se pouze skrývají ve výpisu na frontendu (nedestruktivně).

_EU_COUNTRIES = {"AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR","DE","GR","HU","IE",
                 "IT","LV","LT","LU","MT","NL","PL","PT","RO","SK","SI","ES","SE"}

def _compute_invoice_vat(price: float, hotel: dict, s: dict) -> dict:
    """DPH rozpad dle plátcovství dodavatele a země odběratele. price = základ bez DPH."""
    vat_payer = bool(s.get("company_vat_payer", False))
    rate = float(s.get("company_vat_rate", 21))
    country = (hotel.get("country") or "").upper().strip()
    has_dic = bool((hotel.get("dic") or "").strip())
    if not vat_payer:
        return {"vat_rate": 0, "vat_amount": 0.0, "amount_total": round(price, 2),
                "vat_note": "Nejsme plátci DPH."}
    # Dodavatel je CZ plátce DPH
    if country in ("", "CZ"):
        vat = round(price * rate / 100, 2)
        return {"vat_rate": rate, "vat_amount": vat, "amount_total": round(price + vat, 2), "vat_note": ""}
    if country in _EU_COUNTRIES and has_dic:
        return {"vat_rate": 0, "vat_amount": 0.0, "amount_total": round(price, 2),
                "vat_note": "Přenesení daňové povinnosti / reverse charge — daň odvede zákazník."}
    if country in _EU_COUNTRIES:
        vat = round(price * rate / 100, 2)
        return {"vat_rate": rate, "vat_amount": vat, "amount_total": round(price + vat, 2),
                "vat_note": "EU odběratel bez DIČ — ověř režim DPH s účetní."}
    return {"vat_rate": 0, "vat_amount": 0.0, "amount_total": round(price, 2),
            "vat_note": "Mimo EU — bez DPH (vývoz služby)."}

def _create_invoice_record(db: dict, hotel_id: str, hotel: dict, status: str = "issued", trial: bool = False, payment_due: str = None) -> dict:
    """Vytvoří fakturu / trial záznam pro hotel a vloží do db['invoices']. Volá db_save volající.
    trial=True → nulový záznam (status 'trial') s budoucí částkou a datem první platby."""
    import calendar, re as _re
    from datetime import timedelta as _td
    beds = hotel.get("bed_count") or hotel.get("subscription_paid_beds") or 0
    s = db_get_settings()
    base = int(s.get("pricing_base", 200))
    threshold = int(s.get("pricing_threshold", 100))
    per_bed = float(s.get("pricing_per_bed", 3))
    price = base if beds <= threshold else base + (beds - threshold) * per_bed
    now = datetime.utcnow()
    if "invoices" not in db:
        db["invoices"] = {}
    month_prefix = f"SG-{now.strftime('%Y%m')}-"
    month_count = sum(1 for inv in db["invoices"].values() if inv.get("invoice_number","").startswith(month_prefix))
    invoice_number = f"{month_prefix}{month_count + 1:04d}"
    period_from = now.replace(day=1).date().isoformat()
    last_day = calendar.monthrange(now.year, now.month)[1]
    period_to = now.replace(day=last_day).date().isoformat()
    vat = _compute_invoice_vat(price, hotel, s)
    due_date = (now + _td(days=14)).date().isoformat()
    variable_symbol = _re.sub(r"\D", "", invoice_number) or str(int(now.timestamp()))
    inv_id = str(uuid.uuid4())
    if trial:
        net = 0.0; v_amount = 0.0; total = 0.0; inv_status = "trial"
    else:
        net = round(price, 2); v_amount = vat["vat_amount"]; total = vat["amount_total"]; inv_status = status
    invoice = {
        "id": inv_id, "invoice_number": invoice_number, "hotel_id": hotel_id,
        "hotel_name": hotel.get("name",""),
        "hotel_billing_name": (hotel.get("billing_name") or "").strip(),
        "hotel_address": hotel.get("address",""),
        "hotel_email": (hotel.get("email") or ""),
        "hotel_ico": (hotel.get("ico") or "").strip(),
        "hotel_dic": (hotel.get("dic") or "").strip(),
        "hotel_country": (hotel.get("country") or "").upper().strip(),
        "beds": beds,
        "amount_eur": net,        # základ bez DPH (zpětná kompatibilita)
        "amount_net": net,
        "vat_rate": vat["vat_rate"],
        "vat_amount": v_amount,
        "amount_total": total,
        "amount_local": total,  # částka k úhradě (vč. DPH)
        "vat_note": (f"Trial (14 dní zdarma) — první platba {payment_due or due_date}. Zatím neúčtováno." if trial else vat["vat_note"]),
        "currency_code": "EUR", "currency_symbol": "€",
        "period_from": period_from, "period_to": period_to,
        "due_date": due_date, "variable_symbol": variable_symbol,
        "status": inv_status,
        "created_at": now.isoformat(), "updated_at": now.isoformat(),
    }
    if trial:
        invoice["is_trial"] = True
        invoice["future_amount_net"] = round(price, 2)
        invoice["future_amount_total"] = vat["amount_total"]
        invoice["payment_due"] = payment_due or due_date
    db["invoices"][inv_id] = invoice
    return invoice


@app.post("/api/hotels/{hotel_id}/invoices/generate")
def generate_invoice(hotel_id: str):
    db = db_load()
    if hotel_id not in db["hotels"]:
        raise HTTPException(404, "Hotel nenalezen")
    hotel = db["hotels"][hotel_id]
    if not hotel.get("subscription_active"):
        raise HTTPException(400, "Hotel nemá aktivní předplatné")
    invoice = _create_invoice_record(db, hotel_id, hotel)
    db_save(db)
    return {"status": "ok", "invoice": invoice}

@app.patch("/api/invoices/{invoice_id}/status")
def update_invoice_status(invoice_id: str, status: str):
    db = db_load()
    if "invoices" not in db or invoice_id not in db["invoices"]:
        raise HTTPException(404, "Faktura nenalezena")
    if status not in ("issued", "paid", "cancelled"):
        raise HTTPException(400, "Neplatný stav")
    db["invoices"][invoice_id]["status"] = status
    db["invoices"][invoice_id]["updated_at"] = datetime.utcnow().isoformat()
    if status == "paid":
        db["invoices"][invoice_id]["paid_at"] = datetime.utcnow().isoformat()
    db_save(db)
    return {"status": "ok", "invoice": db["invoices"][invoice_id]}

@app.get("/api/invoices/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: str, request: Request, token: str = ""):
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    db = db_load()
    if "invoices" not in db or invoice_id not in db["invoices"]:
        raise HTTPException(404, "Faktura nenalezena")
    inv = db["invoices"][invoice_id]
    # Přístup: buď hotel z portálu (platný token na svou fakturu), nebo přihlášený admin
    if token:
        h = find_hotel_by_token(token)
        if not h or inv.get("hotel_id") != h.get("id"):
            raise HTTPException(403, "Neplatny token pro tuto fakturu")
    elif _ADMIN_TOKEN and request.cookies.get("sg_admin", "") != _ADMIN_TOKEN:
        raise HTTPException(403, "Neautorizováno")
    s = db_get_settings()
    try:
        pdf_bytes = _build_invoice_pdf_bytes(inv, s)
    except ImportError:
        raise HTTPException(500, "reportlab není nainstalován")
    except Exception as e:
        raise HTTPException(500, f"Chyba PDF: {str(e)}")
    safe_num = (inv.get("invoice_number") or invoice_id).replace("/", "-")
    return StreamingResponse(BytesIO(pdf_bytes), media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="faktura-{safe_num}.pdf"'})
