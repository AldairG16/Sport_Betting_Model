"""
17-nov-26: el closing rehízo el pick de Iraklis–Panathinaikos (8-nov) con momios en juego hasta las 17:33:
The Odds API decía 18:00 y empezó a las 15:30. scripts/fix_inplay_picks.py lo rehace como estaba al empezar.
"""

import pandas as pd

from scripts import fix_inplay_picks as F
from tests.test_program_picks import _mvs

UTC = lambda s: pd.Timestamp(s, tz="UTC")
SYSTEM = UTC("2026-11-08 18:00")


def test_it_finds_the_picks_saved_after_the_real_kickoff():
    picks = [{"id": 1, "match": "iraklis fc vs panathinaikos", "match_date": SYSTEM, "picked_at": UTC("2026-11-08 17:33")},
             {"id": 2, "match": "lecce vs genoa", "match_date": UTC("2026-11-08 17:00"),
              "picked_at": UTC("2026-11-08 16:31")},                              # antes de empezar: bien
             {"id": 3, "match": "roma vs como", "match_date": UTC("2026-11-08 18:30"),
              "picked_at": UTC("2026-11-08 18:02")}]                              # ESPN 18:05: bien
    finals = [{"home": "iraklis fc", "away": "panathinaikos", "kickoff": UTC("2026-11-08 15:30")}]
    kicks = [{"home": "roma", "away": "como", "kickoff": UTC("2026-11-08 18:30"), "espn_kickoff": UTC("2026-11-08 18:05")}]
    assert F.real_kickoffs(picks, kicks, finals) == {1: UTC("2026-11-08 15:30")}


def test_it_rebuilds_the_favourite_from_the_odds_before_the_kickoff():
    r = {**_mvs("iraklis fc vs panathinaikos", model=(0.10, 0.15, 0.75), pin=(0.10, 0.14, 0.76)),
         "match_date": SYSTEM, "league": "soccer_greece_super_league", "created_at": UTC("2026-11-08 12:04")}
    before = {"match": "Iraklis FC vs Panathinaikos", "kickoff": SYSTEM, "captured_at": UTC("2026-11-08 14:32"),
              "pin_home_odds": 10.91, "pin_draw_odds": 5.84, "pin_away_odds": 1.28,
              "consensus_home_odds": 9.44, "consensus_draw_odds": 5.14, "consensus_away_odds": 1.263}
    row = F.rebuild(r, before, None)
    assert (row["market"], row["label"], row["reason"]) == ("away_win", "Gana el visitante", None)   # 🎯
    assert row["playdoit_odds"] == 1.263 and row["star_note"] is None
    in_play = {**before, "captured_at": UTC("2026-11-08 16:31"), "pin_home_odds": 3.06, "pin_draw_odds": 2.77,
               "pin_away_odds": 2.64, "consensus_home_odds": 2.9, "consensus_draw_odds": 2.7,
               "consensus_away_odds": 2.6}
    assert F.rebuild(r, in_play, None)["reason"] is not None                  # con los de en juego: sin 🎯


def test_it_only_touches_the_current_rule_and_keeps_a_backup():
    import inspect
    src = inspect.getsource(F.main)
    assert F.SINCE == "2026-11-06"                                            # la regla del 🎯 de v2.9.0
    assert "INSERT INTO program_picks_fix_backup" in src and "if not apply" in src
    assert src.index("INSERT INTO program_picks_fix_backup") < src.index("UPDATE program_picks")
