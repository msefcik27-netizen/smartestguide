"""Jednoduchý helpdesk hotelu: host nahlásí závadu, úklid nebo přání; hotel u požadavku
vede stav, řešitele a historii.

Čisté funkce nad slovníky (bez FastAPI a bez databáze), testy v test_helpdesk.py.

Kde data žijí:
  db["tickets"][tid] … {id, hid, kind, room, text, lang, guest, dev, status, assignee,
                        created, updated, note, events:[{t, kind, text}]}
  hotel["helpdesk_enabled"] … hotel si helpdesk zapíná sám v portálu (výchozí vypnuto)

`dev` je zkrácený hash zařízení hosta — podle něj host vidí jen SVÉ požadavky.
"""
import re
import uuid
from datetime import datetime, timedelta

KINDS = ("fault", "cleaning", "wish")
STATUSES = ("new", "in_progress", "done")
TEXT_MAX = 600
NOTE_MAX = 400
NAME_MAX = 60
ROOM_MAX = 12
EVENTS_MAX = 40
OPEN_PER_DEVICE = 10      # host nemůže hotel zahltit
KEEP_DONE_DAYS = 180      # vyřízené požadavky se po půl roce uklidí
HOTEL_MAX = 500           # strop na hotel (nejstarší vyřízené vypadnou)

KIND_CS = {"fault": "Závada", "cleaning": "Úklid", "wish": "Přání"}
KIND_EN = {"fault": "Problem", "cleaning": "Cleaning", "wish": "Wish"}
STATUS_CS = {"new": "Nový", "in_progress": "Řeší se", "done": "Vyřízeno"}
STATUS_EN = {"new": "New", "in_progress": "In progress", "done": "Done"}


def _line(s, limit: int) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def _text(s, limit: int) -> str:
    """Víceřádkový text: zachová odřádkování, zahodí ostatní řídicí znaky."""
    s = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]+", " ", str(s or "").replace("\r\n", "\n").replace("\r", "\n"))
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()[:limit]


def kind_label(kind: str, lang: str = "cs") -> str:
    return (KIND_CS if lang == "cs" else KIND_EN).get(kind, kind)


def status_label(status: str, lang: str = "cs") -> str:
    return (STATUS_CS if lang == "cs" else STATUS_EN).get(status, status)


def _event(t: dict, kind: str, text: str, utc_now: datetime):
    ev = t.setdefault("events", [])
    ev.append({"t": utc_now.isoformat(), "kind": kind, "text": _line(text, NOTE_MAX)})
    del ev[:-EVENTS_MAX]


def create(tickets: dict, *, hid: str, kind: str, room: str, text: str, lang: str, guest: str,
           device_hash: str, utc_now: datetime) -> dict:
    """Založí požadavek hosta. Vyhazuje ValueError s kódem: kind | empty | too_many."""
    if kind not in KINDS:
        raise ValueError("kind")
    text = _text(text, TEXT_MAX)
    if len(text) < 3:
        raise ValueError("empty")
    if device_hash:
        open_mine = sum(1 for t in tickets.values()
                        if t.get("hid") == hid and t.get("dev") == device_hash and t.get("status") != "done")
        if open_mine >= OPEN_PER_DEVICE:
            raise ValueError("too_many")
    tid = uuid.uuid4().hex[:12]
    now = utc_now.isoformat()
    t = {"id": tid, "hid": hid, "kind": kind, "room": _line(room, ROOM_MAX), "text": text,
         "lang": _line(lang, 8), "guest": _line(guest, NAME_MAX), "dev": device_hash or "",
         "status": "new", "assignee": "", "note": "", "created": now, "updated": now, "events": []}
    _event(t, "created", "", utc_now)
    tickets[tid] = t
    prune(tickets, hid, utc_now)
    return t


def update(t: dict, *, status=None, assignee=None, note=None, utc_now: datetime) -> bool:
    """Hotel změní stav, řešitele nebo připíše poznámku pro hosta. Každá změna jde do historie.
    Vrací True, když se něco změnilo. Vyhazuje ValueError('status')."""
    changed = False
    if status is not None and status != t.get("status"):
        if status not in STATUSES:
            raise ValueError("status")
        t["status"] = status
        _event(t, "status", status, utc_now)
        changed = True
    if assignee is not None:
        assignee = _line(assignee, NAME_MAX)
        if assignee != (t.get("assignee") or ""):
            t["assignee"] = assignee
            _event(t, "assignee", assignee, utc_now)
            changed = True
    if note is not None:
        note = _line(note, NOTE_MAX)
        if note and note != (t.get("note") or ""):
            t["note"] = note            # poslední poznámka se ukazuje hostovi
            _event(t, "note", note, utc_now)
            changed = True
    if changed:
        t["updated"] = utc_now.isoformat()
    return changed


def prune(tickets: dict, hid: str, utc_now: datetime) -> int:
    """Uklidí staré vyřízené požadavky hotelu. Otevřené nemaže nikdy. Vrací počet smazaných."""
    limit = (utc_now - timedelta(days=KEEP_DONE_DAYS)).isoformat()
    mine = [t for t in tickets.values() if t.get("hid") == hid]
    drop = [t["id"] for t in mine if t.get("status") == "done" and str(t.get("updated", "")) < limit]
    over = len(mine) - len(drop) - HOTEL_MAX
    if over > 0:
        done = sorted((t for t in mine if t.get("status") == "done" and t["id"] not in drop),
                      key=lambda t: str(t.get("updated", "")))
        drop += [t["id"] for t in done[:over]]
    for tid in drop:
        tickets.pop(tid, None)
    return len(drop)


_ORDER = {"new": 0, "in_progress": 1, "done": 2}


def hotel_list(tickets: dict, hid: str) -> list:
    """Požadavky hotelu pro portál: nejdřív otevřené, v každé skupině nejnovější první."""
    rows = [t for t in (tickets or {}).values() if t.get("hid") == hid]
    rows.sort(key=lambda t: str(t.get("created", "")), reverse=True)
    rows.sort(key=lambda t: _ORDER.get(t.get("status"), 9))
    return [{k: t.get(k, "") for k in ("id", "kind", "room", "text", "lang", "guest", "status",
                                         "assignee", "note", "created", "updated")}
            | {"events": list(t.get("events") or [])} for t in rows]


def guest_list(tickets: dict, hid: str, device_hash: str, limit: int = 15) -> list:
    """Požadavky jednoho hosta (podle zařízení) — bez jména řešitele a bez interní historie."""
    if not device_hash:
        return []
    rows = [t for t in (tickets or {}).values() if t.get("hid") == hid and t.get("dev") == device_hash]
    rows.sort(key=lambda t: str(t.get("created", "")), reverse=True)
    return [{k: t.get(k, "") for k in ("id", "kind", "text", "status", "note", "created", "updated")}
            for t in rows[:limit]]


def counts(tickets: dict, hid: str) -> dict:
    out = {s: 0 for s in STATUSES}
    for t in (tickets or {}).values():
        if t.get("hid") == hid and t.get("status") in out:
            out[t["status"]] += 1
    out["open"] = out["new"] + out["in_progress"]
    return out


def prompt_block(hotel: dict) -> str:
    """Řádek pro systémový prompt Alex. Prázdný řetězec, když hotel helpdesk nemá zapnutý."""
    if not (hotel or {}).get("helpdesk_enabled"):
        return ""
    return ("HLÁŠENÍ ZÁVAD A PŘÁNÍ: Hotel má v této aplikaci formulář pro hlášení závady na pokoji, žádost o úklid "
            "nebo jiné přání — otevře se tlačítkem 🛠 nahoře v záhlaví. Když host popíše závadu, chce úklid nebo má "
            "podobné přání, krátce ho na to tlačítko odkaž (v jeho jazyce) a dodej, že tam uvidí i stav vyřízení. "
            "Sám požadavek neodesíláš a neslibuješ termín vyřízení.")
