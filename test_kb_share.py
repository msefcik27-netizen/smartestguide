"""Testy napojení Google Disku přes sdílenou složku (servisní účet). Spuštění: python test_kb_share.py

Google je podvržený (httpx.MockTransport), klíč servisního účtu se generuje jen pro test.
"""
import asyncio, base64, json, os, sys, tempfile
from urllib.parse import parse_qs

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

import kb_cloud as cloud
import kb_routes as kbr

HOTELS = {"h1": {"id": "h1", "name": "Hotel Test", "hotel_token": "tok1"},
          "h2": {"id": "h2", "name": "Hotel Druhý", "hotel_token": "tok2"}}
SA_EMAIL = "kb-test@example.iam.gserviceaccount.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
SA_JSON = json.dumps({"client_email": SA_EMAIL, "token_uri": "https://oauth2.googleapis.com/token",
                      "private_key": KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()).decode()})
FOLDER, SUB, CIZI = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1SubSubSubSubSubSubSubSubSubSub01", "1CiziCiziCiziCiziCiziCiziCizi0001"
DIR = "application/vnd.google-apps.folder"
# co servisní účet „vidí“: id → (název, typ, rodič, obsah)
DRIVE = {FOLDER: ("Provozní dokumenty", DIR, "", b""),
         SUB: ("Wellness", DIR, FOLDER, b""),
         "f1": ("rad.txt", "text/plain", FOLDER, "Snídaně se podává od 7:00 do 10:30 v restauraci v přízemí.".encode()),
         "1FileFileFileFileFileFileFile0002": ("sauna.txt", "text/plain", SUB, "Sauna a vířivka jsou otevřené od 15:00 do 22:00.".encode())}
CALLS = {"token": 0, "jwt": None}
REAL_CLIENT = httpx.AsyncClient


def _meta(i):
    n, m, _p, c = DRIVE[i]
    return {"id": i, "name": n, "mimeType": m, "size": str(len(c)), "md5Checksum": "e" + i, "parents": [_p] if _p else [], "modifiedTime": "2026-11-01T10:00:00Z"}


def google(req: httpx.Request) -> httpx.Response:
    u = req.url
    if u.host == "oauth2.googleapis.com":
        CALLS["token"] += 1
        form = parse_qs(req.content.decode())
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
        CALLS["jwt"] = form["assertion"][0]
        return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
    assert req.headers["authorization"] == "Bearer at-1"
    if u.path == "/drive/v3/files":
        q = u.params["q"]
        assert "sharedWithMe" not in q and "'root'" not in q, "kořen společného účtu se nesmí procházet"
        parent = q.split("'")[1]
        return httpx.Response(200, json={"files": [_meta(i) for i, v in DRIVE.items() if v[2] == parent]})
    i = u.path.rsplit("/", 1)[-1]
    if i not in DRIVE:
        return httpx.Response(404, json={"error": {"message": "File not found"}})
    if u.params.get("alt") == "media":
        return httpx.Response(200, content=DRIVE[i][3])
    return httpx.Response(200, json=_meta(i))


def make(sa=True):
    os.environ.pop("KB_GOOGLE_SA_JSON", None)
    if sa:
        os.environ["KB_GOOGLE_SA_JSON"] = SA_JSON
    os.environ["KB_SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
    cloud._sa_cache.clear()
    CALLS.update(token=0, jwt=None)
    httpx.AsyncClient = lambda *a, **k: REAL_CLIENT(transport=httpx.MockTransport(google), **{x: y for x, y in k.items() if x != "transport"})
    kbr.D.clear()
    app = FastAPI()
    kbr.setup(app, db_load=lambda: {"hotels": HOTELS},
              find_hotel_by_token=lambda t: next((h for h in HOTELS.values() if h["hotel_token"] == t), None),
              resolve_hotel=lambda db, i: (i, db["hotels"].get(i)), admin_token=lambda: "", rate_limit_ok=lambda *a, **k: True,
              client_ip=lambda r: "1.2.3.4", get_base_url=lambda r: "https://x.test", staging_banner=lambda h: h,
              get_settings=lambda: {}, learn_block=lambda *a, **k: "", database_url="")
    kbr.KB.embed_key_fn = lambda: ""
    return TestClient(app)


def post(c, path, body, token="tok1"):
    return c.post("/api/hotel-portal/kb/" + path, params={"token": token}, json=body)


def connect(c, token="tok1"):
    r = post(c, "share-connect", {"audience": "guest"}, token)
    assert r.status_code == 200, r.text
    return r.json()["source"]


def save(c, sid, folders, token="tok1"):
    """Uloží složky; synchronizaci na pozadí nahradí prázdnou a skutečnou pustí hned, ať je test jednoznačný."""
    real = cloud.sync_source

    async def noop(*a, **k):
        return {}
    cloud.sync_source = noop
    try:
        r = post(c, "source-update", {"source": sid, "folders": folders}, token)
    finally:
        cloud.sync_source = real
    if r.status_code == 200:
        src = cloud.get_source(kbr.STORE, sid)
        return r, asyncio.run(cloud.sync_source(kbr.KB, kbr.STORE, src, kbr._secret()))
    return r, None


def test_link_to_id():
    f = cloud.drive_id_from_link
    assert f("https://drive.google.com/drive/folders/%s?usp=sharing" % FOLDER) == FOLDER
    assert f("https://drive.google.com/drive/u/0/folders/" + FOLDER) == FOLDER
    assert f("https://drive.google.com/open?id=" + FOLDER) == FOLDER
    assert f("https://docs.google.com/document/d/%s/edit" % FOLDER) == FOLDER
    assert f("  " + FOLDER + " ") == FOLDER
    assert f("https://example.com/neco") == "" and f("ahoj") == "" and f("") == ""


def test_off_without_key():
    c = make(sa=False)
    p = {x["id"]: x for x in c.get("/api/hotel-portal/kb/status", params={"token": "tok1"}).json()["providers"]}
    assert p["gshare"]["configured"] is False and p["gshare"]["share_email"] == ""
    assert post(c, "share-connect", {}).status_code == 400
    os.environ["KB_GOOGLE_SA_JSON"] = "tohle není klíč"
    assert cloud.configured("gshare") is False


def test_full_drive_only_for_listed_hotels():
    """Celý Disk (přihlášení účtem) je hotelům vypnutý; zapne se jen jmenovitě."""
    c = make()
    old = {k: os.environ.get(k) for k in ("KB_GOOGLE_CLIENT_ID", "KB_GOOGLE_CLIENT_SECRET", "KB_GOOGLE_FULL_HOTELS")}
    os.environ.update(KB_GOOGLE_CLIENT_ID="cid", KB_GOOGLE_CLIENT_SECRET="sec")
    os.environ.pop("KB_GOOGLE_FULL_HOTELS", None)

    def google_on(token):
        p = {x["id"]: x for x in c.get("/api/hotel-portal/kb/status", params={"token": token}).json()["providers"]}
        return p["google"]["configured"], p["gshare"]["configured"]

    def conn(token):
        return c.get("/api/hotel-portal/kb/connect", params={"token": token, "provider": "google"}).status_code
    try:
        assert cloud.configured("google") is True
        assert google_on("tok1") == (False, True) and conn("tok1") == 400, "bez povolení jen sdílená složka"
        os.environ["KB_GOOGLE_FULL_HOTELS"] = "jiny, h1"
        assert google_on("tok1") == (True, True) and conn("tok1") == 200
        assert google_on("tok2") == (False, True) and conn("tok2") == 400, "povolení jednoho hotelu neplatí pro druhý"
        os.environ["KB_GOOGLE_FULL_HOTELS"] = "*"
        assert google_on("tok2") == (True, True) and conn("tok2") == 200
        os.environ.pop("KB_GOOGLE_FULL_HOTELS")
        HOTELS["h2"]["kb_google_full"] = True
        assert google_on("tok2") == (True, True) and conn("tok2") == 200
        assert google_on("tok1") == (False, True)
    finally:
        HOTELS["h2"].pop("kb_google_full", None)
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_status_and_connect():
    c = make()
    p = {x["id"]: x for x in c.get("/api/hotel-portal/kb/status", params={"token": "tok1"}).json()["providers"]}
    assert p["gshare"]["configured"] is True and p["gshare"]["share_email"] == SA_EMAIL
    assert "share_email" not in p["google"]
    sid = connect(c)
    assert connect(c) == sid, "druhé kliknutí nezaloží druhý zdroj"
    s = c.get("/api/hotel-portal/kb/status", params={"token": "tok1"}).json()["sources"]
    assert len(s) == 1 and s[0]["provider"] == "gshare" and s[0]["audience"] == "guest" and s[0]["folders"] == []
    assert c.get("/api/hotel-portal/kb/connect", params={"token": "tok1", "provider": "gshare"}).status_code == 400
    assert post(c, "share-connect", {}, token="spatny").status_code == 401


def test_key_in_base64():
    make()
    os.environ["KB_GOOGLE_SA_JSON"] = base64.b64encode(SA_JSON.encode()).decode()
    assert cloud.share_email() == SA_EMAIL


def test_add_folder_and_sync():
    c = make()
    sid = connect(c)
    r = post(c, "share-link", {"source": sid, "link": "https://drive.google.com/drive/folders/%s?usp=sharing" % FOLDER})
    assert r.status_code == 200, r.text
    it = r.json()["item"]
    assert it["id"] == FOLDER and it["name"] == "Provozní dokumenty" and it["is_folder"] is True
    # podpis žádosti o token sedí s klíčem a žádá jen čtení
    head, claim, sig = CALLS["jwt"].split(".")
    KEY.public_key().verify(base64.urlsafe_b64decode(sig + "=="), (head + "." + claim).encode(), padding.PKCS1v15(), hashes.SHA256())
    cl = json.loads(base64.urlsafe_b64decode(claim + "=="))
    assert cl["iss"] == SA_EMAIL and cl["scope"].endswith("/drive.readonly") and cl["exp"] - cl["iat"] == 3600
    r, st = save(c, sid, [{"id": FOLDER, "name": "podvržený název", "is_folder": True}])
    assert r.status_code == 200, r.text
    assert st["ok"] and st["new"] == 2, st
    j = c.get("/api/hotel-portal/kb/status", params={"token": "tok1"}).json()
    assert j["sources"][0]["folders"] == [{"id": FOLDER, "name": "Provozní dokumenty", "is_folder": True}], "název bereme od Googlu"
    assert sorted(d["title"] for d in j["docs"]) == ["rad.txt", "sauna.txt"]
    assert all(d["audience"] == "guest" for d in j["docs"])
    hits = post(c, "search", {"q": "Do kdy je otevřená sauna?", "as": "guest"}).json()["hits"]
    assert hits and "22:00" in hits[0]["text"]
    assert CALLS["token"] == 1, "token se drží v paměti, ne pro každé volání nový"


def test_not_shared_and_bad_link():
    c = make()
    sid = connect(c)
    r = post(c, "share-link", {"source": sid, "link": "https://drive.google.com/drive/folders/" + CIZI})
    assert r.status_code == 502 and SA_EMAIL in r.json()["detail"], r.text
    assert post(c, "share-link", {"source": sid, "link": "https://example.com/slozka"}).status_code == 400
    r, _ = save(c, sid, [{"id": CIZI, "name": "x", "is_folder": True}])
    assert r.status_code == 502, "nenasdílenou složku nejde uložit ani mimo formulář"
    assert c.get("/api/hotel-portal/kb/status", params={"token": "tok1"}).json()["sources"][0]["folders"] == []


def test_other_hotel_cannot_take_folder():
    c = make()
    s1, s2 = connect(c), connect(c, "tok2")
    assert s1 != s2
    r, _ = save(c, s1, [{"id": FOLDER}])
    assert r.status_code == 200
    assert post(c, "share-link", {"source": s2, "link": FOLDER}, "tok2").status_code == 409
    r, _ = save(c, s2, [{"id": FOLDER, "name": "x", "is_folder": True}], "tok2")
    assert r.status_code == 409
    r, _ = save(c, s2, [{"id": SUB}], "tok2")
    assert r.status_code == 409, "ani podsložku složky jiného hotelu"
    r, _ = save(c, s2, [{"id": "1FileFileFileFileFileFileFile0002"}], "tok2")
    assert r.status_code == 409, "ani jednotlivý soubor z ní"
    assert c.get("/api/hotel-portal/kb/status", params={"token": "tok2"}).json()["docs"] == []
    assert post(c, "share-link", {"source": s1, "link": FOLDER}, "tok2").status_code == 404, "cizí zdroj"
    # první hotel si svou složku dál spravuje
    r, st = save(c, s1, [{"id": FOLDER, "name": "Provozní dokumenty", "is_folder": True}])
    assert r.status_code == 200 and st["unchanged"] == 2, st


def test_other_hotel_cannot_take_parent():
    c = make()
    s1, s2 = connect(c), connect(c, "tok2")
    r, _ = save(c, s1, [{"id": SUB}])
    assert r.status_code == 200
    assert kbr.STORE.x("SELECT folders FROM kb_sources WHERE id=%s", (s1,), "one")[0].count(FOLDER) == 1, "uložený řetěz nadřazených složek"
    r, _ = save(c, s2, [{"id": FOLDER}], "tok2")
    assert r.status_code == 409, "nadřazenou složku cizí složky"
    r, st = save(c, s1, [{"id": SUB}, {"id": FOLDER}])
    assert r.status_code == 200, "vlastní nadřazenou složku ano"


def test_no_browsing():
    c = make()
    sid = connect(c)
    for folder in ("", "root", "__shared", FOLDER):
        r = c.get("/api/hotel-portal/kb/browse", params={"token": "tok1", "source": sid, "folder": folder})
        assert r.status_code == 200 and r.json()["items"] == []
    assert asyncio.run(cloud.list_folder(None, "gshare", {}, "")) == []
    assert asyncio.run(cloud.list_folder(None, "gshare", {}, "__shared")) == []


def test_remove_folder_removes_docs():
    c = make()
    sid = connect(c)
    save(c, sid, [{"id": FOLDER}])
    r, st = save(c, sid, [])
    assert r.status_code == 200 and st["removed"] == 2, st
    assert c.get("/api/hotel-portal/kb/status", params={"token": "tok1"}).json()["docs"] == []


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
