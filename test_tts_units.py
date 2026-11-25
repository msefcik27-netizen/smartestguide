# -*- coding: utf-8 -*-
"""Testy čtení jednotek nahlas (nález testera 24. 11. 2026)."""
import pytest

import tts_units as tu


@pytest.mark.parametrize("vstup, cekam", [
    ("Výška garáže je 2,1 m.", "Výška garáže je dvě celé jedna metru."),
    ("Šířka 190 cm.", "Šířka sto devadesát centimetrů."),
    ("Auto má 1 m.", "Auto má jeden metr."),
    ("Jsou to 2 m.", "Jsou to dva metry."),
    ("Je to 5 km daleko.", "Je to pět kilometrů daleko."),
    ("Cesta trvá 2 min.", "Cesta trvá dvě minuty."),
    ("Cesta trvá 15 min pěšky.", "Cesta trvá patnáct minut pěšky."),
    ("Bazén má 25 m a 28 °C.", "Bazén má dvacet pět metrů a dvacet osm stupňů Celsia."),
    ("Sleva 20 % na masáže.", "Sleva dvacet procent na masáže."),
    ("Sleva 20% na masáže.", "Sleva dvacet procent na masáže."),
    ("Pokoj má 32 m².", "Pokoj má třicet dva metrů čtverečních."),
    ("Je to do 5 km.", "Je to do pěti kilometrů."),
    ("Je to do 1 km.", "Je to do jednoho kilometru."),
    ("Z 2 m to nejde.", "Ze dvou metrů to nejde."),
    ("Místo 250 × 500 cm.", "Místo dvě stě padesát krát pět set centimetrů."),
    ("Dojdeš tam za 20–30 min.", "Dojdeš tam za dvacet až třicet minut."),
    ("Rychlost 30 km/h.", "Rychlost třicet kilometrů za hodinu."),
    ("Láhev 0,5 l.", "Láhev nula celá pět litru."),
    ("Stezka má 1 250 m.", "Stezka má tisíc dvě stě padesát metrů."),
    ("Otevřeno 24 h.", "Otevřeno dvacet čtyři hodin."),
    ("Otevřeno 8–10.", "Otevřeno 8 až 10."),
    ("Rozměr 2 x 3.", "Rozměr 2 krát 3."),
])
def test_jednotky(vstup, cekam):
    assert tu.cz_units_for_speech(vstup) == cekam


@pytest.mark.parametrize("veta", [
    "Máme rychlé 5G připojení.",              # ne „pět gramů“
    "Snídaně je od 7:00 do 10:00.",           # časy řeší jiné pravidlo
    "Cena je 250 Kč.",
    "Pokoj 312 je ve 3. patře.",
    "Zavolej na 224 811 234.",
    "Bez čísel beze změny.",
    "Velikost L máme skladem, 3 kusy.",
])
def test_jednotky_nesaha(veta):
    assert tu.cz_units_for_speech(veta) == veta


@pytest.mark.parametrize("cela, des, cekam", [
    ("1", "4", "jedna celá čtyři"),
    ("2", "1", "dvě celé jedna"),
    ("5", "5", "pět celých pět"),
    ("0", "5", "nula celá pět"),
    ("1", "05", "jedna celá nula pět"),
])
def test_desetinna(cela, des, cekam):
    assert tu.desetinne_slovy(cela, des) == cekam


def test_cislo_slovy_rod():
    assert tu.cislo_slovy(1, "m") == "jeden"
    assert tu.cislo_slovy(2, "m") == "dva"
    assert tu.cislo_slovy(2, "f") == "dvě"
    assert tu.cislo_slovy(1, "n") == "jedno"
    assert tu.cislo_slovy(222, "m") == "dvě stě dvacet dva"
