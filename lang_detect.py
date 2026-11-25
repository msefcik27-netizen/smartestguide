# -*- coding: utf-8 -*-
"""Rozpoznání jazyka zprávy hosta — čistě textové, bez sítě a bez modelu.

Zásada (7. 8. 2026): platí jazyk, který má host v aplikaci vybraný. Přepíná se JEN
při jednoznačném důkazu z textu.

Přepsáno 25. 11. 2026 po noční zkoušce. Původní verze brala za důkaz i písmena,
která sdílí víc jazyků: „è“ a „ç“ znamenaly francouzštinu, takže italská věta
(„a che ora è la colazione“) i portugalská („pequeno-almoço“) dostaly odpověď
francouzsky; „à“ ve vietnamštině = francouzština, „ä/ö“ ve švédštině = němčina.
Thajské a hindské písmo se nepoznalo vůbec, maďarština bez „ő/ű“ také ne.

Nově:
 1. písmo (azbuka, řečtina, thajština, dévanágarí, CJK, arabština, hebrejština),
 2. výslovná prosba hosta („in italiano“, „in English“, „auf Deutsch“),
 3. písmena, která má jediný jazyk (ř ě ů, ł ą ę, ő ű, ß, ñ, ã õ …),
 4. typická slova; sdílená písmena jen přidají bod jazykům, které je používají.
Vybraný jazyk hosta má přednost: jiný jazyk musí vést aspoň o 2 body.
"""
import re
import unicodedata

SUPPORTED = {
    "cs", "sk", "en", "de", "fr", "it", "es", "pl", "hu", "ru", "uk", "zh", "ja", "ko",
    "nl", "pt", "ar", "tr", "el", "ro", "sv", "da", "no", "fi", "hr", "sl", "sr", "bg",
    "he", "et", "lv", "lt", "hi", "th", "vi", "id",
}

_CYR_LANGS = {"ru", "uk", "bg", "sr", "mk", "be"}
_BG_WORDS = {"колко", "къде", "добър", "благодаря", "има", "закуска", "закуската", "моля",
             "вечер", "часа", "какво", "искам", "може", "стая", "стаята", "здравейте", "няма"}
_SR_CHARS = "ђћљњџј"
_UK_CHARS = "іїєґ"

# Jazyky tak blízké, že se detekce plete — volba hosta vyhrává.
_SIMILAR = [{"cs", "sk"}, {"da", "no"}, {"hr", "sr", "sl"}]


def norm_code(code: str, default: str = "en") -> str:
    """Kód jazyka z aplikace → dvoupísmenný podporovaný kód. Cokoli jiného = default.
    (Do zadání pro model se tak nedostane libovolný text poslaný místo kódu jazyka.)"""
    c = (code or "").strip().lower()[:2]
    if c == "nb" or c == "nn":
        c = "no"
    return c if c in SUPPORTED else default


def script_lang(text: str, selected: str = ""):
    """Jazyk podle písma — nejspolehlivější signál."""
    has = set()
    for ch in text:
        o = ord(ch)
        if 0x0400 <= o <= 0x04FF: has.add("cyr")
        elif 0x0370 <= o <= 0x03FF: has.add("gr")
        elif 0x3040 <= o <= 0x30FF: has.add("kana")
        elif 0xAC00 <= o <= 0xD7AF: has.add("hangul")
        elif 0x4E00 <= o <= 0x9FFF: has.add("cjk")
        elif 0x0600 <= o <= 0x06FF: has.add("ar")
        elif 0x0590 <= o <= 0x05FF: has.add("he")
        elif 0x0E00 <= o <= 0x0E7F: has.add("th")
        elif 0x0900 <= o <= 0x097F: has.add("hi")
    if "kana" in has: return "ja"
    if "hangul" in has: return "ko"
    if "cjk" in has:
        return "ja" if selected == "ja" else "zh"
    if "th" in has: return "th"
    if "hi" in has: return "hi"
    if "ar" in has: return "ar"
    if "he" in has: return "he"
    if "gr" in has: return "el"
    if "cyr" in has:
        low = text.lower()
        if any(c in low for c in _UK_CHARS): return "uk"
        if any(c in low for c in _SR_CHARS): return "sr"
        words = set(re.findall(r"[^\W\d_]+", low))
        if words & _BG_WORDS: return "bg"
        if selected in _CYR_LANGS: return selected
        return "ru"
    return None


# Výslovná prosba o jazyk. Vždy s předložkou, aby „my wife doesn't speak Czech“ nic nepřepnulo.
_EXPLICIT = [
    ("en", r"\bin english\b|\benglish,? please\b|\bauf englisch\b|\ben anglais\b|\ben ingl[eé]s\b|\bin inglese\b"),
    ("de", r"\bauf deutsch\b|\bin german\b|\ben allemand\b"),
    ("fr", r"\ben fran[cç]ais\b|\bin french\b|\bauf franz[oö]sisch\b"),
    ("es", r"\ben espa[nñ]ol\b|\bin spanish\b|\bauf spanisch\b"),
    ("it", r"\bin italiano\b|\bin italian\b|\bauf italienisch\b"),
    ("pt", r"\bem portugu[eê]s\b|\bin portuguese\b"),
    ("pl", r"\bpo polsku\b|\bin polish\b"),
    ("nl", r"\bin het nederlands\b|\bin dutch\b"),
    ("cs", r"\bin czech\b|\bauf tschechisch\b|\bpo česku\b"),
    ("sk", r"\bpo slovensky\b|\bin slovak\b"),
    ("hu", r"\bmagyarul\b|\bin hungarian\b"),
]
_EXPLICIT = [(c, re.compile(p, re.I)) for c, p in _EXPLICIT]

_VI_CHARS = set("ơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ")

# Písmena, která má (mezi podporovanými jazyky) jediný jazyk.
_UNIQ_CHARS = [
    ("cs", "řěů"),
    ("pl", "ąęłńśźż"),
    ("sk", "ľĺŕ"),
    ("hu", "őű"),
    ("tr", "ğı"),
    ("ro", "ășțşţ"),
    ("es", "ñ¿¡"),
    ("pt", "ãõ"),
    ("de", "ß"),
    ("fr", "œ"),
    ("it", "ìò"),
]

# Sdílená písmena: sama nerozhodnou, jen přidají bod jazykům, které je používají.
_SHARED_CHARS = {
    "ç": ("fr", "pt", "tr"), "è": ("fr", "it"), "à": ("fr", "it", "pt"), "ù": ("fr", "it"),
    "ê": ("fr", "pt"), "â": ("fr", "pt", "ro", "tr"), "ô": ("fr", "pt", "sk"), "î": ("fr", "ro"),
    "ë": ("fr", "nl"), "ï": ("fr", "nl"), "û": ("fr",),
    "ä": ("de", "sv", "fi", "sk"), "ö": ("de", "sv", "fi", "hu", "tr"), "ü": ("de", "hu", "tr"),
    "å": ("sv", "da", "no", "fi"), "ø": ("da", "no"), "æ": ("da", "no"),
    "č": ("cs", "sk", "hr", "sl"), "š": ("cs", "sk", "hr", "sl"), "ž": ("cs", "sk", "hr", "sl"),
    "đ": ("hr",), "ć": ("hr", "pl"), "ý": ("cs", "sk"), "ď": ("cs", "sk"), "ť": ("cs", "sk"),
    "ň": ("cs", "sk"),
}

# Typická slova — BEZ diakritiky (text se před porovnáním diakritiky zbaví).
_WORDS = {
    "en": "the what where when is are can do does you your please thank thanks have how there and "
          "with for my we hello hi breakfast time much could would which this that it password",
    "de": "ich ist das und nicht wo wann wie kann bitte danke der die ein eine haben sie mit auf "
          "gibt zimmer fruhstuck guten wir es uhr",
    "fr": "je est les des une pas pour avec vous merci bonjour quelle ou que dans le la beaucoup "
          "bonsoir quel heure combien petit dejeuner il",
    "es": "que los las una para por con hay gracias hola donde cual el es en un esta desayuno "
          "cuanto hora buenas noches cuesta",
    "it": "che sono una per con del della non grazie ciao dove quando il la un ora colazione "
          "quanto buonasera adesso favore costa",
    "pt": "voce obrigado obrigada onde quando para com uma nao isso esta por que ola boa noite "
          "horas pequeno almoco quanto custa ha tem cafe manha",
    "nl": "het een ik niet waar wanneer hoe kan dank alstublieft wij zijn van met goedenavond "
          "laat ontbijt wat kost mijn hond",
    "pl": "jest gdzie kiedy jak czy dziekuje prosze nie tak mam moge pokoj sniadanie dzien dobry "
          "ktorej ile kosztuje",
    "cs": "je kde kdy jak prosim dekuji diky mam mate muzu muzete kolik pokoj pokoje snidane ano "
          "jsem jste vam ti dobry den vecer stoji",
    "sk": "kde kedy ako prosim dakujem mozem mozete izba ranajky som ste vam kolko dobry",
    "hu": "jo mikor van hol mennyi koszonom kerem reggeli szoba nem igen estet napot mennyibe "
          "kerul mi parkolo jelszo",
    "sv": "hej nar var hur tack frukost frukosten och jag kan inte rum serveras vad kostar finns",
    "da": "hej hvornar hvor hvordan tak morgenmad og jeg kan ikke vaerelse er der hvad koster",
    "no": "hei nar hvor hvordan takk frokost og jeg kan ikke rom er det hva koster",
    "fi": "hei missa milloin kiitos aamiainen huone mihin aikaan on ei kylla paljonko maksaa "
          "tarjoillaan",
    "ro": "buna unde cand cum multumesc va rog micul dejun camera este la ce ora seara cat costa",
    "hr": "dobar dan dobra vecer gdje kada kako hvala molim dorucak soba je koliko kosta",
    "tr": "merhaba nerede ne zaman nasil tesekkur lutfen kahvalti oda var mi kac saat",
    "id": "selamat dimana kapan terima kasih sarapan kamar berapa jam ada apa",
}
_WORDS = {k: set(v.split()) for k, v in _WORDS.items()}


def _strip_marks(s: str) -> str:
    s = s.replace("ø", "o").replace("æ", "ae").replace("ł", "l").replace("đ", "d").replace("ß", "ss")
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _similar(a: str, b: str) -> bool:
    return any(a in g and b in g for g in _SIMILAR)


def detect_strong(text: str, selected: str = ""):
    """Vrátí kód jazyka jen tehdy, když je důkaz jednoznačný. Jinak None."""
    t = (text or "").strip()
    if len(t) < 3:
        return None
    sc = script_lang(t, selected)
    if sc:
        return sc
    low = t.lower()
    for code, rx in _EXPLICIT:
        if rx.search(low):
            return code
    if any(c in _VI_CHARS for c in low):
        return "vi"
    for code, chars in _UNIQ_CHARS:
        if any(c in low for c in chars):
            return code
    words = set(re.findall(r"[^\W\d_]+", _strip_marks(low)))
    scores = {}
    for code, ws in _WORDS.items():
        n = len(words & ws)
        if n:
            scores[code] = n
    for ch, codes in _SHARED_CHARS.items():
        if ch in low:
            for code in codes:
                scores[code] = scores.get(code, 0) + 1
    if not scores:
        return None
    best = max(scores, key=lambda k: scores[k])
    second = max([v for k, v in scores.items() if k != best], default=0)
    if scores[best] < 2 or scores[best] <= second:
        return None
    # Vybraný jazyk má přednost: i on má v textu oporu → jiný jazyk musí vést aspoň o 2.
    if selected and selected != best and scores[best] < scores.get(selected, 0) + 2:
        return None
    return best


def lock_language(selected: str, message: str):
    """(jazyk odpovědi, bylo_přepnuto). Jazyk = volba hosta; přepíná jen jednoznačný důkaz."""
    sel = norm_code(selected)
    strong = detect_strong(message or "", sel)
    if not strong or strong == sel or strong not in SUPPORTED:
        return sel, False
    if _similar(strong, sel):
        return sel, False
    return strong, True
