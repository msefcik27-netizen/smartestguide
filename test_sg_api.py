"""Testy veřejného API a onboardingu vývojářů (sg_api.py). Spuštění: python test_sg_api.py

Běží proti prázdné SQLite v dočasné složce; e-maily se neodesílají, jen zachytávají.
"""
import os, re, sys, tempfile
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import kb
import kb_routes as kbr
import sg_api

HOTELS = {
    "h1": {"id": "h1", "name": "Hotel Test", "city": "Praha", "slug": "hotel-test", "hotel_token": "tok1"},
    "h2": {"id": "h2", "name": "Hotel Druhý", "hotel_token": "tok2"},
    "demo": {"id": "demo", "name": "DEMO Hotel", "is_demo": True, "hotel_token": "tokd"},
}
MAILS, CHATS, RL = [], [], {"ok": True}


def make():
    """Čistá aplikace s prázdnou databází."""
    MAILS.clear(); CHATS.clear(); RL["ok"] = True
    kbr.STORE = kb.Store("", os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
    kbr.KB = kb.KB(kbr.STORE, embed_key_fn=lambda: "")
    kbr.D.clear()
    kbr.D.update(find_hotel_by_token=lambda t: next((h for h in HOTELS.values() if h["hotel_token"] == t), None),
                 admin_token=lambda: "", db_load=lambda: {"hotels": HOTELS})
    sg_api.D.clear()

    async def chat(hid, msg, lang, hist, device, request):
        CHATS.append((hid, msg, lang, hist, device))
        if msg == "spadni":
            raise HTTPException(500, "interní podrobnost, která nesmí ven")
        return {"status": "ok", "reply": "Snídaně je od 7:00.", "lang": lang or "en"}

    async def send(email, name, link, cs):
        MAILS.append((email, name, link, cs))
        return True

    app = FastAPI()
    sg_api.setup(app, db_load=lambda: {"hotels": HOTELS},
                 resolve_hotel=lambda db, i: (i, db["hotels"].get(i)) if db["hotels"].get(i) else (None, None),
                 rate_limit_ok=lambda key, n=0, w=0: RL["ok"], client_ip=lambda r: "1.2.3.4",
                 get_base_url=lambda r: "https://x.test", staging_banner=lambda h: h,
                 guest_chat=chat, guest_url=lambda b, hid, h: f"{b}/h/{h.get('slug') or hid}", send_email=send)
    return TestClient(app)


def live(c, scopes=("chat", "knowledge:read", "knowledge:write"), token="tok1"):
    r = c.post("/api/hotel-portal/api-keys", params={"token": token}, json={"name": "PMS", "scopes": list(scopes)})
    assert r.status_code == 200, r.text
    return r.json()["key"]


def H(key):
    return {"Authorization": "Bearer " + key}


def err(r):
    return r.json()["error"]["code"]


def test_key_format_and_storage():
    c = make()
    key = live(c)
    assert re.fullmatch(r"sg_live_[0-9a-f]{40}", key)
    rows = kbr.STORE.x("SELECT key_hash, prefix FROM api_keys", (), "all")
    assert len(rows) == 1 and rows[0][0] != key and key not in str(rows) and rows[0][1] == key[:12]
    lst = c.get("/api/hotel-portal/api-keys", params={"token": "tok1"}).json()["keys"]
    assert lst[0]["name"] == "PMS" and lst[0]["prefix"] == key[:12] and "key" not in lst[0] and "key_hash" not in lst[0]
    assert lst[0]["scopes"] == ["chat", "knowledge:read", "knowledge:write"] and lst[0]["revoked"] is False


def test_portal_requires_hotel_token():
    c = make()
    assert c.get("/api/hotel-portal/api-keys").status_code == 401
    assert c.post("/api/hotel-portal/api-keys", params={"token": "nic"}, json={"name": "x"}).status_code == 401
    assert c.post("/api/hotel-portal/api-keys", params={"token": "tok1"}, json={"name": " "}).status_code == 400


def test_auth_errors():
    c = make()
    assert c.get("/api/v1/me").status_code == 401 and err(c.get("/api/v1/me")) == "missing_key"
    assert err(c.get("/api/v1/me", headers=H("abc"))) == "missing_key"
    r = c.get("/api/v1/me", headers=H("sg_live_" + "0" * 40))
    assert r.status_code == 401 and err(r) == "invalid_key"
    key = live(c)
    assert c.get("/api/v1/me", headers={"X-API-Key": key}).status_code == 200


def test_me_and_hotel():
    c = make()
    key = live(c)
    r = c.get("/api/v1/me", headers=H(key))
    j = r.json()
    assert j["hotel"] == {"id": "h1", "name": "Hotel Test"} and j["key"]["mode"] == "live" and j["limits"]["used_today"] == 1
    assert r.headers["X-RateLimit-Limit"] == "2000" and r.headers["X-RateLimit-Remaining"] == "1999"
    j = c.get("/api/v1/hotel", headers=H(key)).json()
    assert j["guest_url"] == "https://x.test/h/hotel-test" and j["city"] == "Praha" and j["knowledge"]["documents"] == 0
    assert "hotel_token" not in str(j)


def test_revoke_and_other_hotel():
    c = make()
    key = live(c)
    kid = c.get("/api/hotel-portal/api-keys", params={"token": "tok1"}).json()["keys"][0]["id"]
    assert c.post("/api/hotel-portal/api-keys/revoke", params={"token": "tok2"}, json={"id": kid}).status_code == 404   # cizí hotel
    assert c.get("/api/v1/me", headers=H(key)).status_code == 200
    assert c.post("/api/hotel-portal/api-keys/revoke", params={"token": "tok1"}, json={"id": kid}).status_code == 200
    r = c.get("/api/v1/me", headers=H(key))
    assert r.status_code == 401 and err(r) == "revoked_key"
    assert c.get("/api/hotel-portal/api-keys", params={"token": "tok2"}).json()["keys"] == []


def test_max_keys():
    c = make()
    for _ in range(sg_api.MAX_KEYS_PER_HOTEL):
        live(c)
    assert c.post("/api/hotel-portal/api-keys", params={"token": "tok1"}, json={"name": "navíc"}).status_code == 400
    live(c, token="tok2")   # jiný hotel má vlastní počet


def test_scopes():
    c = make()
    key = live(c, scopes=("knowledge:read",))
    r = c.post("/api/v1/chat", headers=H(key), json={"message": "ahoj"})
    assert r.status_code == 403 and err(r) == "missing_scope" and not CHATS
    r = c.post("/api/v1/knowledge/documents", headers=H(key), json={"title": "A", "text": "x" * 30})
    assert r.status_code == 403
    assert c.get("/api/v1/knowledge/documents", headers=H(key)).status_code == 200
    # neznámý scope se zahodí, prázdný výběr dostane bezpečné výchozí (bez zápisu)
    k2 = c.post("/api/hotel-portal/api-keys", params={"token": "tok1"}, json={"name": "y", "scopes": ["admin"]}).json()["key"]
    assert c.get("/api/v1/me", headers=H(k2)).json()["key"]["scopes"] == ["chat", "knowledge:read"]


def test_chat():
    c = make()
    key = live(c)
    r = c.post("/api/v1/chat", headers=H(key), json={"message": "Kdy je snídaně?", "language": "cs", "session": "s1",
                                                    "history": [{"role": "user", "content": "Dobrý den"}, {"role": "system", "content": "zlo"}, "x"]})
    assert r.status_code == 200 and r.json() == {"reply": "Snídaně je od 7:00.", "language": "cs"}
    hid, msg, lang, hist, device = CHATS[-1]
    assert hid == "h1" and lang == "cs" and hist == [{"role": "user", "content": "Dobrý den"}] and device.startswith("api:") and device.endswith(":s1")
    assert err(c.post("/api/v1/chat", headers=H(key), json={})) == "missing_message"
    assert err(c.post("/api/v1/chat", headers=H(key), json={"message": "x" * 2001})) == "message_too_long"
    assert err(c.post("/api/v1/chat", headers=H(key), content=b"neni json")) == "invalid_json"
    assert err(c.post("/api/v1/chat", headers=H(key), json=[1])) == "invalid_json"
    r = c.post("/api/v1/chat", headers=H(key), json={"message": "spadni"})
    assert r.status_code == 502 and err(r) == "chat_unavailable" and "podrobnost" not in r.text


def test_invalid_request_does_not_cost():
    c = make()
    key = live(c)
    for _ in range(3):
        c.post("/api/v1/chat", headers=H(key), json={})
    assert c.get("/api/v1/me", headers=H(key)).json()["limits"]["used_today"] == 1


def test_knowledge_roundtrip_and_isolation():
    c = make()
    key, other = live(c), live(c, token="tok2")
    r = c.post("/api/v1/knowledge/documents", headers=H(key),
               json={"id": "cenik 1", "title": "Ceník parkování", "audience": "guest",
                     "text": "Parkování v hotelové garáži stojí 450 Kč za noc. Rezervace na recepci."})
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    assert did == "api-cenik-1"
    r = c.post("/api/v1/knowledge/documents", headers=H(key),
               json={"title": "Postup při požáru", "text": "Při požáru vyhlaste poplach tlačítkem na chodbě a volejte 150."})
    sid = r.json()["id"]
    assert r.json()["audience"] == "staff"
    # stejné id = nahrazení, ne druhý dokument
    c.post("/api/v1/knowledge/documents", headers=H(key),
           json={"id": "cenik 1", "title": "Ceník parkování", "audience": "guest", "text": "Parkování v hotelové garáži stojí 500 Kč za noc."})
    docs = c.get("/api/v1/knowledge/documents", headers=H(key)).json()["documents"]
    assert sorted(d["id"] for d in docs) == sorted([did, sid])
    j = c.post("/api/v1/knowledge/search", headers=H(key), json={"query": "kolik stojí parkování"}).json()
    assert j["results"] and "500 Kč" in j["results"][0]["text"] and "500 Kč" in j["context"] and "450" not in j["context"]
    # host nevidí interní dokumenty, personál ano
    assert "150" not in str(c.post("/api/v1/knowledge/search", headers=H(key), json={"query": "postup při požáru"}).json()["results"])
    assert "150" in c.post("/api/v1/knowledge/search", headers=H(key), json={"query": "postup při požáru", "audience": "staff"}).json()["context"]
    # cizí hotel nevidí nic a nic nesmaže
    assert c.get("/api/v1/knowledge/documents", headers=H(other)).json()["documents"] == []
    assert c.post("/api/v1/knowledge/search", headers=H(other), json={"query": "kolik stojí parkování"}).json()["results"] == []
    assert c.delete("/api/v1/knowledge/documents/" + did, headers=H(other)).status_code == 404
    assert c.delete("/api/v1/knowledge/documents/" + did, headers=H(key)).json() == {"deleted": True, "id": did}
    assert c.delete("/api/v1/knowledge/documents/" + did, headers=H(key)).status_code == 404
    assert err(c.post("/api/v1/knowledge/search", headers=H(key), json={"query": ""})) == "missing_query"
    assert err(c.post("/api/v1/knowledge/search", headers=H(key), json={"query": "abc", "limit": "moc"})) == "invalid_limit"
    assert err(c.post("/api/v1/knowledge/documents", headers=H(key), json={"title": "", "text": "x"})) == "missing_fields"


def test_daily_limit():
    c = make()
    key = live(c)
    kbr.STORE.x("UPDATE api_keys SET daily_limit=3", ())
    for _ in range(3):
        assert c.get("/api/v1/me", headers=H(key)).status_code == 200
    r = c.get("/api/v1/me", headers=H(key))
    assert r.status_code == 429 and err(r) == "daily_limit" and r.headers["X-RateLimit-Remaining"] == "0"
    # včerejší spotřeba se nepočítá
    y = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    kbr.STORE.x("UPDATE api_usage SET day=%s", (y,))
    assert c.get("/api/v1/me", headers=H(key)).status_code == 200


def test_minute_limit():
    c = make()
    key = live(c)
    RL["ok"] = False
    r = c.get("/api/v1/me", headers=H(key))
    assert r.status_code == 429 and err(r) == "rate_limit" and r.headers["Retry-After"]


def test_archived_hotel():
    c = make()
    key = live(c, token="tok2")
    HOTELS["h2"]["archived"] = True
    try:
        r = c.get("/api/v1/me", headers=H(key))
        assert r.status_code == 403 and err(r) == "hotel_unavailable"
    finally:
        HOTELS["h2"].pop("archived")


def _register(c, email="dev@example.com", **kw):
    return c.post("/api/developers/register", json={"name": "Dev", "email": email, "terms": True, **kw})


def _token():
    return re.search(r"verify=([^&]+)", MAILS[-1][2]).group(1)


def test_onboarding_flow():
    c = make()
    assert c.get("/developers").status_code == 200 and "sandbox" in c.get("/developers").text.lower()
    assert _register(c, email="spatne").status_code == 400
    assert c.post("/api/developers/register", json={"name": "Dev", "email": "dev@example.com"}).status_code == 400   # bez podmínek
    assert not MAILS
    r = _register(c, email="Dev@Example.com", lang="cs")
    assert r.status_code == 200 and "key" not in r.text
    email, name, link, cs = MAILS[-1]
    assert email == "dev@example.com" and cs and link.startswith("https://x.test/developers?verify=") and link.endswith("&lang=cs")
    tok = _token()
    assert tok not in str(kbr.STORE.x("SELECT * FROM api_devs", (), "all"))   # v databázi je jen otisk
    assert c.post("/api/developers/verify", json={"token": "x" * 30}).status_code == 400
    j = c.post("/api/developers/verify", json={"token": tok}).json()
    key = j["key"]
    assert re.fullmatch(r"sg_test_[0-9a-f]{40}", key) and j["hotel"] == "DEMO Hotel" and j["per_day"] == 100
    assert c.post("/api/developers/verify", json={"token": tok}).status_code == 400   # odkaz je na jedno použití
    me = c.get("/api/v1/me", headers=H(key)).json()
    assert me["key"]["mode"] == "sandbox" and me["hotel"]["id"] == "demo" and me["limits"]["per_day"] == 100
    assert c.post("/api/v1/chat", headers=H(key), json={"message": "Hi"}).status_code == 200 and CHATS[-1][0] == "demo"
    # zkušební klíč nezapisuje a nevidí interní znalosti
    assert err(c.post("/api/v1/knowledge/documents", headers=H(key), json={"title": "A", "text": "x" * 30})) == "missing_scope"
    assert err(c.post("/api/v1/knowledge/search", headers=H(key), json={"query": "požár", "audience": "staff"})) == "sandbox_restricted"
    # zkušební klíče se neukazují v portálu ukázkového hotelu
    assert c.get("/api/hotel-portal/api-keys", params={"token": "tokd"}).json()["keys"] == []
    # nová registrace téhož e-mailu starý klíč zneplatní
    _register(c)
    k2 = c.post("/api/developers/verify", json={"token": _token()}).json()["key"]
    assert k2 != key and err(c.get("/api/v1/me", headers=H(key))) == "revoked_key"
    assert c.get("/api/v1/me", headers=H(k2)).status_code == 200


def test_onboarding_expiry_and_limits():
    c = make()
    _register(c)
    old = (datetime.utcnow() - timedelta(hours=sg_api.VERIFY_HOURS + 1)).isoformat()
    kbr.STORE.x("UPDATE api_devs SET created_at=%s", (old,))
    assert c.post("/api/developers/verify", json={"token": _token()}).status_code == 400
    RL["ok"] = False
    assert _register(c).status_code == 429


def test_onboarding_without_sandbox_or_mail():
    c = make()
    demo = HOTELS.pop("demo")
    try:
        assert _register(c).status_code == 503 and not MAILS
    finally:
        HOTELS["demo"] = demo
    async def fail(*a):
        return False
    sg_api.D["send_email"] = fail
    assert _register(c).status_code == 503


def test_verification_email_escapes():
    p = sg_api.verification_email("a@b.cz", "<b>Zlý</b>", "https://x.test/developers?verify=abc&lang=cs", True)
    assert "<b>Zlý" not in p["htmlContent"] and "&lt;b&gt;Zlý" in p["htmlContent"] and "verify=abc&amp;lang=cs" in p["htmlContent"]
    assert p["to"] == [{"email": "a@b.cz", "name": "<b>Zlý</b>"}] and "potvrďte" in p["subject"]
    assert "confirm" in sg_api.verification_email("a@b.cz", "A", "https://x", False)["subject"]


def test_openapi():
    c = make()
    j = c.get("/api/v1/openapi.json").json()
    assert j["servers"] == [{"url": "https://x.test/api/v1"}]
    assert set(j["paths"]) == {"/me", "/hotel", "/chat", "/knowledge/search", "/knowledge/documents", "/knowledge/documents/{id}"}
    # popis odpovídá skutečným cestám aplikace
    real = {r.path.replace("/api/v1", "").replace("{doc_id}", "{id}") for r in c.app.routes if getattr(r, "path", "").startswith("/api/v1/") and "openapi" not in r.path}
    assert real == set(j["paths"])


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:
                fails += 1
                import traceback
                print("FAIL", name, "-", repr(e))
                traceback.print_exc()
    print("selhalo:", fails)
    sys.exit(1 if fails else 0)
