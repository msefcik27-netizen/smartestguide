"""Napojení pro AI asistenty (MCP): host se na hotel zeptá přímo ve svém asistentovi (Claude, ChatGPT a další,
které umí vlastní vzdálené MCP konektory). Adresa serveru: <web>/mcp

Protokol: Model Context Protocol, přenos „Streamable HTTP" v nejjednodušší podobě – klient pošle POST s JSON-RPC,
server odpoví jedním JSON (žádný stream, žádná relace, žádná nová závislost). Server jen čte: nástroje nic nemění.

Nástroje:
  find_hotel(query)                 najde hotel podle názvu (a města)
  get_hotel_info(hotel)             název, město, web a odkaz na průvodce pro hosty
  ask_hotel(hotel, question, lang)  odpověď concierge Alex – stejná jako v aplikaci pro hosty

Odpověď Alex stojí peníze, proto jsou u ask_hotel limity (na adresu volajícího, na hotel a celkem).
Hotel lze z napojení vyřadit polem hotelu ai_assistants_off.

Registruje se z app.py voláním setup(app, ...). Jádro (handle, match_hotels) jsou čisté funkce, testy v test_sg_mcp.py.
"""
import hashlib
import html
import json
import logging
import re
import unicodedata

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

D = {}
SERVER_VERSION = "1.0.0"
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")   # od nejnovější
MAX_BODY = 64 * 1024
MAX_QUESTION = 1000
FIND_MAX = 5
ASK_PER_MIN_IP, ASK_PER_HOUR_HOTEL, ASK_PER_DAY_HOTEL, ASK_PER_HOUR_ALL = 6, 40, 200, 400
_STOP = {"hotel", "hotely", "hotelu", "the", "and", "pension", "penzion", "resort", "apartments", "apartmany", "spa"}

INSTRUCTIONS = ("SmartestGuide gives you verified information about hotels that use the SmartestGuide digital concierge. "
                "When the user asks about a specific hotel (breakfast, check-in, parking, wifi, services, tips nearby), "
                "call find_hotel with the hotel name, then ask_hotel with the returned id and the user's question. "
                "Answers come from the hotel itself; present them as the hotel's information and do not add facts of your own. "
                "If find_hotel returns nothing, the hotel does not use SmartestGuide – say so.")

TOOLS = [
    {"name": "find_hotel", "title": "Find a hotel",
     "description": "Find a hotel that uses SmartestGuide by its name (optionally with the city). Returns up to 5 matches "
                    "with the id to use in the other tools. Use the hotel's name, not just a city.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Hotel name, optionally with the city, e.g. 'Grandium Prague'."}},
         "required": ["query"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "get_hotel_info", "title": "Hotel basics",
     "description": "Basic facts about one hotel: name, city, country, website and the link to its guest guide.",
     "inputSchema": {"type": "object", "properties": {
         "hotel": {"type": "string", "description": "Hotel id returned by find_hotel."}},
         "required": ["hotel"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "ask_hotel", "title": "Ask the hotel's concierge",
     "description": "Ask the hotel's digital concierge a guest question (breakfast, check-in and check-out, parking, wifi, "
                    "services, restaurants and tips nearby). The answer is based on information provided by the hotel. "
                    "It cannot make bookings, orders or changes to a stay.",
     "inputSchema": {"type": "object", "properties": {
         "hotel": {"type": "string", "description": "Hotel id returned by find_hotel."},
         "question": {"type": "string", "description": "The guest's question, in any language.", "maxLength": MAX_QUESTION},
         "language": {"type": "string", "description": "Two-letter language code for the answer, e.g. 'en', 'cs', 'de'. "
                                                       "Optional; by default the language of the question is used."}},
         "required": ["hotel", "question"], "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]


class ToolError(Exception):
    """Chyba, kterou má vidět asistent (vrací se jako výsledek nástroje s isError)."""


def norm(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def listed(h: dict) -> bool:
    """Smí se hotel přes napojení najít? Jen živý hotel (ne archivovaný, ne vyřazený, s předplatným nebo zdarma)."""
    return bool(isinstance(h, dict) and h.get("name") and not h.get("archived") and not h.get("ai_assistants_off")
                and (h.get("subscription_active") or h.get("is_free")))


def match_hotels(hotels: dict, query: str, limit: int = FIND_MAX) -> list:
    """Hotely odpovídající dotazu. Aspoň jedno významné slovo dotazu (3+ znaky, ne „hotel") musí být ve VLASTNÍM JMÉNĚ
    hotelu (název bez města a obecných slov) – samotné město nestačí, aby se přes nástroj nedal vypsat seznam klientů.
    Ostatní slova dotazu musí být v názvu nebo městě; jedno slovo navíc (např. „Prague" místo „Praha") se odpustí jen
    tehdy, když dotaz obsahuje celé vlastní jméno hotelu."""
    words = [w for w in norm(query).split() if len(w) >= 3 and w not in _STOP]
    if not words:
        return []
    out = []
    for hid, h in (hotels or {}).items():
        if not listed(h):
            continue
        name, place = norm(h.get("name")), norm(f"{h.get('city', '')} {h.get('country', '')}")
        name_words, place_words = set(name.split()), set(place.split())
        own = {w for w in name_words if len(w) >= 3 and w not in _STOP}
        own = (own - place_words) or own                    # hotel pojmenovaný jen po městě se hledá jeho názvem
        in_name = [w for w in words if w in own]
        if not in_name:
            continue
        all_words = name_words | place_words
        extra = [w for w in words if not (w in all_words or any(x.startswith(w) for x in all_words))]
        if extra and not (len(extra) == 1 and own <= set(words)):
            continue
        out.append((-len(in_name), len(name), hid, h))
    out.sort(key=lambda x: x[:3])
    return [(hid, h) for _, _, hid, h in out[:limit]]


def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid, code: int, message: str):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


async def handle(msg, call_tool):
    """Jedna zpráva JSON-RPC → odpověď (slovník), nebo None u oznámení (zpráva bez id).
    call_tool(name, arguments) → text výsledku; ToolError = chyba pro asistenta."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method, rid, params = msg["method"], msg.get("id"), msg.get("params")
    params = params if isinstance(params, dict) else {}
    if "id" not in msg:                       # oznámení (notifications/initialized apod.) – bez odpovědi
        return None
    if method == "initialize":
        want = str(params.get("protocolVersion") or "")
        return _result(rid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "smartestguide", "title": "SmartestGuide", "version": SERVER_VERSION},
                             "instructions": INSTRUCTIONS})
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments")
        if name not in {t["name"] for t in TOOLS}:
            return _error(rid, -32602, f"Unknown tool: {str(name)[:60]}")
        try:
            return _result(rid, _text(await call_tool(name, args if isinstance(args, dict) else {})))
        except ToolError as e:
            return _result(rid, _text(str(e), True))
        except Exception as e:
            logging.warning("MCP nástroj %s selhal: %s", name, e)
            return _result(rid, _text("The hotel information is not available right now. Please try again later.", True))
    return _error(rid, -32601, f"Method not found: {method[:60]}")


def setup(app, **deps):
    """deps: db_load, resolve_hotel, rate_limit_ok, client_ip, get_base_url, guest_chat, guest_url."""
    D.update(deps)
    rl, ip = deps["rate_limit_ok"], deps["client_ip"]

    def _hotel(ident) -> tuple:
        hid, h = D["resolve_hotel"](D["db_load"](), str(ident or "").strip()[:80])
        if not h or not listed(h):
            raise ToolError("Hotel not found. Call find_hotel first and use the id it returns.")
        return hid, h

    def _info(request, hid, h) -> dict:
        return {"id": h.get("slug") or hid, "name": h.get("name", ""), "city": h.get("city", ""), "country": h.get("country", ""),
                "website": h.get("website") or h.get("url") or "",
                "guest_guide": D["guest_url"](D["get_base_url"](request), hid, h)}

    async def call_tool(request: Request, name: str, args: dict) -> str:
        who = ip(request)
        if name == "find_hotel":
            if not rl("mcpfind:" + who, 20, 60):
                raise ToolError("Too many searches. Try again in a minute.")
            found = match_hotels(D["db_load"]().get("hotels") or {}, str(args.get("query") or "")[:120])
            if not found:
                return ("No hotel using SmartestGuide matches this name. Search by the hotel's name "
                        "(a city alone is not enough).")
            return json.dumps({"hotels": [_info(request, hid, h) for hid, h in found]}, ensure_ascii=False)
        if name == "get_hotel_info":
            if not rl("mcpinfo:" + who, 30, 60):
                raise ToolError("Too many requests. Try again in a minute.")
            return json.dumps(_info(request, *_hotel(args.get("hotel"))), ensure_ascii=False)
        # ask_hotel
        question = str(args.get("question") or "").strip()
        if not question:
            raise ToolError("The question is empty.")
        if len(question) > MAX_QUESTION:
            raise ToolError(f"The question is too long (maximum {MAX_QUESTION} characters).")
        hid, h = _hotel(args.get("hotel"))
        if not (rl("mcpask:" + who, ASK_PER_MIN_IP, 60) and rl("mcpask:h:" + hid, ASK_PER_HOUR_HOTEL, 3600)
                and rl("mcpask:d:" + hid, ASK_PER_DAY_HOTEL, 86400) and rl("mcpask:all", ASK_PER_HOUR_ALL, 3600)):
            raise ToolError("Too many questions right now. Try again later, or open the hotel's guest guide: "
                            + D["guest_url"](D["get_base_url"](request), hid, h))
        lang = re.sub(r"[^a-z]", "", str(args.get("language") or "").lower())[:2]
        device = "mcp:" + hashlib.sha256(who.encode("utf-8")).hexdigest()[:16]
        try:
            res = await D["guest_chat"](hid, question, lang, [], device, request)
        except HTTPException as e:
            logging.warning("MCP ask_hotel: %s %s", e.status_code, e.detail)
            raise ToolError("The hotel's concierge is not available right now. Please try again later.")
        reply = str((res or {}).get("reply") or "").strip()
        if not reply:
            raise ToolError("The hotel's concierge did not answer. Please try again later.")
        return (f"Answer from {h.get('name', 'the hotel')} (via its SmartestGuide concierge):\n\n{reply}\n\n"
                f"Guest guide: {D['guest_url'](D['get_base_url'](request), hid, h)}")

    @app.post("/mcp")
    async def mcp_post(request: Request):
        if not rl("mcp:" + ip(request), 120, 60):
            return JSONResponse(_error(None, -32000, "Too many requests."), status_code=429, headers={"Retry-After": "30"})
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse(_error(None, -32600, "Request too large."), status_code=413)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)

        async def tool(name, args):
            return await call_tool(request, name, args)

        if isinstance(body, list):            # dávka (starší verze protokolu)
            if not body or len(body) > 20:
                return JSONResponse(_error(None, -32600, "Invalid Request"), status_code=400)
            out = [r for r in [await handle(m, tool) for m in body] if r is not None]
            return JSONResponse(out) if out else Response(status_code=202)
        res = await handle(body, tool)
        return JSONResponse(res) if res is not None else Response(status_code=202)

    @app.get("/mcp")
    @app.delete("/mcp")
    def mcp_other(request: Request):
        # Člověk, který adresu otevře v prohlížeči, dostane srozumitelnou stránku (nález testera 24. 11. 2026:
        # „odkaz nefunguje“). Asistenti (bez Accept: text/html) dostanou dál 405.
        if request.method == "GET" and "text/html" in (request.headers.get("accept") or "").lower():
            cs = re.match(r"\s*(cs|sk)", (request.headers.get("accept-language") or "").lower()) is not None
            return HTMLResponse(info_page(D["get_base_url"](request).rstrip("/") + "/mcp", cs))
        # Server neposílá vlastní zprávy a nedrží relace → GET (stream) ani DELETE (ukončení relace) nepodporuje.
        return JSONResponse(_error(None, -32000, "Method not allowed. Send JSON-RPC with POST."), status_code=405,
                            headers={"Allow": "POST"})


def info_page(url: str, cs: bool = True) -> str:
    """Stránka pro člověka, který adresu /mcp otevře v prohlížeči."""
    u = html.escape(url, quote=True)
    if cs:
        t = dict(lang="cs", title="Napojení pro AI asistenty", lead="Tato adresa není webová stránka pro lidi. "
                 "Je to přípojka, přes kterou se AI asistent (například Claude) umí zeptat na hotel přímo u SmartestGuide.",
                 how="Jak ji použít", s1="Otevřete nastavení svého AI asistenta a najděte Konektory (vlastní konektor, MCP).",
                 s2="Přidejte nový konektor a jako adresu vložte:", s3="Pak se asistenta zeptejte třeba: "
                 "„Kdy je snídaně v hotelu …?“ Odpověď přijde přímo od hotelu.", copy="Zkopírovat adresu", done="Zkopírováno",
                 what="Co umí", w1="najít hotel, který používá SmartestGuide,", w2="sdělit základní údaje o hotelu,",
                 w3="odpovědět na dotaz hosta stejně jako concierge Alex.",
                 safe="Přípojka jen čte. Nic neobjednává, nerezervuje ani nemění.", back="Zpět na SmartestGuide")
    else:
        t = dict(lang="en", title="Connection for AI assistants", lead="This address is not a web page for people. "
                 "It is a connector that lets an AI assistant (such as Claude) ask SmartestGuide about a hotel directly.",
                 how="How to use it", s1="Open your AI assistant's settings and find Connectors (custom connector, MCP).",
                 s2="Add a new connector and paste this address:", s3="Then ask the assistant, for example: "
                 "“When is breakfast at hotel …?” The answer comes straight from the hotel.", copy="Copy address", done="Copied",
                 what="What it can do", w1="find a hotel that uses SmartestGuide,", w2="give basic facts about the hotel,",
                 w3="answer a guest question just like the concierge Alex.",
                 safe="The connector is read-only. It makes no orders, bookings or changes.", back="Back to SmartestGuide")
    return f"""<!DOCTYPE html><html lang="{t['lang']}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{t['title']} — SmartestGuide</title>
<link rel="icon" type="image/svg+xml" href="/static/img/favicon.svg"/>
<link rel="stylesheet" href="/static/fonts/fonts.css">
<style>*{{box-sizing:border-box}}body{{margin:0;background:#f6f8fc;font-family:'Manrope',system-ui,sans-serif;color:#0c1b33;line-height:1.6}}
.w{{max-width:640px;margin:0 auto;padding:48px 20px 64px}}
.logo{{font-family:'Sora',sans-serif;font-weight:800;letter-spacing:1px;font-size:20px;display:flex;align-items:center;gap:8px}}
.logo img{{width:26px;height:26px}}.logo span{{color:#2c5fae}}
h1{{font-family:'Sora',sans-serif;font-size:26px;margin:22px 0 10px}}h2{{font-family:'Sora',sans-serif;font-size:17px;margin:0 0 10px}}
.card{{background:#fff;border:1px solid #e3e9f4;border-radius:16px;padding:20px 22px;margin-top:18px;box-shadow:0 8px 24px rgba(12,27,51,.06)}}
ol,ul{{margin:0;padding-left:22px}}li{{margin:6px 0}}
.url{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0 4px}}
code{{background:#eef3fc;border:1px solid #d6e2f5;border-radius:8px;padding:8px 12px;font-size:14px;word-break:break-all}}
button{{border:none;border-radius:99px;background:#2c5fae;color:#fff;font:700 14px 'Manrope',sans-serif;padding:9px 16px;cursor:pointer}}
.safe{{color:#51617e;font-size:14px;margin-top:18px}}a{{color:#2c5fae;font-weight:700;text-decoration:none}}</style></head>
<body><div class="w">
<div class="logo" translate="no"><img src="/static/img/logo.svg" alt=""/>SMARTEST<span>GUIDE</span></div>
<h1>{t['title']}</h1><p>{t['lead']}</p>
<div class="card"><h2>{t['how']}</h2><ol><li>{t['s1']}</li><li>{t['s2']}
<div class="url"><code id="u">{u}</code><button type="button" onclick="cp(this)">{t['copy']}</button></div></li><li>{t['s3']}</li></ol></div>
<div class="card"><h2>{t['what']}</h2><ul><li>{t['w1']}</li><li>{t['w2']}</li><li>{t['w3']}</li></ul></div>
<p class="safe">{t['safe']}</p><p><a href="/">← {t['back']}</a></p></div>
<script>function cp(b){{var t=document.getElementById('u').textContent;
if(navigator.clipboard)navigator.clipboard.writeText(t).then(function(){{b.textContent='✓ {t['done']}';}});}}</script>
</body></html>"""
