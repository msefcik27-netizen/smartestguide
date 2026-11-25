"""Testy hodnocení odpovědí (ratings.py). Spuštění: python -m pytest test_ratings.py -q"""
from datetime import datetime

import pytest

import ratings

NOW = datetime(2026, 11, 23, 10, 0, 0)


def _vote(a, value, sig="s1", dev="d1", q="Kde je bazén?", ans="Bazén je v suterénu."):
    return ratings.record(a, value=value, sig=sig, device_hash=dev, q=q, answer=ans, lang="cs", utc_now=NOW)


def test_new_vote_counts_in_month():
    a = {}
    assert _vote(a, "up") == "new"
    assert a["ratings_monthly"]["2026-11"] == {"up": 1, "down": 0}
    assert ratings.down_list(a) == []


def test_same_vote_twice_is_ignored():
    a = {}
    _vote(a, "up")
    assert _vote(a, "up") == "same"
    assert a["ratings_monthly"]["2026-11"]["up"] == 1


def test_changed_vote_moves_the_count():
    a = {}
    _vote(a, "up")
    assert _vote(a, "down") == "changed"
    assert a["ratings_monthly"]["2026-11"] == {"up": 0, "down": 1}
    assert len(ratings.down_list(a)) == 1
    assert _vote(a, "up") == "changed"
    assert a["ratings_monthly"]["2026-11"] == {"up": 1, "down": 0}
    assert ratings.down_list(a) == []


def test_down_vote_keeps_question_and_answer_for_hotel():
    a = {}
    _vote(a, "down")
    row = ratings.down_list(a)[0]
    assert row["q"] == "Kde je bazén?" and row["a"] == "Bazén je v suterénu." and row["n"] == 1
    assert "sig" not in row


def test_two_guests_same_answer_merge_into_one_row():
    a = {}
    _vote(a, "down", dev="d1")
    _vote(a, "down", dev="d2")
    rows = ratings.down_list(a)
    assert len(rows) == 1 and rows[0]["n"] == 2
    assert a["ratings_monthly"]["2026-11"]["down"] == 2


def test_invalid_value_rejected():
    with pytest.raises(ValueError):
        _vote({}, "maybe")


def test_down_list_is_capped():
    a = {}
    for i in range(ratings.DOWN_MAX + 10):
        _vote(a, "down", sig=f"s{i}")
    assert len(ratings.down_list(a)) == ratings.DOWN_MAX


def test_hotel_verdict_removes_item_and_counts():
    a = {}
    _vote(a, "down")
    item_id = ratings.down_list(a)[0]["id"]
    item = ratings.hotel_verdict(a, item_id, "bad")
    assert item["q"] == "Kde je bazén?"
    assert ratings.down_list(a) == []
    assert a["ratings_hotel"] == {"ok": 0, "bad": 1}
    assert ratings.hotel_verdict(a, item_id, "ok") is None
    # posouzená odpověď zůstává k nahlédnutí mezi „už posouzenými“
    d = ratings.done_list(a)
    assert len(d) == 1 and d[0]["verdict"] == "bad" and d[0]["q"] == "Kde je bazén?" and "sig" not in d[0]
    with pytest.raises(ValueError):
        ratings.hotel_verdict(a, item_id, "x")


def test_period_block_and_summary():
    a = {"ratings_monthly": {"2026-10": {"up": 3, "down": 1}, "2026-11": {"up": 1, "down": 0}}}
    assert ratings.period_block(a, ["2026-11"]) == {"up": 1, "down": 0, "total": 1, "satisfied_pct": 100}
    assert ratings.period_block(a, ["2026-10", "2026-11"])["satisfied_pct"] == 80
    assert ratings.period_block({}, ["2026-11"])["satisfied_pct"] is None
    s = ratings.summary(a)
    assert s["total"] == 5 and s["waiting"] == 0 and s["hotel_ok"] == 0


def test_sig_payload_depends_on_hotel_question_and_answer():
    base = ratings.sig_payload("h1", "q", "a")
    assert base != ratings.sig_payload("h2", "q", "a")
    assert base != ratings.sig_payload("h1", "q2", "a")
    assert base != ratings.sig_payload("h1", "q", "a2")
