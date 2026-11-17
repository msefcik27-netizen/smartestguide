"""Výpočty pro stránku Přehled (/prehled) — podrobná analytika hotelu, oddělená od portálu.

Čisté funkce bez I/O a bez závislosti na app.py: dostanou slovníky z DB a vrátí slovníky.
Dají se proto testovat samostatně (test_analytics_view.py).

Co se kde bere:
  analytics[hid]["monthly"][YYYY-MM]  count, flagged, langs, devices, visits, visit_devices
                                      + nově hours, wdays, topics, topics_flagged (record_question)
  analytics[hid]["unanswered"]        nově: dotazy bez odpovědi, drží se UNANSWERED_KEEP_DAYS dní
  analytics[hid]["orders_monthly"]    requested / confirmed / declined / booked
  analytics[hid]["ai_usage"]          stt_sec, tts_chars (hlas)
  orders                              záznamy požadavků (žijí 30 dní) — doba reakce, nejžádanější
"""
from datetime import datetime, timedelta
import csv
import io
import statistics

DEFAULT_MINUTES = 2.0                       # minut práce recepce na jeden dotaz (odhad, hotel si přepíše)
DEFAULT_WAGE = {"cs": 250.0, "en": 10.0}    # hodinový náklad recepce (odhad, hotel si přepíše)
DEFAULT_CURRENCY = {"cs": "Kč", "en": "€"}
DEFAULT_RECEPTION = (7, 22)                 # provoz recepce od–do (celé hodiny); 0–24 = nonstop
UNANSWERED_KEEP_DAYS = 90
UNANSWERED_MAX = 500
SETTING_KEYS = ("stats_minutes_per_question", "stats_hourly_wage", "stats_currency",
                "stats_reception_from", "stats_reception_to")
PERIODS = (("this", 1, 0), ("prev", 1, 1), ("3m", 3, 0), ("12m", 12, 0))  # (klíč, počet měsíců, posun zpět)

OTHER_TOPIC = "💬 Ostatní"
TOPIC_EN = {
    "🍳 Snídaně / strava": "🍳 Breakfast / dining",
    "⏰ Check-in/out": "⏰ Check-in/out",
    "🅿️ Parkování": "🅿️ Parking",
    "💆 Wellness / spa": "💆 Wellness / spa",
    "📶 WiFi / internet": "📶 WiFi / internet",
    "🗺 Okolí / tipy": "🗺 Around / tips",
    "🚕 Doprava": "🚕 Transport",
    "🛏 Pokoj / služby": "🛏 Room / services",
    "💳 Účet / platby": "💳 Bill / payments",
    OTHER_TOPIC: "💬 Other",
}


# ── Nastavení ────────────────────────────────────────────────────────────────

def _num(v, lo, hi):
    """Číslo v mezích, jinak None. Bere i text s desetinnou čárkou."""
    try:
        if isinstance(v, bool) or v is None or v == "":
            return None
        x = float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return x if lo <= x <= hi else None


def normalize_settings(data: dict):
    """Očistí nastavení přehledu poslané z portálu. Vrátí jen klíče, které v datech byly;
    neplatná hodnota = None (tj. zpět na výchozí). Když v datech žádný není, vrátí None."""
    if not isinstance(data, dict) or not any(k in data for k in SETTING_KEYS):
        return None
    out = {}
    if "stats_minutes_per_question" in data:
        out["stats_minutes_per_question"] = _num(data["stats_minutes_per_question"], 0.5, 30)
    if "stats_hourly_wage" in data:
        out["stats_hourly_wage"] = _num(data["stats_hourly_wage"], 0, 100000)
    if "stats_currency" in data:
        s = str(data["stats_currency"] or "").strip()[:4]
        out["stats_currency"] = s or None
    for k in ("stats_reception_from", "stats_reception_to"):
        if k in data:
            x = _num(data[k], 0, 24)
            out[k] = int(x) if x is not None else None
    return out


def settings(hotel: dict, lang: str) -> dict:
    """Efektivní nastavení (uložené hodnoty hotelu, jinak výchozí)."""
    hotel = hotel or {}
    lg = "cs" if lang == "cs" else "en"
    minutes = _num(hotel.get("stats_minutes_per_question"), 0.5, 30)
    wage = _num(hotel.get("stats_hourly_wage"), 0, 100000)
    cur = str(hotel.get("stats_currency") or "").strip()[:4]
    rf = _num(hotel.get("stats_reception_from"), 0, 24)
    rt = _num(hotel.get("stats_reception_to"), 0, 24)
    if rf is None or rt is None or rf >= rt:
        rf, rt = DEFAULT_RECEPTION
        rec_default = True
    else:
        rec_default = False
    return {
        "minutes_per_question": minutes if minutes is not None else DEFAULT_MINUTES,
        "hourly_wage": wage if wage is not None else DEFAULT_WAGE[lg],
        "currency": cur or DEFAULT_CURRENCY[lg],
        "reception_from": int(rf), "reception_to": int(rt),
        "is_default": {"minutes": minutes is None, "wage": wage is None, "reception": rec_default},
    }


# ── Záznam dotazu (volá se z _log_guest_question) ─────────────────────────────

def record_question(a: dict, m: dict, *, local_now: datetime, utc_now: datetime,
                    message: str, language: str, flagged: bool, topic: str):
    """Doplní k dotazu nové agregace. `a` = analytika hotelu, `m` = její měsíční záznam.
    Hodina a den v týdnu jsou v MÍSTNÍM čase hotelu. Text se ukládá jen u dotazů bez odpovědi."""
    topic = topic or OTHER_TOPIC
    hk, wk = str(local_now.hour), str(local_now.weekday())
    hours = m.setdefault("hours", {})
    hours[hk] = hours.get(hk, 0) + 1
    wdays = m.setdefault("wdays", {})
    wdays[wk] = wdays.get(wk, 0) + 1
    topics = m.setdefault("topics", {})
    topics[topic] = topics.get(topic, 0) + 1
    if flagged:
        tf = m.setdefault("topics_flagged", {})
        tf[topic] = tf.get(topic, 0) + 1
        un = a.setdefault("unanswered", [])
        un.insert(0, {"q": (message or "")[:300], "lang": language or "", "at": utc_now.isoformat(), "topic": topic})
        prune_unanswered(a, utc_now)


def prune_unanswered(a: dict, utc_now: datetime):
    """Zahodí dotazy bez odpovědi starší než UNANSWERED_KEEP_DAYS a ořízne seznam na UNANSWERED_MAX."""
    un = a.get("unanswered")
    if not un:
        return
    limit = (utc_now - timedelta(days=UNANSWERED_KEEP_DAYS)).isoformat()
    a["unanswered"] = [u for u in un if str(u.get("at", "")) >= limit][:UNANSWERED_MAX]


# ── Sestavení přehledu ────────────────────────────────────────────────────────

def month_shift(month_key: str, delta: int) -> str:
    y, mo = int(month_key[:4]), int(month_key[5:7])
    idx = y * 12 + (mo - 1) + delta
    return f"{idx // 12:04d}-{(idx % 12) + 1:02d}"


def _saved(count: int, st: dict):
    hours = count * st["minutes_per_question"] / 60.0
    return round(hours, 1), round(hours * st["hourly_wage"])


def _pct(part, whole):
    return round(part / whole * 100) if whole else 0


def _add(dst: dict, src: dict):
    for k, v in (src or {}).items():
        try:
            dst[k] = dst.get(k, 0) + int(v)
        except (TypeError, ValueError):
            pass


def _month_row(key: str, a: dict, st: dict) -> dict:
    m = (a.get("monthly") or {}).get(key) or {}
    om = (a.get("orders_monthly") or {}).get(key) or {}
    au = (a.get("ai_usage") or {}).get(key) or {}
    count = int(m.get("count", 0))
    flagged = min(int(m.get("flagged", 0)), count)
    sh, sm = _saved(count, st)
    return {
        "key": key, "count": count, "flagged": flagged, "answered": count - flagged,
        "visits": int(m.get("visits", 0)), "visitors": len(m.get("visit_devices") or {}),
        "askers": len(m.get("devices") or {}),
        "saved_hours": sh, "saved_money": sm,
        "orders": {k: int(om.get(k, 0)) for k in ("requested", "confirmed", "declined", "booked")},
        "stt_min": round(float(au.get("stt_sec", 0) or 0) / 60.0, 1),
        "tts_chars": int(au.get("tts_chars", 0) or 0),
    }


def _period(keys: list, a: dict, st: dict, lang: str, lang_names: dict) -> dict:
    monthly = a.get("monthly") or {}
    count = flagged = visits = 0
    visitors, askers = {}, {}
    langs, topics, topics_flagged, hours, wdays = {}, {}, {}, {}, {}
    orders = {"requested": 0, "confirmed": 0, "declined": 0, "booked": 0}
    stt_sec, tts_chars = 0.0, 0
    for k in keys:
        m = monthly.get(k) or {}
        count += int(m.get("count", 0))
        flagged += int(m.get("flagged", 0))
        visits += int(m.get("visits", 0))
        _add(visitors, m.get("visit_devices"))
        _add(askers, m.get("devices"))
        _add(langs, m.get("langs"))
        _add(topics, m.get("topics"))
        _add(topics_flagged, m.get("topics_flagged"))
        _add(hours, m.get("hours"))
        _add(wdays, m.get("wdays"))
        _add(orders, (a.get("orders_monthly") or {}).get(k))
        au = (a.get("ai_usage") or {}).get(k) or {}
        stt_sec += float(au.get("stt_sec", 0) or 0)
        tts_chars += int(au.get("tts_chars", 0) or 0)
    flagged = min(flagged, count)

    # Stejně dlouhé období těsně předtím — pro trend
    prev_keys = [month_shift(k, -len(keys)) for k in keys]
    prev_count = sum(int((monthly.get(k) or {}).get("count", 0)) for k in prev_keys)
    trend = round((count - prev_count) / prev_count * 100) if prev_count > 0 else None

    hours_arr = [int(hours.get(str(i), 0)) for i in range(24)]
    wdays_arr = [int(wdays.get(str(i), 0)) for i in range(7)]
    tracked = sum(hours_arr)   # hodiny se sbírají až od nasazení přehledu → může být méně než count
    off = sum(n for i, n in enumerate(hours_arr) if not (st["reception_from"] <= i < st["reception_to"]))

    names = (lang_names or {})
    lang_rows = [{"code": c, "name": names.get(c, c), "count": n, "pct": _pct(n, count)}
                 for c, n in sorted(langs.items(), key=lambda kv: kv[1], reverse=True)]
    topic_total = sum(topics.values())
    topic_rows = [{"label": (TOPIC_EN.get(t, t) if lang != "cs" else t), "count": n, "pct": _pct(n, topic_total),
                   "flagged": int(topics_flagged.get(t, 0))}
                  for t, n in sorted(topics.items(), key=lambda kv: kv[1], reverse=True)]
    sh, sm = _saved(count, st)
    off_h, off_m = _saved(off, st)
    return {
        "keys": keys, "count": count, "prev_count": prev_count, "trend_pct": trend,
        "flagged": flagged, "answered": count - flagged,
        "answer_rate_pct": _pct(count - flagged, count) if count else None,
        "visits": visits, "visitors": len(visitors), "askers": len(askers),
        # zařízení posílá návštěvu nejvýš 1× denně → hodnota > 1 = host se vrátil další den
        "returning": sum(1 for v in visitors.values() if v > 1),
        "conversion_pct": _pct(min(len(askers), len(visitors)), len(visitors)) if visitors else None,
        "questions_per_asker": round(count / len(askers), 1) if askers else None,
        "saved_hours": sh, "saved_money": sm,
        "langs": lang_rows, "topics": topic_rows, "topics_tracked": topic_total,
        "hours": hours_arr, "wdays": wdays_arr, "time_tracked": tracked,
        "off_hours_count": off, "off_hours_pct": _pct(off, tracked) if tracked else None,
        "off_hours_saved_hours": off_h,
        "orders": orders, "stt_min": round(stt_sec / 60.0, 1), "tts_chars": tts_chars,
    }


def _parse(ts: str):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _orders_block(orders: list) -> dict:
    """Z živých záznamů požadavků (drží se ~30 dní): stavy, doba reakce recepce, nejžádanější."""
    rows = list(orders or [])
    st = {"pending": 0, "confirmed": 0, "declined": 0}
    waits, titles = [], {}
    for r in rows:
        s = r.get("status") or "pending"
        st[s if s in st else "pending"] += 1
        c, d = _parse(r.get("created")), _parse(r.get("decided"))
        if c and d and d >= c:
            waits.append((d - c).total_seconds() / 60.0)
        t = (r.get("title") or "").strip()
        if t:
            titles[t] = titles.get(t, 0) + 1
    top = [{"title": t, "count": n} for t, n in sorted(titles.items(), key=lambda kv: kv[1], reverse=True)[:8]]
    return {"total": len(rows), **st,
            "median_response_min": round(statistics.median(waits)) if waits else None,
            "decided_count": len(waits), "top": top}


def _gaps_block(a: dict, utc_now: datetime, lang: str) -> dict:
    limit = (utc_now - timedelta(days=UNANSWERED_KEEP_DAYS)).isoformat()
    if "unanswered" in a:
        items = [u for u in (a.get("unanswered") or []) if str(u.get("at", "")) >= limit]
        source = "unanswered"
    else:
        # Než začne nový sběr: aspoň označené dotazy z posledních 50 (bez tématu)
        items = [{"q": q.get("q", ""), "lang": q.get("lang", ""), "at": q.get("at", ""), "topic": ""}
                 for q in (a.get("recent_questions") or []) if q.get("flagged") and str(q.get("at", "")) >= limit]
        source = "recent"
    by = {}
    for u in items:
        t = u.get("topic") or OTHER_TOPIC
        by[t] = by.get(t, 0) + 1
    tr = (lambda t: t) if lang == "cs" else (lambda t: TOPIC_EN.get(t, t))
    return {
        "keep_days": UNANSWERED_KEEP_DAYS, "source": source, "count": len(items),
        "items": [{"q": u.get("q", ""), "lang": u.get("lang", ""), "at": u.get("at", ""),
                   "topic": tr(u.get("topic") or OTHER_TOPIC) if u.get("topic") or source == "unanswered" else ""}
                  for u in items[:200]],
        "by_topic": [{"label": tr(t), "count": n} for t, n in sorted(by.items(), key=lambda kv: kv[1], reverse=True)],
    }


def build_overview(a: dict, orders: list, hotel: dict, *, lang: str, utc_now: datetime, lang_names: dict = None) -> dict:
    """Celý přehled pro stránku /prehled. `orders` = záznamy požadavků TOHOTO hotelu."""
    a = a or {}
    st = settings(hotel, lang)
    now_key = utc_now.strftime("%Y-%m")
    months = [_month_row(month_shift(now_key, -i), a, st) for i in range(11, -1, -1)]
    periods = {}
    for name, n, back in PERIODS:
        last = month_shift(now_key, -back)
        keys = [month_shift(last, -i) for i in range(n - 1, -1, -1)]
        periods[name] = _period(keys, a, st, lang, lang_names)
    total = int(a.get("total", 0))
    ah, am = _saved(total, st)
    return {
        "lang": "cs" if lang == "cs" else "en",
        "generated_at": utc_now.isoformat(),
        "month_key": now_key,
        "settings": st,
        "months": months,
        "periods": periods,
        "all_time": {"questions": total, "visits": int(a.get("visits_total", 0)),
                     "saved_hours": ah, "saved_money": am, "last_chat": a.get("last_chat")},
        "gaps": _gaps_block(a, utc_now, lang),
        "orders": _orders_block(orders),
    }


# ── Export ───────────────────────────────────────────────────────────────────

def _csv(rows: list, lang: str) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    for r in rows:
        out = []
        for v in r:
            if isinstance(v, float):
                v = (f"{v:.1f}").replace(".", ",") if lang == "cs" else f"{v:.1f}"
            elif isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                v = "'" + v   # text od hosta nesmí Excel spustit jako vzorec
            out.append(v)
        w.writerow(out)
    return "﻿" + buf.getvalue()   # BOM, ať Excel přečte diakritiku


def months_csv(ov: dict) -> str:
    lang = ov.get("lang", "en")
    cur = ov["settings"]["currency"]
    head = (["Měsíc", "Dotazy", "Zodpovězeno", "Bez odpovědi", "Otevření průvodce", "Návštěvníci", "Tazatelé",
             "Ušetřené hodiny", f"Úspora ({cur})", "Požadavky", "Potvrzeno", "Nelze vyřídit", "Zapsáno do PMS"]
            if lang == "cs" else
            ["Month", "Questions", "Answered", "Unanswered", "Guide opens", "Visitors", "Askers",
             "Hours saved", f"Savings ({cur})", "Requests", "Confirmed", "Declined", "Booked in PMS"])
    rows = [head]
    for m in ov["months"]:
        o = m["orders"]
        rows.append([m["key"], m["count"], m["answered"], m["flagged"], m["visits"], m["visitors"], m["askers"],
                     float(m["saved_hours"]), m["saved_money"], o["requested"], o["confirmed"], o["declined"], o["booked"]])
    return _csv(rows, lang)


def gaps_csv(ov: dict) -> str:
    lang = ov.get("lang", "en")
    rows = [["Datum", "Jazyk", "Téma", "Dotaz"] if lang == "cs" else ["Date", "Language", "Topic", "Question"]]
    for u in ov["gaps"]["items"]:
        rows.append([str(u.get("at", ""))[:10], u.get("lang", ""), u.get("topic", ""), u.get("q", "")])
    return _csv(rows, lang)
