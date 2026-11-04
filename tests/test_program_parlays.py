"""
El parlay del día (v2.6.0, 3-nov-26).

El dueño: "quiero agregar la función parley, donde seleccione los partidos más
seguros, de las mejores ligas, de los que más se ganan y los agrupe en una
pestaña". Eligió: las ligas donde los picks van ganando, de cada partido la
apuesta más segura aunque no sea el pick, y 2, 3 o 4 partidos a elegir. Se
guarda y se mide como los picks. Sin DB.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from dashboard import parlay as X
from src.models import program_parlays as PL
from src.models import program_picks as PP
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine

NOW = pd.Timestamp("2030-01-05 15:00", tz="UTC")


def _mvs(match="napoli vs lecce", kick="2030-01-05 19:45", league="soccer_italy_serie_a", **kw):
    return {"match": match, "match_date": pd.Timestamp(kick, tz="UTC"), "league": league,
            "p_home": 0.62, "p_draw": 0.22, "p_away": 0.16, "pin_home": 0.58, "pin_draw": 0.25, "pin_away": 0.17,
            "lam_home": 2.0, "lam_away": 0.9, "p_over25": 0.62, "p_btts": None,
            "created_at": pd.Timestamp("2030-01-05 12:00", tz="UTC"), **kw}


def _up(home="napoli", away="lecce", **kw):
    return {"home_team_norm": home, "away_team_norm": away, "home_team": home.title(), "away_team": away.title(),
            "pin_home_odds": 1.65, "pin_draw_odds": 4.0, "pin_away_odds": 5.5,
            "home_odds": 1.66, "draw_odds": 4.1, "away_odds": 5.6,
            "consensus_home_odds": 1.60, "consensus_draw_odds": 3.9, "consensus_away_odds": 5.2,
            "over25_odds": 1.80, "under25_odds": 2.10, "consensus_over25_odds": 1.75, "consensus_under25_odds": 2.05,
            "over15_odds": 1.24, "under15_odds": 4.4, "consensus_over15_odds": 1.22, "consensus_under15_odds": 4.2,
            "btts_yes_odds": None, "btts_no_odds": None, **kw}


def _cand(market, model, mk, odds, family="Goles"):
    return {"market": market, "label": market, "family": family, "model": model, "market_prob": mk,
            "best_odds": odds, "playdoit_odds": odds}


# ─── la apuesta más segura de cada partido ──────────────────────────────────

def test_the_safest_bet_is_the_one_both_believe_most_not_the_biggest_gap():
    u = SimpleNamespace(**_up())
    leg = PL.safest_leg(_mvs(), u)
    # más de 1.5 es la más segura (~77% el mercado) pero paga −455: no suma al parlay
    assert leg["market"] == "home_win" and leg["playdoit_odds"] == pytest.approx(1.60)
    assert PL.safety(leg) == pytest.approx(min(leg["model"], leg["market_prob"]))
    assert PL.safety(leg) >= PL.MIN_SAFE


def test_both_the_program_and_the_market_have_to_believe_it(monkeypatch):
    cs = [_cand("over_1.5", 0.90, 0.52, 1.80),      # el programa 90%, el mercado 52%: no es segura
          _cand("under_3.5", 0.70, 0.66, 1.45)]
    monkeypatch.setattr(PP, "_candidates_of", lambda *a, **k: cs)
    assert PL.safest_leg(_mvs(), None)["market"] == "under_3.5"
    cs[1]["model"] = 0.54                            # por debajo del 55%: nada
    assert PL.safest_leg(_mvs(), None) is None


def test_no_leg_from_a_weakened_winner_low_history_or_an_unadjusted_cup(monkeypatch):
    cs = [_cand("home_win", 0.70, 0.66, 1.45, "Ganador"), _cand("under_3.5", 0.62, 0.60, 1.55)]
    monkeypatch.setattr(PP, "_candidates_of", lambda *a, **k: cs)
    assert PL.safest_leg(_mvs(), None, weak={"home_win": "Alineación: …"})["market"] == "under_3.5"
    assert PL.safest_leg(_mvs(low_history="lecce con 3 partidos"), None) is None
    assert PL.safest_leg(_mvs(skipped_at=NOW), None) is None
    assert PL.safest_leg(_mvs(league="soccer_uefa_champs_league"), None) is None
    assert PL.safest_leg(_mvs(league="soccer_uefa_champs_league", league_factor=1.1), None)["market"] == "home_win"


# ─── las ligas donde los picks van ganando ──────────────────────────────────

def _picks(league, n, profit, result="win", when="2026-10-25 19:00", reason=None):
    return [{"league": league, "match_date": pd.Timestamp(when, tz="UTC"), "result": result, "profit": profit,
             "reason": reason}] * n


def test_only_leagues_with_units_in_favor_and_enough_picks_since_the_model_fix():
    df = pd.DataFrame(_picks("soccer_italy_serie_a", 6, 0.8) + _picks("soccer_italy_serie_a", 5, -1.0, "loss")
                      + _picks("soccer_usa_mls", 12, -1.0, "loss")                       # pierde
                      + _picks("soccer_epl", 9, 0.9)                                     # 9 picks: pocos
                      + _picks("soccer_spain_la_liga", 12, 0.9, when="2026-10-10 19:00")  # modelo con error
                      + _picks("soccer_france_ligue_one", 12, 0.9, reason=PP.LOW_HISTORY + ": x")
                      + _picks("soccer_germany_bundesliga", 12, None, result=None))       # sin liquidar
    assert PL.winning_leagues(df) == []                              # Serie A: 6 × 0.8 − 5 = −0.2
    df.loc[df["league"] == "soccer_italy_serie_a", "profit"] = df["profit"].where(df["result"] == "loss", 1.0)
    assert PL.winning_leagues(df) == [{"league": "soccer_italy_serie_a", "n": 11, "won": 6, "lost": 5,
                                       "units": 1.0}]                 # 6 − 5 = +1
    assert PL.winning_leagues(df, before="2026-10-20") == []        # antes de esos partidos no había nada
    assert PL.FIXED_SINCE == "2026-10-22"
    from dashboard.board import MODEL_FIXED
    assert pd.Timestamp(PL.FIXED_SINCE, tz="UTC") == MODEL_FIXED


# ─── el parlay de cada día ─────────────────────────────────────────────────

def _five():
    teams = [("napoli", "lecce"), ("roma", "como"), ("inter", "genoa"), ("milan", "torino"), ("juventus", "parma")]
    mvs = pd.DataFrame([_mvs(f"{h} vs {a}") for h, a in teams])
    up = pd.DataFrame([_up(h, a) for h, a in teams])
    return mvs, up


def test_a_day_keeps_the_four_safest_one_per_match_and_only_winning_leagues(monkeypatch):
    mvs, up = _five()
    safe = {"napoli": 0.80, "roma": 0.62, "inter": 0.75, "milan": 0.58, "juventus": 0.70}
    monkeypatch.setattr(PP, "_candidates_of", lambda r, *a, **k: [
        _cand("home_win", safe[r["match"].split(" vs ")[0]], 0.9, 1.40, "Ganador"),
        _cand("under_3.5", 0.56, 0.56, 1.70)])
    days = PL.build_parlays(mvs, up, ["soccer_italy_serie_a"])
    (day, legs), = days.items()
    assert day == pd.Timestamp("2030-01-05")                         # 19:45 UTC = 13:45 de México
    assert [x["match"] for x in legs] == ["napoli vs lecce", "inter vs genoa", "juventus vs parma", "roma vs como"]
    assert [x["rank"] for x in legs] == [1, 2, 3, 4] and legs[0]["display"] == "Napoli vs Lecce"
    assert PL.build_parlays(mvs, up, ["soccer_epl"]) == {}            # la liga no va ganando: nada


def test_a_late_match_in_mexico_counts_on_its_mexican_day(monkeypatch):
    mvs = pd.DataFrame([_mvs(kick="2030-01-06 02:00")])               # 20:00 del 5 en México
    monkeypatch.setattr(PP, "_candidates_of", lambda *a, **k: [_cand("under_3.5", 0.7, 0.7, 1.5)])
    days = PL.build_parlays(mvs, pd.DataFrame([_up()]), ["soccer_italy_serie_a"])
    assert list(days) == [pd.Timestamp("2030-01-05")]


def _reads(stored=None, picks=None):
    mvs, up = _five()
    return {"FROM model_vs_sharp": mvs, "FROM upcoming_matches": up,
            "FROM program_picks": pd.DataFrame(picks if picks is not None else
                                               _picks("soccer_italy_serie_a", 10, 0.5)),
            "FROM program_parlays": stored if stored is not None else pd.DataFrame(columns=["day", "first"]),
            "playdoit_prices": pd.DataFrame(), "FROM matches": pd.DataFrame(), "FROM lineups": pd.DataFrame()}


def _patch(monkeypatch, reads):
    monkeypatch.setattr(PL.pd, "read_sql", lambda sql, con=None, params=None, **kw: next(
        df for k, df in reads.items() if k in str(sql)))
    monkeypatch.setattr(PL.pd.Timestamp, "now", classmethod(lambda cls, tz=None: NOW))
    monkeypatch.setattr(PP, "_candidates_of", lambda *a, **k: [_cand("under_3.5", 0.7, 0.7, 1.5)])


def test_the_snapshot_redoes_the_parlay_until_its_first_leg_starts(monkeypatch):
    eng = FakeEngine()
    _patch(monkeypatch, _reads())
    assert PL.snapshot_parlays(engine=eng) == 4
    (sql, _), = eng.statements("DELETE FROM program_parlays")
    assert "result IS NULL" in sql                                   # un borrador, nunca algo liquidado
    (sql, params), = eng.statements("INSERT INTO program_parlays")
    assert params["rank0"] == 1 and params["day0"] == pd.Timestamp("2030-01-05").date()
    assert params["playdoit_odds0"] == 1.5 and params["safety0"] == 0.7


def test_a_parlay_whose_first_leg_started_is_not_touched(monkeypatch):
    eng = FakeEngine()
    stored = pd.DataFrame([{"day": pd.Timestamp("2030-01-05").date(),
                            "first": pd.Timestamp("2030-01-05 14:30", tz="UTC")}])   # empezó hace 30 min
    _patch(monkeypatch, _reads(stored))
    assert PL.snapshot_parlays(engine=eng) == 0
    assert eng.statements("DELETE FROM program_parlays") == [] and eng.statements("INSERT INTO program_parlays") == []


def test_without_a_winning_league_a_stale_draft_is_cleared(monkeypatch):
    eng = FakeEngine()
    stored = pd.DataFrame([{"day": pd.Timestamp("2030-01-05").date(),
                            "first": pd.Timestamp("2030-01-05 19:45", tz="UTC")}])
    _patch(monkeypatch, _reads(stored, picks=_picks("soccer_italy_serie_a", 10, -0.5, "loss")))
    assert PL.snapshot_parlays(engine=eng) == 0
    assert len(eng.statements("DELETE FROM program_parlays")) == 1   # el de antes ya no vale
    assert eng.statements("INSERT INTO program_parlays") == []


def test_a_failed_parlay_snapshot_is_an_error_that_reaches_telegram(monkeypatch):
    RUN_ISSUES.reset()

    def boom(*a, **k):
        raise RuntimeError("neon caído")
    monkeypatch.setattr(PL.pd, "read_sql", boom)
    assert PL.snapshot_parlays(engine=FakeEngine()) == 0
    assert any("Parlay del día no guardado" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


def test_legs_settle_at_the_playdoit_price(monkeypatch):
    import src.models.save_bets as sb
    eng = FakeEngine()
    kick = pd.Timestamp("2030-01-05 19:45", tz="UTC")
    pending = pd.DataFrame([{"id": 7, "match": "napoli vs lecce", "match_date": kick, "market": "under_3.5",
                             "playdoit_odds": 1.5}])
    matches = pd.DataFrame([{"home_team_l": "napoli", "away_team_l": "lecce", "date": kick.tz_convert(None),
                             "home_goals": 2, "away_goals": 0, "home_corners": None, "away_corners": None,
                             "home_yellow": None, "away_yellow": None, "home_goals_ht": None, "away_goals_ht": None,
                             "home_goals_h2": None, "away_goals_h2": None, "home_shots_target": None,
                             "away_shots_target": None}])
    monkeypatch.setattr(PL.pd, "read_sql", lambda *a, **k: pending)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: matches)
    monkeypatch.setattr(sb, "normalize_team", lambda t: t)
    assert PL.resolve_parlays(engine=eng)["resolved"] == 1
    (sql, plan), = eng.statements("UPDATE program_parlays SET result = :result")
    assert plan[0]["result"] == "win" and plan[0]["profit"] == pytest.approx(0.5)


def test_the_closing_saves_the_parlay_right_after_the_picks(monkeypatch):
    from scripts import orchestrator as O
    called = []
    monkeypatch.setattr(PP, "snapshot_picks", lambda: called.append("picks"))
    monkeypatch.setattr(PL, "snapshot_parlays", lambda: called.append("parlay"))
    O.step_program_picks()
    assert called == ["picks", "parlay"]


def test_a_rescheduled_match_voids_its_parlay_leg_too():
    from src.models import rescheduled as R
    assert "program_parlays" in R.WRITTEN and "SET result = 'stale'" in R.VOID_SQL["program_parlays"]


# ─── la pestaña ────────────────────────────────────────────────────────────

def _leg(rank, odds=1.5, result=None, kick="2030-01-05 19:45", match=None, market="under_3.5", day="2030-01-05"):
    return {"day": pd.Timestamp(day).date(), "rank": rank, "match": match or f"team{rank} vs rival{rank}",
            "match_date": pd.Timestamp(kick, tz="UTC"), "league": "soccer_italy_serie_a", "market": market,
            "label": "Menos de 3.5 goles", "family": "Goles", "model_prob": 0.72, "market_prob": 0.70,
            "safety": 0.70, "playdoit_odds": odds, "lineup": None, "result": result,
            "profit": None}


def _board(legs, live=None, now=NOW, official=None):
    return X.parlay_board(pd.DataFrame(legs), pd.DataFrame(live or []), [], now=now,
                          official=pd.DataFrame(official) if official else None)


def test_the_parlay_of_k_multiplies_the_k_safest_legs():
    b = _board([_leg(1, 1.5), _leg(2, 1.4), _leg(3, 1.3)])
    (d,) = b["days"]
    p2, p3, p4 = d["parlays"]["2"], d["parlays"]["3"], d["parlays"]["4"]
    assert p2["ranks"] == [1, 2] and p2["odds"] == pytest.approx(2.1) and p2["odds_us"] == "+110"   # americano
    assert p3["odds"] == pytest.approx(2.73) and p3["p_market"] == pytest.approx(0.343)
    assert p4["k"] == 3 and not p4["full"]                           # ese día solo había 3
    assert p3["status"] == "pending" and not p3["locked"] and p3["first_kickoff"].startswith("2030-01-05T19:45")


def test_one_lost_leg_loses_the_parlay_and_a_void_one_drops_out():
    b = _board([_leg(1, 1.5, "win"), _leg(2, 1.4, "loss"), _leg(3, 1.3, "push"), _leg(4, 1.2)])
    p = b["days"][0]["parlays"]
    assert p["2"]["status"] == "loss" and p["2"]["profit"] == -1.0 and p["2"]["settled"]
    p = _board([_leg(1, 1.5, "win"), _leg(2, 1.4, "stale"), _leg(3, 1.3, "win")])["days"][0]["parlays"]
    assert p["3"]["status"] == "win" and p["3"]["profit"] == pytest.approx(1.5 * 1.3 - 1)
    assert p["4"]["k"] == 3


def test_a_leg_with_the_final_score_is_provisional_and_does_not_count_yet():
    live = [{"home": "team1", "away": "rival1", "kickoff": pd.Timestamp("2030-01-05 19:45", tz="UTC"),
             "home_goals": 1, "away_goals": 0, "status": "final", "clock": ""},
            {"home": "team2", "away": "rival2", "kickoff": pd.Timestamp("2030-01-05 19:45", tz="UTC"),
             "home_goals": 3, "away_goals": 2, "status": "live", "clock": "80'"}]
    b = _board([_leg(1), _leg(2)], live, now=pd.Timestamp("2030-01-05 21:30", tz="UTC"))
    legs, p = b["days"][0]["legs"], b["days"][0]["parlays"]["2"]
    assert legs[0]["result"] == "win" and legs[0]["provisional"]
    assert legs[1]["result"] == "pending" and legs[1]["live"]["clock"] == "80'"   # en juego: solo con el final
    assert p["status"] == "pending" and p["won_legs"] == 1 and p["locked"]
    live[1]["status"] = "final"
    b = _board([_leg(1), _leg(2)], live, now=pd.Timestamp("2030-01-05 22:00", tz="UTC"))
    p = b["days"][0]["parlays"]["2"]
    assert b["days"][0]["legs"][1]["result"] == "loss"                # 5 goles: menos de 3.5 perdió
    assert p["status"] == "loss" and not p["settled"]
    assert b["record"]["2"]["n"] == 0                                 # provisional: no cuenta todavía


def test_espn_missing_the_official_score_still_resolves_the_leg():
    official = [{"date": pd.Timestamp("2030-01-05"), "h": "team1", "a": "rival1", "home_goals": 0, "away_goals": 0,
                 "home_corners": None, "away_corners": None, "home_yellow": None, "away_yellow": None,
                 "home_red": None, "away_red": None}]
    b = _board([_leg(1), _leg(2)], now=pd.Timestamp("2030-01-06 02:00", tz="UTC"), official=official)
    assert b["days"][0]["legs"][0]["result"] == "win" and b["days"][0]["legs"][0]["provisional"]
    assert b["days"][0]["legs"][1]["result"] == "pending"


def test_the_record_counts_only_settled_days_that_had_all_k_legs():
    legs = ([_leg(1, 1.5, "win", day="2030-01-03"), _leg(2, 1.4, "win", day="2030-01-03"),
             _leg(3, 1.3, "loss", day="2030-01-03")]
            + [_leg(1, 1.6, "win", day="2030-01-04"), _leg(2, 1.5, "win", day="2030-01-04")])
    r = _board(legs)["record"]
    assert r["2"]["n"] == 2 and r["2"]["won"] == 2 and r["2"]["units"] == pytest.approx(1.1 + 1.4, abs=0.01)
    assert r["3"] == {**r["3"], "n": 1, "won": 0, "lost": 1, "units": -1.0, "legs": 3, "legs_won": 2}
    assert r["4"]["n"] == 0                                          # ningún día tuvo 4


def test_parlay_endpoint_and_tab(monkeypatch):
    import dashboard.app as A
    picks = pd.DataFrame(_picks("soccer_italy_serie_a", 10, 0.5))
    legs = pd.DataFrame([_leg(1), _leg(2)])

    def fake_q(sql, params=None, optional=False):
        s = " ".join(sql.split())
        if "FROM program_parlays" in s:
            return legs
        if "FROM program_picks" in s:
            return picks
        if "FROM live_scores" in s or "FROM matches" in s:
            return pd.DataFrame()
        raise AssertionError(s[:90])
    monkeypatch.setattr(A, "_q", fake_q)
    c = A.app.test_client()
    d = c.get("/api/board/parlay").get_json()
    assert d["ok"] and d["days"][0]["parlays"]["2"]["odds_us"] == "+125"
    assert d["leagues"][0]["name"] == "Serie A" and d["rules"]["min_odds_us"] == "-400"
    html = c.get("/").get_data(as_text=True)
    for el_id in ('data-tab="parlay"', 'id="tab-parlay"', 'id="xsize"', 'id="xnext"', 'id="xkpis"', 'id="xhist"'):
        assert el_id in html
    from pathlib import Path
    js = (Path(A.__file__).parent / "ui_es5.js").read_text(encoding="utf-8")
    assert "'parlay'" in js and "/api/board/parlay" in js
