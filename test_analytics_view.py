"""Testy výpočtů přehledu. Spuštění: python test_analytics_view.py"""
from datetime import datetime, timedelta
import analytics_view as av

NOW = datetime(2026, 11, 17, 10, 0, 0)


def _ask(a, when_utc, local_hour, msg, lang, flagged, topic):
    """Napodobí to, co dělá _log_guest_question + record_question."""
    mk = when_utc.strftime("%Y-%m")
    m = a.setdefault("monthly", {}).setdefault(mk, {"count": 0, "flagged": 0, "langs": {}})
    m["count"] += 1
    a["total"] = a.get("total", 0) + 1
    if flagged:
        m["flagged"] += 1
    m["langs"][lang] = m["langs"].get(lang, 0) + 1
    av.record_question(a, m, local_now=when_utc.replace(hour=local_hour), utc_now=when_utc,
                       message=msg, language=lang, flagged=flagged, topic=topic)


def test_settings_defaults_and_overrides():
    s = av.settings({}, "cs")
    assert s["minutes_per_question"] == 2.0 and s["hourly_wage"] == 250.0 and s["currency"] == "Kč"
    assert (s["reception_from"], s["reception_to"]) == (7, 22) and s["is_default"]["wage"]
    assert av.settings({}, "en")["hourly_wage"] == 10.0 and av.settings({}, "en")["currency"] == "€"
    s = av.settings({"stats_minutes_per_question": "3,5", "stats_hourly_wage": 300, "stats_currency": "EUR",
                     "stats_reception_from": 0, "stats_reception_to": 24}, "cs")
    assert s["minutes_per_question"] == 3.5 and s["hourly_wage"] == 300 and s["currency"] == "EUR"
    assert (s["reception_from"], s["reception_to"]) == (0, 24) and not s["is_default"]["reception"]
    # nesmysly → výchozí
    s = av.settings({"stats_minutes_per_question": "abc", "stats_hourly_wage": -5,
                     "stats_reception_from": 20, "stats_reception_to": 8}, "cs")
    assert s["minutes_per_question"] == 2.0 and s["hourly_wage"] == 250.0 and s["reception_from"] == 7


def test_normalize_settings():
    assert av.normalize_settings({"name": "Hotel"}) is None
    out = av.normalize_settings({"stats_minutes_per_question": "4", "stats_hourly_wage": "x",
                                 "stats_currency": "  Kč  ", "stats_reception_from": "6", "stats_reception_to": 99})
    assert out == {"stats_minutes_per_question": 4.0, "stats_hourly_wage": None, "stats_currency": "Kč",
                   "stats_reception_from": 6, "stats_reception_to": None}
    assert av.normalize_settings({"stats_hourly_wage": True}) == {"stats_hourly_wage": None}


def test_record_and_overview():
    a = {}
    _ask(a, NOW, 23, "Kde je bazén?", "en", False, "💆 Wellness / spa")
    _ask(a, NOW, 23, "Máte dětskou postýlku?", "cs", True, "🛏 Pokoj / služby")
    _ask(a, NOW, 9, "Kdy je snídaně?", "cs", False, "🍳 Snídaně / strava")
    _ask(a, NOW - timedelta(days=30), 12, "WiFi?", "de", False, "📶 WiFi / internet")
    a["monthly"]["2026-11"]["visit_devices"] = {"aa": 1, "bb": 3, "cc": 1, "dd": 1}
    a["monthly"]["2026-11"]["devices"] = {"aa": 2, "bb": 1}
    a["monthly"]["2026-11"]["visits"] = 6
    ov = av.build_overview(a, [], {}, lang="cs", utc_now=NOW, lang_names={"cs": "čeština", "en": "angličtina"})
    p = ov["periods"]["this"]
    assert p["count"] == 3 and p["flagged"] == 1 and p["answered"] == 2 and p["answer_rate_pct"] == 67
    assert p["prev_count"] == 1 and p["trend_pct"] == 200
    assert p["visitors"] == 4 and p["askers"] == 2 and p["returning"] == 1 and p["conversion_pct"] == 50
    assert p["questions_per_asker"] == 1.5
    assert p["hours"][23] == 2 and p["hours"][9] == 1 and p["time_tracked"] == 3
    assert p["off_hours_count"] == 2 and p["off_hours_pct"] == 67      # recepce 7–22
    assert p["saved_hours"] == 0.1 and p["saved_money"] == 25           # 3 × 2 min × 250 Kč/h
    assert p["langs"][0] == {"code": "cs", "name": "čeština", "count": 2, "pct": 67}
    assert {t["label"] for t in p["topics"]} == {"💆 Wellness / spa", "🛏 Pokoj / služby", "🍳 Snídaně / strava"}
    assert [t["flagged"] for t in p["topics"] if t["label"] == "🛏 Pokoj / služby"] == [1]
    assert ov["periods"]["prev"]["count"] == 1 and ov["periods"]["3m"]["count"] == 4
    assert len(ov["months"]) == 12 and ov["months"][-1]["key"] == "2026-11" and ov["months"][0]["key"] == "2025-12"
    assert ov["all_time"]["questions"] == 4
    g = ov["gaps"]
    assert g["source"] == "unanswered" and g["count"] == 1 and g["items"][0]["q"] == "Máte dětskou postýlku?"
    assert g["by_topic"] == [{"label": "🛏 Pokoj / služby", "count": 1}]
    # nonstop recepce → žádné dotazy mimo provoz
    ov2 = av.build_overview(a, [], {"stats_reception_from": 0, "stats_reception_to": 24}, lang="cs", utc_now=NOW)
    assert ov2["periods"]["this"]["off_hours_count"] == 0
    # anglicky se témata přeloží
    ov3 = av.build_overview(a, [], {}, lang="en", utc_now=NOW)
    assert "🛏 Room / services" in {t["label"] for t in ov3["periods"]["this"]["topics"]}
    assert ov3["gaps"]["items"][0]["topic"] == "🛏 Room / services"


def test_empty_hotel():
    ov = av.build_overview({}, [], {}, lang="en", utc_now=NOW)
    p = ov["periods"]["12m"]
    assert p["count"] == 0 and p["trend_pct"] is None and p["answer_rate_pct"] is None
    assert p["conversion_pct"] is None and p["off_hours_pct"] is None and p["questions_per_asker"] is None
    assert ov["gaps"]["count"] == 0 and ov["orders"]["total"] == 0 and ov["orders"]["median_response_min"] is None
    assert av.months_csv(ov).count("\r\n") == 13


def test_old_data_without_new_fields():
    """Hotel s daty z doby před přehledem: počty sedí, nové bloky jsou prázdné, mezery z recent_questions."""
    a = {"total": 40, "monthly": {"2026-10": {"count": 30, "flagged": 4, "langs": {"en": 30}},
                                   "2026-11": {"count": 10, "flagged": 1, "langs": {"en": 10}}},
         "recent_questions": [{"q": "late checkout?", "lang": "en", "flagged": True, "at": "2026-11-10T08:00:00"},
                              {"q": "pool?", "lang": "en", "flagged": False, "at": "2026-11-10T09:00:00"},
                              {"q": "old", "lang": "en", "flagged": True, "at": "2026-01-01T09:00:00"}]}
    ov = av.build_overview(a, [], {}, lang="en", utc_now=NOW)
    p = ov["periods"]["this"]
    assert p["count"] == 10 and p["time_tracked"] == 0 and p["topics"] == [] and p["trend_pct"] == -67
    assert ov["gaps"]["source"] == "recent" and [u["q"] for u in ov["gaps"]["items"]] == ["late checkout?"]
    assert ov["periods"]["prev"]["saved_hours"] == 1.0 and ov["periods"]["prev"]["saved_money"] == 10


def test_unanswered_pruning():
    a = {"unanswered": [{"q": "new", "at": (NOW - timedelta(days=5)).isoformat()},
                        {"q": "old", "at": (NOW - timedelta(days=91)).isoformat()}]}
    av.prune_unanswered(a, NOW)
    assert [u["q"] for u in a["unanswered"]] == ["new"]
    a = {"unanswered": [{"q": str(i), "at": NOW.isoformat()} for i in range(700)]}
    av.prune_unanswered(a, NOW)
    assert len(a["unanswered"]) == av.UNANSWERED_MAX and a["unanswered"][0]["q"] == "0"


def test_orders_block():
    orders = [
        {"status": "confirmed", "title": "Snídaně do pokoje", "created": "2026-11-10T08:00:00", "decided": "2026-11-10T08:10:00"},
        {"status": "declined", "title": "Pozdní check-out", "created": "2026-11-11T08:00:00", "decided": "2026-11-11T08:30:00"},
        {"status": "confirmed", "title": "Snídaně do pokoje", "created": "2026-11-12T08:00:00", "decided": "2026-11-12T09:00:00"},
        {"status": "pending", "title": "Taxi", "created": "2026-11-16T08:00:00", "decided": ""},
        {"title": "", "created": "nesmysl"},
    ]
    o = av._orders_block(orders)
    assert o["total"] == 5 and o["confirmed"] == 2 and o["declined"] == 1 and o["pending"] == 2
    assert o["median_response_min"] == 30 and o["decided_count"] == 3
    assert o["top"][0] == {"title": "Snídaně do pokoje", "count": 2}


def test_csv_export():
    a = {}
    _ask(a, NOW, 10, "=HYPERLINK(\"http://x\")", "en", True, "")
    ov = av.build_overview(a, [], {}, lang="cs", utc_now=NOW)
    g = av.gaps_csv(ov)
    assert g.startswith("﻿") and "'=HYPERLINK" in g and "💬 Ostatní" in g
    m = av.months_csv(ov)
    assert "Úspora (Kč)" in m and "2026-11;1;0;1;" in m and "0,0" in m
    assert "0.0" in av.months_csv(av.build_overview(a, [], {}, lang="en", utc_now=NOW))


def test_month_shift():
    assert av.month_shift("2026-01", -1) == "2025-12" and av.month_shift("2026-12", 1) == "2027-01"
    assert av.month_shift("2026-11", -11) == "2025-12"


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for n, f in tests:
        try:
            f()
            print("ok  ", n)
        except AssertionError as e:
            failed += 1
            import traceback
            print("FAIL", n)
            traceback.print_exc()
    print(f"{len(tests) - failed}/{len(tests)} testů prošlo")
    raise SystemExit(1 if failed else 0)
