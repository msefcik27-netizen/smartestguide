# -*- coding: utf-8 -*-
"""Pojistky nad hotovou odpovědí Alex — čistě textové, bez sítě a bez modelu.

1) ŽENSKÝ ROD V PRVNÍ OSOBĚ (nález testera 4. 8. a znovu 24. 11. 2026).
   Alex mluví ženským hlasem, takže o sobě musí mluvit v ženském rodě. Pravidlo
   v zadání pro model nestačí a původní pojistka v app.py chytala jen příčestí
   stojící TĚSNĚ vedle „jsem / bych“. Věta „Abych ti to mohl ověřit“ jí proto
   utekla — mezi „abych“ a „mohl“ stojí dvě slova.
   Tady se hledá v celém úseku věty za „jsem / nejsem / bych / abych / kdybych“
   (slovensky „som“): příčestí minulé (ověřil → ověřila), přídavná jména
   (jistý → jistá, rád → ráda) a označení role (asistent → asistentka).
   Úsek končí u čárky, tečky nebo spojky, aby se pravidlo nedostalo k jinému
   podmětu („jsem ráda, že host přišel“ — „přišel“ zůstává).

2) CIZÍ PÍSMO (nález testera 24. 11. 2026: v české odpovědi „Персонал“).
   Když se odpovídá jazykem psaným latinkou a host sám azbukou nepsal, slova
   v azbuce se přepíšou do latinky.
"""
import re

# ── 1) Ženský rod ────────────────────────────────────────────────────────────

_TRIGGERS_CS = {"jsem", "nejsem", "bych", "abych", "kdybych"}
_TRIGGERS_SK = {"som"}
_TRIGGERS = _TRIGGERS_CS | _TRIGGERS_SK

# U těchto slov úsek končí — za nimi může stát jiný podmět než Alex.
_STOP = {
    "že", "jsi", "jste", "bys", "byste", "by", "kdo", "co", "který", "která",
    "které", "kteří", "když", "kdyby", "aby", "abys", "abyste", "jak", "kde",
    "kdy", "zda", "jestli", "pokud", "protože", "neboť", "a", "i", "ale",
    "nebo", "či", "ani", "host", "hosté", "pan", "pán", "on", "kolega",
    "ste", "keď", "alebo", "ktorý", "ktorá", "jaký", "jaká", "jaké", "než",
    "kolegyně", "recepční", "personál", "hotel",
}
_OKNO = 5                # kolik slov za spouštěčem se prohlíží

# Slova končící na -l, která NEJSOU příčestí. Bez tohohle seznamu by z věty
# „bohužel jsem to nenašla“ vzniklo „bohužela“.
_NE_SLOVESO = {
    "bohužel", "žel", "dál", "opodál", "půl", "napůl", "kol", "zel",
    "hotel", "motel", "hostel", "kostel", "sál", "areál", "personál", "detail",
    "email", "mail", "festival", "karneval", "materiál", "originál", "terminál",
    "portál", "kanál", "signál", "tunel", "kabel", "panel", "model", "level",
    "gril", "koktejl", "fotbal", "volejbal", "basketbal", "futsal", "nohejbal",
    "účel", "díl", "podíl", "rozdíl", "úkol", "cíl", "manuál", "rituál",
    "speciál", "symbol", "alkohol", "protokol", "pytel", "anděl", "apríl",
    "kapitál", "potenciál", "interval", "bál", "král", "sokol", "popel",
    "uhel", "úhel", "kotel", "osel", "orel", "kašel", "omyl", "úmysl", "smysl",
    "průmysl", "výmysl", "nesmysl", "automobil", "mobil", "profil", "styl",
    "textil", "titul", "modul", "vestibul", "obal", "sandál", "skandál",
    "žurnál", "seriál", "pool", "meal", "deal", "cool", "school", "manžel",
    "chmel", "zřetel", "datel", "kemp", "bowl", "normál", "ideál", "kriminál",
    "minerál", "metal", "kanál", "vokál", "lokál", "bazal", "sval", "val",
    "příval", "sál", "žal", "pól", "gól", "idol", "atol", "karamel", "cukl",
    "mol", "nul", "kypl", "apel", "skalpel", "hřídel", "jetel", "kužel",
    "pantofel", "kufl", "pudl", "fenykl", "artikl", "cyklus", "výdel",
    # 2. pád množného čísla: „seznam jídel“, „pár mil“, „nabídka vil“
    "těl", "mil", "škol", "vil", "skal", "hal", "pil", "žil", "sil", "cel",
    "křídel", "kouzel", "jmel", "jatel", "mul", "stodol", "kobyl", "víl",
    "tabul", "cibul", "košil", "chvil", "neděl", "postel", "koupel", "ocel",
    "mysl", "sůl", "hůl", "fazol", "akvarel", "bordel", "pastel", "duel",
    "tunel", "kartel", "hřebel", "normal", "ideal", "local", "special",
    "meal", "final", "total", "real", "hostel", "spiel", "ziel", "viel",
}
# Koncovky 2. pádu množného čísla středního rodu: jídel, čísel, skel, pravidel.
_NE_KONCOVKY = ("tel", "ůl", "del", "sel", "kel")

# Přídavná jména a příslovce v první osobě (česky).
_ADJ_CS = {
    "rád": "ráda", "nerad": "nerada", "sám": "sama",
    "jistý": "jistá", "jist": "jistá", "schopný": "schopná", "schopen": "schopna",
    "připravený": "připravená", "připraven": "připravena",
    "ochotný": "ochotná", "ochoten": "ochotna",
    "přesvědčený": "přesvědčená", "přesvědčen": "přesvědčena",
    "potěšený": "potěšená", "potěšen": "potěšena", "nadšený": "nadšená",
    "šťastný": "šťastná", "vděčný": "vděčná", "zvědavý": "zvědavá",
    "hotový": "hotová", "oprávněný": "oprávněná", "oprávněn": "oprávněna",
    "povinen": "povinna", "nucen": "nucena", "naučený": "naučená",
    "vyškolený": "vyškolená", "překvapený": "překvapená", "zmatený": "zmatená",
    "omezený": "omezená", "spokojený": "spokojená", "dostupný": "dostupná",
    "informovaný": "informovaná", "informován": "informována",
    "naprogramovaný": "naprogramovaná", "naprogramován": "naprogramována",
    "vytvořený": "vytvořená", "vytvořen": "vytvořena",
    "navržený": "navržená", "navržen": "navržena",
    "určený": "určená", "určen": "určena", "zaměřený": "zaměřená",
    "propojený": "propojená", "propojen": "propojena",
    "napojený": "napojená", "napojen": "napojena",
}
_ADJ_SK = {
    "rád": "rada", "nerád": "nerada", "sám": "sama", "istý": "istá",
    "pripravený": "pripravená", "schopný": "schopná", "ochotný": "ochotná",
    "presvedčený": "presvedčená", "šťastný": "šťastná", "vďačný": "vďačná",
    "potešený": "potešená", "nadšený": "nadšená",
}

# „Jsem tvůj virtuální asistent“ → „Jsem tvoje virtuální asistentka“.
_ROLE = {
    "asistent": "asistentka", "pomocník": "pomocnice", "průvodce": "průvodkyně",
    "rádce": "rádkyně", "společník": "společnice", "poradce": "poradkyně",
    "sprievodca": "sprievodkyňa", "pomocník_sk": "pomocníčka",
}
# Přívlastky před rolí — mění se jen tehdy, když za nimi role opravdu stojí.
_ROLE_PRIVL = {
    "tvůj": "tvoje", "váš": "vaše", "hotelový": "hotelová", "chytrý": "chytrá",
    "nový": "nová", "elektronický": "elektronická", "umělý": "umělá",
    "váš_sk": "vaša", "tvoj": "tvoja",
}

_JIT_RE = re.compile(
    r"^(ne)?(|na|při|ode|ve|vy|pře|pro|za|do|ob|se|u|roze|vze|po|pří|pode|nade|pri|od|pre)šel$")
_SLOVO_RE = re.compile(r"^[a-záäčďéěíĺľňóôŕřšťúůýž]+$")
_SOUHL = "bcčdďfghjklĺľmnňpqrŕřsštťvwxzž"


def _je_pricesti(w: str) -> bool:
    """Vypadá slovo jako příčestí minulé mužského rodu (ověřil, mohl, našel)?"""
    lw = w.lower()
    if len(lw) < 3 or not lw.endswith("l") or lw.endswith("ll"):
        return False
    if not _SLOVO_RE.match(lw):
        return False
    if lw in _NE_SLOVESO or lw.endswith(_NE_KONCOVKY):
        return False
    return True


def _zensky_cs(w: str) -> str:
    """ověřil → ověřila, mohl → mohla, našel → našla (ale slyšel → slyšela)."""
    if _JIT_RE.match(w.lower()):
        return w[:-2] + "la"
    return w + "a"


def _zensky_sk(w: str) -> str:
    """povedal → povedala, mohol → mohla, našiel → našla, bol → bola."""
    lw = w.lower()
    if lw.endswith("šiel"):
        return w[:-3] + "la"
    if lw in ("bol", "nebol"):
        return w + "a"
    if lw.endswith("ol") and len(lw) >= 4 and lw[-3] in _SOUHL:
        return w[:-2] + "la"
    return w + "a"


def _velikost(vzor: str, nove: str) -> str:
    """Zachová velké počáteční písmeno původního slova."""
    return nove[0].upper() + nove[1:] if vzor[:1].isupper() else nove


def _oprav_slovo(w: str, sk: bool, prvni_ve_vete: bool):
    """Vrátí ženský tvar slova, nebo None, když se slovo měnit nemá."""
    lw = w.lower()
    adj = _ADJ_SK if sk else _ADJ_CS
    if lw in adj:
        return _velikost(w, adj[lw])
    # Velké písmeno uprostřed věty = vlastní jméno (Karel, Hotel Grandium) — nesahat.
    if w[:1].isupper() and not prvni_ve_vete:
        return None
    if _je_pricesti(w):
        return _zensky_sk(w) if sk else _zensky_cs(w)
    return None


_KONEC_USEKU_RE = re.compile(r"[,.;:!?()\[\]\n\"„“”«»—–]")
_KONEC_VETY_RE = re.compile(r"[.!?\n]\s*$")


def feminine_first_person(text: str) -> str:
    """Přepíše mužské tvary, kterými Alex mluví O SOBĚ, na ženské.
    Na oslovení hosta nesahá: tam se „jsem / bych“ nevyskytuje."""
    if not text:
        return text
    low = text.lower()
    if not any(t in low for t in _TRIGGERS):
        return text
    tok = re.split(r"(\w+)", text)          # liché indexy = slova, sudé = oddělovače
    slova = list(range(1, len(tok), 2))

    def _zacatek_vety(i: int) -> bool:
        return i == 1 and not tok[0].strip() or bool(_KONEC_VETY_RE.search(tok[i - 1]))

    for pos, i in enumerate(slova):
        trig = tok[i].lower()
        if trig not in _TRIGGERS:
            continue
        sk = trig in _TRIGGERS_SK

        # Slovo TĚSNĚ před spouštěčem: „ověřil jsem si“, „rád bych“, „mohl bych“.
        # Slovensky stojí mezi nimi ještě „by“: „rád by som“, „mohol by som“.
        if pos > 0:
            j = slova[pos - 1]
            if sk and pos > 1 and tok[j].lower() == "by" and not _KONEC_USEKU_RE.search(tok[j + 1]):
                j = slova[pos - 2]
            if not _KONEC_USEKU_RE.search(tok[j + 1]):
                nove = _oprav_slovo(tok[j], sk, _zacatek_vety(j))
                if nove:
                    tok[j] = nove

        # Úsek ZA spouštěčem: „abych ti to mohl ověřit“, „jsem si jistý“.
        okno = []
        for k in range(pos + 1, min(pos + 1 + _OKNO, len(slova))):
            j = slova[k]
            if _KONEC_USEKU_RE.search(tok[j - 1]):
                break
            if tok[j].lower() in _STOP or tok[j].lower() in _TRIGGERS:
                break
            okno.append(j)
        for j in okno:
            nove = _oprav_slovo(tok[j], sk, False)
            if nove:
                tok[j] = nove
        # Role („jsem tvůj asistent“) — jen po „jsem / som“, ne po „bych“.
        if trig in ("jsem", "som"):
            for n, j in enumerate(okno):
                lw = tok[j].lower()
                role = _ROLE.get(lw)
                if sk and lw == "pomocník":
                    role = _ROLE["pomocník_sk"]
                if not role:
                    continue
                tok[j] = _velikost(tok[j], role)
                for jj in okno[:n]:
                    p = tok[jj].lower()
                    if sk and p == "váš":
                        tok[jj] = _velikost(tok[jj], _ROLE_PRIVL["váš_sk"])
                    elif p in _ROLE_PRIVL:
                        tok[jj] = _velikost(tok[jj], _ROLE_PRIVL[p])
    return "".join(tok)


# „Rád“ se slovesem v první osobě — tady žádné „jsem / bych“ není:
# „Rád ti pomůžu“, „rád vám to zjistím“.
_RAD_SLOVESA = (
    "pomohu|pomůžu|poradím|zjistím|ověřím|doporučím|zařídím|předám|pošlu|ukážu|"
    "vysvětlím|připravím|odpovím|udělám|zodpovím|najdu|doplním|řeknu|povím|"
    "pomáhám|přidám|podívám|zeptám|navrhnu|upřesním|potvrdím|poradím|vyřídím|"
    "zprostředkuji|zprostředkuju|přeložím|napíšu|shrnu|popíšu|seženu|dohledám"
)
_RAD_SLOVESO_RE = re.compile(
    r"\b([Rr])ád((?:\s+\w+){0,3}?\s+(?:" + _RAD_SLOVESA + r"))\b")


def feminine_rad(text: str) -> str:
    """„Rád ti pomůžu“ → „Ráda ti pomůžu“."""
    if not text or "ád" not in text:
        return text
    return _RAD_SLOVESO_RE.sub(lambda m: m.group(1) + "áda" + m.group(2), text)


def feminine_fix(text: str) -> str:
    """Obě pojistky ženského rodu za sebou."""
    return feminine_rad(feminine_first_person(text))


# ── 2) Cizí písmo ────────────────────────────────────────────────────────────

# Jazyky psané cyrilicí — tam je azbuka v pořádku.
_CYR_LANGS = {"ru", "uk", "bg", "sr", "mk", "be", "kk", "ky", "mn", "tg"}
_CYR_RE = re.compile(r"[Ѐ-ӿ]")
_CYR_WORD_RE = re.compile(r"[A-Za-zÀ-žЀ-ӿ]*[Ѐ-ӿ][A-Za-zÀ-žЀ-ӿ]*")

# Hotová slova pro češtinu a slovenštinu — holý přepis by ztratil čárky.
_CYR_SLOVNIK = {
    "cs": {"персонал": "personál", "ресепшн": "recepce", "ресепшен": "recepce",
           "отель": "hotel", "номер": "pokoj", "завтрак": "snídaně",
           "также": "také", "можно": "lze", "или": "nebo", "и": "a"},
    "sk": {"персонал": "personál", "ресепшн": "recepcia", "ресепшен": "recepcia",
           "отель": "hotel", "номер": "izba", "завтрак": "raňajky",
           "также": "tiež", "или": "alebo", "и": "a"},
}

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "jo", "ж": "ž",
    "з": "z", "и": "i", "й": "j", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "ch", "ц": "c",
    "ч": "č", "ш": "š", "щ": "šč", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "ju",
    "я": "ja", "і": "i", "ї": "ji", "є": "je", "ґ": "g", "ј": "j", "љ": "lj",
    "њ": "nj", "ћ": "ć", "ђ": "đ", "џ": "dž", "ў": "u",
}
# Jazyky bez háčků dostanou přepis bez nich.
_BEZ_HACKU = {"ž": "zh", "č": "ch", "š": "sh", "šč": "shch", "ch": "kh", "c": "ts",
              "j": "y", "jo": "yo", "ju": "yu", "ja": "ya", "ji": "yi", "je": "ye"}
_S_HACKY = {"cs", "sk", "pl", "hr", "sl", "lt", "lv"}
# Písmena azbuky, která vypadají stejně jako latinka — model je občas vloží
# doprostřed jinak latinkou psaného slova („rесерсе“).
_STEJNE = {"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
           "к": "k", "м": "m", "т": "t", "в": "b", "н": "h", "і": "i", "ј": "j"}


def _prepis(word: str, lang: str) -> str:
    out = []
    for ch in word:
        lat = _TRANSLIT.get(ch.lower())
        if lat is None:
            out.append(ch)
            continue
        if lang not in _S_HACKY:
            lat = _BEZ_HACKU.get(lat, lat)
        out.append(lat.capitalize() if ch.isupper() and lat else lat)
    return "".join(out)


def fix_foreign_script(text: str, lang: str, user_message: str = "") -> str:
    """Slova v azbuce přepíše do latinky, když se odpovídá jazykem psaným latinkou.
    Když host sám psal azbukou, nechává vše být — mohl se ptát na konkrétní slovo."""
    if not text or not _CYR_RE.search(text):
        return text
    lang2 = (lang or "").strip().lower()[:2]
    if lang2 in _CYR_LANGS or _CYR_RE.search(user_message or ""):
        return text
    slovnik = _CYR_SLOVNIK.get(lang2, {})

    def _rep(m):
        w = m.group(0)
        cyr = [c for c in w if _CYR_RE.match(c)]
        if len(cyr) < len(w):
            # Smíšené slovo: latinka s pár zatoulanými písmeny azbuky.
            return "".join(
                (_STEJNE.get(c.lower(), _TRANSLIT.get(c.lower(), c)).upper() if c.isupper()
                 else _STEJNE.get(c.lower(), _TRANSLIT.get(c.lower(), c)))
                if _CYR_RE.match(c) else c for c in w)
        hotove = slovnik.get(w.lower())
        if hotove:
            return _velikost(w, hotove)
        return _prepis(w, lang2)

    return _CYR_WORD_RE.sub(_rep, text)
