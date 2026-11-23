# Testy napojení pro AI asistenty (sg_mcp.py). Spuštění: python -m pytest test_sg_mcp.py -q
import asyncio
import json

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import sg_mcp

HOTELS = {
    "h1": {"name": "Hotel Grandium Praha", "slug": "hotel-grandium-praha", "city": "Praha", "country": "CZ",
           "website": "https://grandium.example", "subscription_active": True},
    "h2": {"name": "Grand Hotel Bohemia", "slug": "grand-bohemia", "city": "Praha", "is_free": True},
    "h3": {"name": "Archivovaný Grandium", "city": "Brno", "subscription_active": True, "archived": True},
    "h4": {"name": "Grandium Bez Předplatného", "city": "Brno"},
    "h5": {"name": "Grandium Vyřazený", "city": "Brno", "subscription_active": True, "ai_assistants_off": True},
    "h6": {"name": "Pension U Lípy", "slug": "u-lipy", "city": "Praha", "subscription_active": True},
}


def run(coro):
    return asyncio.run(coro)


def rpc(method, params=None, rid=1):
    m = {"jsonrpc": "2.0", "method": method}
    if rid is not None:
        m["id"] = rid
    if params is not None:
        m["params"] = params
    return m


# ── čisté funkce ──
def test_match_hotels_by_name_only_live_hotels():
    ids = [hid for hid, _ in sg_mcp.match_hotels(HOTELS, "Grandium Praha")]
    assert ids == ["h1"]
    assert [hid for hid, _ in sg_mcp.match_hotels(HOTELS, "grandium")] == ["h1"]        # archiv, bez předplatného a vyřazený ne
    assert [hid for hid, _ in sg_mcp.match_hotels(HOTELS, "Grand Hotel Bohemia Prague")] == ["h2"]   # jedno slovo navíc u celého jména
    assert sg_mcp.match_hotels(HOTELS, "Grand Europa") == []                              # cizí jméno nenajde nic
    assert [hid for hid, _ in sg_mcp.match_hotels(HOTELS, "bohemia")] == ["h2"]
    assert [hid for hid, _ in sg_mcp.match_hotels(HOTELS, "U Lípy")] == ["h6"]


def test_match_hotels_does_not_list_clients_by_city_or_generic_word():
    for q in ("Praha", "hotel", "hotel Praha", "", "a b", "pension", "CZ"):
        assert sg_mcp.match_hotels(HOTELS, q) == [], q


def test_match_hotels_limit():
    many = {f"x{i}": {"name": f"Aurora {i}", "subscription_active": True} for i in range(20)}
    assert len(sg_mcp.match_hotels(many, "aurora")) == sg_mcp.FIND_MAX


def test_handle_initialize_negotiates_version():
    async def tool(n, a):
        return "x"
    r = run(sg_mcp.handle(rpc("initialize", {"protocolVersion": "2025-06-18"}), tool))
    assert r["result"]["protocolVersion"] == "2025-06-18" and r["result"]["serverInfo"]["name"] == "smartestguide"
    assert "tools" in r["result"]["capabilities"]
    r = run(sg_mcp.handle(rpc("initialize", {"protocolVersion": "1999-01-01"}), tool))
    assert r["result"]["protocolVersion"] == sg_mcp.PROTOCOLS[0]


def test_handle_list_ping_notifications_and_errors():
    async def tool(n, a):
        return "x"
    r = run(sg_mcp.handle(rpc("tools/list"), tool))
    assert [t["name"] for t in r["result"]["tools"]] == ["find_hotel", "get_hotel_info", "ask_hotel"]
    assert all(t["inputSchema"]["type"] == "object" and t["annotations"]["readOnlyHint"] for t in r["result"]["tools"])
    assert run(sg_mcp.handle(rpc("ping"), tool))["result"] == {}
    assert run(sg_mcp.handle(rpc("notifications/initialized", rid=None), tool)) is None
    assert run(sg_mcp.handle(rpc("resources/list"), tool))["error"]["code"] == -32601
    assert run(sg_mcp.handle({"id": 1, "method": "ping"}, tool))["error"]["code"] == -32600
    assert run(sg_mcp.handle("nesmysl", tool))["error"]["code"] == -32600
    assert run(sg_mcp.handle(rpc("tools/call", {"name": "delete_hotel", "arguments": {}}), tool))["error"]["code"] == -32602


def test_handle_tool_call_results_and_failures():
    async def ok(n, a):
        return f"{n}:{a.get('query')}"

    async def tool_error(n, a):
        raise sg_mcp.ToolError("Hotel not found.")

    async def crash(n, a):
        raise RuntimeError("tajná vnitřní chyba")

    r = run(sg_mcp.handle(rpc("tools/call", {"name": "find_hotel", "arguments": {"query": "x"}}), ok))
    assert r["result"] == {"content": [{"type": "text", "text": "find_hotel:x"}], "isError": False}
    r = run(sg_mcp.handle(rpc("tools/call", {"name": "find_hotel", "arguments": "špatně"}), ok))
    assert r["result"]["content"][0]["text"] == "find_hotel:None"
    r = run(sg_mcp.handle(rpc("tools/call", {"name": "ask_hotel", "arguments": {}}), tool_error))
    assert r["result"]["isError"] and r["result"]["content"][0]["text"] == "Hotel not found."
    r = run(sg_mcp.handle(rpc("tools/call", {"name": "ask_hotel", "arguments": {}}), crash))
    assert r["result"]["isError"] and "tajná" not in json.dumps(r, ensure_ascii=False)


# ── cesty ──
def make_client(chat=None, allow=None):
    app, calls = FastAPI(), []

    def resolve(db, ident):
        for hid, h in db["hotels"].items():
            if ident in (hid, h.get("slug")):
                return hid, h
        return None, None

    async def guest_chat(hid, msg, lang, hist, device, request):
        calls.append({"hid": hid, "msg": msg, "lang": lang, "hist": hist, "device": device})
        if chat:
            return await chat(hid, msg)
        return {"reply": "Snídaně je od 7 do 10 hodin.", "tajne": "nesmí ven"}

    sg_mcp.setup(app, db_load=lambda: {"hotels": HOTELS}, resolve_hotel=resolve,
                 rate_limit_ok=allow or (lambda key, n, w: True), client_ip=lambda r: "203.0.113.7",
                 get_base_url=lambda r: "https://app.example", guest_chat=guest_chat,
                 guest_url=lambda base, hid, h: f"{base}/g/{h.get('slug') or hid}")
    return TestClient(app), calls


def call(c, name, args):
    r = c.post("/mcp", json=rpc("tools/call", {"name": name, "arguments": args}))
    assert r.status_code == 200
    return r.json()["result"]


def test_route_find_and_info():
    c, _ = make_client()
    res = call(c, "find_hotel", {"query": "Grandium"})
    data = json.loads(res["content"][0]["text"])
    assert data == {"hotels": [{"id": "hotel-grandium-praha", "name": "Hotel Grandium Praha", "city": "Praha", "country": "CZ",
                                "website": "https://grandium.example",
                                "guest_guide": "https://app.example/g/hotel-grandium-praha"}]}
    assert "No hotel" in call(c, "find_hotel", {"query": "Praha"})["content"][0]["text"]
    info = json.loads(call(c, "get_hotel_info", {"hotel": "hotel-grandium-praha"})["content"][0]["text"])
    assert info["name"] == "Hotel Grandium Praha"
    for hidden in ("h3", "h4", "h5", "neexistuje", ""):
        assert call(c, "get_hotel_info", {"hotel": hidden})["isError"], hidden


def test_route_ask_hotel_returns_only_the_reply():
    c, calls = make_client()
    res = call(c, "ask_hotel", {"hotel": "hotel-grandium-praha", "question": "Kdy je snídaně?", "language": "CS-cz"})
    txt = res["content"][0]["text"]
    assert not res["isError"] and "Snídaně je od 7 do 10 hodin." in txt and "Hotel Grandium Praha" in txt
    assert "nesmí ven" not in txt and "https://app.example/g/hotel-grandium-praha" in txt
    assert calls[0]["hid"] == "h1" and calls[0]["lang"] == "cs" and calls[0]["hist"] == []
    assert calls[0]["device"].startswith("mcp:") and "203.0.113.7" not in calls[0]["device"]


def test_route_ask_hotel_refusals():
    c, calls = make_client()
    assert call(c, "ask_hotel", {"hotel": "hotel-grandium-praha", "question": "  "})["isError"]
    assert call(c, "ask_hotel", {"hotel": "hotel-grandium-praha", "question": "x" * 1001})["isError"]
    assert call(c, "ask_hotel", {"hotel": "h5", "question": "Kdy je snídaně?"})["isError"]      # vyřazený hotel
    assert calls == []                                                                           # nic z toho nestálo peníze

    async def fail(hid, msg):
        raise HTTPException(status_code=502, detail="vnitřní detail")
    c, _ = make_client(chat=fail)
    res = call(c, "ask_hotel", {"hotel": "hotel-grandium-praha", "question": "Kdy je snídaně?"})
    assert res["isError"] and "vnitřní detail" not in res["content"][0]["text"]


def test_route_limits():
    c, calls = make_client(allow=lambda key, n, w: not key.startswith("mcpask:h:"))
    res = call(c, "ask_hotel", {"hotel": "hotel-grandium-praha", "question": "Kdy je snídaně?"})
    assert res["isError"] and "Too many" in res["content"][0]["text"] and calls == []
    c, _ = make_client(allow=lambda key, n, w: not key.startswith("mcp:"))
    assert c.post("/mcp", json=rpc("ping")).status_code == 429


def test_route_transport():
    c, _ = make_client()
    r = c.post("/mcp", json=rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "1"}}))
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert c.post("/mcp", json=rpc("notifications/initialized", rid=None)).status_code == 202
    assert c.post("/mcp", content=b"{nesmysl").status_code == 400
    assert c.post("/mcp", content=b" " * (sg_mcp.MAX_BODY + 1)).status_code == 413
    assert c.get("/mcp").status_code == 405 and c.delete("/mcp").status_code == 405
    batch = c.post("/mcp", json=[rpc("ping", rid=1), rpc("notifications/initialized", rid=None), rpc("tools/list", rid=2)])
    assert [x["id"] for x in batch.json()] == [1, 2]
    assert c.post("/mcp", json=[]).status_code == 400
