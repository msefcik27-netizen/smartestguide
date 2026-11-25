"""Znalostní báze hotelu (KB): dokumenty → text → úryvky → index → hybridní vyhledávání.

Proč: Alex má odpovídat z dokumentace hotelu (provozní řády, ceníky, manuály, směrnice), ale do
promptu se nesmí posílat celé dokumenty. Každý dokument se proto jednou rozdělí na úryvky, ke
každému se uloží vektor (embedding) a při dotazu se vybere jen pár nejbližších úryvků. Odpověď je
přesná (stojí na konkrétním textu se zdrojem) a levná (stovky tokenů místo desetitisíců).

Vyhledávání je hybridní:
  * vektorové (kosinová podobnost) – rozumí významu i napříč jazyky (dotaz anglicky, dokument česky),
  * lexikální BM25 – přesná slova, čísla, názvy; funguje i bez klíče k embeddingům,
  * výsledky se slučují metodou RRF a propouští je práh relevance – co se dotazu netýká, do promptu nejde.

Úložiště: vlastní tabulky v Postgresu (DATABASE_URL), bez něj soubor SQLite (vývoj). Hlavní JSONB
řádek aplikace se nezvětšuje. Čisté funkce (dělení, BM25, slučování) mají testy v test_kb.py.
"""
import os, re, io, json, math, time, uuid, zipfile, hashlib, logging, sqlite3, threading, unicodedata
from array import array
from datetime import datetime


try:
    import numpy as _np
except Exception:  # numpy je jen zrychlení
    _np = None


EMB_MODEL = "text-embedding-3-small"
EMB_DIM = 512
CHUNK_TARGET = 900          # znaků na úryvek (~200–250 tokenů)
CHUNK_OVERLAP = 120
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_DOC_CHARS = 400_000
MAX_DOCS_PER_HOTEL = 300
MAX_CHUNKS_PER_HOTEL = 6000
VEC_MIN = 0.28              # práh kosinové podobnosti (text-embedding-3-small: nesouvisející ~0,1–0,2)
RRF_K = 60


AUDIENCES = ("guest", "staff")   # guest = smí použít i Alex pro hosty; staff = jen personál


SUPPORTED_EXT = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".html", ".htm", ".json", ".rtf"}


# ─────────────────────────────────────────────────────────────
# Úložiště
# ─────────────────────────────────────────────────────────────
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS kb_sources (
        id TEXT PRIMARY KEY, hotel_id TEXT NOT NULL, provider TEXT NOT NULL, label TEXT, account TEXT,
        audience TEXT, folders TEXT, tokens TEXT, status TEXT, last_sync TEXT, last_error TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS kb_docs (
        id TEXT PRIMARY KEY, hotel_id TEXT NOT NULL, source_id TEXT, ext_id TEXT, title TEXT, path TEXT, url TEXT,
        mime TEXT, audience TEXT, chars INTEGER, chunks INTEGER, etag TEXT, status TEXT, error TEXT,
        updated_at TEXT, enabled INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS kb_chunks (
        id TEXT PRIMARY KEY, hotel_id TEXT NOT NULL, doc_id TEXT NOT NULL, seq INTEGER, section TEXT,
        text TEXT, emb __BLOB__)""",
    "CREATE TABLE IF NOT EXISTS kb_meta (hotel_id TEXT PRIMARY KEY, version INTEGER, staff_key TEXT)",
    "CREATE TABLE IF NOT EXISTS kb_oauth (state TEXT PRIMARY KEY, hotel_id TEXT, provider TEXT, audience TEXT, via TEXT, created REAL)",
    """CREATE TABLE IF NOT EXISTS api_keys (
        id TEXT PRIMARY KEY, key_hash TEXT UNIQUE, prefix TEXT, kind TEXT, hotel_id TEXT, name TEXT,
        owner_email TEXT, scopes TEXT, daily_limit INTEGER, created_at TEXT, last_used TEXT, revoked INTEGER DEFAULT 0)""",
    "CREATE TABLE IF NOT EXISTS api_usage (key_id TEXT, day TEXT, kind TEXT, n INTEGER, PRIMARY KEY (key_id, day, kind))",
    """CREATE TABLE IF NOT EXISTS api_devs (
        email TEXT PRIMARY KEY, name TEXT, company TEXT, purpose TEXT, created_at TEXT, verify_hash TEXT, verified INTEGER DEFAULT 0)""",
    # paměť chytrého hledání v portálu (smart_search.py): co se hledá a na co se kliklo
    """CREATE TABLE IF NOT EXISTS kb_queries (
        hotel_id TEXT NOT NULL, qn TEXT NOT NULL, q TEXT, n INTEGER DEFAULT 0, zero INTEGER DEFAULT 0, last_at TEXT,
        PRIMARY KEY (hotel_id, qn))""",
    """CREATE TABLE IF NOT EXISTS kb_clicks (
        hotel_id TEXT NOT NULL, qn TEXT NOT NULL, doc_id TEXT NOT NULL, n INTEGER DEFAULT 0,
        PRIMARY KEY (hotel_id, qn, doc_id))""",
    "CREATE INDEX IF NOT EXISTS kb_docs_hotel ON kb_docs (hotel_id)",
    "CREATE INDEX IF NOT EXISTS kb_chunks_hotel ON kb_chunks (hotel_id)",
    "CREATE INDEX IF NOT EXISTS kb_chunks_doc ON kb_chunks (doc_id)",
    "CREATE INDEX IF NOT EXISTS kb_sources_hotel ON kb_sources (hotel_id)",
]


class Store:
    """Tenká vrstva nad Postgresem / SQLite. SQL se píše s %s; pro SQLite se převádí na ?."""

    def __init__(self, database_url: str = "", sqlite_path: str = ""):
        self.url = (database_url or "").strip()
        self.sqlite_path = sqlite_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb.sqlite3")
        self.pg = bool(self.url)
        self._lock = threading.Lock()
        self._ready = False

    def _connect(self):
        if self.pg:
            import psycopg2
            return psycopg2.connect(self.url, connect_timeout=10)
        c = sqlite3.connect(self.sqlite_path, timeout=30)
        return c

    def init(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            conn = self._connect()
            try:
                cur = conn.cursor()
                for s in _SCHEMA:
                    cur.execute(s.replace("__BLOB__", "BYTEA" if self.pg else "BLOB"))
                conn.commit()
            finally:
                conn.close()
            self._ready = True

    def x(self, sql: str, args=(), fetch: str = ""):
        """Jeden příkaz. fetch: '' | 'one' | 'all'."""
        return self.tx([(sql, args)], fetch=fetch)

    def tx(self, statements: list, fetch: str = ""):
        """Více příkazů v jedné transakci; vrací výsledek posledního (podle fetch)."""
        self.init()
        conn = self._connect()
        try:
            cur = conn.cursor()
            for sql, args in statements:
                if self.pg:
                    import psycopg2
                    args = tuple(psycopg2.Binary(a) if isinstance(a, (bytes, bytearray)) else a for a in args)
                else:
                    sql = sql.replace("%s", "?")
                cur.execute(sql, args)
            out = cur.fetchone() if fetch == "one" else (cur.fetchall() if fetch == "all" else None)
            conn.commit()
            return out
        finally:
            conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────
# Text z dokumentů
# ─────────────────────────────────────────────────────────────
def _decode(data: bytes) -> str:
    encs = ("utf-16",) if data[:2] in (bytes([255, 254]), bytes([254, 255])) else ("utf-8-sig", "cp1250")
    for enc in encs:
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "replace")

_XML_TAG = re.compile(r"<[^>]+>")


def _xml_unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)


def _docx_text(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    xml = z.read("word/document.xml").decode("utf-8", "replace")
    body = xml.split("<w:body", 1)[-1]
    out = []
    # tabulky: řádek = buňky oddělené " | "; odstavce: nadpisy označit "# "
    pos = 0
    for m in re.finditer(r"<w:tbl[ >].*?</w:tbl>|<w:p[ >].*?</w:p>", body, re.S):
        blk = m.group(0)
        if blk.startswith("<w:tbl"):
            for row in re.findall(r"<w:tr[ >].*?</w:tr>", blk, re.S):
                cells = []
                for c in re.findall(r"<w:tc[ >].*?</w:tc>", row, re.S):
                    cells.append(" ".join(_par_text(p) for p in re.findall(r"<w:p[ >].*?</w:p>", c, re.S)).strip())
                line = " | ".join(x for x in cells if x)
                if line:
                    out.append(line)
            out.append("")
        else:
            t = _par_text(blk).strip()
            if not t:
                out.append("")
                continue
            st = re.search(r'<w:pStyle w:val="([^"]+)"', blk)
            if st and re.match(r"(?i)(heading|nadpis|title|nzev|titul)", st.group(1)):
                out.append("")
                out.append("# " + t)
                out.append("")
            else:
                out.append(t)
                out.append("")
    return "\n".join(out)


def _par_text(p_xml: str) -> str:
    p_xml = re.sub(r"<w:tab/>", "\t", p_xml)
    p_xml = re.sub(r"<w:br[^>]*/>", "\n", p_xml)
    return _xml_unescape("".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", p_xml, re.S)) if "<w:t" in p_xml else "")


def _xlsx_text(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        sx = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        for si in re.findall(r"<si>(.*?)</si>", sx, re.S):
            shared.append(_xml_unescape("".join(re.findall(r"<t(?: [^>]*)?>(.*?)</t>", si, re.S))))
    sheet_names = []
    if "xl/workbook.xml" in names:
        sheet_names = re.findall(r'<sheet [^>]*name="([^"]+)"', z.read("xl/workbook.xml").decode("utf-8", "replace"))
    out = []
    sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    for i, n in enumerate(sheets):
        sx = z.read(n).decode("utf-8", "replace")
        title = _xml_unescape(sheet_names[i]) if i < len(sheet_names) else f"List {i + 1}"
        out.append("")
        out.append("# " + title)
        for row in re.findall(r"<row[ >].*?</row>", sx, re.S)[:3000]:
            cells = []
            for attrs, inner in re.findall(r"<c([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
                v = re.search(r"<v>(.*?)</v>", inner or "", re.S)
                if 't="s"' in attrs and v:
                    try:
                        cells.append(shared[int(v.group(1))])
                    except Exception:
                        pass
                elif 't="inlineStr"' in attrs:
                    cells.append(_xml_unescape("".join(re.findall(r"<t(?: [^>]*)?>(.*?)</t>", inner or "", re.S))))
                elif v:
                    cells.append(_xml_unescape(v.group(1)))
            line = " | ".join(c.strip() for c in cells if c and c.strip())
            if line:
                out.append(line)
    return "\n".join(out)


def _pptx_text(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                    key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
    out = []
    for i, n in enumerate(slides, 1):
        sx = z.read(n).decode("utf-8", "replace")
        pars = []
        for p in re.findall(r"<a:p[ >].*?</a:p>", sx, re.S):
            t = _xml_unescape("".join(re.findall(r"<a:t>(.*?)</a:t>", p, re.S))).strip()
            if t:
                pars.append(t)
        if pars:
            out += ["", f"# Snímek {i}: {pars[0][:80]}", "\n".join(pars[1:])]
    return "\n".join(out)


def _pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    if getattr(r, "is_encrypted", False):
        try:
            r.decrypt("")
        except Exception:
            pass
    out = []
    for p in r.pages[:600]:
        try:
            t = p.extract_text() or ""
        except Exception:
            t = ""
        if t.strip():
            out.append(t)
    return "\n\n".join(out)


def _html_text(data: bytes) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(_decode(data), "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    for h in soup.find_all(re.compile(r"^h[1-4]$")):
        h.insert_before("\n\n# ")
        h.insert_after("\n\n")
    return soup.get_text("\n")


def _rtf_text(data: bytes) -> str:
    s = _decode(data)
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes([int(m.group(1), 16)]).decode("cp1250", "replace"), s)
    s = re.sub(r"\\u(-?\d+)\??", lambda m: chr(int(m.group(1)) % 65536), s)
    s = re.sub(r"\\par[d]?\b", "\n", s)
    s = re.sub(r"\{\\\*[^{}]*\}", "", s)
    s = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", s)
    return s.replace("{", "").replace("}", "")


def file_ext(name: str) -> str:
    return os.path.splitext((name or "").lower())[1]


SCAN_NOTE = "PDF neobsahuje text (sken) – nahrajte verzi s textem"


def extract_text(name: str, data: bytes, mime: str = "") -> tuple:
    """Vrátí (text, poznámka). Prázdný text + poznámka = nešlo přečíst (důvod lidsky)."""
    ext = file_ext(name)
    mime = (mime or "").lower()
    if not ext:
        ext = {"application/pdf": ".pdf", "text/plain": ".txt", "text/html": ".html", "text/csv": ".csv",
               "text/markdown": ".md",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
               "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx"}.get(mime.split(";")[0], "")
    if len(data) > MAX_FILE_BYTES:
        return "", "soubor je větší než 20 MB"
    try:
        if ext == ".pdf":
            t = _pdf_text(data)
            if len(t.strip()) < 40:
                return "", SCAN_NOTE
        elif ext == ".docx":
            t = _docx_text(data)
        elif ext == ".xlsx":
            t = _xlsx_text(data)
        elif ext == ".pptx":
            t = _pptx_text(data)
        elif ext in (".html", ".htm"):
            t = _html_text(data)
        elif ext == ".rtf":
            t = _rtf_text(data)
        elif ext in (".txt", ".md", ".csv", ".json"):
            t = _decode(data)
        else:
            return "", "nepodporovaný typ souboru"
    except Exception as e:
        logging.warning("KB: čtení %s selhalo: %s", name, e)
        return "", "soubor se nepodařilo přečíst"
    t = clean_text(t)
    if not t:
        return "", "v souboru není žádný text"
    if len(t) > MAX_DOC_CHARS:
        return t[:MAX_DOC_CHARS], "dokument je dlouhý – zpracován začátek (400 tisíc znaků)"
    return t, ""


def clean_text(t: str) -> str:
    t = (t or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").replace("\u00ad", "")
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r" ?\n ?", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ─────────────────────────────────────────────────────────────
# Dělení na úryvky
# ─────────────────────────────────────────────────────────────
_HEAD_MD = re.compile(r"^#{1,6}\s+(.+)$")
_HEAD_NUM = re.compile(r"^(?:\d{1,2}(?:\.\d{1,2}){0,3}\.?|[IVX]{1,5}\.|Článek\s+\d+|Čl\.\s*\d+|§\s*\d+)\s+\S.{2,90}$")
_SENT = re.compile(r"(?<=[.!?…])\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ0-9„\"(])")


def _heading(line: str, next_blank: bool) -> str:
    s = line.strip()
    if not s or len(s) > 110:
        return ""
    m = _HEAD_MD.match(s)
    if m:
        return m.group(1).strip()
    if s.endswith((".", ",", ";", ":")) and not _HEAD_NUM.match(s):
        return ""
    if _HEAD_NUM.match(s) and not s.endswith("."):
        return s
    letters = [c for c in s if c.isalpha()]
    if len(letters) >= 4 and len(s) <= 70 and next_blank and all(c.isupper() for c in letters):
        return s
    return ""


def _split_long(par: str, target: int) -> list:
    """Dlouhý odstavec rozdělit po větách (nouzově po řádcích/znacích)."""
    if len(par) <= target * 1.3:
        return [par]
    parts = []
    for piece in (_SENT.split(par) if _SENT.search(par) else par.split("\n")):
        while len(piece) > target * 1.3:
            cut = piece.rfind(" ", 0, target)
            cut = cut if cut > target * 0.5 else target
            parts.append(piece[:cut].strip())
            piece = piece[cut:].strip()
        if piece:
            parts.append(piece)
    out, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) + 1 > target:
            out.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, target: int = CHUNK_TARGET, overlap: int = CHUNK_OVERLAP) -> list:
    """Rozdělí text na úryvky [(sekce, text)]. Drží odstavce pohromadě, nadpis jde s úryvkem jako sekce,
    sousední úryvky téže sekce se překrývají koncem předchozího (aby odpověď nepřepadla přes hranu)."""
    lines = clean_text(text).split("\n")
    blocks, cur, section = [], [], ""

    def flush():
        nonlocal cur
        if cur:
            blocks.append((section, "\n".join(cur).strip()))
            cur = []
    for i, ln in enumerate(lines):
        nxt_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
        prev_blank = i == 0 or not lines[i - 1].strip()
        h = _heading(ln, nxt_blank) if (prev_blank or _HEAD_MD.match(ln.strip())) else ""
        if h:
            flush()
            section = h[:120]
            continue
        if not ln.strip():
            flush()
            continue
        cur.append(ln)
    flush()
    chunks, buf, buf_sec = [], "", ""

    def emit():
        nonlocal buf
        if buf.strip():
            chunks.append((buf_sec, buf.strip()))
        buf = ""
    for sec, par in blocks:
        for piece in _split_long(par, target):
            if buf and (sec != buf_sec or len(buf) + len(piece) + 2 > target):
                tail = ""
                if sec == buf_sec and overlap and len(buf) > overlap:
                    tail = buf[-overlap:]
                    sp = tail.find(" ")
                    tail = tail[sp + 1:] if sp != -1 else tail
                emit()
                buf = (tail + "\n" if tail else "")
            buf_sec = sec
            buf = (buf + ("\n\n" if buf and not buf.endswith("\n") else "") + piece)
    emit()
    # slepit drobky (krátké úryvky bez vlastního obsahu) k sousedovi téže sekce
    out = []
    for sec, t in chunks:
        if out and len(t) < 120 and out[-1][0] == sec and len(out[-1][1]) + len(t) < target * 1.4:
            out[-1] = (sec, out[-1][1] + "\n\n" + t)
        else:
            out.append((sec, t))
    return out


# ─────────────────────────────────────────────────────────────
# Lexikální vyhledávání (BM25)
# ─────────────────────────────────────────────────────────────
_STOP = set("""a aby ale ani ano asi az bez bude budou by byl byla byli bylo byt ci co coz da do ho i ja jak jako je
jeho jej jeji jejich jen jeste ji jich jim jine jiz jsem jsi jsme jsou jste k kam kde kdo kdy kdyz ke ktera ktere kteri
ktery ma mam mate me mezi mi mit mne mu muze my na nad nam nas nase ne nebo nez nic o od on ona oni ono pak po pod podle
pokud pro proc proto protoze pred pri s se si sve svuj ta tak take tam te ten tento tim to tohle tom tomu toto tu ty u uz
v vam vas vase ve vice vsak vy z za ze zda kolik jaky jaka jake jakou muzu mohu lze prosim dobry den
the of and to in is it for on with as at by an be this that are or from was were can do does how what when where which who
will you your we our my me have has not no yes if there i a much many please hello
der die das und ist in zu den mit von fur auf ein eine im nicht es sie wir ich wie wo wann was kann""".split())


def fold(s: str) -> str:
    """Malá písmena bez diakritiky."""
    return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower()) if not unicodedata.combining(c))

_TOK = re.compile(r"[a-z0-9]+")


def stem(w: str) -> str:
    """Hrubý kmen: prvních 5 znaků slova (jen u slov od 5 znaků, ne u čísel)."""
    return "~" + w[:5] if len(w) >= 5 and not w.isdigit() else ""


def tokens(s: str) -> list:
    """Termy pro BM25: slova bez diakritiky; u delších slov navíc kmen (prvních 5 znaků, předpona ~),
    aby se potkaly různé tvary (snídaně / snídani / snídaní; parking / parkování)."""
    out = []
    for w in _TOK.findall(fold(s)):
        if len(w) < 2 or w in _STOP:
            continue
        out.append(w)
        st = stem(w)
        if st:
            out.append(st)
    return out


class Bm25:
    def __init__(self, docs_tokens: list, k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs_tokens)
        self.len = [len(t) for t in docs_tokens]
        self.avg = (sum(self.len) / self.n) if self.n else 0.0
        self.tf, self.df = [], {}
        for toks in docs_tokens:
            d = {}
            for t in toks:
                d[t] = d.get(t, 0) + 1
            self.tf.append(d)
            for t in d:
                self.df[t] = self.df.get(t, 0) + 1

    def scores(self, q_tokens: list) -> list:
        """[(skóre, počet různých plných slov dotazu nalezených v úryvku)] pro každý dokument."""
        q = list(dict.fromkeys(q_tokens))
        idf = {t: math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5)) for t in q if t in self.df}
        words = [t for t in q if t[0] != "~"]
        out = []
        for i, d in enumerate(self.tf):
            s = 0.0
            for t, w in idf.items():
                f = d.get(t)
                if not f:
                    continue
                if t[0] == "~":
                    w *= 0.5     # kmen je slabší důkaz než celé slovo
                s += w * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1)))
            hit = sum(1 for t in words if t in d or (stem(t) and stem(t) in d))
            out.append((s, hit))
        return out


# ─────────────────────────────────────────────────────────────
# Vektory
# ─────────────────────────────────────────────────────────────
def pack_vec(v: list) -> bytes:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return array("f", [x / n for x in v]).tobytes()


def unpack_vec(b) -> array:
    a = array("f")
    a.frombytes(bytes(b))
    return a


def cosine_all(q: array, vecs: list) -> list:
    """Kosinová podobnost dotazu ke všem vektorům (None → 0). Vektory jsou normované → stačí skalární součin."""
    if not vecs:
        return []
    if _np is not None:
        idx = [i for i, v in enumerate(vecs) if v is not None and len(v) == len(q)]
        out = [0.0] * len(vecs)
        if idx:
            m = _np.frombuffer(b"".join(vecs[i].tobytes() for i in idx), dtype=_np.float32).reshape(len(idx), len(q))
            s = m @ _np.frombuffer(q.tobytes(), dtype=_np.float32)
            for j, i in enumerate(idx):
                out[i] = float(s[j])
        return out
    return [sum(a * b for a, b in zip(q, v)) if v is not None and len(v) == len(q) else 0.0 for v in vecs]


async def embed(texts: list, api_key: str, client=None) -> list:
    """Embeddingy z OpenAI (text-embedding-3-small, 512 rozměrů). Bez klíče nebo při chybě vrací None
    za každý text – index pak jede jen lexikálně."""
    if not texts:
        return []
    if not api_key:
        return [None] * len(texts)
    import httpx
    out = []
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        for i in range(0, len(texts), 64):
            batch = [t[:6000] or " " for t in texts[i:i + 64]]
            r = None
            for attempt in range(3):
                r = await client.post("https://api.openai.com/v1/embeddings",
                                      headers={"Authorization": "Bearer " + api_key},
                                      json={"model": EMB_MODEL, "input": batch, "dimensions": EMB_DIM})
                if r.status_code in (429, 500, 502, 503) and attempt < 2:
                    import asyncio
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                break
            if r is None or r.status_code != 200:
                logging.warning("KB embeddingy selhaly: %s %s", getattr(r, "status_code", "?"), (r.text[:200] if r is not None else ""))
                out += [None] * len(batch)
                continue
            data = sorted(r.json().get("data", []), key=lambda d: d.get("index", 0))
            out += [d.get("embedding") for d in data] + [None] * (len(batch) - len(data))
    except Exception as e:
        logging.warning("KB embeddingy chyba: %s", e)
        out += [None] * (len(texts) - len(out))
    finally:
        if own:
            await client.aclose()
    return out


# ─────────────────────────────────────────────────────────────
# Slučování a výběr
# ─────────────────────────────────────────────────────────────
def fuse(vec_scores: list, bm_scores: list, q_terms: int, k: int = 5, per_doc: int = 3, doc_of=None,
         vec_min: float = VEC_MIN) -> list:
    """Sloučí vektorové a lexikální pořadí (RRF) a propustí jen relevantní úryvky.
    vec_scores: [cos] nebo []; bm_scores: [(skóre, zásahy)]. Vrací [(index, skóre, cos, zásahy)].
    Práh: úryvek musí být významově blízko (cos ≥ vec_min), nebo obsahovat dost slov dotazu
    (všechna u dotazu o 1–2 slovech, jinak aspoň 2 a zároveň polovinu)."""
    n = max(len(vec_scores), len(bm_scores))
    if not n:
        return []
    need = q_terms if q_terms <= 2 else max(2, (q_terms + 1) // 2)
    have_vec = bool(vec_scores) and any(v > 0 for v in vec_scores)
    rrf = {}
    if have_vec:
        for rank, i in enumerate(sorted(range(n), key=lambda i: -vec_scores[i])[:30]):
            if vec_scores[i] >= vec_min * 0.8:
                rrf[i] = rrf.get(i, 0) + 1.0 / (RRF_K + rank)
    if bm_scores:
        order = [i for i in sorted(range(n), key=lambda i: -bm_scores[i][0]) if bm_scores[i][0] > 0][:30]
        for rank, i in enumerate(order):
            rrf[i] = rrf.get(i, 0) + 1.0 / (RRF_K + rank)
    out, per = [], {}
    for i in sorted(rrf, key=lambda i: -rrf[i]):
        cos = vec_scores[i] if have_vec else 0.0
        hits = bm_scores[i][1] if bm_scores else 0
        ok = (cos >= vec_min) or (q_terms > 0 and hits >= need)
        if have_vec and cos < vec_min * 0.6 and hits < q_terms:
            ok = False      # slova sedí jen zčásti a významově je to úplně jinde → šum
        if not ok:
            continue
        d = doc_of(i) if doc_of else None
        if d is not None:
            if per.get(d, 0) >= per_doc:
                continue
            per[d] = per.get(d, 0) + 1
        out.append((i, rrf[i], cos, hits))
        if len(out) >= k:
            break
    return out


def prompt_block(hits: list, max_chars: int = 2400, staff: bool = False) -> str:
    """Blok do systémového promptu: očíslované úryvky se zdrojem. Délku drží rozpočet znaků."""
    if not hits:
        return ""
    parts, used = [], 0
    for n, h in enumerate(hits, 1):
        head = h["title"] + (" › " + h["section"] if h.get("section") else "")
        body = h["text"].strip()
        room = max_chars - used - len(head) - 12
        if room < 200:
            break
        if len(body) > room:
            body = body[:room].rsplit(" ", 1)[0] + " …"
        parts.append(f"[{n}] {head}\n{body}")
        used += len(head) + len(body) + 12
    if not parts:
        return ""
    if staff:
        intro = ("HOTEL KNOWLEDGE BASE – excerpts from the hotel's internal documents that match the question. "
                 "Answer from them and cite the excerpt numbers you used like [1]. If they do not contain the answer, say so plainly.")
    else:
        intro = ("HOTEL KNOWLEDGE BASE – excerpts from the hotel's own documents that match the guest's last message. "
                 "Treat them as authoritative hotel information, the same as the profile above; translate them into the guest's language. "
                 "Use only what actually answers the question; if they are not relevant, ignore them. "
                 "Do not mention documents, excerpts or their numbers to the guest.")
    return intro + "\n\n" + "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────
# Index hotelu (paměťová keš nad úložištěm)
# ─────────────────────────────────────────────────────────────
class Index:
    """Načtené úryvky jednoho hotelu: texty, vektory, BM25. Platí, dokud se nezmění verze KB hotelu."""

    def __init__(self, version: int, rows: list):
        self.version = version
        self.rows = rows            # dict: id, doc_id, title, section, text, url, audience, vec
        self.bm = Bm25([tokens((r["title"] + " " + r["section"] + " ") + r["text"]) for r in rows])
        self.vecs = [r["vec"] for r in rows]
        self.has_vec = any(v is not None for v in self.vecs)


class KB:
    def __init__(self, store: Store, embed_key_fn=None):
        self.store = store
        self.embed_key_fn = embed_key_fn or (lambda: os.getenv("OPENAI_API_KEY", "").strip())
        self.ocr_fn = None        # async (hotel_id, pdf_bajty) -> (text, poznámka); čte skenovaná PDF
        self._idx = {}
        self._ver = {}            # hotel_id -> (verze, čas načtení)
        self._lock = threading.Lock()
    # — verze a keš —
    def version(self, hotel_id: str, fresh: bool = False) -> int:
        c = self._ver.get(hotel_id)
        if c and not fresh and time.time() - c[1] < 20:
            return c[0]
        row = self.store.x("SELECT version FROM kb_meta WHERE hotel_id=%s", (hotel_id,), "one")
        v = int(row[0]) if row and row[0] is not None else 0
        self._ver[hotel_id] = (v, time.time())
        return v

    def bump(self, hotel_id: str):
        row = self.store.x("SELECT version FROM kb_meta WHERE hotel_id=%s", (hotel_id,), "one")
        if row:
            self.store.x("UPDATE kb_meta SET version=%s WHERE hotel_id=%s", (int(row[0] or 0) + 1, hotel_id))
        else:
            self.store.x("INSERT INTO kb_meta (hotel_id, version, staff_key) VALUES (%s, %s, %s)", (hotel_id, 1, ""))
        self._ver.pop(hotel_id, None)

    def index(self, hotel_id: str):
        v = self.version(hotel_id)
        if v == 0:
            return None
        ix = self._idx.get(hotel_id)
        if ix and ix.version == v:
            return ix
        rows = self.store.x(
            "SELECT c.id, c.doc_id, d.title, c.section, c.text, d.url, d.audience, c.emb, d.path "
            "FROM kb_chunks c JOIN kb_docs d ON d.id = c.doc_id "
            "WHERE c.hotel_id=%s AND d.enabled=1 ORDER BY c.doc_id, c.seq", (hotel_id,), "all") or []
        ix = Index(v, [{"id": r[0], "doc_id": r[1], "title": r[2] or "", "section": r[3] or "", "text": r[4] or "",
                        "url": r[5] or "", "audience": r[6] or "staff", "path": r[8] or "",
                        "vec": (unpack_vec(r[7]) if r[7] else None)} for r in rows])
        with self._lock:
            if len(self._idx) > 200:
                self._idx.clear()
            self._idx[hotel_id] = ix
        return ix
    # — vyhledávání —
    async def search(self, hotel_id: str, query: str, audiences=("guest",), k: int = 5, client=None,
                     lexical: bool = False) -> list:
        """lexical=True: jen podle slov, bez dotazu na vektory (hledání při psaní – nic nestojí)."""
        query = (query or "").strip()
        if not query:
            return []
        ix = self.index(hotel_id)
        if not ix or not ix.rows:
            return []
        allowed = [i for i, r in enumerate(ix.rows) if r["audience"] in audiences]
        if not allowed:
            return []
        qtok = tokens(query)
        q_terms = len({t for t in qtok if t[0] != "~"})
        bm_all = ix.bm.scores(qtok)
        vec_all = []
        if ix.has_vec and not lexical:
            qv = (await embed([query[:2000]], self.embed_key_fn(), client=client))[0]
            if qv:
                vec_all = cosine_all(unpack_vec(pack_vec(qv)), ix.vecs)
        mask = set(allowed)
        bm = [bm_all[i] if i in mask else (0.0, 0) for i in range(len(ix.rows))]
        vec = [vec_all[i] if i in mask else 0.0 for i in range(len(ix.rows))] if vec_all else []
        picked = fuse(vec, bm, q_terms, k=k, doc_of=lambda i: ix.rows[i]["doc_id"])
        out = []
        for i, score, cos, hits in picked:
            r = ix.rows[i]
            out.append({"chunk_id": r["id"], "doc_id": r["doc_id"], "title": r["title"], "section": r["section"],
                        "text": r["text"], "url": r["url"], "audience": r["audience"], "path": r["path"],
                        "score": round(score, 4), "similarity": round(cos, 3), "word_hits": hits})
        return out
    # — zápis dokumentů —
    def counts(self, hotel_id: str) -> dict:
        d = self.store.x("SELECT COUNT(*), COALESCE(SUM(chunks),0), COALESCE(SUM(chars),0) FROM kb_docs WHERE hotel_id=%s",
                         (hotel_id,), "one") or (0, 0, 0)
        e = self.store.x("SELECT COUNT(*) FROM kb_chunks WHERE hotel_id=%s AND emb IS NOT NULL", (hotel_id,), "one") or (0,)
        return {"docs": int(d[0] or 0), "chunks": int(d[1] or 0), "chars": int(d[2] or 0), "embedded": int(e[0] or 0)}

    async def put_document(self, hotel_id: str, *, title: str, text: str = "", data: bytes = None, name: str = "",
                           mime: str = "", audience: str = "staff", source_id: str = "", ext_id: str = "",
                           path: str = "", url: str = "", etag: str = "", doc_id: str = "", client=None) -> dict:
        """Uloží (nebo přepíše) dokument a přeindexuje ho. Vrací {id, status, error, chunks}."""
        audience = audience if audience in AUDIENCES else "staff"
        note = ""
        if data is not None:
            text, note = extract_text(name or title, data, mime)
            if note == SCAN_NOTE and not text and self.ocr_fn:
                try:
                    text, note = await self.ocr_fn(hotel_id, data)
                    text = clean_text(text)[:MAX_DOC_CHARS]
                except Exception as e:
                    logging.warning("KB: rozpoznání skenu selhalo: %s", e)
                    text, note = "", SCAN_NOTE
        else:
            text = clean_text(text)[:MAX_DOC_CHARS]
        existing = None
        if doc_id:
            existing = self.store.x("SELECT id FROM kb_docs WHERE id=%s AND hotel_id=%s", (doc_id, hotel_id), "one")
        elif source_id and ext_id:
            existing = self.store.x("SELECT id FROM kb_docs WHERE hotel_id=%s AND source_id=%s AND ext_id=%s",
                                    (hotel_id, source_id, ext_id), "one")
        did = existing[0] if existing else (doc_id or uuid.uuid4().hex)
        c = self.counts(hotel_id)
        if not existing and c["docs"] >= MAX_DOCS_PER_HOTEL:
            return {"id": "", "status": "error", "error": f"limit {MAX_DOCS_PER_HOTEL} dokumentů na hotel", "chunks": 0}
        chunks = chunk_text(text) if text else []
        if chunks and c["chunks"] + len(chunks) > MAX_CHUNKS_PER_HOTEL and not existing:
            return {"id": "", "status": "error", "error": "znalostní báze je plná (limit rozsahu textu)", "chunks": 0}
        vecs = await embed([(title + (" – " + s if s else "") + "\n" + t) for s, t in chunks], self.embed_key_fn(), client=client) if chunks else []
        status = "ok" if chunks else "error"
        error = note if (note or chunks) else "v dokumentu není žádný text"
        st = []
        if existing:
            st.append(("DELETE FROM kb_chunks WHERE doc_id=%s", (did,)))
            st.append(("UPDATE kb_docs SET title=%s, path=%s, url=%s, mime=%s, chars=%s, chunks=%s, etag=%s, status=%s, "
                       "error=%s, updated_at=%s WHERE id=%s",
                       (title[:300], path[:500], url[:1000], mime[:120], len(text), len(chunks), etag[:200], status, error, _now(), did)))
        else:
            st.append(("INSERT INTO kb_docs (id, hotel_id, source_id, ext_id, title, path, url, mime, audience, chars, chunks, "
                       "etag, status, error, updated_at, enabled) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)",
                       (did, hotel_id, source_id, ext_id[:500], title[:300], path[:500], url[:1000], mime[:120], audience,
                        len(text), len(chunks), etag[:200], status, error, _now())))
        for seq, ((sec, t), v) in enumerate(zip(chunks, vecs)):
            st.append(("INSERT INTO kb_chunks (id, hotel_id, doc_id, seq, section, text, emb) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                       (uuid.uuid4().hex, hotel_id, did, seq, sec, t, pack_vec(v) if v else None)))
        self.store.tx(st)
        self.bump(hotel_id)
        return {"id": did, "status": status, "error": error, "chunks": len(chunks),
                "embedded": sum(1 for v in vecs if v)}

    def document_text(self, hotel_id: str, doc_id: str):
        """Text dokumentu tak, jak ho má báze (složený z úryvků, bez jejich překryvů). None = dokument není."""
        d = self.store.x("SELECT title, error FROM kb_docs WHERE id=%s AND hotel_id=%s", (doc_id, hotel_id), "one")
        if not d:
            return None
        rows = self.store.x("SELECT section, text FROM kb_chunks WHERE doc_id=%s AND hotel_id=%s ORDER BY seq",
                            (doc_id, hotel_id), "all") or []
        out, prev_sec = "", None
        for sec, tx in rows:
            sec, tx = sec or "", tx or ""
            if sec != prev_sec:
                out += ("\n\n" if out else "") + (("## " + sec + "\n") if sec else "") + tx
            else:
                cut = 0
                for n in range(min(len(tx), len(out), 400), 15, -1):      # konec předchozího úryvku se opakuje
                    if out.endswith(tx[:n]):
                        cut = n
                        break
                out += ("" if cut else "\n") + tx[cut:]
            prev_sec = sec
        return {"title": d[0] or "", "note": d[1] or "", "text": out}

    def delete_document(self, hotel_id: str, doc_id: str) -> bool:
        row = self.store.x("SELECT id FROM kb_docs WHERE id=%s AND hotel_id=%s", (doc_id, hotel_id), "one")
        if not row:
            return False
        self.store.tx([("DELETE FROM kb_chunks WHERE doc_id=%s", (doc_id,)), ("DELETE FROM kb_docs WHERE id=%s", (doc_id,))])
        self.bump(hotel_id)
        return True

    def update_document(self, hotel_id: str, doc_id: str, audience: str = None, enabled=None) -> bool:
        row = self.store.x("SELECT id FROM kb_docs WHERE id=%s AND hotel_id=%s", (doc_id, hotel_id), "one")
        if not row:
            return False
        if audience in AUDIENCES:
            self.store.x("UPDATE kb_docs SET audience=%s WHERE id=%s", (audience, doc_id))
        if enabled is not None:
            self.store.x("UPDATE kb_docs SET enabled=%s WHERE id=%s", (1 if enabled else 0, doc_id))
        self.bump(hotel_id)
        return True

    def list_documents(self, hotel_id: str) -> list:
        rows = self.store.x("SELECT id, source_id, title, path, url, mime, audience, chars, chunks, status, error, updated_at, enabled "
                            "FROM kb_docs WHERE hotel_id=%s ORDER BY title", (hotel_id,), "all") or []
        return [{"id": r[0], "source_id": r[1] or "", "title": r[2] or "", "path": r[3] or "", "url": r[4] or "",
                 "mime": r[5] or "", "audience": r[6] or "staff", "chars": int(r[7] or 0), "chunks": int(r[8] or 0),
                 "status": r[9] or "", "error": r[10] or "", "updated_at": r[11] or "", "enabled": bool(r[12])} for r in rows]

    async def reembed_missing(self, hotel_id: str, client=None) -> int:
        """Doplní vektory úryvkům, které je nemají (klíč přibyl později / dočasný výpadek)."""
        rows = self.store.x("SELECT c.id, d.title, c.section, c.text FROM kb_chunks c JOIN kb_docs d ON d.id=c.doc_id "
                            "WHERE c.hotel_id=%s AND c.emb IS NULL LIMIT 2000", (hotel_id,), "all") or []
        if not rows or not self.embed_key_fn():
            return 0
        vecs = await embed([(r[1] or "") + (" – " + r[2] if r[2] else "") + "\n" + (r[3] or "") for r in rows], self.embed_key_fn(), client=client)
        st = [("UPDATE kb_chunks SET emb=%s WHERE id=%s", (pack_vec(v), r[0])) for r, v in zip(rows, vecs) if v]
        if st:
            self.store.tx(st)
            self.bump(hotel_id)
        return len(st)
    # — klíč pro personál —
    def staff_key(self, hotel_id: str, rotate: bool = False) -> str:
        row = self.store.x("SELECT staff_key, version FROM kb_meta WHERE hotel_id=%s", (hotel_id,), "one")
        if row and row[0] and not rotate:
            return row[0]
        key = uuid.uuid4().hex + uuid.uuid4().hex[:8]
        if row:
            self.store.x("UPDATE kb_meta SET staff_key=%s WHERE hotel_id=%s", (key, hotel_id))
        else:
            self.store.x("INSERT INTO kb_meta (hotel_id, version, staff_key) VALUES (%s, %s, %s)", (hotel_id, 0, key))
        return key

    def staff_key_ok(self, hotel_id: str, key: str) -> bool:
        import hmac
        row = self.store.x("SELECT staff_key FROM kb_meta WHERE hotel_id=%s", (hotel_id,), "one")
        return bool(row and row[0] and key and hmac.compare_digest(str(row[0]), str(key)))
