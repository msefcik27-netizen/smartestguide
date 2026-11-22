"""Veřejné API SmartestGuide (/api/v1) a onboarding vývojářů (/developers).

Dva druhy klíčů:
  * sg_live_…  vydává hotel v portálu (Znalostní báze → Napojení jiných systémů). Platí pro data toho hotelu.
  * sg_test_…  zkušební klíč: vývojář se zaregistruje na /developers, potvrdí e-mail a dostane klíč
               k ukázkovému hotelu. Jen čtení a chat, malý denní limit.
Klíč se ukládá jen jako otisk (SHA-256); celý ho vidí majitel jednou, při vydání.

Registruje se z app.py voláním setup(app, ...) až po kb_routes.setup (sdílí jeho úložiště a bázi).
Testy: test_sg_api.py
"""
import os, re, json, hashlib, secrets, asyncio, logging
from datetime import datetime, timedelta
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

import kb as kbmod
import kb_routes as kbr

D = {}
_HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.0"
SCOPES = ("chat", "knowledge:read", "knowledge:write")
SANDBOX_SCOPES = ("chat", "knowledge:read")
LIVE_DAILY, SANDBOX_DAILY = 2000, 100
PER_MINUTE = 60
MAX_KEYS_PER_HOTEL = 10
VERIFY_HOURS = 48
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict = None):
        self.status, self.code, self.message, self.headers = status, code, message, headers or {}


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _today() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _store():
    return kbr.STORE


def new_key(kind: str, hotel_id: str, name: str, scopes, daily_limit: int, owner_email: str = "") -> dict:
    """Vytvoří klíč, uloží otisk a vrátí {id, key, prefix}. Celý klíč už potom nejde zjistit."""
    key = ("sg_live_" if kind == "live" else "sg_test_") + secrets.token_hex(20)
    kid = secrets.token_hex(8)
    scopes = [s for s in scopes if s in SCOPES]
    _store().x("INSERT INTO api_keys (id, key_hash, prefix, kind, hotel_id, name, owner_email, scopes, daily_limit, created_at, last_used, revoked) "
               "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)",
               (kid, _hash(key), key[:12], kind, hotel_id, name[:80], owner_email[:254], json.dumps(scopes), int(daily_limit), kbmod._now(), ""))
    return {"id": kid, "key": key, "prefix": key[:12]}


_COLS = "id, prefix, kind, hotel_id, name, owner_email, scopes, daily_limit, created_at, last_used, revoked"


def _row(r) -> dict:
    d = dict(zip([c.strip() for c in _COLS.split(",")], r))
    try:
        d["scopes"] = json.loads(d["scopes"] or "[]")
    except Exception:
        d["scopes"] = []
    d["revoked"] = bool(d["revoked"])
    return d


def _usage_today(key_id: str) -> int:
    r = _store().x("SELECT COALESCE(SUM(n),0) FROM api_usage WHERE key_id=%s AND day=%s", (key_id, _today()), "one")
    return int((r or [0])[0] or 0)


def _auth_sync(token: str, scope: str, kind: str) -> dict:
    r = _store().x(f"SELECT {_COLS} FROM api_keys WHERE key_hash=%s", (_hash(token),), "one")
    if not r:
        raise ApiError(401, "invalid_key", "Unknown API key.")
    k = _row(r)
    if k["revoked"]:
        raise ApiError(401, "revoked_key", "This API key has been revoked.")
    if scope and scope not in k["scopes"]:
        raise ApiError(403, "missing_scope", f"This key does not have the '{scope}' scope.")
    used = _usage_today(k["id"])
    if used >= k["daily_limit"]:
        raise ApiError(429, "daily_limit", f"Daily limit of {k['daily_limit']} requests reached. It resets at 00:00 UTC.",
                       {"X-RateLimit-Limit": str(k["daily_limit"]), "X-RateLimit-Remaining": "0"})
    _store().tx([
        ("INSERT INTO api_usage (key_id, day, kind, n) VALUES (%s,%s,%s,1) "
         "ON CONFLICT (key_id, day, kind) DO UPDATE SET n = api_usage.n + 1", (k["id"], _today(), kind)),
        ("UPDATE api_keys SET last_used=%s WHERE id=%s", (kbmod._now(), k["id"])),
    ])
    k["used_today"] = used + 1
    return k


async def auth(request: Request, scope: str = "", kind: str = "other"):
    """Ověří Bearer klíč, scope a limity; vrátí (klíč, hotel)."""
    h = request.headers.get("authorization", "")
    token = h[7:].strip() if h.lower().startswith("bearer ") else (request.headers.get("x-api-key", "") or "").strip()
    if not token.startswith(("sg_live_", "sg_test_")) or len(token) > 80:
        raise ApiError(401, "missing_key", "Send your API key in the header: Authorization: Bearer sg_…")
    if not D["rate_limit_ok"]("apiv1:" + _hash(token)[:16], PER_MINUTE, 60):
        raise ApiError(429, "rate_limit", f"Too many requests. The limit is {PER_MINUTE} per minute.", {"Retry-After": "30"})
    k = await asyncio.to_thread(_auth_sync, token, scope, kind)
    hotel = D["db_load"]()["hotels"].get(k["hotel_id"])
    if not hotel or hotel.get("archived"):
        raise ApiError(403, "hotel_unavailable", "The hotel this key belongs to is no longer available.")
    request.state.sg_key = k
    return k, hotel


def _limit_headers(k: dict) -> dict:
    return {"X-RateLimit-Limit": str(k["daily_limit"]), "X-RateLimit-Remaining": str(max(0, k["daily_limit"] - k.get("used_today", 0)))}


def _ok(k: dict, data: dict, status: int = 200):
    return JSONResponse(data, status_code=status, headers=_limit_headers(k))


def sandbox_hotel():
    """(id, hotel) ukázkového hotelu pro zkušební klíče, nebo (None, None)."""
    db = D["db_load"]()
    ident = os.getenv("SG_API_SANDBOX_HOTEL", "").strip()
    if ident:
        hid, h = D["resolve_hotel"](db, ident)
        if h:
            return hid, h
    for hid, h in db.get("hotels", {}).items():
        if h.get("archived"):
            continue
        if h.get("is_demo") or (h.get("name") or "").strip().upper().startswith("DEMO"):
            return hid, h
    return None, None


async def _json(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
    if not isinstance(b, dict):
        raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
    return b


def _doc_public(d: dict) -> dict:
    return {"id": d.get("id"), "title": d.get("title"), "audience": d.get("audience"), "enabled": bool(d.get("enabled", True)),
            "chars": d.get("chars"), "chunks": d.get("chunks"), "source": d.get("provider") or "manual", "updated_at": d.get("updated_at")}


def setup(app, **deps):
    """deps: db_load, resolve_hotel, rate_limit_ok, client_ip, get_base_url, staging_banner, guest_chat, guest_url, send_email."""
    D.update(deps)
    rl, ip = deps["rate_limit_ok"], deps["client_ip"]

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, e: ApiError):
        return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=e.status, headers=e.headers)

    # ───────────── veřejné API ─────────────
    @app.get("/api/v1/openapi.json")
    def v1_openapi(request: Request):
        return JSONResponse(openapi(D["get_base_url"](request)))

    @app.get("/api/v1/me")
    async def v1_me(request: Request):
        k, h = await auth(request)
        return _ok(k, {"key": {"id": k["id"], "name": k["name"], "prefix": k["prefix"], "mode": "live" if k["kind"] == "live" else "sandbox",
                               "scopes": k["scopes"], "created_at": k["created_at"]},
                       "hotel": {"id": k["hotel_id"], "name": h.get("name", "")},
                       "limits": {"per_day": k["daily_limit"], "used_today": k["used_today"], "per_minute": PER_MINUTE},
                       "api_version": VERSION})

    @app.get("/api/v1/hotel")
    async def v1_hotel(request: Request):
        k, h = await auth(request)
        c = await asyncio.to_thread(kbr.KB.counts, k["hotel_id"])
        return _ok(k, {"id": k["hotel_id"], "name": h.get("name", ""), "city": h.get("city", ""), "country": h.get("country", ""),
                       "website": h.get("website") or h.get("url") or "",
                       "guest_url": D["guest_url"](D["get_base_url"](request), k["hotel_id"], h),
                       "knowledge": {"documents": c.get("docs", 0), "chunks": c.get("chunks", 0), "chars": c.get("chars", 0)}})

    @app.post("/api/v1/chat")
    async def v1_chat(request: Request):
        """Odpověď concierge Alexe na otázku hosta – stejná jako v aplikaci pro hosty."""
        b = await _json(request)
        msg = str(b.get("message") or "").strip()
        if not msg:
            raise ApiError(400, "missing_message", "Field 'message' is required.")
        if len(msg) > 2000:
            raise ApiError(400, "message_too_long", "Field 'message' can have at most 2000 characters.")
        hist = [{"role": m["role"], "content": m["content"][:4000]} for m in (b.get("history") or [])
                if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)][-10:]
        lang = str(b.get("language") or "")[:8].lower()
        k, h = await auth(request, "chat", "chat")
        try:
            res = await D["guest_chat"](k["hotel_id"], msg, lang, hist, "api:" + k["id"] + ":" + str(b.get("session") or "")[:40], request)
        except HTTPException as e:
            if e.status_code == 429:
                raise ApiError(429, "rate_limit", "Too many chat requests at once. Try again in a moment.", {"Retry-After": "30"})
            logging.warning("API v1 chat: %s %s", e.status_code, e.detail)
            raise ApiError(502, "chat_unavailable", "The concierge is not available right now.")
        return _ok(k, {"reply": res.get("reply", ""), "language": res.get("lang") or lang or None})

    @app.post("/api/v1/knowledge/search")
    async def v1_search(request: Request):
        """Vyhledání v bázi hotelu: vrací jen nejrelevantnější úryvky, ne celé dokumenty."""
        b = await _json(request)
        q = str(b.get("query") or "").strip()[:500]
        if len(q) < 2:
            raise ApiError(400, "missing_query", "Field 'query' is required.")
        k, h = await auth(request, "knowledge:read", "search")
        staff = b.get("audience") == "staff"
        if staff and k["kind"] != "live":
            raise ApiError(403, "sandbox_restricted", "Sandbox keys can only search guest-facing knowledge.")
        try:
            limit = max(1, min(10, int(b.get("limit") or (kbr.STAFF_K if staff else kbr.GUEST_K))))
        except (TypeError, ValueError):
            raise ApiError(400, "invalid_limit", "Field 'limit' must be a number from 1 to 10.")
        hits = await kbr.KB.search(k["hotel_id"], q, audiences=("guest", "staff") if staff else ("guest",), k=limit)
        ctx = kbmod.prompt_block(hits, max_chars=kbr.STAFF_CHARS if staff else kbr.GUEST_CHARS, staff=staff)
        return _ok(k, {"query": q,
                       "results": [{"title": x["title"], "section": x["section"], "text": x["text"], "url": x["url"],
                                    "audience": x["audience"], "similarity": x.get("similarity")} for x in hits],
                       "context": ctx})

    @app.get("/api/v1/knowledge/documents")
    async def v1_docs(request: Request):
        k, h = await auth(request, "knowledge:read", "docs")
        docs = await asyncio.to_thread(kbr.KB.list_documents, k["hotel_id"])
        if k["kind"] != "live":
            docs = [d for d in docs if d.get("audience") == "guest"]
        return _ok(k, {"documents": [_doc_public(d) for d in docs]})

    @app.post("/api/v1/knowledge/documents")
    async def v1_doc_put(request: Request):
        """Vloží nebo nahradí textový dokument (např. export z interního systému hotelu)."""
        b = await _json(request)
        title, text = str(b.get("title") or "").strip()[:200], str(b.get("text") or "").strip()
        if not title or len(text) < 10:
            raise ApiError(400, "missing_fields", "Fields 'title' and 'text' (at least 10 characters) are required.")
        if len(text) > 400_000:
            raise ApiError(413, "text_too_long", "Field 'text' can have at most 400,000 characters.")
        aud = b.get("audience") if b.get("audience") in kbmod.AUDIENCES else "staff"
        k, h = await auth(request, "knowledge:write", "write")
        ext = re.sub(r"[^A-Za-z0-9_.-]", "-", str(b.get("id") or ""))[:40]
        doc_id = ("api-" + ext) if ext else ""
        res = await kbr.KB.put_document(k["hotel_id"], title=title, text=text, mime="text/plain", audience=aud, doc_id=doc_id)
        if not res.get("id"):
            raise ApiError(400, "document_rejected", str(res.get("error") or "The document could not be stored."))
        return _ok(k, {"id": res["id"], "title": title, "audience": aud, "chunks": res.get("chunks")}, 201)

    @app.delete("/api/v1/knowledge/documents/{doc_id}")
    async def v1_doc_delete(doc_id: str, request: Request):
        k, h = await auth(request, "knowledge:write", "write")
        if not await asyncio.to_thread(kbr.KB.delete_document, k["hotel_id"], doc_id[:64]):
            raise ApiError(404, "not_found", "Document not found.")
        return _ok(k, {"deleted": True, "id": doc_id})

    # ───────────── klíče hotelu (portál) ─────────────
    @app.get("/api/hotel-portal/api-keys")
    async def portal_keys(request: Request):
        h = kbr._hotel(request)
        rows = await asyncio.to_thread(_store().x, f"SELECT {_COLS} FROM api_keys WHERE hotel_id=%s AND kind='live' ORDER BY created_at DESC",
                                       (h["id"],), "all")
        return {"keys": [{x: k[x] for x in ("id", "name", "prefix", "scopes", "daily_limit", "created_at", "last_used", "revoked")}
                         for k in (_row(r) for r in rows or [])]}

    @app.post("/api/hotel-portal/api-keys")
    async def portal_key_new(request: Request):
        h = kbr._hotel(request)
        b = await kbr._body(request)
        name = str(b.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Zadejte název klíče (k čemu bude sloužit).")
        scopes = [s for s in (b.get("scopes") or []) if s in SCOPES] or list(SANDBOX_SCOPES)
        n = await asyncio.to_thread(_store().x, "SELECT COUNT(*) FROM api_keys WHERE hotel_id=%s AND kind='live' AND revoked=0", (h["id"],), "one")
        if int((n or [0])[0] or 0) >= MAX_KEYS_PER_HOTEL:
            raise HTTPException(400, f"Hotel může mít nejvýš {MAX_KEYS_PER_HOTEL} platných klíčů. Nepoužívané nejdřív zrušte.")
        k = await asyncio.to_thread(new_key, "live", h["id"], name, scopes, LIVE_DAILY, h.get("email", "") or "")
        return {"key": k["key"], "id": k["id"], "prefix": k["prefix"]}

    @app.post("/api/hotel-portal/api-keys/revoke")
    async def portal_key_revoke(request: Request):
        h = kbr._hotel(request)
        kid = str((await kbr._body(request)).get("id") or "")
        r = await asyncio.to_thread(_store().x, "SELECT id FROM api_keys WHERE id=%s AND hotel_id=%s", (kid, h["id"]), "one")
        if not r:
            raise HTTPException(404, "Klíč nenalezen.")
        await asyncio.to_thread(_store().x, "UPDATE api_keys SET revoked=1 WHERE id=%s", (kid,))
        return {"ok": True}

    # ───────────── onboarding vývojářů ─────────────
    @app.get("/developers", response_class=HTMLResponse)
    @app.get("/vyvojari", response_class=HTMLResponse)
    def developers_page():
        with open(os.path.join(_HERE, "developers.html"), "r", encoding="utf-8") as f:
            return D["staging_banner"](f.read())

    @app.post("/api/developers/register")
    async def dev_register(request: Request):
        b = await kbr._body(request)
        cs = str(b.get("lang") or "")[:2] == "cs"
        email = str(b.get("email") or "").strip().lower()
        name, company = str(b.get("name") or "").strip()[:120], str(b.get("company") or "").strip()[:160]
        purpose = str(b.get("purpose") or "").strip()[:1000]
        if not _EMAIL_RE.match(email) or not name:
            raise HTTPException(400, "Vyplňte jméno a platný e-mail." if cs else "Please fill in your name and a valid e-mail.")
        if not b.get("terms"):
            raise HTTPException(400, "Bez souhlasu s podmínkami použití klíč vydat nemůžeme." if cs else "Please accept the terms of use first.")
        if not rl("devreg:" + ip(request), 5, 3600) or not rl("devreg:" + _hash(email)[:16], 3, 3600):
            raise HTTPException(429, "Příliš mnoho pokusů. Zkuste to za hodinu." if cs else "Too many attempts. Please try again in an hour.")
        if not sandbox_hotel()[1]:
            raise HTTPException(503, "Zkušební prostředí teď není dostupné." if cs else "The sandbox is not available right now.")
        token = secrets.token_urlsafe(32)
        await asyncio.to_thread(_store().x,
            "INSERT INTO api_devs (email, name, company, purpose, created_at, verify_hash, verified) VALUES (%s,%s,%s,%s,%s,%s,0) "
            "ON CONFLICT (email) DO UPDATE SET name=excluded.name, company=excluded.company, purpose=excluded.purpose, "
            "created_at=excluded.created_at, verify_hash=excluded.verify_hash",
            (email, name, company, purpose, kbmod._now(), _hash(token)))
        link = f"{D['get_base_url'](request)}/developers?verify={token}" + ("&lang=cs" if cs else "")
        if not await D["send_email"](email, name, link, cs):
            raise HTTPException(503, "Potvrzovací e-mail se nepodařilo odeslat. Zkuste to prosím později." if cs
                                else "We could not send the confirmation e-mail. Please try again later.")
        return {"ok": True}

    def _verify_sync(token: str):
        r = _store().x("SELECT email, name, created_at FROM api_devs WHERE verify_hash=%s", (_hash(token),), "one")
        if not r:
            return None
        email, name, created = r[0], r[1], str(r[2] or "")
        try:
            if datetime.utcnow() - datetime.fromisoformat(created[:19]) > timedelta(hours=VERIFY_HOURS):
                return None
        except ValueError:
            return None
        hid, h = sandbox_hotel()
        if not h:
            return False
        # jeden platný zkušební klíč na e-mail; potvrzovací odkaz je na jedno použití
        _store().tx([("UPDATE api_keys SET revoked=1 WHERE owner_email=%s AND kind='sandbox'", (email,)),
                     ("UPDATE api_devs SET verified=1, verify_hash='' WHERE email=%s", (email,))])
        k = new_key("sandbox", hid, "Sandbox – " + name, SANDBOX_SCOPES, SANDBOX_DAILY, email)
        return {"key": k["key"], "hotel": h.get("name", ""), "scopes": list(SANDBOX_SCOPES), "per_day": SANDBOX_DAILY, "per_minute": PER_MINUTE}

    @app.post("/api/developers/verify")
    async def dev_verify(request: Request):
        b = await kbr._body(request)
        cs = str(b.get("lang") or "")[:2] == "cs"
        if not rl("devver:" + ip(request), 20, 3600):
            raise HTTPException(429, "Příliš mnoho pokusů." if cs else "Too many attempts.")
        token = str(b.get("token") or "")
        res = await asyncio.to_thread(_verify_sync, token) if 20 <= len(token) <= 100 else None
        if res is False:
            raise HTTPException(503, "Zkušební prostředí teď není dostupné." if cs else "The sandbox is not available right now.")
        if not res:
            raise HTTPException(400, "Odkaz už neplatí nebo byl použit. Vyplňte prosím formulář znovu." if cs
                                else "This link has expired or was already used. Please fill in the form again.")
        return res


def verification_email(to_email: str, name: str, link: str, cs: bool) -> dict:
    """Tělo potvrzovacího e-mailu pro Brevo."""
    if cs:
        subject = "SmartestGuide API: potvrďte e-mail a vyzvedněte si zkušební klíč"
        lines = [f"Dobrý den, {name},", "děkujeme za zájem o API SmartestGuide. Kliknutím na tlačítko potvrdíte e-mail a na stránce uvidíte svůj zkušební klíč.",
                 "Klíč se zobrazí jen jednou, tak si ho hned uložte. Odkaz platí 48 hodin.",
                 "Pokud jste o klíč nežádali, e-mail prostě smažte – nic se nestane."]
        btn = "Potvrdit e-mail a zobrazit klíč"
    else:
        subject = "SmartestGuide API: confirm your e-mail to get your sandbox key"
        lines = [f"Hello {name},", "thanks for your interest in the SmartestGuide API. Click the button to confirm your e-mail – the page will then show your sandbox key.",
                 "The key is shown only once, so save it right away. The link is valid for 48 hours.",
                 "If you did not ask for a key, just delete this e-mail – nothing will happen."]
        btn = "Confirm e-mail and show my key"
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#22324d">{esc(x)}</p>' for x in lines[:2])
    body += (f'<p style="margin:22px 0"><a href="{esc(link)}" style="display:inline-block;background:#2c5fae;color:#ffffff;text-decoration:none;'
             f'border-radius:99px;padding:13px 26px;font-weight:700;font-size:15px">{esc(btn)}</a></p>')
    body += "".join(f'<p style="margin:0 0 12px;font-size:13.5px;line-height:1.6;color:#51617e">{esc(x)}</p>' for x in lines[2:])
    html = ('<div style="background:#f6f8fc;padding:32px 16px;font-family:Arial,Helvetica,sans-serif"><div style="max-width:520px;margin:0 auto;'
            'background:#ffffff;border:1px solid #e3e9f4;border-radius:16px;padding:30px 28px">'
            '<div style="font-weight:800;font-size:18px;color:#0c1b33;margin-bottom:18px">SmartestGuide API</div>' + body + '</div></div>')
    return {"sender": {"name": "SMARTEST GUIDE", "email": "admin@smartestguide.com"}, "to": [{"email": to_email, "name": name}],
            "subject": subject, "htmlContent": html}


def openapi(base: str) -> dict:
    """Popis API ve formátu OpenAPI 3.0 (pro Postman, generátory klientů apod.)."""
    err = {"type": "object", "properties": {"error": {"type": "object", "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}}
    def op(summary, scope, body=None, resp=None, params=None, created=False):
        o = {"summary": summary, "security": [{"bearer": []}],
             "description": ("Required scope: `" + scope + "`.") if scope else "Works with any valid key.",
             "responses": {("201" if created else "200"): {"description": "OK", "content": {"application/json": {"schema": resp or {"type": "object"}}}},
                           "401": {"description": "Missing, unknown or revoked key", "content": {"application/json": {"schema": err}}},
                           "403": {"description": "Key lacks the required scope", "content": {"application/json": {"schema": err}}},
                           "429": {"description": "Rate or daily limit reached", "content": {"application/json": {"schema": err}}}}}
        if body:
            o["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
        if params:
            o["parameters"] = params
        return o
    S = lambda **p: {"type": "object", "properties": p}
    s, i, a = {"type": "string"}, {"type": "integer"}, lambda x: {"type": "array", "items": x}
    msg = S(role={"type": "string", "enum": ["user", "assistant"]}, content=s)
    hit = S(title=s, section=s, text=s, url=s, audience=s, similarity={"type": "number", "nullable": True})
    doc = S(id=s, title=s, audience=s, enabled={"type": "boolean"}, chars=i, chunks=i, source=s, updated_at=s)
    return {
        "openapi": "3.0.3",
        "info": {"title": "SmartestGuide API", "version": VERSION,
                 "description": "AI concierge and hotel knowledge base as a service. Keys: `sg_live_…` (issued by a hotel in its portal) "
                                "or `sg_test_…` (sandbox, from " + base + "/developers)."},
        "servers": [{"url": base + "/api/v1"}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "paths": {
            "/me": {"get": op("Key, hotel and today's usage", "")},
            "/hotel": {"get": op("Basic hotel profile and the guest app link", "")},
            "/chat": {"post": op("Ask the AI concierge on behalf of a guest", "chat",
                                 {**S(message=s, language={"type": "string", "description": "ISO code, e.g. en, cs, de. Optional – detected from the message."},
                                      history=a(msg), session={"type": "string", "description": "Your own id of the guest conversation (optional)."}),
                                  "required": ["message"]}, S(reply=s, language={"type": "string", "nullable": True}))},
            "/knowledge/search": {"post": op("Find the most relevant passages in the hotel's knowledge base", "knowledge:read",
                                             {**S(query=s, audience={"type": "string", "enum": ["guest", "staff"], "default": "guest"},
                                                  limit={"type": "integer", "minimum": 1, "maximum": 10}), "required": ["query"]},
                                             S(query=s, results=a(hit), context={"type": "string", "description": "The passages formatted for direct use in an LLM prompt."}))},
            "/knowledge/documents": {
                "get": op("List documents in the knowledge base", "knowledge:read", resp=S(documents=a(doc))),
                "post": op("Create or replace a text document", "knowledge:write",
                           {**S(title=s, text=s, audience={"type": "string", "enum": ["guest", "staff"], "default": "staff"},
                                id={"type": "string", "description": "Your own stable id; sending it again replaces the document."}),
                            "required": ["title", "text"]}, S(id=s, title=s, audience=s, chunks=i), created=True)},
            "/knowledge/documents/{id}": {"delete": op("Delete a document", "knowledge:write", resp=S(deleted={"type": "boolean"}, id=s),
                                                       params=[{"name": "id", "in": "path", "required": True, "schema": s}])},
        },
    }
