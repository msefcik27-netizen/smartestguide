# Texty tiskovin (print_i18n.py) – hlídá nálezy testera z 24. 11. 2026:
# na tiskovině v cizím jazyce nesmí zůstat anglický nebo prázdný text.
# Spuštění: python -m pytest test_print_i18n.py -q
import re

import pytest

import print_i18n

CODES = [c for c, _n in print_i18n.options()]
TEXT_KEYS = ("n", "h", "sub", "scan", "long", "noapp", "langs", "tag", "room", "a")


def test_every_language_is_offered_exactly_once():
    assert len(CODES) == len(set(CODES)) >= 30
    assert CODES[0] == "en" and "cs" in CODES


@pytest.mark.parametrize("code", CODES)
def test_language_has_all_texts(code):
    t = print_i18n.get(code)
    assert t["code"] == code
    for k in TEXT_KEYS:
        assert isinstance(t[k], str) and t[k].strip(), f"{code}: chybí text „{k}“"
    assert len(t["f"]) == 5 and all(x.strip() for x in t["f"]), f"{code}: výhod má být pět"
    assert "{a}" in t["sub"], f"{code}: v podtitulku chybí místo pro jméno asistentky"
    assert "<" not in t["tag"], f"{code}: řádek pod logem nesmí obsahovat značky"
    assert re.search(r"\d", t["langs"]), f"{code}: „100+ jazyků“ má obsahovat číslo"
    assert isinstance(t["rtl"], bool)


@pytest.mark.parametrize("code", [c for c in CODES if c != "en"])
def test_translation_is_not_left_in_english(code):
    en, t = print_i18n.get("en"), print_i18n.get(code)
    for k in ("h", "sub", "scan", "long", "noapp", "langs"):
        assert t[k] != en[k], f"{code}: „{k}“ zůstalo anglicky"
    assert t["f"] != en["f"], f"{code}: výhody zůstaly anglicky"
    # hlavní řádek tiskovin („Váš osobní AI concierge“) nesmí být anglická věta
    # („personal“, „scan“ a „for“ jsou správně i v jiných jazycích, proto jen jednoznačná slova)
    assert not re.search(r"\b(your|the)\b", t["tag"] + " " + t["long"], re.I), f"{code}: anglická slova v nadpisu"


def test_room_word_differs_where_it_should():
    assert print_i18n.get("cs")["room"] == "Pokoj" and print_i18n.get("en")["room"] == "Room"
    assert print_i18n.get("de")["room"] == "Zimmer"
    assert sum(1 for c in CODES if print_i18n.get(c)["room"] == "Room") == 1


def test_right_to_left_only_for_arabic_and_hebrew():
    assert {c for c in CODES if print_i18n.get(c)["rtl"]} == {"ar", "he"}


def test_unknown_and_regional_codes_fall_back_safely():
    assert print_i18n.get("cz")["code"] == "cs"
    assert print_i18n.get("de-AT")["code"] == "de"
    assert print_i18n.get("")["code"] == "en"
    assert print_i18n.get("xx")["code"] == "en"
    assert print_i18n.get(None)["code"] == "en"
