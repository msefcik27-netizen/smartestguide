"""Napojení znalostní báze na cloudové disky: Google Drive, OneDrive/SharePoint, Dropbox, Box.

Jednotné rozhraní nad čtyřmi službami: přihlášení (OAuth 2.0, jen čtení), procházení složek,
stažení souboru a synchronizace vybraných složek do znalostní báze (kb.py).

Nastavení (proměnné prostředí, nic se neukládá do repozitáře):
  KB_GOOGLE_CLIENT_ID / KB_GOOGLE_CLIENT_SECRET      – Google Cloud Console, OAuth klient „Web application“
  KB_MS_CLIENT_ID / KB_MS_CLIENT_SECRET              – Microsoft Entra, registrace aplikace (účty: libovolná organizace + osobní)
  KB_DROPBOX_CLIENT_ID / KB_DROPBOX_CLIENT_SECRET    – Dropbox App Console (App key / App secret)
  KB_BOX_CLIENT_ID / KB_BOX_CLIENT_SECRET            – Box Developer Console (Custom App, OAuth 2.0)
  KB_GOOGLE_SA_JSON                                  – Google Cloud, klíč servisního účtu (JSON, případně v base64);
                                                       zapíná „Google Drive – sdílená složka“: hotel složku nasdílí na
                                                       adresu servisního účtu a aplikace vidí jen ji, bez přihlašování
  KB_SECRET                                          – volitelné heslo k šifrování uložených tokenů
U všech služeb je návratová adresa stejná:  {BASE_URL}/api/kb/oauth/callback
Služba bez nastavených klíčů se v portálu ukáže jako „připravujeme“.
"""
import os, re, json, time, base64, hashlib, logging, asyncio
from urllib.parse import urlencode, quote

import kb as kbmod

PROVIDERS = {
    "google": {"name": "Google Drive", "env": "KB_GOOGLE", "icon": "🟢"},
    "gshare": {"name": "Google Drive – sdílená složka", "env": "KB_GOOGLE_SA", "icon": "🟢"},
    "microsoft": {"name": "OneDrive / SharePoint", "env": "KB_MS", "icon": "🔷"},
    "dropbox": {"name": "Dropbox", "env": "KB_DROPBOX", "icon": "🟦"},
    "box": {"name": "Box", "env": "KB_BOX", "icon": "📦"},
}

MAX_FILES_PER_SOURCE = 300
MAX_DEPTH = 6
SYNC_EVERY_H = 6

GOOGLE_EXPORT = {   # nativní dokumenty Googlu se stahují jako Office soubory
    "application/vnd.google-apps.document": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
}


class CloudError(Exception):
    """Chyba vůči cloudové službě s textem, který lze ukázat hotelu."""


def creds(provider: str) -> tuple:
    env = PROVIDERS[provider]["env"]
    return os.getenv(env + "_CLIENT_ID", "").strip(), os.getenv(env + "_CLIENT_SECRET", "").strip()


def configured(provider: str) -> bool:
    if provider == "gshare":
        return bool(sa_info())
    return provider in PROVIDERS and all(creds(provider))


# ─────────────────────────────────────────────────────────────
# Google Drive přes sdílenou složku (servisní účet)
# ─────────────────────────────────────────────────────────────
def sa_info() -> dict:
    """Klíč servisního účtu z prostředí; {} když není nastavený nebo nejde přečíst."""
    raw = os.getenv("KB_GOOGLE_SA_JSON", "").strip()
    if not raw:
        return {}
    try:
        if not raw.startswith("{"):
            raw = base64.b64decode(raw).decode()
        j = json.loads(raw)
    except Exception:
        return {}
    return j if isinstance(j, dict) and j.get("client_email") and j.get("private_key") else {}


def share_email() -> str:
    """Adresa, na kterou hotel složku nasdílí."""
    return sa_info().get("client_email", "")


def drive_id_from_link(link: str) -> str:
    """Odkaz na složku či soubor na Google Disku (nebo holé ID) → ID položky; '' když ho nenajdeme."""
    s = (link or "").strip()
    m = re.search(r"/(?:folders|d)/([A-Za-z0-9_-]{10,})", s) or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", s)
    if m:
        return m.group(1)
    return s if re.fullmatch(r"[A-Za-z0-9_-]{15,}", s) else ""


_sa_cache = {}


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


async def sa_tokens(client) -> dict:
    """Přístupový token servisního účtu (jen čtení Disku); drží se v paměti do vypršení."""
    info = sa_info()
    if not info:
        raise CloudError("Sdílení složek z Google Disku zatím není zapnuté.")
    c = _sa_cache.get(info["client_email"])
    if c and c.get("expires_at", 0) > time.time() + 120:
        return c
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        uri = info.get("token_uri") or _AUTH["google"][1]
        now = int(time.time())
        body = _b64u(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()) + "." + _b64u(json.dumps(
            {"iss": info["client_email"], "scope": _SCOPE["google"], "aud": uri, "iat": now, "exp": now + 3600}).encode())
        key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
        jwt = body + "." + _b64u(key.sign(body.encode(), padding.PKCS1v15(), hashes.SHA256()))
    except Exception:
        logging.exception("KB: klíč servisního účtu Google nejde použít")
        raise CloudError("Sdílení složek z Google Disku teď nefunguje. Dejte nám prosím vědět.")
    r = await client.post(uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt}, timeout=30)
    if r.status_code != 200:
        logging.warning("KB servisní účet Google: %s %s", r.status_code, r.text[:300])
        raise CloudError("Sdílení složek z Google Disku teď nefunguje. Dejte nám prosím vědět.")
    t = _tokens_from(r.json())
    _sa_cache[info["client_email"]] = t
    return t


# ─────────────────────────────────────────────────────────────
# Šifrování tokenů v databázi
# ─────────────────────────────────────────────────────────────
def _fernet(secret: str):
    if not secret:
        return None
    try:
        from cryptography.fernet import Fernet
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("sg-kb-v1|" + secret).encode()).digest()))
    except Exception:
        return None


def seal(tokens: dict, secret: str) -> str:
    raw = json.dumps(tokens)
    f = _fernet(secret)
    return "f:" + f.encrypt(raw.encode()).decode() if f else "p:" + raw


def unseal(blob: str, secret: str) -> dict:
    blob = blob or ""
    try:
        if blob.startswith("f:"):
            f = _fernet(secret)
            return json.loads(f.decrypt(blob[2:].encode()).decode()) if f else {}
        if blob.startswith("p:"):
            return json.loads(blob[2:])
    except Exception:
        logging.warning("KB: uložené tokeny nejdou přečíst (změnil se šifrovací klíč?)")
    return {}


# ─────────────────────────────────────────────────────────────
# OAuth
# ─────────────────────────────────────────────────────────────
_AUTH = {
    "google": ("https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token"),
    "microsoft": ("https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                  "https://login.microsoftonline.com/common/oauth2/v2.0/token"),
    "dropbox": ("https://www.dropbox.com/oauth2/authorize", "https://api.dropboxapi.com/oauth2/token"),
    "box": ("https://account.box.com/api/oauth2/authorize", "https://api.box.com/oauth2/token"),
}
_SCOPE = {
    "google": "https://www.googleapis.com/auth/drive.readonly",
    "microsoft": "offline_access User.Read Files.Read.All",
}


def auth_url(provider: str, redirect_uri: str, state: str) -> str:
    cid, _ = creds(provider)
    q = {"client_id": cid, "redirect_uri": redirect_uri, "response_type": "code", "state": state}
    if provider == "google":
        q.update(scope=_SCOPE["google"], access_type="offline", prompt="consent", include_granted_scopes="true")
    elif provider == "microsoft":
        q.update(scope=_SCOPE["microsoft"], response_mode="query", prompt="select_account")
    elif provider == "dropbox":
        q.update(token_access_type="offline")
    return _AUTH[provider][0] + "?" + urlencode(q)


def _tokens_from(resp: dict, old: dict = None) -> dict:
    old = old or {}
    return {"access_token": resp.get("access_token", ""),
            "refresh_token": resp.get("refresh_token") or old.get("refresh_token", ""),
            "expires_at": time.time() + int(resp.get("expires_in") or 3600) - 60}


async def _token_call(client, provider: str, data: dict) -> dict:
    cid, sec = creds(provider)
    data = dict(data, client_id=cid, client_secret=sec)
    if provider == "microsoft":
        data["scope"] = _SCOPE["microsoft"]
    r = await client.post(_AUTH[provider][1], data=data, timeout=30)
    if r.status_code != 200:
        logging.warning("KB OAuth %s: %s %s", provider, r.status_code, r.text[:300])
        raise CloudError("Přihlášení ke službě se nepodařilo. Zkuste disk připojit znovu.")
    return r.json()


async def exchange_code(client, provider: str, code: str, redirect_uri: str) -> dict:
    return _tokens_from(await _token_call(client, provider, {"grant_type": "authorization_code", "code": code,
                                                             "redirect_uri": redirect_uri}))


async def fresh_tokens(client, provider: str, tokens: dict) -> tuple:
    """Vrátí (tokeny, změněno). Obnoví přístupový token, když končí."""
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time() + 120:
        return tokens, False
    if not tokens.get("refresh_token"):
        raise CloudError("Přihlášení vypršelo. Připojte disk znovu.")
    return _tokens_from(await _token_call(client, provider, {"grant_type": "refresh_token",
                                                             "refresh_token": tokens["refresh_token"]}), tokens), True


# ─────────────────────────────────────────────────────────────
# Volání služeb
# ─────────────────────────────────────────────────────────────
async def _req(client, method: str, url: str, tokens: dict, **kw):
    headers = dict(kw.pop("headers", {}) or {})
    headers["Authorization"] = "Bearer " + tokens.get("access_token", "")
    r = None
    for attempt in range(3):
        r = await client.request(method, url, headers=headers, timeout=kw.pop("timeout", 60), follow_redirects=True, **kw)
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            await asyncio.sleep(min(float(r.headers.get("retry-after") or 0) or 1.5 * (attempt + 1), 10))
            continue
        break
    if r.status_code == 401:
        raise CloudError("Služba odmítla přihlášení. Připojte disk znovu.")
    if r.status_code == 403:
        raise CloudError("K této položce nemá připojený účet přístup.")
    if r.status_code == 404:
        raise CloudError("Položka na disku už neexistuje.")
    if r.status_code >= 400:
        logging.warning("KB cloud %s %s: %s %s", method, url.split("?")[0], r.status_code, r.text[:300])
        raise CloudError("Služba vrátila chybu (%s). Zkuste to později." % r.status_code)
    return r


def _item(id_, name, is_folder, mime="", size=0, etag="", url="", modified=""):
    return {"id": str(id_), "name": name or "", "is_folder": bool(is_folder), "mime": mime or "", "size": int(size or 0),
            "etag": str(etag or ""), "url": url or "", "modified": modified or ""}


# — Google Drive —
_G = "https://www.googleapis.com/drive/v3"
_G_FIELDS = "id,name,mimeType,size,modifiedTime,md5Checksum,version,webViewLink"


def _g_item(f: dict) -> dict:
    return _item(f["id"], f.get("name"), f.get("mimeType") == "application/vnd.google-apps.folder", f.get("mimeType"),
                 f.get("size"), f.get("md5Checksum") or f.get("version") or f.get("modifiedTime"), f.get("webViewLink"),
                 f.get("modifiedTime"))


async def _g_list(client, tokens, folder_id):
    out = []
    if folder_id in ("", "root"):
        out.append(_item("__shared", "Sdílené se mnou", True))
        try:
            r = await _req(client, "GET", _G + "/drives?pageSize=100&fields=drives(id,name)", tokens)
            out += [_item(d["id"], "Sdílený disk: " + d.get("name", ""), True) for d in r.json().get("drives", [])]
        except CloudError:
            pass
        q = "'root' in parents and trashed=false"
    elif folder_id == "__shared":
        q = "sharedWithMe and trashed=false"
    else:
        q = "'%s' in parents and trashed=false" % folder_id.replace("'", "")
    page = ""
    while len(out) < 2000:
        url = (_G + "/files?" + urlencode({"q": q, "pageSize": 200, "orderBy": "folder,name", "supportsAllDrives": "true",
                                           "includeItemsFromAllDrives": "true",
                                           "fields": "nextPageToken,files(%s)" % _G_FIELDS}) + ("&pageToken=" + quote(page) if page else ""))
        j = (await _req(client, "GET", url, tokens)).json()
        out += [_g_item(f) for f in j.get("files", [])]
        page = j.get("nextPageToken", "")
        if not page:
            break
    return out


async def _g_get(client, tokens, item_id):
    r = await _req(client, "GET", _G + "/files/%s?supportsAllDrives=true&fields=%s" % (quote(item_id), _G_FIELDS), tokens)
    return _g_item(r.json())


async def _g_download(client, tokens, it):
    exp = GOOGLE_EXPORT.get(it["mime"])
    if exp:
        r = await _req(client, "GET", _G + "/files/%s/export?mimeType=%s" % (quote(it["id"]), quote(exp[0])), tokens, timeout=120)
        return r.content, it["name"] + exp[1], exp[0]
    r = await _req(client, "GET", _G + "/files/%s?alt=media&supportsAllDrives=true" % quote(it["id"]), tokens, timeout=120)
    return r.content, it["name"], it["mime"]


async def _g_account(client, tokens):
    r = await _req(client, "GET", _G + "/about?fields=user(emailAddress,displayName)", tokens)
    u = r.json().get("user", {})
    return u.get("emailAddress") or u.get("displayName") or ""


async def _gs_list(client, tokens, folder_id):
    """Sdílená složka: kořen se neprochází – servisní účet je společný všem hotelům a ukázal by i cizí složky."""
    if folder_id in ("", "root", "__shared"):
        return []
    return await _g_list(client, tokens, folder_id)


async def g_ancestors(client, tokens, item_id: str) -> list:
    """ID nadřazených složek, kam až účet dohlédne (nad nasdílenou složku nevidí)."""
    out, cur = [], item_id
    for _ in range(MAX_DEPTH * 2):
        try:
            r = await _req(client, "GET", _G + "/files/%s?supportsAllDrives=true&fields=parents" % quote(cur), tokens)
        except CloudError:
            break
        par = r.json().get("parents") or []
        if not par or par[0] in out:
            break
        cur = par[0]
        out.append(cur)
    return out


async def _gs_account(client, tokens):
    return ""


# — Microsoft OneDrive / SharePoint (Graph) —
_M = "https://graph.microsoft.com/v1.0"
_M_SEL = "$select=id,name,size,file,folder,eTag,cTag,webUrl,lastModifiedDateTime,parentReference,remoteItem&$top=200"


def _m_item(f: dict) -> dict:
    src = f.get("remoteItem") or f       # položka sdílená z cizího disku ukazuje na originál
    drive = (src.get("parentReference") or {}).get("driveId", "")
    return _item(drive + "|" + src.get("id", ""), f.get("name") or src.get("name"), "folder" in src,
                 (src.get("file") or {}).get("mimeType", ""), src.get("size"), src.get("cTag") or src.get("eTag"),
                 src.get("webUrl") or f.get("webUrl"), src.get("lastModifiedDateTime"))


def _m_path(item_id: str) -> str:
    drive, _, iid = item_id.partition("|")
    return "/drives/%s/items/%s" % (quote(drive), quote(iid))


async def _m_list(client, tokens, folder_id):
    out = []
    if folder_id in ("", "root"):
        out.append(_item("__shared", "Sdílené se mnou", True))
        url = _M + "/me/drive/root/children?" + _M_SEL
    elif folder_id == "__shared":
        url = _M + "/me/drive/sharedWithMe?$top=200"
    else:
        url = _M + _m_path(folder_id) + "/children?" + _M_SEL
    while url and len(out) < 2000:
        j = (await _req(client, "GET", url, tokens)).json()
        out += [_m_item(f) for f in j.get("value", [])]
        url = j.get("@odata.nextLink", "")
    return sorted(out, key=lambda i: (not i["is_folder"], i["name"].lower()))


async def _m_get(client, tokens, item_id):
    return _m_item((await _req(client, "GET", _M + _m_path(item_id), tokens)).json())


async def _m_download(client, tokens, it):
    r = await _req(client, "GET", _M + _m_path(it["id"]) + "/content", tokens, timeout=120)
    return r.content, it["name"], it["mime"]


async def _m_account(client, tokens):
    u = (await _req(client, "GET", _M + "/me?$select=mail,userPrincipalName,displayName", tokens)).json()
    return u.get("mail") or u.get("userPrincipalName") or u.get("displayName") or ""


async def resolve_share_link(client, tokens, link: str) -> dict:
    """SharePoint / OneDrive: sdílecí odkaz (nebo adresa složky v knihovně) → položka disku."""
    enc = "u!" + base64.urlsafe_b64encode(link.strip().encode()).decode().rstrip("=")
    r = await _req(client, "GET", _M + "/shares/%s/driveItem?$select=id,name,size,file,folder,eTag,cTag,webUrl,lastModifiedDateTime,parentReference" % enc, tokens)
    return _m_item(r.json())


# — Dropbox —
_D = "https://api.dropboxapi.com/2"


def _d_item(e: dict) -> dict:
    return _item(e.get("id") or e.get("path_lower", ""), e.get("name"), e.get(".tag") == "folder", "", e.get("size"),
                 e.get("content_hash") or e.get("rev"), "https://www.dropbox.com/home" + quote(e.get("path_display", "")),
                 e.get("server_modified"))


async def _d_list(client, tokens, folder_id):
    path = "" if folder_id in ("", "root") else folder_id
    r = await _req(client, "POST", _D + "/files/list_folder", tokens, json={"path": path, "limit": 500})
    j, out = r.json(), []
    while True:
        out += [_d_item(e) for e in j.get("entries", []) if e.get(".tag") in ("file", "folder")]
        if not j.get("has_more") or len(out) >= 2000:
            break
        j = (await _req(client, "POST", _D + "/files/list_folder/continue", tokens, json={"cursor": j["cursor"]})).json()
    return sorted(out, key=lambda i: (not i["is_folder"], i["name"].lower()))


async def _d_get(client, tokens, item_id):
    return _d_item((await _req(client, "POST", _D + "/files/get_metadata", tokens, json={"path": item_id})).json())


async def _d_download(client, tokens, it):
    r = await _req(client, "POST", "https://content.dropboxapi.com/2/files/download", tokens,
                   headers={"Dropbox-API-Arg": json.dumps({"path": it["id"]})}, timeout=120)
    return r.content, it["name"], ""


async def _d_account(client, tokens):
    r = await _req(client, "POST", _D + "/users/get_current_account", tokens, content=b"null",
                   headers={"Content-Type": "application/json"})
    return r.json().get("email", "")


# — Box —
_B = "https://api.box.com/2.0"


def _b_item(e: dict) -> dict:
    is_folder = e.get("type") == "folder"
    return _item(e.get("id"), e.get("name"), is_folder, "", e.get("size"), e.get("sha1") or e.get("etag"),
                 "https://app.box.com/%s/%s" % ("folder" if is_folder else "file", e.get("id")), e.get("modified_at"))


async def _b_list(client, tokens, folder_id):
    fid = "0" if folder_id in ("", "root") else folder_id
    out, off = [], 0
    while len(out) < 2000:
        j = (await _req(client, "GET", _B + "/folders/%s/items?fields=id,type,name,size,modified_at,etag,sha1&limit=1000&offset=%d"
                        % (quote(fid), off), tokens)).json()
        ent = j.get("entries", [])
        out += [_b_item(e) for e in ent if e.get("type") in ("file", "folder")]
        off += len(ent)
        if not ent or off >= int(j.get("total_count") or 0):
            break
    return sorted(out, key=lambda i: (not i["is_folder"], i["name"].lower()))


async def _b_get(client, tokens, item_id):
    return _b_item((await _req(client, "GET", _B + "/files/%s?fields=id,type,name,size,modified_at,etag,sha1" % quote(item_id), tokens)).json())


async def _b_download(client, tokens, it):
    r = await _req(client, "GET", _B + "/files/%s/content" % quote(it["id"]), tokens, timeout=120)
    return r.content, it["name"], ""


async def _b_account(client, tokens):
    return (await _req(client, "GET", _B + "/users/me?fields=login,name", tokens)).json().get("login", "")


_API = {
    "google": (_g_list, _g_get, _g_download, _g_account),
    "gshare": (_gs_list, _g_get, _g_download, _gs_account),
    "microsoft": (_m_list, _m_get, _m_download, _m_account),
    "dropbox": (_d_list, _d_get, _d_download, _d_account),
    "box": (_b_list, _b_get, _b_download, _b_account),
}


async def list_folder(client, provider, tokens, folder_id=""):
    return await _API[provider][0](client, tokens, folder_id or "")


async def get_item(client, provider, tokens, item_id):
    return await _API[provider][1](client, tokens, item_id)


async def download(client, provider, tokens, it):
    return await _API[provider][2](client, tokens, it)


async def account(client, provider, tokens):
    try:
        return await _API[provider][3](client, tokens)
    except Exception:
        return ""


def supported(it: dict) -> bool:
    if it["mime"] in GOOGLE_EXPORT:
        return True
    return kbmod.file_ext(it["name"]) in kbmod.SUPPORTED_EXT


# ─────────────────────────────────────────────────────────────
# Synchronizace zdroje do znalostní báze
# ─────────────────────────────────────────────────────────────
_running = set()


def source_row(r) -> dict:
    return {"id": r[0], "hotel_id": r[1], "provider": r[2], "label": r[3] or "", "account": r[4] or "",
            "audience": r[5] or "staff", "folders": json.loads(r[6] or "[]"), "tokens": r[7] or "",
            "status": r[8] or "", "last_sync": r[9] or "", "last_error": r[10] or "", "created_at": r[11] or ""}


_SRC_COLS = "id, hotel_id, provider, label, account, audience, folders, tokens, status, last_sync, last_error, created_at"


def get_source(store, source_id: str, hotel_id: str = ""):
    r = store.x("SELECT " + _SRC_COLS + " FROM kb_sources WHERE id=%s", (source_id,), "one")
    s = source_row(r) if r else None
    return s if s and (not hotel_id or s["hotel_id"] == hotel_id) else None


def list_sources(store, hotel_id: str = "") -> list:
    if hotel_id:
        rows = store.x("SELECT " + _SRC_COLS + " FROM kb_sources WHERE hotel_id=%s ORDER BY created_at", (hotel_id,), "all")
    else:
        rows = store.x("SELECT " + _SRC_COLS + " FROM kb_sources", (), "all")
    return [source_row(r) for r in rows or []]


async def source_tokens(client, store, src: dict, secret: str) -> dict:
    """Platné tokeny zdroje; obnovené se hned uloží (Box refresh token po použití mění)."""
    if src["provider"] == "gshare":
        return await sa_tokens(client)
    tokens, changed = await fresh_tokens(client, src["provider"], unseal(src["tokens"], secret))
    if changed:
        src["tokens"] = seal(tokens, secret)
        store.x("UPDATE kb_sources SET tokens=%s WHERE id=%s", (src["tokens"], src["id"]))
    return tokens


async def _walk(client, provider, tokens, sel: dict, found: list, depth: int = 0, path: str = ""):
    if len(found) >= MAX_FILES_PER_SOURCE:
        return
    if not sel.get("is_folder"):
        it = await get_item(client, provider, tokens, sel["id"])
        found.append((it, path))
        return
    if depth > MAX_DEPTH:
        return
    here = (path + "/" if path else "") + sel.get("name", "")
    for it in await list_folder(client, provider, tokens, sel["id"]):
        if len(found) >= MAX_FILES_PER_SOURCE:
            return
        if it["is_folder"]:
            await _walk(client, provider, tokens, it, found, depth + 1, here)
        else:
            found.append((it, here))


async def sync_source(kb, store, src: dict, secret: str, client=None) -> dict:
    """Stáhne nové a změněné soubory z vybraných složek, přeindexuje je a smaže z báze ty, které z disku zmizely."""
    if src["id"] in _running:
        return {"ok": False, "error": "synchronizace už běží"}
    _running.add(src["id"])
    import httpx
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    stats = {"ok": True, "new": 0, "updated": 0, "unchanged": 0, "removed": 0, "skipped": 0, "failed": 0, "error": ""}
    try:
        store.x("UPDATE kb_sources SET status=%s WHERE id=%s", ("syncing", src["id"]))
        tokens = await source_tokens(client, store, src, secret)
        found = []
        for sel in src["folders"]:
            await _walk(client, src["provider"], tokens, sel, found)
        rows = store.x("SELECT id, ext_id, etag, status FROM kb_docs WHERE hotel_id=%s AND source_id=%s",
                       (src["hotel_id"], src["id"]), "all") or []
        have = {r[1]: (r[0], r[2], r[3]) for r in rows}
        seen = set()
        for it, path in found:
            if not supported(it) or it["size"] > kbmod.MAX_FILE_BYTES:
                stats["skipped"] += 1
                continue
            seen.add(it["id"])
            old = have.get(it["id"])
            if old and it["etag"] and old[1] == it["etag"]:
                stats["unchanged"] += 1
                continue
            try:
                data, name, mime = await download(client, src["provider"], tokens, it)
                res = await kb.put_document(src["hotel_id"], title=it["name"], data=data, name=name, mime=mime,
                                            audience=src["audience"], source_id=src["id"], ext_id=it["id"], path=path,
                                            url=it["url"], etag=it["etag"], client=client)
                if not res["id"]:
                    stats["failed"] += 1
                    stats["error"] = res["error"]
                    break               # limit báze – další soubory nemá smysl stahovat
                stats["updated" if old else "new"] += 1
            except CloudError as e:
                stats["failed"] += 1
                stats["error"] = str(e)
        for ext_id, (doc_id, _e, _s) in have.items():
            if ext_id not in seen:
                kb.delete_document(src["hotel_id"], doc_id)
                stats["removed"] += 1
        if len(found) >= MAX_FILES_PER_SOURCE:
            stats["error"] = stats["error"] or "načteno prvních %d souborů – vyberte užší složky" % MAX_FILES_PER_SOURCE
        store.x("UPDATE kb_sources SET status=%s, last_sync=%s, last_error=%s WHERE id=%s",
                ("ok", kbmod._now(), stats["error"][:300], src["id"]))
    except CloudError as e:
        stats.update(ok=False, error=str(e))
        store.x("UPDATE kb_sources SET status=%s, last_error=%s WHERE id=%s", ("error", str(e)[:300], src["id"]))
    except Exception as e:
        logging.exception("KB sync %s selhal", src["id"])
        stats.update(ok=False, error="synchronizace selhala")
        store.x("UPDATE kb_sources SET status=%s, last_error=%s WHERE id=%s", ("error", "synchronizace selhala", src["id"]))
    finally:
        _running.discard(src["id"])
        if own:
            await client.aclose()
    return stats


async def background_loop(kb, store, secret_fn):
    """Jednou za hodinu projde zdroje a synchronizuje ty, které nebyly obnoveny déle než SYNC_EVERY_H hodin."""
    from datetime import datetime, timedelta
    await asyncio.sleep(90)
    while True:
        try:
            limit = (datetime.utcnow() - timedelta(hours=SYNC_EVERY_H)).isoformat(timespec="seconds")
            for src in await asyncio.to_thread(list_sources, store):
                if src["folders"] and src["status"] != "syncing" and (src["last_sync"] or "") < limit and configured(src["provider"]):
                    await sync_source(kb, store, src, secret_fn())
                elif src["status"] == "syncing" and src["id"] not in _running:
                    store.x("UPDATE kb_sources SET status=%s WHERE id=%s", ("ok", src["id"]))   # zbytek po restartu
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("KB: smyčka synchronizace")
        await asyncio.sleep(3600)
