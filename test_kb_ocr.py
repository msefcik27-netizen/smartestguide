"""Testy rozpoznávání skenovaných PDF (kb_ocr.py). Model se nevolá – čtení se podvrhuje. Spuštění: python test_kb_ocr.py"""
import asyncio, io, os, sys, tempfile

import kb
import kb_ocr as o


def pdf(pages: int) -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    b = io.BytesIO()
    w.write(b)
    return b.getvalue()


def npages(data: bytes) -> int:
    from pypdf import PdfReader
    return len(PdfReader(io.BytesIO(data)).pages)


LONG = "Snídaně se podává v restauraci v přízemí každý den, o víkendu déle. Bezlepkové pečivo připravíme na požádání. "


def fake(main, check=None, calls=None):
    """call(model, pdf): main/check = funkce (číslo strany v části, počet stran části) -> text strany, nebo výjimka."""
    async def call(model, data):
        n = npages(data)
        if calls is not None:
            calls.append((model, n))
        fn = main if model == o.MODEL else (check or main)
        return "\n".join(f"=== strana {i} ===\n{fn(i, n)}" for i in range(1, n + 1))
    return call


def run(data, call, hotel=""):
    return asyncio.run(o.ocr_pdf(data, "", hotel_id=hotel, call=call))


def test_numbers_and_masking():
    assert o.numbers("Cena 1 200 Kč, od 7:00 do 10:30, tel. 777 123 456") == o.numbers("1200 a 7 a 00 a 10 a 30 a 777123456")
    assert o.mask_numbers("od 7:00 do 10:30") == (f"od {o.UNREADABLE}:{o.UNREADABLE} do {o.UNREADABLE}:{o.UNREADABLE}", 4)
    assert o.mask_numbers("450 Kč a 24 míst", {"450"}) == (f"{o.UNREADABLE} Kč a 24 míst", 1)


def test_split_pages():
    t = "=== strana 1 ===\nprvní\n=== STRANA 2 ===\ndruhá\n\n=== strana 9 ===\nmimo"
    assert o.split_pages(t, 2) == {1: "první", 2: "druhá"}
    assert o.split_pages("bez značek", 1) == {1: "bez značek"}
    assert o.split_pages("bez značek", 3) == {}                 # u více stran se rozdělení nedá věřit
    assert o.split_pages("", 1) == {}


def test_reconcile_keeps_only_confirmed_numbers():
    r = o.reconcile("Parkování stojí 450 Kč, garáž má 24 míst.", "Parkování stojí 480 Kč, garáž má 24 míst.")
    assert r["text"] == f"Parkování stojí {o.UNREADABLE} Kč, garáž má 24 míst." and r["masked"] == 1 and not r["unverified"]
    assert r["differs"]                                         # druhé čtení vidělo číslo, které první nemá
    same = o.reconcile("Cena 1 200 Kč.", "Cena 1200 Kč.")
    assert same["text"] == "Cena 1 200 Kč." and same["masked"] == 0 and not same["differs"]
    twice = o.reconcile("7 a znovu 7", "jen 7")                 # počet výskytů musí sedět
    assert twice["masked"] == 2
    none = o.reconcile("Cena 450 Kč, tel. 777 123 456.", None)  # bez druhého čtení žádné číslo neprojde
    assert none["unverified"] and "450" not in none["text"] and "777" not in none["text"] and none["masked"] == 2


def test_reconcile_flags_very_different_text():
    a = LONG * 2
    b = "Tento dokument popisuje pravidla parkování a provoz garáže, včetně cen a nabíjení elektromobilů v areálu hotelu. " * 2
    assert o.reconcile(a, b)["differs"]
    assert not o.reconcile(a, a.replace("déle", "dele"))["differs"]


def test_split_pdf_and_page_cap():
    total, parts = o.split_pdf(pdf(7))
    assert total == 7 and [(s, n) for s, n, _b in parts] == [(1, 3), (4, 3), (7, 1)]
    assert [npages(b) for _s, _n, b in parts] == [3, 3, 1]
    total, parts = o.split_pdf(pdf(34))
    assert total == 34 and sum(n for _s, n, _b in parts) == o.MAX_PAGES


def test_ocr_happy_path():
    calls = []
    text, note = run(pdf(4), fake(lambda i, n: f"{LONG} Cena 450 Kč.", calls=calls))
    assert text.count("[strana ") == 4 and "[strana 4]" in text and text.count("450") == 4
    assert note.startswith(o.NOTE_PREFIX) and "4 str." in note and "vynechána" not in note
    assert sorted(calls) == sorted([(o.MODEL, 3), (o.MODEL, 1), (o.CHECK_MODEL, 3), (o.CHECK_MODEL, 1)])


def test_ocr_masks_number_the_models_disagree_on():
    main = lambda i, n: f"{LONG} Cena {450 if i == 1 else 300} Kč, otevřeno do 22 hodin."
    check = lambda i, n: f"{LONG} Cena {480 if i == 1 else 300} Kč, otevřeno do 22 hodin."
    text, note = run(pdf(2), fake(main, check))
    assert "450" not in text and "480" not in text and "300" in text and text.count("22") == 2
    assert "vynechána (1×, str. 1)" in note and "Na str. 1 se čtení liší" in note


def test_ocr_without_second_reading_has_no_numbers():
    def check(i, n):
        raise RuntimeError("AI 529")
    text, note = run(pdf(2), fake(lambda i, n: f"{LONG} Cena 450 Kč, tel. 777 123 456.", check))
    assert "Snídaně" in text and "450" not in text and "777" not in text
    assert "Str. 1, 2 bez druhého čtení" in note


def test_ocr_main_reading_fails():
    def main(i, n):
        raise RuntimeError("AI: refusal")
    text, note = run(pdf(2), fake(main, lambda i, n: LONG))
    assert text == "" and "nepodařilo přečíst" in note
    text, note = run(pdf(1), fake(lambda i, n: ""))            # prázdný sken
    assert text == "" and note
    assert run(b"tohle neni pdf", fake(lambda i, n: LONG))[0] == ""
    assert asyncio.run(o.ocr_pdf(pdf(1), ""))[0] == ""          # bez klíče se nic nevolá


def test_ocr_partial_failure_is_reported():
    def main(i, n):
        if n == 1:
            raise RuntimeError("AI 500")                        # poslední část (strana 4) spadne
        return LONG
    text, note = run(pdf(4), fake(main, lambda i, n: LONG))
    assert "[strana 3]" in text and "[strana 4]" not in text
    assert "Strany 4 se přečíst nepodařilo" in note and "3 str." in note


def test_long_document_note_and_daily_budget():
    o._used.clear()
    text, note = run(pdf(32), fake(lambda i, n: LONG), hotel="h1")
    assert "[strana 30]" in text and "[strana 31]" not in text and "Dokument má 32 stran, čteme prvních 30" in note
    assert o.budget_left("h1") == o.PAGES_PER_DAY - 30
    o._used[("h1", o._day())] = o.PAGES_PER_DAY - 2
    calls = []
    text, note = run(pdf(3), fake(lambda i, n: LONG, calls=calls), hotel="h1")
    assert text == "" and "denní limit" in note and calls == []
    assert run(pdf(3), fake(lambda i, n: LONG), hotel="h2")[0]  # jiný hotel má vlastní počet
    o._used.clear()


def test_knowledge_base_uses_ocr_for_scans_only():
    store = kb.Store("", os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
    K = kb.KB(store, embed_key_fn=lambda: "")
    seen = []

    async def ocr(hotel_id, data):
        seen.append(hotel_id)
        return f"[strana 1]\nParkování v garáži stojí {o.UNREADABLE} Kč za noc. Rezervace na recepci.", o.NOTE_PREFIX + " (1 str.) – zkontrolujte ho proti originálu."
    r = asyncio.run(K.put_document("h1", title="sken.pdf", data=pdf(1), name="sken.pdf"))
    assert r["status"] == "error" and r["error"] == kb.SCAN_NOTE          # bez OCR jako dřív
    K.ocr_fn = ocr
    r = asyncio.run(K.put_document("h1", title="sken2.pdf", data=pdf(1), name="sken2.pdf"))
    assert r["status"] == "ok" and r["error"].startswith(o.NOTE_PREFIX) and seen == ["h1"]
    d = K.document_text("h1", r["id"])
    assert d["title"] == "sken2.pdf" and o.UNREADABLE in d["text"] and d["note"].startswith(o.NOTE_PREFIX)
    assert K.document_text("h2", r["id"]) is None                        # cizí hotel text nevidí
    asyncio.run(K.put_document("h1", title="poznámka", text="Snídaně je od 7:00 do 10:30 v restauraci."))
    assert seen == ["h1"]                                                # běžný text se nerozpoznává

    async def broken(hotel_id, data):
        raise RuntimeError("síť")
    K.ocr_fn = broken
    r = asyncio.run(K.put_document("h1", title="sken3.pdf", data=pdf(1), name="sken3.pdf"))
    assert r["status"] == "error" and r["error"] == kb.SCAN_NOTE


def test_document_text_removes_chunk_overlap():
    store = kb.Store("", os.path.join(tempfile.mkdtemp(), "t.sqlite3"))
    K = kb.KB(store, embed_key_fn=lambda: "")
    src = "\n\n".join(f"Odstavec číslo {i} popisuje pravidlo hotelu {i} a jeho výjimky pro hosty i personál v plném znění." for i in range(60))
    r = asyncio.run(K.put_document("h1", title="dlouhý", text=src))
    assert r["chunks"] > 1
    t = K.document_text("h1", r["id"])["text"]
    for i in range(60):
        assert t.count(f"Odstavec číslo {i} popisuje") == 1, i


def test_call_model_request_shape():
    sent = []

    class R:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, str(body)

        def json(self):
            return self._b

    class C:
        def __init__(self, answers):
            self.answers = list(answers)

        async def post(self, url, headers=None, json=None, timeout=None):
            sent.append((url, headers, json))
            return self.answers.pop(0)
    ok = R(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "=== strana 1 ===\nahoj"}]})
    assert asyncio.run(o.call_model(C([ok]), "k", "claude-opus-5-5", b"%PDF")) == "=== strana 1 ===\nahoj"
    url, h, b = sent[-1]
    assert url == o.API_URL and h["x-api-key"] == "k" and "anthropic-beta" in h and b["fallbacks"] == "default"
    doc, txt = b["messages"][0]["content"]
    assert doc["type"] == "document" and doc["source"]["media_type"] == "application/pdf" and "\n" not in doc["source"]["data"]
    assert txt["type"] == "text" and "temperature" not in b and "thinking" not in b
    asyncio.run(o.call_model(C([ok]), "k", "claude-haiku-5-5", b"%PDF"))
    assert "anthropic-beta" not in sent[-1][1] and "fallbacks" not in sent[-1][2]
    # 400 kvůli doplňkovému parametru → druhý pokus s holým požadavkem
    sent.clear()
    asyncio.run(o.call_model(C([R(400, {"error": "unknown field"}), ok]), "k", "claude-opus-5-5", b"%PDF"))
    assert len(sent) == 2 and "fallbacks" not in sent[1][2] and "output_config" not in sent[1][2] and "anthropic-beta" not in sent[1][1]
    for bad in ("refusal", "max_tokens"):
        try:
            asyncio.run(o.call_model(C([R(200, {"stop_reason": bad, "content": []})]), "k", "claude-haiku-5-5", b"%PDF"))
            assert False, bad
        except RuntimeError:
            pass


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
