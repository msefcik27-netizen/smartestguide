"""Testy znalostní báze (kb.py). Spuštění: python test_kb.py"""
import asyncio, io, os, sys, tempfile, zipfile

import kb

RAD = """PROVOZNÍ ŘÁD HOTELU

1. Snídaně
Snídaně se podává v restauraci v přízemí každý den od 7:00 do 10:30, o víkendu do 11:00.
Bezlepkové pečivo připravíme na požádání den předem.

2. Parkování
Hotelová garáž má 24 míst. Cena je 450 Kč za noc, rezervace na recepci.
Nabíječka pro elektromobily je na místě číslo 7.

3. Wellness
Sauna a vířivka jsou otevřené od 15:00 do 22:00. Vstup pro děti do 12 let jen s doprovodem.

4. Postup při požáru
Zaměstnanec, který zjistí požár, okamžitě vyhlásí poplach tlačítkem na chodbě a volá 150.
Evakuace hostů probíhá únikovým schodištěm B na shromaždiště před hlavním vchodem.
"""


def _store():
    d = tempfile.mkdtemp()
    return kb.Store("", os.path.join(d, "t.sqlite3"))


def _kb():
    return kb.KB(_store(), embed_key_fn=lambda: "")


def run(c):
    return asyncio.run(c)


def test_fold_tokens():
    assert kb.fold("Snídaně ŽLUŤOUČKÝ") == "snidane zlutoucky"
    t = kb.tokens("Kdy se podává snídaně?")
    assert "snidane" in t and "~snida" in t and "se" not in t and "kdy" not in t


def test_chunk_sections():
    ch = kb.chunk_text(RAD)
    secs = [s for s, _ in ch]
    assert "1. Snídaně" in secs and "2. Parkování" in secs and "4. Postup při požáru" in secs, secs
    park = [t for s, t in ch if s == "2. Parkování"][0]
    assert "450 Kč" in park and "Snídaně se podává" not in park


def test_chunk_long_and_overlap():
    veta = "Tohle je věta číslo %d o provozu hotelu a jeho službách pro hosty. "
    text = "Dlouhá kapitola\n\n" + "".join(veta % i for i in range(120))
    ch = kb.chunk_text(text)
    assert len(ch) > 3
    assert all(len(t) <= kb.CHUNK_TARGET * 1.5 for _, t in ch), max(len(t) for _, t in ch)
    # nic se neztratí: každá věta je aspoň v jednom úryvku
    spoj = " ".join(t for _, t in ch)
    assert all(("číslo %d o" % i) in spoj for i in range(120))


def test_bm25_ranks():
    docs = ["snídaně se podává od sedmi", "parkování v garáži stojí 450 Kč", "sauna je otevřená odpoledne"]
    bm = kb.Bm25([kb.tokens(d) for d in docs])
    sc = bm.scores(kb.tokens("kolik stojí parkování"))
    assert sc[1][0] > sc[0][0] and sc[1][0] > sc[2][0] and sc[1][1] == 2
    sc = bm.scores(kb.tokens("snídani"))            # jiný tvar slova → přes kmen
    assert sc[0][0] > 0 and sc[1][0] == 0


def test_vec_roundtrip_cosine():
    a = kb.unpack_vec(kb.pack_vec([3.0, 4.0]))
    assert abs(a[0] - 0.6) < 1e-6 and abs(a[1] - 0.8) < 1e-6
    cs = kb.cosine_all(a, [kb.unpack_vec(kb.pack_vec([3, 4])), kb.unpack_vec(kb.pack_vec([-4, 3])), None])
    assert abs(cs[0] - 1) < 1e-5 and abs(cs[1]) < 1e-5 and cs[2] == 0.0


def test_fuse_threshold():
    # bez vektorů: projde jen úryvek s dost slovy dotazu
    out = kb.fuse([], [(2.0, 2), (0.5, 1), (0.0, 0)], q_terms=2)
    assert [o[0] for o in out] == [0]
    # s vektory: významově blízký projde i bez shody slov, vzdálený ne
    out = kb.fuse([0.55, 0.05, 0.31], [(0.0, 0), (0.4, 1), (0.0, 0)], q_terms=3)
    assert [o[0] for o in out] == [0, 2], out
    # limit úryvků na dokument
    out = kb.fuse([0.9, 0.8, 0.7, 0.6, 0.5], [], q_terms=1, per_doc=2, doc_of=lambda i: "d" if i < 4 else "e")
    assert [o[0] for o in out] == [0, 1, 4]


def test_put_search_audience():
    k = _kb()
    r = run(k.put_document("h1", title="Provozní řád", text=RAD, audience="staff"))
    assert r["status"] == "ok" and r["chunks"] >= 4, r
    # personál najde, host ne (dokument je jen pro personál)
    hits = run(k.search("h1", "kolik stojí parkování v garáži", audiences=("guest", "staff")))
    assert hits and "450 Kč" in hits[0]["text"] and hits[0]["section"] == "2. Parkování", hits
    assert run(k.search("h1", "kolik stojí parkování v garáži", audiences=("guest",))) == []
    # po povolení pro hosty najde i host
    assert k.update_document("h1", r["id"], audience="guest")
    assert run(k.search("h1", "kolik stojí parkování v garáži", audiences=("guest",)))
    # nesouvisející dotaz nevrátí nic → do promptu nejde žádný balast
    assert run(k.search("h1", "jaké je počasí v Tokiu zítra", audiences=("guest", "staff"))) == []
    # jiný hotel nevidí nic
    assert run(k.search("h2", "parkování", audiences=("guest", "staff"))) == []


def test_update_and_delete():
    k = _kb()
    r = run(k.put_document("h1", title="Ceník", text="Parkování stojí 450 Kč za noc.", source_id="s", ext_id="f1"))
    r2 = run(k.put_document("h1", title="Ceník", text="Parkování stojí 500 Kč za noc.", source_id="s", ext_id="f1"))
    assert r2["id"] == r["id"] and k.counts("h1")["docs"] == 1
    hits = run(k.search("h1", "parkování", audiences=("staff",)))
    assert len(hits) == 1 and "500" in hits[0]["text"]
    assert k.update_document("h1", r["id"], enabled=False)
    assert run(k.search("h1", "parkování", audiences=("staff",))) == []
    assert k.delete_document("h1", r["id"]) and k.counts("h1")["docs"] == 0
    assert not k.delete_document("h1", r["id"])


def test_prompt_block_budget():
    hits = [{"title": "Řád", "section": "S%d" % i, "text": "x " * 500} for i in range(6)]
    b = kb.prompt_block(hits, max_chars=2400)
    assert b.startswith("HOTEL KNOWLEDGE BASE") and "[1] Řád › S0" in b
    assert len(b) < 2400 + 700 and "[6]" not in b
    assert kb.prompt_block([]) == ""


def _docx(pars):
    body = "".join(
        '<w:p>%s<w:r><w:t>%s</w:t></w:r></w:p>' % (('<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>' if h else ""), t)
        for h, t in pars)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", '<w:document><w:body>%s</w:body></w:document>' % body)
    return buf.getvalue()


def test_extract_docx_xlsx_txt():
    t, note = kb.extract_text("rad.docx", _docx([(1, "Snídaně"), (0, "Podává se od 7:00 &amp; do 10:30."), (1, "Bazén"), (0, "Otevřeno do 22:00.")]))
    assert note == "" and "# Snídaně" in t and "7:00 & do 10:30" in t
    assert [s for s, _ in kb.chunk_text(t)] == ["Snídaně", "Bazén"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", '<workbook><sheets><sheet name="Ceník" sheetId="1"/></sheets></workbook>')
        z.writestr("xl/sharedStrings.xml", "<sst><si><t>Služba</t></si><si><t>Praní prádla</t></si></sst>")
        z.writestr("xl/worksheets/sheet1.xml", '<worksheet><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c><c><v>250</v></c></row></sheetData></worksheet>')
    t, note = kb.extract_text("cenik.xlsx", buf.getvalue())
    assert "# Ceník" in t and "Služba | Praní prádla | 250" in t, t
    t, note = kb.extract_text("a.txt", "Příliš žluťoučký kůň".encode("cp1250"))
    assert "žluťoučký" in t
    t, note = kb.extract_text("a.exe", b"MZ")
    assert t == "" and note
    t, note = kb.extract_text("velky.txt", b"a" * (kb.MAX_FILE_BYTES + 1))
    assert t == "" and "20 MB" in note


def test_vectors_with_fake_embeddings():
    """Vektorová cesta bez sítě: embed nahrazen slovníkem témat (dotaz anglicky, dokument česky)."""
    temata = {"snída": 0, "breakfast": 0, "parkov": 1, "garáž": 1, "parking": 1, "sauna": 2, "wellness": 2, "požár": 3, "fire": 3}
    async def fake(texts, api_key, client=None):
        out = []
        for t in texts:
            v = [0.01] * 4
            for w, i in temata.items():
                if w in t.lower():
                    v[i] += 1.0
            out.append(v)
        return out
    orig = kb.embed
    kb.embed = fake
    try:
        k = kb.KB(_store(), embed_key_fn=lambda: "x")
        r = run(k.put_document("h1", title="Provozní řád", text=RAD, audience="guest"))
        assert r["embedded"] == r["chunks"]
        hits = run(k.search("h1", "Where can I leave my car? parking", audiences=("guest",)))
        assert hits and hits[0]["section"] == "2. Parkování" and hits[0]["similarity"] > 0.9, hits
        hits = run(k.search("h1", "fire", audiences=("guest",), k=2))
        assert hits and hits[0]["section"] == "4. Postup při požáru"
    finally:
        kb.embed = orig


def test_staff_key():
    k = _kb()
    key = k.staff_key("h1")
    assert len(key) >= 32 and k.staff_key("h1") == key
    assert k.staff_key_ok("h1", key) and not k.staff_key_ok("h1", "x") and not k.staff_key_ok("h2", key)
    key2 = k.staff_key("h1", rotate=True)
    assert key2 != key and not k.staff_key_ok("h1", key) and k.staff_key_ok("h1", key2)


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
