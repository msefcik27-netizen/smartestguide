# Texty portálu hotelu (hotel.html) – hlídá nálezy testera z 24. 11. 2026:
# portál ukládá sám, takže nesmí chtít po hoteliérovi „uložit“ tlačítkem, které tam není,
# a věty o ukládání musí mít anglický překlad.
# Spuštění: python -m pytest test_portal_texts.py -q
import re
from pathlib import Path

SRC = (Path(__file__).parent / "hotel.html").read_text(encoding="utf-8")


def en_map():
    block = SRC[SRC.index("const EN_MAP = {"):]
    return dict(re.findall(r'"((?:[^"\\]|\\.)+)"\s*:\s*"((?:[^"\\]|\\.)*)"', block[:block.index("\n};")]))


def test_portal_says_it_saves_by_itself():
    assert 'id="last-saved">Ukládá se automaticky<' in SRC
    assert "Změny se ukládají samy" in SRC


def test_saving_texts_have_english_translation():
    m = en_map()
    for cs in ("Ukládá se automaticky", "✓ Automaticky uloženo v", "Ukládá se…"):
        assert m.get(cs, "").strip(), f"chybí anglický překlad: {cs}"
        assert m[cs] != cs


def test_portal_does_not_ask_to_press_a_save_button():
    # pokyn typu „klikněte na Uložit“ / „nezapomeňte uložit“ mate – nic se potvrzovat nemusí
    bad = re.findall(r"(?i)(?:klikn|stiskn|zmáčkn|nezapome)[^<\"'`\n]{0,40}ulo[zž]|tlačítk[^<\"'`\n]{0,30}Ulož", SRC)
    assert bad == []


def test_room_cards_open_through_the_print_endpoint():
    assert "/room-cards?rooms=" in SRC
