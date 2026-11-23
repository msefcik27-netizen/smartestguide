"""Hodnocení odpovědí Alex palcem: host hodnotí odpověď v chatu, hotel pak v portálu posoudí
odpovědi, které hostům nepomohly (odpověď byla v pořádku / byla špatně + oprava).

Čisté funkce nad slovníky (bez FastAPI a bez databáze), testy v test_ratings.py.

Kde data žijí (vše v analytics[hid]):
  ratings_monthly  … {"YYYY-MM": {"up": n, "down": n}}          počty hlasů hostů po měsících
  rating_votes     … {"<podpis>:<hash zařízení>": {"v", "m"}}   kdo jak hlasoval (kvůli změně hlasu a duplicitám)
  rating_down      … [{id, q, a, lang, at, n}]                  odpovědi s palcem dolů, čekají na posouzení hotelem
  ratings_hotel    … {"ok": n, "bad": n}                        jak je posoudil hotel

Text odpovědi se ukládá JEN u palce dolů (hotel ji potřebuje vidět, aby ji mohl opravit).
"""
import re
import uuid
from datetime import datetime

VALUES = ("up", "down")
Q_MAX = 300
A_MAX = 700
DOWN_MAX = 40          # strop seznamu čekajícího na hotel
VOTES_MAX = 600        # strop paměti hlasů (nejstarší vypadnou)


def _clean(s, limit: int) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def sig_payload(hid: str, q: str, answer: str) -> str:
    """Text, který server podepisuje při odeslání odpovědi. Host pak nemůže hodnotit odpověď,
    kterou Alex nikdy neřekl (ani podstrčit vlastní text do portálu hotelu)."""
    return f"rate:{hid}:{str(q or '').strip()}:{str(answer or '').strip()}"


def _bump(a: dict, month: str, value: str, delta: int):
    m = a.setdefault("ratings_monthly", {}).setdefault(month, {"up": 0, "down": 0})
    m[value] = max(0, int(m.get(value, 0)) + delta)


def record(a: dict, *, value: str, sig: str, device_hash: str, q: str, answer: str,
           lang: str, utc_now: datetime) -> str:
    """Zapíše hlas hosta. Vrací: new | changed | same. Vyhazuje ValueError('value')."""
    if value not in VALUES:
        raise ValueError("value")
    votes = a.setdefault("rating_votes", {})
    key = f"{sig}:{device_hash}"
    month = utc_now.strftime("%Y-%m")
    prev = votes.get(key)
    if prev and prev.get("v") == value:
        return "same"
    if prev:
        _bump(a, prev.get("m") or month, prev.get("v"), -1)
        if prev.get("v") == "down":
            _down_remove(a, sig)
        result = "changed"
    else:
        result = "new"
    _bump(a, month, value, 1)
    votes[key] = {"v": value, "m": month}
    while len(votes) > VOTES_MAX:
        votes.pop(next(iter(votes)))
    if value == "down":
        _down_add(a, sig, q, answer, lang, utc_now)
    return result


def _down_add(a: dict, sig: str, q: str, answer: str, lang: str, utc_now: datetime):
    items = a.setdefault("rating_down", [])
    for x in items:
        if x.get("sig") == sig:
            x["n"] = int(x.get("n", 1)) + 1
            x["at"] = utc_now.isoformat()
            return
    items.insert(0, {"id": uuid.uuid4().hex[:10], "sig": sig, "q": _clean(q, Q_MAX), "a": _clean(answer, A_MAX),
                     "lang": _clean(lang, 8), "at": utc_now.isoformat(), "n": 1})
    del items[DOWN_MAX:]


def _down_remove(a: dict, sig: str):
    """Host si palec dolů rozmyslel: ubere jeden hlas, bez hlasů položka zmizí."""
    items = a.get("rating_down") or []
    for x in list(items):
        if x.get("sig") == sig:
            x["n"] = int(x.get("n", 1)) - 1
            if x["n"] <= 0:
                items.remove(x)
            return


def down_list(a: dict) -> list:
    """Odpovědi s palcem dolů pro portál (bez podpisu)."""
    return [{"id": x.get("id", ""), "q": x.get("q", ""), "a": x.get("a", ""), "lang": x.get("lang", ""),
             "at": x.get("at", ""), "n": int(x.get("n", 1))}
            for x in (a.get("rating_down") or []) if isinstance(x, dict) and x.get("id")]


def hotel_verdict(a: dict, item_id: str, verdict: str):
    """Hotel posoudil odpověď: ok (odpověď byla správně) | bad (byla špatně). Položka zmizí ze seznamu.
    Vrací položku (kvůli případné opravě do znalostí), nebo None, když neexistuje.
    Vyhazuje ValueError('verdict')."""
    if verdict not in ("ok", "bad"):
        raise ValueError("verdict")
    items = a.get("rating_down") or []
    for x in items:
        if x.get("id") == item_id:
            items.remove(x)
            h = a.setdefault("ratings_hotel", {"ok": 0, "bad": 0})
            h[verdict] = int(h.get(verdict, 0)) + 1
            return x
    return None


def _pct(up: int, down: int):
    return round(up / (up + down) * 100) if (up + down) else None


def period_block(a: dict, keys: list) -> dict:
    """Součet hlasů hostů za dané měsíce + podíl spokojených."""
    mon = (a or {}).get("ratings_monthly") or {}
    up = sum(int((mon.get(k) or {}).get("up", 0)) for k in keys)
    down = sum(int((mon.get(k) or {}).get("down", 0)) for k in keys)
    return {"up": up, "down": down, "total": up + down, "satisfied_pct": _pct(up, down)}


def summary(a: dict) -> dict:
    """Souhrn za celou dobu pro portál."""
    a = a or {}
    mon = a.get("ratings_monthly") or {}
    blk = period_block(a, list(mon.keys()))
    hv = a.get("ratings_hotel") or {}
    blk["hotel_ok"] = int(hv.get("ok", 0))
    blk["hotel_bad"] = int(hv.get("bad", 0))
    blk["waiting"] = len(down_list(a))
    return blk
