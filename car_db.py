"""Databáze rozměrů aut pro parkování („vejde se moje auto?").

Model (Alex) nahlásí auto značkou [[CAR:výška,šířka,délka|Značka Model varianta rok]]. Rozměry má z hlavy
a o pár centimetrů se v nich plete. Tady se název auta spáruje s tabulkou ověřených údajů (car_dims.json,
u každého řádku je zdroj) a rozměry se z ní zpřesní.

Pravidlo: špatná shoda je horší než žádná. Proto
  - značka i model musí být v názvu celým slovem,
  - z více kandidátů (generace, karoserie) vyhrává ten, který je nejblíž rozměrům od modelu,
  - shoda platí jen tehdy, když se od rozměrů modelu neliší o víc než TOL_CM v žádném rozměru
    (větší rozdíl znamená jinou generaci nebo variantu → databáze se nepoužije),
  - přepisují se jen rozměry, které model uvedl (0 = neznámé, např. výška se střešním boxem, zůstává neznámé).

Čisté funkce, bez FastAPI a bez sítě. Testy v test_car_db.py.
"""
import json
import os
import re
import unicodedata

TOL_CM = 10
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "car_dims.json")
_CACHE = None

MAKE_ALIASES = {
    "vw": "volkswagen", "mercedes": "mercedes benz", "merc": "mercedes benz", "mb": "mercedes benz",
    "landrover": "land rover", "alfa": "alfa romeo", "skodovka": "skoda",
}
# Zápisy modelů, které lidé (a model) píšou jinak než tabulka: „BMW 320d" / „řada 3" → „3 Series", „C 220" / „třída C" → „C-Class".
_REWRITES = [
    (r"(?<![a-z0-9])bmw (?:rady|rada|reihe) ([1-8])(?![a-z0-9])", r"bmw \1 series"),
    (r"(?<![a-z0-9])bmw ([1-8])er(?![a-z0-9])", r"bmw \1 series"),
    (r"(?<![a-z0-9])bmw ([1-8])\d\d[a-z]{0,2}(?![a-z0-9])", r"bmw \1 series"),
    (r"(?<![a-z0-9])bmw ([1-8])(?= |$)(?! series)", r"bmw \1 series"),
    (r"(?<![a-z0-9])(?:trida|tridy|klasse|klasa) ([abcegsv])(?![a-z0-9])", r"\1 class"),
    (r"(?<![a-z0-9])([abcegsv]) (?:trida|klasse|klasa|klass)(?![a-z0-9])", r"\1 class"),
    (r"(?<![a-z0-9])mercedes benz ([abcegsv]) ?\d{2,3}[a-z]{0,2}(?![a-z0-9])", r"mercedes benz \1 class"),
    (r"(?<![a-z0-9])id([3-7])(?![a-z0-9])", r"id \1"),
]
# Slova pro tutéž karoserii nebo délku v různých jazycích a u různých značek.
_SYNONYMS = [
    {"combi", "kombi", "estate", "wagon", "touring", "tourer", "variant", "avant", "sw", "sportswagon", "break",
     "sportback wagon", "sports tourer", "touring sports", "shooting brake", "t modell"},
    {"sedan", "saloon", "limousine", "limuzina"},
    {"hatchback", "hatch", "liftback"},
    {"long", "lang", "lwb", "dlouha", "dlouhy", "l2", "maxi"},
    {"extra long", "extralang", "extra dlouha", "xl", "l3"},
    {"compact", "kompakt", "kompaktni", "short", "kratka", "kratky", "swb", "l1"},
]


def norm(s) -> str:
    """Malá písmena, bez diakritiky, jen písmena a číslice oddělené jednou mezerou."""
    s = unicodedata.normalize("NFKD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _has(text: str, phrase: str) -> bool:
    return bool(phrase) and re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def _valid(e) -> bool:
    try:
        return (isinstance(e, dict) and norm(e.get("make")) and norm(e.get("model"))
                and 2400 <= int(e["length_mm"]) <= 7500 and 1400 <= int(e["width_mm"]) <= 2300
                and 1100 <= int(e["height_mm"]) <= 3100)
    except (KeyError, TypeError, ValueError):
        return False


def prepare(rows) -> list:
    """Řádky z JSON → interní záznamy s rozměry v cm (zaokrouhleno NAHORU: u garáže je bezpečnější větší auto)."""
    out = []
    for e in rows or []:
        if not _valid(e):
            continue
        make = norm(e["make"])
        out.append({
            "make": MAKE_ALIASES.get(make, make), "model": norm(e["model"]), "variant": norm(e.get("variant")),
            "generation": norm(e.get("generation")),
            "year_from": e.get("year_from") if isinstance(e.get("year_from"), int) else None,
            "year_to": e.get("year_to") if isinstance(e.get("year_to"), int) else None,
            "cm": tuple(-(-int(e[k]) // 10) for k in ("height_mm", "width_mm", "length_mm")),   # (výška, šířka, délka)
            "label": " ".join(str(e.get(k) or "").strip() for k in ("make", "model", "variant")).strip(),
            "source": str(e.get("source") or ""),
        })
    return out


def load() -> list:
    global _CACHE
    if _CACHE is None:
        try:
            with open(_PATH, "r", encoding="utf-8") as f:
                _CACHE = prepare(json.load(f))
        except (OSError, ValueError):
            _CACHE = []
    return _CACHE


def _variant_hit(name: str, variant: str) -> bool:
    """Je varianta záznamu (karoserie, délka) zmíněná v názvu? Bere i synonyma (Combi = Estate = Touring…)."""
    if not variant:
        return False
    if _has(name, variant):
        return True
    for group in _SYNONYMS:
        if any(_has(variant, w) for w in group) and any(_has(name, w) for w in group):
            return True
    return False


def canon(name) -> str:
    """Název auta sjednocený na zápis tabulky (značka plným jménem, „řada 3" → „3 series" apod.)."""
    n = norm(name)
    for alias, full in MAKE_ALIASES.items():
        if _has(n, alias) and not _has(n, full):
            n = re.sub(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", full, n, count=1)
    if _has(n, "range rover") and not _has(n, "land rover"):
        n = "land rover " + n
    for pat, rep in _REWRITES:
        n = re.sub(pat, rep, n)
    return n


def candidates(name, rows=None) -> list:
    """Záznamy, které odpovídají názvu auta: značka + nejdelší shodný model, zúžené o variantu, generaci a rok."""
    n = canon(name)
    if not n:
        return []
    rows = load() if rows is None else rows
    hits = [e for e in rows if _has(n, e["make"]) and _has(n, e["model"])]
    if not hits:
        return []
    longest = max(len(e["model"]) for e in hits)          # „Yaris Cross" má přednost před „Yaris"
    hits = [e for e in hits if len(e["model"]) == longest]
    with_variant = [e for e in hits if _variant_hit(n, e["variant"])]
    if with_variant:
        hits = with_variant
    with_gen = [e for e in hits if e["generation"] and len(e["generation"]) >= 2 and _has(n, e["generation"])]
    if with_gen:
        hits = with_gen
    m = re.search(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)", n)
    if m:
        y = int(m.group(1))
        in_range = [e for e in hits if e["year_from"] and e["year_from"] <= y <= (e["year_to"] or 9999)]
        if in_range:
            hits = in_range
    return hits


def lookup(name, car, rows=None):
    """Zpřesní rozměry auta podle databáze.
    car = (výška, šířka, délka) v cm od modelu, neznámý rozměr None.
    → {"car": (v, š, d), "label": "Škoda Octavia Combi", "source": url} nebo None, když shoda není jistá."""
    if not car or not any(car):
        return None
    best, best_d, tie = None, None, False
    for e in candidates(name, rows):
        diffs = [abs(c - d) for c, d in zip(car, e["cm"]) if c]
        if not diffs or max(diffs) > TOL_CM:
            continue
        d = sum(diffs)
        if best is None or d < best_d:
            best, best_d, tie = e, d, False
        elif d == best_d and e["cm"] != best["cm"]:
            tie = True
    if best is None or tie:
        return None
    return {"car": tuple(d if c else None for c, d in zip(car, best["cm"])), "label": best["label"], "source": best["source"]}
