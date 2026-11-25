"""Další kontakty hotelu: hotel si v portálu přidá libovolný počet kontaktů (telefon, e-mail,
WhatsApp), pojmenuje je (Recepce 2, Masáže, Půjčovna kol…) a připíše poznámku, co tam host vyřídí
(druhy masáží, ceny, způsob platby). Alex je pak hostům sděluje.

Čisté funkce nad slovníky (bez FastAPI a bez databáze), testy v test_contacts.py.

Kde data žijí: hotel["extra_contacts"] … [{type, label, value, note}]
"""
import re

MAX_CONTACTS = 20
TYPES = {"phone": "telefon", "email": "e-mail", "whatsapp": "WhatsApp"}
LIMITS = {"label": 60, "value": 120, "note": 600}


def _clean(s, limit: int) -> str:
    """Jeden řádek bez řídicích znaků — text jde do promptu, nesmí v něm tvořit vlastní odstavce."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def normalize_contact(c):
    """Jeden kontakt z portálu → čistý slovník, nebo None, když chybí hodnota (číslo / adresa)."""
    if not isinstance(c, dict):
        return None
    out = {k: _clean(c.get(k), lim) for k, lim in LIMITS.items()}
    out["type"] = c.get("type") if c.get("type") in TYPES else "phone"
    if not out["value"]:
        return None
    if out["type"] == "email":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", out["value"]):
            return None
    else:
        # telefon / WhatsApp: jen číslice, +, mezery a běžné oddělovače; aspoň 5 číslic
        if not re.fullmatch(r"[+\d][\d\s\-()/.]*", out["value"]) or len(re.sub(r"\D", "", out["value"])) < 5:
            return None
    if not out["label"]:
        out["label"] = TYPES[out["type"]].capitalize()
    return out


def normalize(update_data: dict):
    """Pro PATCH z portálu: None, když se kontakty neposílají; jinak {"extra_contacts": [...]}."""
    if "extra_contacts" not in (update_data or {}):
        return None
    raw = update_data.get("extra_contacts")
    rows = [x for x in (normalize_contact(c) for c in (raw if isinstance(raw, list) else [])) if x]
    return {"extra_contacts": rows[:MAX_CONTACTS]}


def contacts(hotel: dict) -> list:
    return [c for c in ((hotel or {}).get("extra_contacts") or [])
            if isinstance(c, dict) and c.get("value") and c.get("type") in TYPES][:MAX_CONTACTS]


def prompt_block(hotel: dict) -> str:
    """Blok do systémového promptu Alex. Prázdný, když hotel žádný další kontakt nemá."""
    rows = contacts(hotel)
    if not rows:
        return ""
    lines = []
    for c in rows:
        line = f"- {_clean(c.get('label'), LIMITS['label'])} ({TYPES[c['type']]}): {_clean(c.get('value'), LIMITS['value'])}"
        note = _clean(c.get("note"), LIMITS["note"])
        if note:
            line += f" — {note}"
        lines.append(line)
    return ("DALŠÍ KONTAKTY HOTELU (zadal je hotel; když se host ptá na danou službu nebo oddělení, uveď příslušný "
            "kontakt a informace z poznámky; údaje neměň a nic k nim nepřimýšlej):\n" + "\n".join(lines))
