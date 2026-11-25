"""Rozpoznání textu ze skenovaných PDF pro znalostní bázi.

Sken čte model dvakrát nezávisle (dva různé modely). Do báze se dostane jen to, co obstojí:
  • model má přepisovat doslova a místo hádání psát [nečitelné],
  • každé číslo (cena, čas, telefon, datum) musí obě čtení přečíst stejně – jinak se v textu nahradí
    značkou [nečitelné] a strana se vypíše hotelu ke kontrole,
  • strana, kterou se nepodařilo přečíst podruhé, zůstane bez čísel,
  • hotel u dokumentu vidí, že text vznikl rozpoznáním, a může si ho celý zobrazit.
Raději chybějící údaj než vymyšlený: na [nečitelné] Alex odpoví, že to neví.
"""
import os, re, io, base64, asyncio, logging, difflib
from collections import Counter
from datetime import datetime

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.getenv("KB_OCR_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5"
CHECK_MODEL = os.getenv("KB_OCR_CHECK_MODEL", "claude-haiku-5-5").strip() or "claude-haiku-5-5"
MAX_PAGES = 30                 # strop stran na jeden dokument
PAGES_PER_CALL = 3
PAGES_PER_DAY = 150            # strop stran na hotel a den (synchronizace disku může přinést desítky skenů)
PARALLEL = 4
UNREADABLE = "[nečitelné]"
NOTE_PREFIX = "Text rozpoznán ze skenu"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5")

SYSTEM = (
    "You transcribe scanned document pages for a hotel knowledge base. Your output is used to answer hotel guests, "
    "so a wrong price, time or phone number does real harm, while a gap does not.\n"
    "Transcribe exactly what is printed, in the original language and wording. Do not translate, summarise, correct, "
    "complete or add anything, and do not describe the page.\n"
    f"If a word or a number cannot be read with certainty, write {UNREADABLE} in its place. Never guess – above all "
    "never guess digits. A partly legible number is unreadable as a whole.\n"
    "Leave nothing readable out. Write tables row by row with ' | ' between cells. Ignore logos, pictures, stamps and "
    "signatures. Include handwriting only where it is clearly legible.\n"
    "Start every page with a line of its own in the form === strana N ===, where N is the number of the page within "
    "this file, starting at 1. For an empty page write only that line.\n"
    "Output the transcription only, with no introduction and no closing remark."
)
USER = "Přepiš doslova všechny strany tohoto dokumentu podle pravidel."

_PAGE = re.compile(r"^\s*=+\s*strana\s+(\d+)\s*=+\s*$", re.I | re.M)
_NUM = re.compile(r"\d+(?:[  ]\d{3})+(?!\d)|\d+")
_used = {}                     # (hotel_id, den) -> počet stran; po restartu se počítá znovu


def numbers(text: str) -> Counter:
    """Čísla v textu; '1 200' a '1200' je totéž číslo."""
    return Counter(re.sub(r"\s", "", m.group(0)) for m in _NUM.finditer(text or ""))


def split_pages(text: str, count: int) -> dict:
    """Rozdělí přepis podle řádků '=== strana N ===' na {N: text}. Bez značek je celý text strana 1 (u jedné strany),
    jinak nic – rozdělení se pak nedá věřit."""
    text = (text or "").strip()
    marks = list(_PAGE.finditer(text))
    if not marks:
        return {1: text} if count == 1 and text else {}
    out = {}
    for i, m in enumerate(marks):
        n = int(m.group(1))
        body = text[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(text))].strip()
        if 1 <= n <= count and n not in out:
            out[n] = body
    return out


def mask_numbers(text: str, only=None) -> tuple:
    """Nahradí čísla značkou [nečitelné]; only = množina čísel, kterých se to týká (None = všechna). Vrací (text, kolik)."""
    hit = 0

    def rep(m):
        nonlocal hit
        if only is None or re.sub(r"\s", "", m.group(0)) in only:
            hit += 1
            return UNREADABLE
        return m.group(0)
    return _NUM.sub(rep, text or ""), hit


def similar(a: str, b: str) -> float:
    wa, wb = re.findall(r"\w+", (a or "").lower()), re.findall(r"\w+", (b or "").lower())
    if not wa and not wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def reconcile(first: str, second) -> dict:
    """Porovná dvě čtení jedné strany. second=None = druhé čtení chybí.
    Vrací {text, masked, unverified, differs}: v textu zůstanou jen čísla, která obě čtení přečetla stejně."""
    first = (first or "").strip()
    if second is None:
        text, n = mask_numbers(first)
        return {"text": text, "masked": n, "unverified": True, "differs": False}
    a, b = numbers(first), numbers(second)
    bad = {t for t, n in a.items() if b.get(t, 0) != n}
    text, n = mask_numbers(first, bad) if bad else (first, 0)
    missed = any(a.get(t, 0) < n2 for t, n2 in b.items())       # druhé čtení vidělo číslo navíc
    return {"text": text, "masked": n, "unverified": False,
            "differs": bool(missed) or (len(first) > 80 and similar(first, second) < 0.6)}


def _pages_cs(pages: list) -> str:
    return ", ".join(str(p) for p in pages[:20]) + (" …" if len(pages) > 20 else "")


def build_note(*, total: int, done: int, masked_pages: list, masked: int, unverified: list, differs: list,
               unreadable: int, failed: list) -> str:
    """Poznámka u dokumentu – co hotel potřebuje vědět a zkontrolovat."""
    s = [f"{NOTE_PREFIX} ({done} str.) – zkontrolujte ho proti originálu."]
    if total > done + len(failed):
        s.append(f"Dokument má {total} stran, čteme prvních {MAX_PAGES}.")
    if failed:
        s.append(f"Strany {_pages_cs(failed)} se přečíst nepodařilo.")
    if masked:
        s.append(f"Čísla, která se nepodařilo potvrdit, jsou vynechána ({masked}×, str. {_pages_cs(masked_pages)}).")
    if unverified:
        s.append(f"Str. {_pages_cs(unverified)} bez druhého čtení, proto bez čísel.")
    if differs:
        s.append(f"Na str. {_pages_cs(differs)} se čtení liší víc než obvykle.")
    if unreadable:
        s.append(f"Nečitelných míst: {unreadable}.")
    return " ".join(s)[:900]


def split_pdf(data: bytes, max_pages: int = MAX_PAGES, per: int = PAGES_PER_CALL) -> tuple:
    """Rozdělí PDF na části po několika stranách. Vrací (celkem stran, [(první strana, počet, bajty)])."""
    from pypdf import PdfReader, PdfWriter
    rd = PdfReader(io.BytesIO(data))
    if rd.is_encrypted:
        rd.decrypt("")
    total = len(rd.pages)
    parts = []
    for start in range(0, min(total, max_pages), per):
        w = PdfWriter()
        n = 0
        for i in range(start, min(start + per, total, max_pages)):
            w.add_page(rd.pages[i])
            n += 1
        buf = io.BytesIO()
        w.write(buf)
        parts.append((start + 1, n, buf.getvalue()))
    return total, parts


async def call_model(client, api_key: str, model: str, pdf: bytes) -> str:
    """Jeden přepis části PDF. Vrací text, nebo vyhodí výjimku (chyba, odmítnutí, useknutá odpověď)."""
    body = {"model": model, "max_tokens": 16000, "system": SYSTEM, "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                "data": base64.standard_b64encode(pdf).decode("ascii")}},
                {"type": "text", "text": USER}]}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    extra_h, extra_b = {}, {}
    if model.startswith(_FALLBACK_MODELS):      # při odmítnutí ať stejný požadavek dokončí záložní model
        extra_h, extra_b = {"anthropic-beta": _FALLBACK_BETA}, {"fallbacks": "default"}
    r = None
    for attempt in range(3):
        r = await client.post(API_URL, headers={**headers, **extra_h}, json={**body, **extra_b}, timeout=240.0)
        if r.status_code == 400 and (extra_b or "output_config" in body):
            # parametr, který tenhle model nezná – zkusit holý požadavek
            logging.warning("OCR %s: 400 %s – zkouším bez doplňkových parametrů", model, r.text[:200])
            extra_h, extra_b = {}, {}
            body.pop("output_config", None)
            continue
        if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
            await asyncio.sleep(2 + 4 * attempt)
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"AI {r.status_code}: {r.text[:200]}")
    j = r.json()
    if j.get("stop_reason") in ("refusal", "max_tokens"):
        raise RuntimeError("AI: " + str(j.get("stop_reason")))
    return "".join(p.get("text", "") for p in j.get("content", []) if p.get("type") == "text")


def _day() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def budget_left(hotel_id: str) -> int:
    return max(0, PAGES_PER_DAY - _used.get((hotel_id, _day()), 0))


def _spend(hotel_id: str, pages: int):
    d = _day()
    for k in [k for k in _used if k[1] != d]:
        _used.pop(k, None)
    _used[(hotel_id, d)] = _used.get((hotel_id, d), 0) + pages


async def ocr_pdf(data: bytes, api_key: str, *, hotel_id: str = "", call=None, client=None) -> tuple:
    """Přečte skenované PDF. Vrací (text, poznámka); prázdný text = nepodařilo se (poznámka říká proč).
    call(model, pdf_bajty) -> text je vyměnitelné kvůli testům."""
    if not api_key and call is None:
        return "", "PDF neobsahuje text (sken) – nahrajte verzi s textem"
    try:
        total, parts = await asyncio.to_thread(split_pdf, data)
    except Exception as e:
        logging.warning("OCR: PDF nejde rozdělit: %s", e)
        return "", "PDF se nepodařilo otevřít (sken) – nahrajte verzi s textem"
    if not parts:
        return "", "PDF nemá žádné strany"
    want = sum(n for _s, n, _b in parts)
    if hotel_id and budget_left(hotel_id) < want:
        return "", f"Sken dnes nepřečteme – denní limit rozpoznávání je {PAGES_PER_DAY} stran. Zkuste to zítra."
    if hotel_id:
        _spend(hotel_id, want)
    own = None
    if call is None:
        if client is None:
            import httpx
            own = client = httpx.AsyncClient()

        async def call(model, pdf):
            return await call_model(client, api_key, model, pdf)
    sem = asyncio.Semaphore(PARALLEL)

    async def read(model, pdf, count):
        async with sem:
            for attempt in range(2):
                try:
                    pages = split_pages(await call(model, pdf), count)
                    if pages:
                        return pages
                except Exception as e:
                    logging.warning("OCR %s: %s", model, e)
            return None
    try:
        res = await asyncio.gather(*[asyncio.gather(read(MODEL, b, n), read(CHECK_MODEL, b, n)) for _s, n, b in parts])
    finally:
        if own:
            await own.aclose()
    out, failed, masked_pages, unverified, differs, masked = [], [], [], [], [], 0
    for (start, n, _b), (first, second) in zip(parts, res):
        for i in range(1, n + 1):
            page = start + i - 1
            if not first or i not in first:
                failed.append(page)
                continue
            r = reconcile(first[i], (second or {}).get(i))
            if r["masked"]:
                masked += r["masked"]
                masked_pages.append(page)
            if r["unverified"] and r["masked"]:
                unverified.append(page)
                masked_pages.pop()
            if r["differs"]:
                differs.append(page)
            if r["text"]:
                out.append(f"[strana {page}]\n{r['text']}")
    text = "\n\n".join(out).strip()
    if len(re.sub(r"\[[^\]]*\]|\s", "", text)) < 40:
        return "", "Sken se nepodařilo přečíst – nahrajte čitelnější verzi nebo verzi s textem"
    done = want - len(failed)
    return text, build_note(total=total, done=done, masked_pages=masked_pages, masked=masked, unverified=unverified,
                            differs=differs, unreadable=text.count(UNREADABLE) - masked, failed=failed)
