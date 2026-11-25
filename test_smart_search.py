"""Testy chytrého hledání v portálu (smart_search.py). Spuštění: python test_smart_search.py"""
import asyncio, os, sys, tempfile
from datetime import datetime

import kb
import alex_learn as al
import smart_search as ss

RAD = """PROVOZNÍ ŘÁD HOTELU

1. Snídaně
Snídaně se podává v restauraci v přízemí každý den od 7:00 do 10:30, o víkendu do 11:00.
Bezlepkové pečivo připravíme na požádání den předem.

2. Parkování
Hotelová garáž má 24 míst. Cena je 450 Kč za noc, rezervace na recepci.
Nabíječka pro elektromobily je na místě číslo 7.

3. Wellness
Sauna a vířivka jsou otevřené od 15:00 do 22:00. Vstup pro děti do 12 let jen s doprovodem.
"""
REKLAMACE = """REKLAMAČNÍ POSTUP

Reklamaci pokoje přijímá vedoucí recepce. Hostovi nabídneme výměnu pokoje nebo slevu 20 procent.
Každou reklamaci zapíšeme do knihy reklamací a do 24 hodin informujeme ředitele.
"""


def _kb():
    d = tempfile.mkdtemp()
    store = kb.Store("", os.path.join(d, "t.sqlite3"))
    K = kb.KB(store, embed_key_fn=lambda: "")
    r1 = asyncio.run(K.put_document("h1", title="Provozní řád", text=RAD, audience="guest"))
    r2 = asyncio.run(K.put_document("h1", title="Reklamace", text=REKLAMACE, audience="staff"))
    return K, store, r1["id"], r2["id"]


def test_qnorm_and_words():
    assert ss.qnorm("  Kdy je  SNÍDANĚ?! ") == "kdy je snidane"
    assert ss.words("Kdy je snídaně o víkendu?") == ["snidane", "vikendu"]


def test_edit_distance():
    assert ss.edit_distance("snidane", "snidane") == 0
    assert ss.edit_distance("snidnae", "snidane") == 1          # prohozená písmena
    assert ss.edit_distance("parkvani", "parkovani") == 1
    assert ss.edit_distance("sauna", "garaz") > 2


def test_correct_fixes_typos_only_to_known_words():
    K, _s, _a, _b = _kb()
    df = K.index("h1").bm.df
    used, fixes = ss.correct("parkvani cena", df)
    assert used == "parkovani cena", used
    assert fixes == [("parkvani", "parkovani")]
    assert ss.correct("snídaně", df) == ("snídaně", [])           # známé slovo se nemění
    assert ss.correct("snídani", df)[1] == []                     # jiný pád – stačí shoda kmene
    assert ss.correct("xyzqwv", df) == ("xyzqwv", [])             # nesmysl se neopravuje na cokoli
    assert ss.correct("450", df)[1] == []                         # čísla nechat


def test_complete_last_word():
    K, _s, _a, _b = _kb()
    df = K.index("h1").bm.df
    assert ss.complete_last("kde je nabij", df) == "kde je nabijecka"
    assert ss.complete_last("sauna ", df) == "sauna "             # po mezeře je slovo dopsané
    assert ss.complete_last("sa", df) == "sa"                     # moc krátké


def test_known_words_narrow_a_question():
    K, _s, a, _b = _kb()
    df = K.index("h1").bm.df
    assert asyncio.run(K.search("h1", "kolik stojí parkování", audiences=("guest", "staff"), lexical=True)) == []
    assert ss.known_words("kolik stojí parkování", df) == ["parkovani"]
    hits = asyncio.run(ss.closest(K, "h1", "kolik stojí parkování", df, 6))
    assert hits and hits[0]["doc_id"] == a and "Parkování" in hits[0]["section"]
    assert ss.known_words("helikoptéra střecha", df) == []      # nezná nic – nehádat
    assert asyncio.run(ss.closest(K, "h1", "helikoptéra střecha", df, 6)) == []
    assert ss.known_words("parkování", df) == []                # jedno slovo se nezužuje


def test_snippet_shows_the_matching_place():
    t = ("Úvodní odstavec o hotelu a jeho historii. " * 12) + "Nabíječka pro elektromobily je na místě číslo 7. " + ("Závěr. " * 40)
    s = ss.snippet(t, ["nabijecka"])
    assert "Nabíječka" in s and len(s) <= ss.SNIPPET + 6, s
    assert s.startswith("… ")
    assert ss.snippet("krátký text", ["cokoli"]) == "krátký text"


def test_learned_hits():
    items = [{"q": "Máte žehličku?", "a": "Ano, na recepci k zapůjčení zdarma."},
             {"q": "Do you have a crib?", "a": "Yes, free of charge."}]
    assert [x["q"] for x in ss.learned_hits(items, "žehlička")] == ["Máte žehličku?"]
    assert ss.learned_hits(items, "bazén") == []
    assert ss.learned_hits(items, "") == []


def test_instant_search_is_lexical_and_finds():
    K, _s, a, _b = _kb()
    hits = asyncio.run(K.search("h1", "parkovani", audiences=("guest", "staff"), lexical=True))
    assert hits and hits[0]["doc_id"] == a
    assert asyncio.run(K.search("h1", "helikoptera", audiences=("guest", "staff"), lexical=True)) == []


def test_query_log_suggestions_and_zero_results():
    _K, store, _a, _b = _kb()
    for _ in range(3):
        ss.log_query(store, "h1", "Parkování cena", 2)
    ss.log_query(store, "h1", "sauna", 1)
    ss.log_query(store, "h1", "půjčovna kol", 0)
    ss.log_query(store, "h2", "cizí hotel", 1)
    rows = store.x("SELECT q, n, zero, last_at FROM kb_queries WHERE hotel_id=%s", ("h1",), "all")
    assert len(rows) == 3
    assert ss.suggestions(rows, "") == ["Parkování cena", "sauna"]      # nejčastější první, nenalezené ne
    assert ss.suggestions(rows, "par") == ["Parkování cena"]
    assert ss.suggestions(rows, "cen") == ["Parkování cena"]            # i podle druhého slova
    assert ss.suggestions(rows, "sauna") == []                          # co už je napsané, nenabízet
    ss.log_query(store, "h1", "půjčovna kol", 1)                        # po doplnění báze už se najde
    rows = store.x("SELECT q, n, zero, last_at FROM kb_queries WHERE hotel_id=%s", ("h1",), "all")
    assert "půjčovna kol" in ss.suggestions(rows, "")


def test_clicks_move_document_up():
    K, store, a, b = _kb()
    hits = [{"doc_id": a, "score": 0.03}, {"doc_id": b, "score": 0.02}]
    assert [h["doc_id"] for h in ss.rerank([dict(h) for h in hits], {})] == [a, b]
    for _ in range(3):
        ss.log_click(store, "h1", "reklamace pokoje", b)
    rows = store.x("SELECT qn, doc_id, n FROM kb_clicks WHERE hotel_id=%s", ("h1",), "all")
    boost = ss.boosts(rows, "Reklamace pokoje?")
    assert boost == {b: 3}
    assert [h["doc_id"] for h in ss.rerank([dict(h) for h in hits], boost)] == [b, a]
    assert ss.boosts(rows, "sauna") == {}                               # jiný dotaz se neovlivní
    assert ss.boosts(rows, "reklamace pokoje hosta") == {b: 3}          # podobný ano


def test_query_log_is_capped():
    _K, store, _a, _b = _kb()
    old = ss.MAX_QUERIES_PER_HOTEL
    ss.MAX_QUERIES_PER_HOTEL = 120
    try:
        for i in range(125):
            ss.log_query(store, "h1", f"dotaz cislo {i:04d}", 1)
        n = store.x("SELECT COUNT(*) FROM kb_queries WHERE hotel_id=%s", ("h1",), "one")[0]
        assert n <= 120, n
    finally:
        ss.MAX_QUERIES_PER_HOTEL = old


def test_worth_learning():
    assert ss.worth_learning("půjčujete kola")
    assert ss.worth_learning("defibrilátor")
    assert not ss.worth_learning("kol")
    assert not ss.worth_learning("a")


def test_add_pending_feeds_alex_learn():
    now = datetime(2026, 11, 23, 10, 0, 0)
    a = {"total": 0, "topics": {}, "unanswered": []}
    assert al.add_pending(a, "Půjčujete kola?", "cs", now, "Hledání v portálu")
    assert not al.add_pending(a, "půjčujete kola", "cs", now)             # týž dotaz podruhé ne
    assert not al.add_pending(a, "  ", "cs", now)
    p = al.pending(a, now)
    assert len(p) == 1 and p[0]["q"] == "Půjčujete kola?"
    hotel = {}
    al.add_answer(hotel, a, "Půjčujete kola?", "Ano, 300 Kč na den.", now, "cs")
    assert al.pending(a, now) == []
    assert ss.learned_hits(al.learned(hotel), "kola")                     # a hledání ji příště najde


def test_parse_proposals():
    raw = ('Zde jsou návrhy:\n[{"q": "Kolik stojí parkování?", "options": ["Parkování je zdarma.", "Parkování stojí ___ Kč za noc.", "Parkování je zdarma."]},'
           ' {"q": "Máte žehličku?", "options": ["Ano."]},'
           ' {"q": "kolik stoji parkovani", "options": ["x"]},'
           ' {"q": "Je nutná rezervace?", "options": []},'
           ' "nesmysl", {"q": "ab", "options": ["y"]},'
           ' {"q": "Je parkoviště hlídané?", "options": ["Ano, kamerami.", 5, "Ne."]}]\nHotovo.')
    out = ss.parse_proposals(raw, known_questions=["Máte žehličku?"])
    assert [x["q"] for x in out] == ["Kolik stojí parkování?", "Je parkoviště hlídané?"], out
    assert out[0]["options"] == ["Parkování je zdarma.", "Parkování stojí ___ Kč za noc."]     # bez opakování
    assert out[1]["options"] == ["Ano, kamerami.", "Ne."]
    assert ss.parse_proposals("omlouvám se, nevím") == []
    assert ss.parse_proposals("[rozbitý json") == []
    assert ss.parse_proposals('{"q": "x"}') == []
    many = "[" + ",".join('{"q": "Otázka číslo %d?", "options": ["Ano."]}' % i for i in range(9)) + "]"
    assert len(ss.parse_proposals(many)) == ss.PROPOSALS_MAX


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
