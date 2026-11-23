"""Testy helpdesku (helpdesk.py). Spuštění: python -m pytest test_helpdesk.py -q"""
from datetime import datetime, timedelta

import pytest

import helpdesk

NOW = datetime(2026, 11, 23, 10, 0, 0)


def _new(tickets, **kw):
    args = dict(hid="h1", kind="fault", room="204", text="Nesvítí lampa u postele", lang="cs",
                guest="Jana", device_hash="dev1", utc_now=NOW)
    args.update(kw)
    return helpdesk.create(tickets, **args)


def test_create_stores_ticket_with_history():
    tickets = {}
    t = _new(tickets)
    assert tickets[t["id"]] is t
    assert t["status"] == "new" and t["room"] == "204" and t["assignee"] == ""
    assert [e["kind"] for e in t["events"]] == ["created"]


def test_create_rejects_bad_kind_and_empty_text():
    with pytest.raises(ValueError, match="kind"):
        _new({}, kind="pizza")
    with pytest.raises(ValueError, match="empty"):
        _new({}, text="  a ")


def test_text_is_cleaned_and_limited():
    t = _new({}, text="Řádek 1\r\n\r\n\r\n\r\nŘádek 2\x00" + "x" * 2000, room="  12A \n")
    assert "\x00" not in t["text"] and "\n\n\n" not in t["text"]
    assert len(t["text"]) <= helpdesk.TEXT_MAX
    assert t["room"] == "12A"


def test_guest_cannot_flood_hotel():
    tickets = {}
    for _ in range(helpdesk.OPEN_PER_DEVICE):
        _new(tickets)
    with pytest.raises(ValueError, match="too_many"):
        _new(tickets)
    _new(tickets, device_hash="dev2")          # jiný host může


def test_update_changes_status_assignee_note_and_logs_history():
    t = _new({})
    later = NOW + timedelta(minutes=5)
    assert helpdesk.update(t, status="in_progress", assignee="Petr údržba", note="Technik přijde do 30 minut.", utc_now=later)
    assert t["status"] == "in_progress" and t["assignee"] == "Petr údržba" and t["note"].startswith("Technik")
    assert [e["kind"] for e in t["events"]] == ["created", "status", "assignee", "note"]
    assert t["updated"] == later.isoformat()
    assert helpdesk.update(t, status="in_progress", assignee="Petr údržba", utc_now=later) is False
    with pytest.raises(ValueError):
        helpdesk.update(t, status="lost", utc_now=later)


def test_hotel_list_open_first_and_only_own_hotel():
    tickets = {}
    a = _new(tickets, text="První závada")
    b = _new(tickets, text="Druhá závada", utc_now=NOW + timedelta(minutes=1))
    _new(tickets, hid="h2", text="Cizí hotel")
    helpdesk.update(b, status="done", utc_now=NOW + timedelta(minutes=2))
    rows = helpdesk.hotel_list(tickets, "h1")
    assert [r["id"] for r in rows] == [a["id"], b["id"]]
    assert "dev" not in rows[0] and rows[0]["events"]


def test_guest_list_shows_only_own_tickets_without_internal_fields():
    tickets = {}
    mine = _new(tickets)
    _new(tickets, device_hash="dev2", text="Cizí požadavek")
    helpdesk.update(mine, assignee="Petr", note="Už se na tom pracuje.", utc_now=NOW)
    rows = helpdesk.guest_list(tickets, "h1", "dev1")
    assert len(rows) == 1 and rows[0]["note"] == "Už se na tom pracuje."
    assert "assignee" not in rows[0] and "events" not in rows[0] and "guest" not in rows[0]
    assert helpdesk.guest_list(tickets, "h1", "") == []


def test_prune_removes_only_old_done_tickets():
    tickets = {}
    old_done = _new(tickets, utc_now=NOW - timedelta(days=400))
    helpdesk.update(old_done, status="done", utc_now=NOW - timedelta(days=399))
    old_open = _new(tickets, utc_now=NOW - timedelta(days=400), device_hash="dev3")
    assert helpdesk.prune(tickets, "h1", NOW) == 1
    assert old_done["id"] not in tickets and old_open["id"] in tickets


def test_counts():
    tickets = {}
    a = _new(tickets)
    _new(tickets)
    helpdesk.update(a, status="done", utc_now=NOW)
    assert helpdesk.counts(tickets, "h1") == {"new": 1, "in_progress": 0, "done": 1, "open": 1}


def test_prompt_block_only_when_enabled():
    assert helpdesk.prompt_block({}) == ""
    assert "🛠" in helpdesk.prompt_block({"helpdesk_enabled": True})


def test_labels():
    assert helpdesk.kind_label("fault") == "Závada" and helpdesk.kind_label("fault", "en") == "Problem"
    assert helpdesk.status_label("done") == "Vyřízeno"
