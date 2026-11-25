# -*- coding: utf-8 -*-
"""Měrné jednotky a čísla u nich pro HLAS (čeština).

Nález testera 24. 11. 2026: „Neumí správně vyslovovat čísla a měrné jednotky.“
Čísla se pro hlas upravují od září 2026 (app.py, _cz_numbers_for_speech), ale
jednotky ne — hlas dostával „2,1 m“ nebo „190 cm“ a zkratku si domýšlel.
Tady se zkratka rozepíše celým slovem ve správném tvaru podle počtu
(1 metr, 2 metry, 5 metrů, 2,1 metru) a číslo se napíše slovy ve správném rodě
(dva metry, ale dvě minuty).

Mění se jen text posílaný do hlasu. V chatu zůstávají číslice a zkratky.
"""
import re

_JEDN = ("", "jedna", "dvě", "tři", "čtyři", "pět", "šest", "sedm", "osm", "devět")
_NACT = ("deset", "jedenáct", "dvanáct", "třináct", "čtrnáct", "patnáct",
         "šestnáct", "sedmnáct", "osmnáct", "devatenáct")
_DES = ("", "", "dvacet", "třicet", "čtyřicet", "padesát", "šedesát", "sedmdesát",
        "osmdesát", "devadesát")
_STA = ("", "sto", "dvě stě", "tři sta", "čtyři sta", "pět set", "šest set",
        "sedm set", "osm set", "devět set")
_TIS = ("", "tisíc", "dva tisíce", "tři tisíce", "čtyři tisíce", "pět tisíc",
        "šest tisíc", "sedm tisíc", "osm tisíc", "devět tisíc")


def cislo_slovy(n: int, rod: str = "f") -> str:
    """Celé číslo 0–9999 slovy. rod: m (jeden, dva), f (jedna, dvě), n (jedno, dvě)."""
    if n < 0 or n > 9999:
        return ""
    if n == 0:
        return "nula"
    casti = []
    tis, zb = divmod(n, 1000)
    if tis:
        casti.append(_TIS[tis])
    sto, zb = divmod(zb, 100)
    if sto:
        casti.append(_STA[sto])
    if 10 <= zb <= 19:
        casti.append(_NACT[zb - 10])
    else:
        des, jed = divmod(zb, 10)
        if des:
            casti.append(_DES[des])
        if jed:
            w = _JEDN[jed]
            if jed == 1:
                w = {"m": "jeden", "f": "jedna", "n": "jedno"}[rod]
            elif jed == 2 and rod == "m":
                w = "dva"
            casti.append(w)
    return " ".join(casti)


def desetinne_slovy(cela: str, des: str) -> str:
    """„2,1“ → „dvě celé jedna“, „1,5“ → „jedna celá pět“, „0,05“ → „nula celá nula pět“."""
    c = int(cela)
    a = cislo_slovy(c, "f")
    if not a:
        return ""
    if c == 1:
        cele = "celá"
    elif 2 <= c <= 4:
        cele = "celé"
    else:
        cele = "celých" if c else "celá"
    nuly = len(des) - len(des.lstrip("0"))
    zbytek = des.lstrip("0")
    b = " ".join(["nula"] * nuly + ([cislo_slovy(int(zbytek), "f")] if zbytek else []))
    return f"{a} {cele} {b}".strip()


# 2. pád číslovek po předložkách „od, do, z, kolem…“: do dvou metrů, do pěti kilometrů.
_GEN = {2: "dvou", 3: "tří", 4: "čtyř", 5: "pěti", 6: "šesti", 7: "sedmi", 8: "osmi",
        9: "devíti", 10: "deseti", 11: "jedenácti", 12: "dvanácti", 13: "třinácti",
        14: "čtrnácti", 15: "patnácti", 16: "šestnácti", 17: "sedmnácti",
        18: "osmnácti", 19: "devatenácti"}
_GEN_DES = {2: "dvaceti", 3: "třiceti", 4: "čtyřiceti", 5: "padesáti", 6: "šedesáti",
            7: "sedmdesáti", 8: "osmdesáti", 9: "devadesáti"}
_GEN_PREP = {"od", "do", "z", "ze", "kolem", "okolo", "bez", "během", "u", "podle"}


def _gen(n: int) -> str:
    if n in _GEN:
        return _GEN[n]
    if 20 <= n <= 99:
        d, j = divmod(n, 10)
        if j == 0:
            return _GEN_DES[d]
        if j == 1:
            return ""
        return _GEN_DES[d] + " " + _GEN[j]
    return ""


# zkratka → (rod, 1 kus, 2–4 kusy, 5 a víc, desetinné číslo)
_UNITS = {
    "km/h": ("m", "kilometr za hodinu", "kilometry za hodinu", "kilometrů za hodinu", "kilometru za hodinu"),
    "m²": ("m", "metr čtvereční", "metry čtvereční", "metrů čtverečních", "metru čtverečního"),
    "m2": ("m", "metr čtvereční", "metry čtvereční", "metrů čtverečních", "metru čtverečního"),
    "km": ("m", "kilometr", "kilometry", "kilometrů", "kilometru"),
    "cm": ("m", "centimetr", "centimetry", "centimetrů", "centimetru"),
    "mm": ("m", "milimetr", "milimetry", "milimetrů", "milimetru"),
    "kg": ("m", "kilogram", "kilogramy", "kilogramů", "kilogramu"),
    "ml": ("m", "mililitr", "mililitry", "mililitrů", "mililitru"),
    "dl": ("m", "decilitr", "decilitry", "decilitrů", "decilitru"),
    "°c": ("m", "stupeň Celsia", "stupně Celsia", "stupňů Celsia", "stupně Celsia"),
    "%": ("n", "procento", "procenta", "procent", "procenta"),
    "min": ("f", "minuta", "minuty", "minut", "minuty"),
    "hod": ("f", "hodina", "hodiny", "hodin", "hodiny"),
    "ks": ("m", "kus", "kusy", "kusů", "kusu"),
    "m": ("m", "metr", "metry", "metrů", "metru"),
    "g": ("m", "gram", "gramy", "gramů", "gramu"),
    "l": ("m", "litr", "litry", "litrů", "litru"),
    "h": ("f", "hodina", "hodiny", "hodin", "hodiny"),
}

# Číslo i s mezerou v tisících („1 250 m“). Zkratky jednotek jsou schválně citlivé
# na velikost písmen: „5 g“ jsou gramy, ale „5G“ je mobilní síť.
_NUM = r"(?:\d{1,2}[  ]\d{3}|\d{1,4})(?:,\d{1,2})?"
# Tečka za „min“/„hod“ patří ke zkratce jen uprostřed věty (následuje malé písmeno);
# na konci věty zůstává, aby hlas udělal pauzu.
_TECKA = r"(?:\.(?=\s+[a-zá-ž]))?"
_UNIT_ALT = r"km/h|m²|m2|km|cm|mm|kg|ml|dl|°C|%|min" + _TECKA + r"|hod" + _TECKA + r"|ks|m|g|l|h"
_SPOJ = r"\s?(?:×|x|krát|–|-|až)\s?"
_UNIT_RE = re.compile(
    r"(?:\b((?i:od|do|z|ze|kolem|okolo|bez|během|u|podle))\s+)?"
    r"(?<![\w,.:/+-])(?<!\d[  ])((?:" + _NUM + _SPOJ + r")*" + _NUM + r")\s?(" + _UNIT_ALT + r")"
    r"(?![\w²°/])")
_KRAT_RE = re.compile(r"(?<=\d)\s?[×x]\s?(?=\d)")
_AZ_RE = re.compile(r"(?<=\d)\s?–\s?(?=\d)")
_CAST_RE = re.compile(r"(" + _NUM + r")|(×|x|krát|–|-|až)")


def _cislo(s: str, rod: str) -> str:
    s = re.sub(r"[  ]", "", s)
    if "," in s:
        cela, des = s.split(",")
        return desetinne_slovy(cela, des)
    return cislo_slovy(int(s), rod)


def cz_units_for_speech(text: str) -> str:
    """„výška 2,1 m“ → „výška dvě celé jedna metru“, „190 cm“ → „sto devadesát centimetrů“,
    „do 5 km“ → „do pěti kilometrů“, „190 × 480 cm“ → „sto devadesát krát čtyři sta
    osmdesát centimetrů“, „20–30 min“ → „dvacet až třicet minut“."""
    if not text or not re.search(r"\d", text):
        return text

    def _rep(m):
        prep, cisla, zkr = m.group(1), m.group(2), m.group(3)
        u = _UNITS.get(zkr.lower().rstrip("."))
        if not u:
            return m.group(0)
        rod, jeden, par, mnoho, deset = u
        casti = _CAST_RE.findall(cisla)
        nums = [re.sub(r"[  ]", "", c[0]) for c in casti if c[0]]
        posledni = nums[-1]
        # Tvar jednotky podle POSLEDNÍHO čísla.
        if "," in posledni:
            tvar = deset
        else:
            n = int(posledni)
            tvar = jeden if n == 1 else par if 2 <= n <= 4 else mnoho
        out = []
        # „do 5 km“ → „do pěti kilometrů“ (jen jedno celé číslo; jinak základní tvar)
        if prep and len(nums) == 1 and "," not in posledni and prep.lower() in _GEN_PREP:
            if int(posledni) == 1:      # „do 1 km“ → „do jednoho kilometru“
                return f"{prep} {'jedné' if rod == 'f' else 'jednoho'} {deset}"
            g = _gen(int(posledni))
            if g:
                p = prep
                if p.lower() in ("z", "ze"):
                    ze = g[0] in "sšzž" or (g[0] not in "aeiouy" and g[1] not in "aeiouyěí")
                    p = ("Z" if prep[0].isupper() else "z") + ("e" if ze else "")
                return f"{p} {g} {mnoho}"
        for num, spoj in casti:
            if num:
                slovy = _cislo(num, rod)
                if not slovy:
                    return m.group(0)
                out.append(slovy)
            else:
                s = spoj.lower()
                out.append("krát" if s in ("×", "x", "krát") else "až")
        vysl = " ".join(out) + " " + tvar
        return (prep + " " + vysl) if prep else vysl

    text = _UNIT_RE.sub(_rep, text)
    # Rozměry a rozsahy bez jednotky: „2 × 3“ → „2 krát 3“, „8–10“ → „8 až 10“.
    text = _KRAT_RE.sub(" krát ", text)
    text = _AZ_RE.sub(" až ", text)
    return text
