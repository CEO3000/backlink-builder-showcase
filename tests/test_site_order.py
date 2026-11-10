"""The sites share one Gemini allowance, used in the order the sites run. The first run with a real quota shortage left
SocialField (always last) with nothing, so the starting site now moves on by one each day."""
from datetime import date, timedelta

from src.backlinks import pipeline as P

SITES = [{"site_id": "hypnotreatment-uk"}, {"site_id": "genbooks-uk"}, {"site_id": "social-field"}]


def order(day):
    return [c["site_id"] for c in P.rotated(SITES, day)]


def test_every_site_is_always_present_exactly_once():
    for k in range(10):
        assert sorted(order(date(2026, 10, 28) + timedelta(days=k))) == sorted(c["site_id"] for c in SITES)


def test_the_starting_site_changes_every_day_and_each_site_gets_a_turn_first():
    days = [date(2026, 10, 28) + timedelta(days=k) for k in range(3)]
    firsts = [order(d)[0] for d in days]
    assert len(set(firsts)) == 3, "over three days each site goes first once"
    assert order(days[0])[0] != order(days[1])[0]


def test_each_site_is_last_on_some_day_but_never_always():
    lasts = {order(date(2026, 10, 28) + timedelta(days=k))[-1] for k in range(3)}
    assert lasts == {c["site_id"] for c in SITES}


def test_the_relative_order_is_kept_and_nothing_breaks_with_one_or_no_sites():
    assert order(date(2026, 10, 28)) in (["hypnotreatment-uk", "genbooks-uk", "social-field"], ["genbooks-uk", "social-field", "hypnotreatment-uk"],
                                         ["social-field", "hypnotreatment-uk", "genbooks-uk"])
    assert P.rotated([], date(2026, 10, 28)) == [] and P.rotated([{"site_id": "a"}], date(2026, 10, 28)) == [{"site_id": "a"}]
