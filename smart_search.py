"""Chytré hledání v portálu hotelu: jedno pole nad dokumenty znalostní báze a odpověďmi recepce.

Jak se chová (a proč):
  * hledá už při psaní – jen lexikálně, bez volání AI, takže nic nestojí; nedopsané slovo se doplní
    podle slov, která v dokumentech opravdu jsou;
  * po Enteru hledá i významově (vektory) a nabídne krátkou odpověď se zdroji; stejná otázka nad
    nezměněnou bází se podruhé bere z keše a AI se neplatí;
  * snese překlepy a chybějící diakritiku (oprava jen na slovo, které v bázi existuje);
  * učí se z dotazů: pamatuje si, co se hledá (našeptávání), na co se kliklo (příště výš) a co se
    nenašlo – to posílá do „Alex se učí“, kde hotel odpověď doplní a příště ji najde hledání i Alex.

Čisté funkce mají testy v test_smart_search.py. Cesty registruje kb_routes.setup().
"""
import re, time, asyncio, logging

import kb as kbmod

MAX_QUERIES_PER_HOTEL = 600
SNIPPET = 260
BOOST_STEP, BOOST_MAX = 0.25, 4          # každé kliknutí +25 % ke skóre, nejvýš 4 kliknutí
_ANSWERS = {}                             # (hotel, verze báze, dotaz) -> odpověď; keš v paměti procesu
_ANSWERS_MAX = 400


# ─────────────────────────────────────────────────────────────
# Dotaz: normalizace, překlepy, doplnění nedopsaného slova
# ─────────────────────────────────────────────────────────────
def qnorm(q: str) -> str:
    """Klíč dotazu pro paměť hledání: bez diakritiky, interpunkce a nadbytečných mezer."""
    return " ".join(re.findall(r"[a-z0-9]+", kbmod.fold(q)))[:200]


def words(q: str) -> list:
    """Významová slova dotazu (bez diakritiky, bez stop-slov)."""
    return [t for t in kbmod.tokens(q) if t[0] != "~"]


def edit_distance(a: str, b: str, limit: int = 2) -> int:
    """Damerau-Levenshtein (prohození sousedních písmen = 1). Nad limit vrací limit + 1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if prev2 is not None and i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _known(w: str, df: dict) -> bool:
    return w in df or (kbmod.stem(w) and kbmod.stem(w) in df)


def correct(query: str, df: dict) -> tuple:
    """Opraví překlepy podle slovníku báze (df: slovo -> v kolika úryvcích je).
    Vrací (opravený dotaz, [(původní, opravené)]). Slovo, které báze zná (i jen kmenem), se nemění;
    opravuje se jen na slovo se stejným prvním písmenem a vzdáleností 1 (u slov od 8 znaků 2)."""
    out, fixes = [], []
    for raw in re.findall(r"\S+", query or ""):
        w = "".join(re.findall(r"[a-z0-9]+", kbmod.fold(raw)))
        if len(w) < 4 or w.isdigit() or w in kbmod._STOP or _known(w, df):
            out.append(raw)
            continue
        lim = 2 if len(w) >= 8 else 1
        best = None
        for cand, n in df.items():
            if cand[0] == "~" or cand[0] != w[0] or abs(len(cand) - len(w)) > lim or len(cand) < 4:
                continue
            d = edit_distance(w, cand, lim)
            if d <= lim and (best is None or (d, -n) < (best[0], -best[1])):
                best = (d, n, cand)
        if best:
            out.append(best[2])
            fixes.append((raw, best[2]))
        else:
            out.append(raw)
    return " ".join(out), fixes


def complete_last(query: str, df: dict) -> str:
    """Při psaní: nedopsané poslední slovo doplní nejčastějším slovem báze, které jím začíná."""
    if not query or query[-1].isspace():
        return query
    parts = query.split()
    w = "".join(re.findall(r"[a-z0-9]+", kbmod.fold(parts[-1])))
    if len(w) < 3 or w in df:
        return query
    best = None
    for cand, n in df.items():
        if cand[0] != "~" and len(cand) > len(w) and cand.startswith(w) and (best is None or n > best[0]):
            best = (n, cand)
    return " ".join(parts[:-1] + [best[1]]) if best else query


def known_words(query: str, df: dict) -> list:
    """Slova dotazu, která báze zná, od nejvzácnějšího (to o tématu říká nejvíc)."""
    qw = list(dict.fromkeys(words(query)))
    keep = [w for w in qw if _known(w, df)]
    return sorted(keep, key=lambda w: df.get(w) or df.get(kbmod.stem(w) or "", 0) or 10 ** 6) if len(qw) >= 2 else []


async def closest(KB, hotel_id: str, query: str, df: dict, k: int, audiences=("guest", "staff")) -> list:
    """Když celá otázka nic nenajde („kolik stojí parkování“), hledá se po jednotlivých slovech, která
    v dokumentech opravdu jsou („parkování“) – lepší nejbližší shoda než prázdno. Jen podle slov, zdarma."""
    out, seen = [], set()
    for w in known_words(query, df)[:4]:
        for h in await KB.search(hotel_id, w, audiences=audiences, k=k, lexical=True):
            if h["chunk_id"] not in seen:
                seen.add(h["chunk_id"])
                out.append(h)
    return out[:k]


def snippet(text: str, qwords: list, width: int = SNIPPET) -> str:
    """Výřez z úryvku kolem místa, kde je nejvíc slov dotazu (aby bylo hned vidět proč se našel)."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= width:
        return text
    f = kbmod.fold(text)
    if len(f) != len(text):                       # rozložené znaky – pozice by neseděly
        return text[:width].rsplit(" ", 1)[0] + " …"
    keys = {w[:5] if len(w) >= 5 else w for w in qwords}
    pos = [m.start() for m in re.finditer(r"[a-z0-9]+", f) if (m.group(0)[:5] if len(m.group(0)) >= 5 else m.group(0)) in keys]
    if not pos:
        return text[:width].rsplit(" ", 1)[0] + " …"
    best, best_n = pos[0], 0
    for p in pos:
        n = sum(1 for x in pos if p <= x < p + width - 40)
        if n > best_n:
            best, best_n = p, n
    a = max(0, best - 40)
    if a:
        sp = text.find(" ", a)
        a = sp + 1 if 0 <= sp < best else a
    b = min(len(text), a + width)
    out = text[a:b]
    if b < len(text):
        out = out.rsplit(" ", 1)[0] + " …"
    return ("… " if a else "") + out


def learned_hits(items: list, query: str, k: int = 3) -> list:
    """Odpovědi recepce („Alex se učí“), které se dotazu týkají. Stejné pravidlo jako u dokumentů:
    u 1–2 slov musí sedět všechna, jinak aspoň dvě a zároveň polovina."""
    qw = list(dict.fromkeys(words(query)))
    if not qw:
        return []
    need = len(qw) if len(qw) <= 2 else max(2, (len(qw) + 1) // 2)
    keys = [w[:5] if len(w) >= 5 else w for w in qw]
    scored = []
    for x in items or []:
        tq = {(w[:5] if len(w) >= 5 else w) for w in words(x.get("q", ""))}
        ta = {(w[:5] if len(w) >= 5 else w) for w in words(x.get("a", ""))}
        hit = sum(1 for w in keys if w in tq or w in ta)
        if hit >= need:
            scored.append((hit + sum(0.5 for w in keys if w in tq), x))
    scored.sort(key=lambda s: -s[0])
    return [x for _s, x in scored[:k]]


# ─────────────────────────────────────────────────────────────
# Paměť hledání: co se hledá, na co se kliklo, co se nenašlo
# ─────────────────────────────────────────────────────────────
def log_query(store, hotel_id: str, q: str, hits: int):
    qn = qnorm(q)
    if len(qn) < 3:
        return
    now = kbmod._now()
    store.x("INSERT INTO kb_queries (hotel_id, qn, q, n, zero, last_at) VALUES (%s,%s,%s,1,%s,%s) "
            "ON CONFLICT (hotel_id, qn) DO UPDATE SET n = kb_queries.n + 1, zero = excluded.zero, "
            "q = excluded.q, last_at = excluded.last_at", (hotel_id, qn, q.strip()[:200], 0 if hits else 1, now))
    row = store.x("SELECT COUNT(*) FROM kb_queries WHERE hotel_id=%s", (hotel_id,), "one")
    if row and int(row[0] or 0) > MAX_QUERIES_PER_HOTEL:      # nejstarší málo používané pryč
        old = store.x("SELECT qn FROM kb_queries WHERE hotel_id=%s ORDER BY last_at LIMIT 100", (hotel_id,), "all") or []
        store.tx([("DELETE FROM kb_queries WHERE hotel_id=%s AND qn=%s", (hotel_id, r[0])) for r in old]
                 + [("DELETE FROM kb_clicks WHERE hotel_id=%s AND qn=%s", (hotel_id, r[0])) for r in old])


def log_click(store, hotel_id: str, q: str, doc_id: str):
    qn = qnorm(q)
    if len(qn) < 3 or not doc_id:
        return
    store.x("INSERT INTO kb_clicks (hotel_id, qn, doc_id, n) VALUES (%s,%s,%s,1) "
            "ON CONFLICT (hotel_id, qn, doc_id) DO UPDATE SET n = kb_clicks.n + 1", (hotel_id, qn, doc_id[:64]))


def suggestions(rows: list, prefix: str, k: int = 6) -> list:
    """Našeptávání z dřívějších dotazů hotelu. rows: [(q, n, zero, last_at)].
    Bez textu: nejčastější úspěšné dotazy. S textem: dotazy, kde některé slovo začíná psaným textem."""
    p = qnorm(prefix)
    out = []
    for q, n, zero, _at in sorted(rows, key=lambda r: (-int(r[1] or 0), str(r[3] or "")), reverse=False):
        if zero:
            continue
        qn = qnorm(q)
        if p and (qn == p or not any(w.startswith(p) for w in [qn] + qn.split())):
            continue
        out.append(q)
        if len(out) >= k:
            break
    return out


def boosts(click_rows: list, q: str) -> dict:
    """Kolikrát se u stejného nebo velmi podobného dotazu kliklo na který dokument.
    click_rows: [(qn, doc_id, n)]. Podobný = aspoň 60 % společných slov."""
    qn = qnorm(q)
    qs = set(qn.split())
    out = {}
    for rqn, doc, n in click_rows:
        if rqn != qn:
            rs = set(str(rqn).split())
            if not qs or not rs or len(qs & rs) / len(qs | rs) < 0.6:
                continue
        out[doc] = out.get(doc, 0) + int(n or 0)
    return out


def rerank(hits: list, boost: dict) -> list:
    """Dokumenty, které si lidé u téhož dotazu otevírali, posune výš (pořadí jinak zachová)."""
    if not boost:
        return hits
    for h in hits:
        n = min(BOOST_MAX, boost.get(h["doc_id"], 0))
        h["boost"] = n
        h["score"] = round(h.get("score", 0) * (1 + BOOST_STEP * n), 4)
    return sorted(hits, key=lambda h: -h["score"])


def worth_learning(q: str) -> bool:
    """Nenalezený dotaz stojí za zařazení do „Alex se učí“: je to věta nebo aspoň dvě slova, ne zlomek slova."""
    w = words(q)
    return len(w) >= 2 or (len(w) == 1 and len(qnorm(q)) >= 8)


# ─────────────────────────────────────────────────────────────
# Cesty
# ─────────────────────────────────────────────────────────────
def setup(app, *, KB, STORE, hotel_fn, body_fn, rate_limit_ok, answer_fn, learned_fn, note_unanswered=None, k=6):
    from fastapi import Request, HTTPException

    def _df(hid):
        ix = KB.index(hid)
        return ix.bm.df if ix else {}

    @app.post("/api/hotel-portal/search")
    async def portal_search(request: Request):
        """{q, mode: 'instant' | 'full'} → úryvky z dokumentů, odpovědi recepce, našeptávání, opravy překlepů."""
        h = hotel_fn(request)
        hid = h["id"]
        b = await body_fn(request)
        q = str(b.get("q") or "").strip()[:300]
        full = b.get("mode") == "full"
        if not rate_limit_ok(("sqf:" if full else "sqi:") + hid, 40 if full else 240, 60):
            raise HTTPException(429, "Zkuste to za chvíli.")
        qrows = await asyncio.to_thread(STORE.x, "SELECT q, n, zero, last_at FROM kb_queries WHERE hotel_id=%s ORDER BY n DESC LIMIT 300",
                                        (hid,), "all") or []
        out = {"q": q, "used": q, "fixes": [], "hits": [], "learned": [], "terms": [],
               "suggest": suggestions(qrows, q), "docs": 0}
        df = await asyncio.to_thread(_df, hid)
        out["docs"] = (await asyncio.to_thread(KB.counts, hid))["docs"]
        if len(q) < 2:
            return out
        used, fixes = (q, []) if b.get("exact") else correct(q, df)
        if not full:
            used = complete_last(used, df)
        out["used"], out["fixes"] = used, [list(f) for f in fixes]
        qw = list(dict.fromkeys(words(used)))
        out["terms"] = qw
        hits = await KB.search(hid, used, audiences=("guest", "staff"), k=k, lexical=not full)
        if not hits:
            hits = await closest(KB, hid, used, df, k)
            out["partial"] = bool(hits)
        clicks = await asyncio.to_thread(STORE.x, "SELECT qn, doc_id, n FROM kb_clicks WHERE hotel_id=%s", (hid,), "all") or []
        hits = rerank(hits, boosts(clicks, used))
        out["hits"] = [{"doc_id": x["doc_id"], "title": x["title"], "section": x["section"], "url": x["url"],
                        "audience": x["audience"], "path": x["path"], "snippet": snippet(x["text"], qw),
                        "text": x["text"], "boost": x.get("boost", 0)} for x in hits]
        out["learned"] = learned_hits(learned_fn(h), used)
        if full:
            found = len(out["hits"]) + len(out["learned"])
            await asyncio.to_thread(log_query, STORE, hid, used if fixes else q, found)   # s opraveným překlepem
            out["noted"] = False
            # co se našlo aspoň v nastavení portálu (hlásí stránka), není mezera ve znalostech
            if not found and not b.get("local") and note_unanswered and worth_learning(q):
                try:
                    await asyncio.to_thread(note_unanswered, hid, q, str(b.get("lang") or "")[:5])
                    out["noted"] = True
                except Exception as e:
                    logging.warning("Hledání: dotaz bez výsledku se nepodařilo zapsat: %s", e)
        return out

    @app.post("/api/hotel-portal/search/answer")
    async def portal_search_answer(request: Request):
        """Krátká odpověď na dotaz složená z nalezených úryvků (se zdroji). Stejný dotaz nad stejnou bází = keš."""
        h = hotel_fn(request)
        hid = h["id"]
        b = await body_fn(request)
        q = str(b.get("q") or "").strip()[:300]
        if len(q) < 3:
            raise HTTPException(400, "Prázdný dotaz.")
        key = (hid, await asyncio.to_thread(KB.version, hid), len(learned_fn(h)), qnorm(q), str(b.get("lang") or "")[:2])
        if key in _ANSWERS:
            return dict(_ANSWERS[key], cached=True)
        if not rate_limit_ok("sqa:" + hid, 15, 60):
            raise HTTPException(429, "Zkuste to za chvíli.")
        df = await asyncio.to_thread(_df, hid)
        used, _f = correct(q, df)
        hits = await KB.search(hid, used, audiences=("guest", "staff"), k=k)
        if not hits:
            hits = await closest(KB, hid, used, df, k)
        res = await answer_fn(h, q, [], hits, str(b.get("lang") or "")[:2])
        if res.get("found"):
            if len(_ANSWERS) >= _ANSWERS_MAX:
                _ANSWERS.clear()
            _ANSWERS[key] = res
        return dict(res, cached=False)

    @app.post("/api/hotel-portal/search/click")
    async def portal_search_click(request: Request):
        h = hotel_fn(request)
        b = await body_fn(request)
        if rate_limit_ok("sqc:" + h["id"], 120, 60):
            await asyncio.to_thread(log_click, STORE, h["id"], str(b.get("q") or "")[:300], str(b.get("doc") or ""))
        return {"ok": True}

    @app.post("/api/hotel-portal/search/feedback")
    async def portal_search_feedback(request: Request):
        """Palec dolů u odpovědi: dotaz jde do „Alex se učí“, ať hotel doplní správnou odpověď; keš se zahodí."""
        h = hotel_fn(request)
        b = await body_fn(request)
        q = str(b.get("q") or "").strip()[:300]
        if not q or not rate_limit_ok("sqb:" + h["id"], 30, 60):
            return {"ok": False}
        if b.get("ok") is not False:
            return {"ok": True, "noted": False}
        for key in [k_ for k_ in _ANSWERS if k_[0] == h["id"] and k_[3] == qnorm(q)]:
            _ANSWERS.pop(key, None)
        if note_unanswered:
            await asyncio.to_thread(note_unanswered, h["id"], q, str(b.get("lang") or "")[:5])
            return {"ok": True, "noted": True}
        return {"ok": True, "noted": False}

    @app.get("/api/hotel-portal/search/insights")
    async def portal_search_insights(request: Request):
        """Co se v portálu hledá nejčastěji a co se nenašlo (mezery v bázi)."""
        h = hotel_fn(request)
        rows = await asyncio.to_thread(STORE.x, "SELECT q, n, zero, last_at FROM kb_queries WHERE hotel_id=%s ORDER BY n DESC, last_at DESC LIMIT 200",
                                       (h["id"],), "all") or []
        return {"top": [{"q": r[0], "n": int(r[1] or 0)} for r in rows if not r[2]][:8],
                "missing": [{"q": r[0], "n": int(r[1] or 0)} for r in rows if r[2]][:8]}
