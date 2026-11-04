"""
El parlay del día (v2.6.0, 3-nov-26).

El dueño: "quiero agregar la función parley, donde seleccione los partidos más
seguros, de las mejores ligas, de los que más se ganan y los agrupe en una
pestaña". Eligió: las ligas donde los picks van ganando, de cada partido la
apuesta más segura aunque no sea el pick, y 2, 3 o 4 partidos a elegir. Se
guarda y se mide como los picks. Sin DB.
"""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from dashboard import parlay as X
from src.models import parlay_alerts as PA
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


def test_a_slot_keeps_the_four_safest_one_per_match_and_only_winning_leagues(monkeypatch):
    mvs, up = _five()
    safe = {"napoli": 0.80, "roma": 0.62, "inter": 0.75, "milan": 0.58, "juventus": 0.70}
    monkeypatch.setattr(PP, "_candidates_of", lambda r, *a, **k: [
        _cand("home_win", safe[r["match"].split(" vs ")[0]], 0.9, 1.40, "Ganador"),
        _cand("under_3.5", 0.56, 0.56, 1.70)])
    blocks = PL.build_parlays(mvs, up, ["soccer_italy_serie_a"])
    (slot, legs), = blocks.items()                                   # los 5 empiezan a las 19:45
    assert slot == pd.Timestamp("2030-01-05 19:45", tz="UTC")
    assert legs[0]["day"] == pd.Timestamp("2030-01-05").date()       # 19:45 UTC = 13:45 de México
    assert [x["match"] for x in legs] == ["napoli vs lecce", "inter vs genoa", "juventus vs parma", "roma vs como"]
    assert [x["rank"] for x in legs] == [1, 2, 3, 4] and legs[0]["display"] == "Napoli vs Lecce"
    assert PL.build_parlays(mvs, up, ["soccer_epl"]) == {}            # la liga no va ganando: nada


def test_a_late_match_in_mexico_counts_on_its_mexican_day(monkeypatch):
    mvs = pd.DataFrame([_mvs(kick="2030-01-06 02:00"), _mvs("roma vs como", kick="2030-01-06 02:00")])
    monkeypatch.setattr(PP, "_candidates_of", lambda *a, **k: [_cand("under_3.5", 0.7, 0.7, 1.5)])
    blocks = PL.build_parlays(mvs, pd.DataFrame([_up(), _up("roma", "como")]), ["soccer_italy_serie_a"])
    (legs,) = blocks.values()
    assert {x["day"] for x in legs} == {pd.Timestamp("2030-01-05").date()}   # 20:00 del 5 en México


# ─── un parlay por horario (4-nov-26) ──────────────────────────────────────
# El dueño: "si son al mismo horario sería mejor porque las alineaciones ya
# estarían para todos los picks del parlay".

def _at(*kicks):
    teams = [("napoli", "lecce"), ("roma", "como"), ("inter", "genoa"), ("milan", "torino"), ("juventus", "parma"),
             ("lazio", "verona")]
    mvs = pd.DataFrame([_mvs(f"{h} vs {a}", kick=k) for (h, a), k in zip(teams, kicks)])
    up = pd.DataFrame([_up(h, a) for h, a in teams[:len(kicks)]])
    return mvs, up


def test_one_parlay_per_slot_of_matches_that_start_within_30_minutes(monkeypatch):
    monkeypatch.setattr(PP, "_candidates_of", lambda *a, **k: [_cand("under_3.5", 0.7, 0.7, 1.5)])
    # 12:00, 12:15, 12:30 juntos; 13:00 abre otro horario con 13:20; 21:00 solo
    mvs, up = _at("2030-01-05 18:00", "2030-01-05 18:15", "2030-01-05 18:30", "2030-01-05 19:00",
                  "2030-01-05 19:20", "2030-01-06 03:00")
    blocks = PL.build_parlays(mvs, up, ["soccer_italy_serie_a"])
    assert sorted(blocks) == [pd.Timestamp("2030-01-05 18:00", tz="UTC"), pd.Timestamp("2030-01-05 19:00", tz="UTC")]
    first, second = (blocks[s] for s in sorted(blocks))
    assert {x["match"] for x in first} == {"napoli vs lecce", "roma vs como", "inter vs genoa"}
    assert {x["match"] for x in second} == {"milan vs torino", "juventus vs parma"}
    assert [x["rank"] for x in second] == [1, 2]                     # cada horario con su orden
    assert all(x["slot"] == pd.Timestamp("2030-01-05 19:00", tz="UTC") for x in second)
    assert PL.BLOCK_MINUTES == 30 and PL.MIN_BLOCK_LEGS == 2          # el de las 21:00 solo: no es parlay


def test_a_match_already_in_a_fixed_parlay_does_not_enter_another(monkeypatch):
    monkeypatch.setattr(PP, "_candidates_of", lambda *a, **k: [_cand("under_3.5", 0.7, 0.7, 1.5)])
    mvs, up = _at("2030-01-05 18:00", "2030-01-05 18:15", "2030-01-05 18:20")
    blocks = PL.build_parlays(mvs, up, ["soccer_italy_serie_a"], exclude={"Napoli vs Lecce"})
    (legs,) = blocks.values()
    assert {x["match"] for x in legs} == {"roma vs como", "inter vs genoa"}
    assert legs[0]["slot"] == pd.Timestamp("2030-01-05 18:15", tz="UTC")


def test_a_leg_is_ready_with_its_lineup_or_where_espn_has_none():
    assert PL.lineup_ready("confirmada", "soccer_italy_serie_a", {"soccer_italy_serie_a"})
    assert PL.lineup_ready("confirmada: Lecce con 4 fuera", "soccer_italy_serie_a")
    assert not PL.lineup_ready(None, "soccer_italy_serie_a", {"soccer_italy_serie_a"})   # todavía no sale
    assert PL.lineup_ready(None, "soccer_korea_kleague1", {"soccer_italy_serie_a"})     # nunca va a salir
    assert not PL.lineup_ready(None, "soccer_korea_kleague1")                           # sin saber: espera
    assert PL.LINEUP_OK == PP.LINEUP_OK


def test_the_table_moves_once_from_one_parlay_per_day_to_one_per_slot():
    from tests.fake_db import FakeResult
    old = FakeEngine(lambda sql, p: FakeResult())                   # sin la columna slot
    with old.begin() as conn:
        PL._ensure(conn)
    sqls = [s for s, _ in old.executed]
    assert any("ADD COLUMN IF NOT EXISTS slot" in s for s in sqls)
    assert any("SET slot = f.first" in s for s in sqls)              # lo guardado: el primer partido de su día
    assert any("DROP CONSTRAINT IF EXISTS program_parlays_day_rank_key" in s for s in sqls)
    assert any("UNIQUE (slot, rank)" in s for s in sqls)
    new = FakeEngine(lambda sql, p: FakeResult(rows=[(1,)]) if "information_schema" in sql else FakeResult())
    with new.begin() as conn:
        PL._ensure(conn)
    assert not any("ALTER TABLE" in s for s, _ in new.executed)     # ya migrada: nada


def _reads(stored=None, picks=None):
    mvs, up = _five()
    return {"FROM model_vs_sharp": mvs, "FROM upcoming_matches": up,
            "FROM program_picks": pd.DataFrame(picks if picks is not None else
                                               _picks("soccer_italy_serie_a", 10, 0.5)),
            "FROM program_parlays": stored if stored is not None else pd.DataFrame(columns=["slot", "match"]),
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
    (sql, params), = eng.statements("DELETE FROM program_parlays")
    assert "result IS NULL" in sql and "slot > :lock" in sql         # un borrador, nunca algo fijo ni liquidado
    assert params["lock"] == NOW + pd.Timedelta(minutes=5)
    (sql, params), = eng.statements("INSERT INTO program_parlays")
    assert "ON CONFLICT (slot, rank)" in sql
    assert params["rank0"] == 1 and params["day0"] == pd.Timestamp("2030-01-05").date()
    assert params["slot0"] == pd.Timestamp("2030-01-05 19:45", tz="UTC")
    assert params["playdoit_odds0"] == 1.5 and params["safety0"] == 0.7


def test_a_fixed_parlay_keeps_its_matches(monkeypatch):
    eng = FakeEngine()
    # el parlay de las 15:04 quedó fijo (empieza en menos de 5 min) con napoli y roma
    stored = pd.DataFrame([{"slot": pd.Timestamp("2030-01-05 15:04", tz="UTC"), "match": "napoli vs lecce"},
                           {"slot": pd.Timestamp("2030-01-05 15:04", tz="UTC"), "match": "roma vs como"}])
    _patch(monkeypatch, _reads(stored))
    assert PL.snapshot_parlays(engine=eng) == 3                      # los otros 3 arman su propio parlay
    (_, params), = eng.statements("INSERT INTO program_parlays")
    assert {v for k, v in params.items() if k.startswith("match") and not k.startswith("match_date")} == \
        {"inter vs genoa", "milan vs torino", "juventus vs parma"}
    (sql, _), = eng.statements("DELETE FROM program_parlays")
    assert "slot > :lock" in sql                                     # el fijo no se borra


def test_without_a_winning_league_a_stale_draft_is_cleared(monkeypatch):
    eng = FakeEngine()
    stored = pd.DataFrame([{"slot": pd.Timestamp("2030-01-05 19:45", tz="UTC"), "match": "napoli vs lecce"}])
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
    assert any("Parlays no guardados" in e for e in RUN_ISSUES.errors())
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


def test_the_closing_warns_after_redoing_the_parlay_with_the_lineups(monkeypatch):
    from tests.test_program_picks import _steps
    names, steps, O = _steps(monkeypatch, "run_closing")
    assert names.index("Parlay listo (Telegram)") > names.index("Picks del programa")
    assert names.index("Picks del programa") > names.index("Alineaciones publicadas")
    assert steps["Parlay listo (Telegram)"] is O.step_parlay_alerts
    import inspect
    assert "run_parlay_alerts" in inspect.getsource(O.step_parlay_alerts)


def test_a_rescheduled_match_voids_its_parlay_leg_too():
    from src.models import rescheduled as R
    assert "program_parlays" in R.WRITTEN and "SET result = 'stale'" in R.VOID_SQL["program_parlays"]


# ─── la pestaña ────────────────────────────────────────────────────────────

def _leg(rank, odds=1.5, result=None, kick="2030-01-05 19:45", match=None, market="under_3.5", day="2030-01-05",
         slot=None, lineup=None, league="soccer_italy_serie_a"):
    return {"day": pd.Timestamp(day).date(), "rank": rank, "match": match or f"team{rank} vs rival{rank}",
            "match_date": pd.Timestamp(kick, tz="UTC"), "league": league, "market": market,
            "label": "Menos de 3.5 goles", "family": "Goles", "model_prob": 0.72, "market_prob": 0.70,
            "safety": 0.70, "playdoit_odds": odds, "lineup": lineup, "result": result,
            "profit": None, "slot": pd.Timestamp(slot, tz="UTC") if slot else None}


def _board(legs, live=None, now=NOW, official=None, covered=None):
    return X.parlay_board(pd.DataFrame(legs), pd.DataFrame(live or []), [], now=now,
                          official=pd.DataFrame(official) if official else None, covered=covered)


def test_each_slot_is_its_own_parlay_and_says_if_it_saw_every_lineup():
    s1, s2 = "2030-01-05 18:00", "2030-01-05 19:00"
    legs = [_leg(1, 1.5, kick=s1, slot=s1, lineup="confirmada"), _leg(2, 1.4, kick="2030-01-05 18:20", slot=s1,
                                                                       lineup="confirmada: Lecce con 4 fuera"),
            _leg(1, 1.6, kick=s2, slot=s2, match="a vs b", lineup="confirmada"),
            _leg(2, 1.3, kick=s2, slot=s2, match="c vs d"),
            _leg(3, 1.3, kick=s2, slot=s2, match="e vs f", league="soccer_korea_kleague1")]   # ESPN no la tiene
    b = _board(legs, covered={"soccer_italy_serie_a"})
    late, early = b["blocks"]                                        # del más reciente
    assert early["slot"].startswith("2030-01-05T18:00") and late["slot"].startswith("2030-01-05T19:00")
    assert early["day"] == late["day"] == "2030-01-05"
    assert early["lock"].startswith("2030-01-05T17:55")              # queda fijo 5 min antes
    assert early["ready"] and early["waiting"] == []
    assert early["parlays"]["2"]["odds"] == pytest.approx(2.1) and early["parlays"]["3"]["k"] == 2
    assert not late["ready"] and late["waiting"] == [late["legs"][1]["match"]]   # la de Corea no se espera
    assert [x["no_lineups"] for x in late["legs"]] == [False, False, True]


def test_parlays_saved_before_the_slot_column_take_their_days_first_match():
    legs = [_leg(1, kick="2030-01-05 19:00"), _leg(2, kick="2030-01-06 02:00")]   # v2.6.0: sin slot
    (b,) = _board(legs)["blocks"]
    assert b["slot"].startswith("2030-01-05T19:00") and len(b["legs"]) == 2


def test_the_parlay_of_k_multiplies_the_k_safest_legs():
    b = _board([_leg(1, 1.5), _leg(2, 1.4), _leg(3, 1.3)])
    (d,) = b["blocks"]
    p2, p3, p4 = d["parlays"]["2"], d["parlays"]["3"], d["parlays"]["4"]
    assert p2["ranks"] == [1, 2] and p2["odds"] == pytest.approx(2.1) and p2["odds_us"] == "+110"   # americano
    assert p3["odds"] == pytest.approx(2.73) and p3["p_market"] == pytest.approx(0.343)
    assert p4["k"] == 3 and not p4["full"]                           # ese día solo había 3
    assert p3["status"] == "pending" and not p3["locked"] and p3["first_kickoff"].startswith("2030-01-05T19:45")


def test_one_lost_leg_loses_the_parlay_and_a_void_one_drops_out():
    b = _board([_leg(1, 1.5, "win"), _leg(2, 1.4, "loss"), _leg(3, 1.3, "push"), _leg(4, 1.2)])
    p = b["blocks"][0]["parlays"]
    assert p["2"]["status"] == "loss" and p["2"]["profit"] == -1.0 and p["2"]["settled"]
    p = _board([_leg(1, 1.5, "win"), _leg(2, 1.4, "stale"), _leg(3, 1.3, "win")])["blocks"][0]["parlays"]
    assert p["3"]["status"] == "win" and p["3"]["profit"] == pytest.approx(1.5 * 1.3 - 1)
    assert p["4"]["k"] == 3


def test_a_leg_with_the_final_score_is_provisional_and_does_not_count_yet():
    live = [{"home": "team1", "away": "rival1", "kickoff": pd.Timestamp("2030-01-05 19:45", tz="UTC"),
             "home_goals": 1, "away_goals": 0, "status": "final", "clock": ""},
            {"home": "team2", "away": "rival2", "kickoff": pd.Timestamp("2030-01-05 19:45", tz="UTC"),
             "home_goals": 3, "away_goals": 2, "status": "live", "clock": "80'"}]
    b = _board([_leg(1), _leg(2)], live, now=pd.Timestamp("2030-01-05 21:30", tz="UTC"))
    legs, p = b["blocks"][0]["legs"], b["blocks"][0]["parlays"]["2"]
    assert legs[0]["result"] == "win" and legs[0]["provisional"]
    assert legs[1]["result"] == "pending" and legs[1]["live"]["clock"] == "80'"   # en juego: solo con el final
    assert p["status"] == "pending" and p["won_legs"] == 1 and p["locked"]
    live[1]["status"] = "final"
    b = _board([_leg(1), _leg(2)], live, now=pd.Timestamp("2030-01-05 22:00", tz="UTC"))
    p = b["blocks"][0]["parlays"]["2"]
    assert b["blocks"][0]["legs"][1]["result"] == "loss"                # 5 goles: menos de 3.5 perdió
    assert p["status"] == "loss" and not p["settled"]
    assert b["record"]["2"]["n"] == 0                                 # provisional: no cuenta todavía


def test_espn_missing_the_official_score_still_resolves_the_leg():
    official = [{"date": pd.Timestamp("2030-01-05"), "h": "team1", "a": "rival1", "home_goals": 0, "away_goals": 0,
                 "home_corners": None, "away_corners": None, "home_yellow": None, "away_yellow": None,
                 "home_red": None, "away_red": None}]
    b = _board([_leg(1), _leg(2)], now=pd.Timestamp("2030-01-06 02:00", tz="UTC"), official=official)
    assert b["blocks"][0]["legs"][0]["result"] == "win" and b["blocks"][0]["legs"][0]["provisional"]
    assert b["blocks"][0]["legs"][1]["result"] == "pending"


def test_the_record_counts_only_settled_days_that_had_all_k_legs():
    d3, d4 = dict(day="2030-01-03", kick="2030-01-03 19:45"), dict(day="2030-01-04", kick="2030-01-04 19:45")
    legs = ([_leg(1, 1.5, "win", **d3), _leg(2, 1.4, "win", **d3), _leg(3, 1.3, "loss", **d3)]
            + [_leg(1, 1.6, "win", **d4), _leg(2, 1.5, "win", **d4)])
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
    assert d["ok"] and d["blocks"][0]["parlays"]["2"]["odds_us"] == "+125"
    assert d["leagues"][0]["name"] == "Serie A" and d["rules"]["min_odds_us"] == "-400"
    assert d["rules"]["block_minutes"] == 30
    assert not d["blocks"][0]["ready"] and len(d["blocks"][0]["waiting"]) == 2      # sin alineaciones todavía
    html = c.get("/").get_data(as_text=True)
    for el_id in ('data-tab="parlay"', 'id="tab-parlay"', 'id="xsize"', 'id="xnext"', 'id="xkpis"', 'id="xhist"'):
        assert el_id in html
    from pathlib import Path
    js = (Path(A.__file__).parent / "ui_es5.js").read_text(encoding="utf-8")
    assert "'parlay'" in js and "/api/board/parlay" in js
    assert "_px.blocks" in js and "_px.days" not in js                # un parlay por horario


# ─── el aviso de Telegram (4-nov-26) ───────────────────────────────────────

MX = ZoneInfo("America/Mexico_City")
SLOT = pd.Timestamp("2030-01-05 18:00", tz="UTC")


def _row(rank, match, lineup="confirmada", odds=1.5, league="soccer_italy_serie_a", slot=SLOT, kick=None, market="under_3.5"):
    return {"slot": slot, "rank": rank, "match": match, "display": match.title(), "match_date": kick or slot,
            "league": league, "label": "Menos de 3.5 goles", "market": market, "model_prob": 0.72,
            "market_prob": 0.70, "playdoit_odds": odds, "lineup": lineup}


def test_a_parlay_is_announced_once_when_every_leg_saw_its_lineup():
    rows = [_row(1, "napoli vs lecce", odds=1.5), _row(2, "roma vs como", odds=1.4, lineup=None)]
    covered = {"soccer_italy_serie_a"}
    assert PA.to_alert(rows, {}, covered) == []                      # falta la alineación de roma
    rows[1]["lineup"] = "confirmada: Como con 4 fuera"
    ((key, legs, changed),) = PA.to_alert(rows, {}, covered)
    sent = {key: PA.signature(legs)}
    assert key == SLOT.isoformat() and not changed
    assert PA.to_alert(rows, sent, covered) == []                    # ya avisado
    rows[1]["market"] = "over_1.5"                                   # cambió la apuesta antes de quedar fija
    ((_, _, changed),) = PA.to_alert(rows, sent, covered)
    assert changed
    assert PA.to_alert([_row(1, "napoli vs lecce")], {}, covered) == []          # uno solo no es parlay


def test_the_announcement_lists_the_legs_and_what_the_parlay_pays():
    rows = [_row(2, "roma vs como", odds=1.4, kick=SLOT + pd.Timedelta(minutes=15), lineup="confirmada: Como con 4 fuera"),
            _row(1, "napoli vs lecce", odds=1.5), _row(3, "inter vs genoa", odds=1.3, league="soccer_korea_kleague1",
                                                       lineup=None)]
    t = PA.alert_text(rows, MX)
    assert "Parlay de las 12:00" in t and "queda fijo a las 11:55" in t           # hora de México
    assert t.index("Napoli Vs Lecce") < t.index("Roma Vs Como") < t.index("Inter Vs Genoa")   # de la más segura
    assert "12:15" in t and "Como con 4 fuera" in t and "sin alineación en ESPN" in t
    assert "Parlay de 2: <b>+110</b>" in t and "Parlay de 3: <b>+173</b>" in t     # americano, nunca decimal
    assert "2.1" not in t and "Parlay de 4" not in t
    assert "Cambió el parlay de las 12:00" in PA.alert_text(rows, MX, changed=True)


def test_the_closing_sends_each_ready_parlay_and_remembers_it(monkeypatch):
    rows = pd.DataFrame([_row(1, "napoli vs lecce"), _row(2, "roma vs como")])
    monkeypatch.setattr(PA.pd, "read_sql", lambda *a, **k: rows)
    state, sent = {}, []
    kw = dict(engine=object(), now=pd.Timestamp("2030-01-05 17:30", tz="UTC").to_pydatetime(),
              load_state=lambda k: state.get(k), save_state=lambda k, v, history=True: state.__setitem__(k, v),
              covered={"soccer_italy_serie_a"})
    assert PA.run_parlay_alerts(send=lambda t: sent.append(t) or True, **kw)["sent"] == 1
    assert state[PA.ALERT_KEY]["sent"] == {SLOT.isoformat(): "napoli vs lecce:under_3.5 | roma vs como:under_3.5"}
    assert PA.run_parlay_alerts(send=lambda t: sent.append(t) or True, **kw)["sent"] == 0   # una sola vez
    assert len(sent) == 1


def test_a_failed_announcement_is_retried_on_the_next_closing(monkeypatch):
    RUN_ISSUES.reset()
    rows = pd.DataFrame([_row(1, "napoli vs lecce"), _row(2, "roma vs como")])
    monkeypatch.setattr(PA.pd, "read_sql", lambda *a, **k: rows)
    state = {}
    out = PA.run_parlay_alerts(engine=object(), now=pd.Timestamp("2030-01-05 17:30", tz="UTC").to_pydatetime(),
                               send=lambda t: False, load_state=lambda k: state.get(k),
                               save_state=lambda k, v, history=True: state.__setitem__(k, v),
                               covered={"soccer_italy_serie_a"})
    assert out["sent"] == 0 and PA.ALERT_KEY not in state            # no quedó como avisado
    assert any("no se mandó a Telegram" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


def test_old_announcements_are_forgotten(monkeypatch):
    now = pd.Timestamp("2030-01-10 12:00", tz="UTC").to_pydatetime()
    old = {SLOT.isoformat(): "x", pd.Timestamp("2030-01-10 18:00", tz="UTC").isoformat(): "y"}
    state = {PA.ALERT_KEY: {"sent": old}}
    monkeypatch.setattr(PA.pd, "read_sql", lambda *a, **k: pd.DataFrame(columns=["slot"]))
    PA.run_parlay_alerts(engine=object(), now=now, send=lambda t: True, load_state=lambda k: state.get(k),
                         save_state=lambda k, v, history=True: state.__setitem__(k, v), covered=set())
    assert list(state[PA.ALERT_KEY]["sent"]) == [pd.Timestamp("2030-01-10 18:00", tz="UTC").isoformat()]
