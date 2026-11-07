"""
v2.8.0 (5-nov-26), las dos cosas que pidió el dueño ("haz las 2"):

1. El pick y el parlay quedan fijos a la hora de ESPN si es más temprana: el
   1-nov Panathinaikos–AEK y Vancouver–Seattle se rehicieron con momios en
   juego porque The Odds API tenía la hora 1-1.5 h tarde. Más el chequeo
   diario que avisa si vuelve a pasar.
2. El pick de antes de la alineación se guarda con su momio, se liquida
   aparte y el reporte semanal dice si los cambios con la alineación ayudan
   o estorban.
"""

import inspect

import pandas as pd
import pytest

from src.features import live_scores as LS
from src.models import lineup_changes as LC
from src.models import program_parlays as PL
from src.models import program_picks as PP
from tests.fake_db import FakeEngine, FakeResult

UTC = lambda s: pd.Timestamp(s, tz="UTC")


# ─── 1. la hora real de ESPN ────────────────────────────────────────────────

def test_the_closing_saves_espn_kickoff_of_what_starts_soon_even_if_it_already_began(monkeypatch):
    from tests.test_live_scores import _ev
    system = UTC("2026-10-24 19:00").to_pydatetime()               # The Odds API: 19:00
    eng = FakeEngine(lambda sql, p: FakeResult(rows=[("soccer_epl", "chelsea", "tottenham", system)])
                     if "FROM upcoming_matches" in sql else FakeResult())
    ev = _ev("9", "Chelsea", "Tottenham Hotspur", 1, 0, state="in", completed=False)
    ev["date"] = "2026-10-24T17:30Z"                                 # ESPN: empezó a las 17:30

    class R:
        status_code = 200

        def json(self):
            return {"events": [ev]}
    LS.capture_live_scores(eng, get=lambda url, headers=None, timeout=None: R(),
                           now=UTC("2026-10-24 18:01").to_pydatetime())
    (sql, window), = [x for x in eng.executed if "FROM upcoming_matches" in x[0]]
    assert window["b"] == UTC("2026-10-24 21:01")                    # también lo que empieza en las próximas 3 h
    (sql, rows), = eng.statements("INSERT INTO espn_kickoffs")
    assert rows[0]["home"] == "chelsea" and rows[0]["kickoff"] == system
    assert rows[0]["espn_kickoff"] == UTC("2026-10-24 17:30")
    assert eng.statements("INSERT INTO live_scores") == []           # el marcador sigue igual que antes
    assert LS.kick_differences(rows) == rows                         # 90 min de diferencia: se avisa en el log


def test_a_pick_freezes_at_espn_kickoff_when_it_is_earlier():
    now = UTC("2026-11-01 18:01")
    kicks = [{"home": "panathinaikos", "away": "aek athens", "kickoff": UTC("2026-11-01 19:00"),
              "espn_kickoff": UTC("2026-11-01 17:30")},                 # ya empezó según ESPN
             {"home": "lecce", "away": "genoa", "kickoff": UTC("2026-11-01 19:00"),
              "espn_kickoff": UTC("2026-11-01 19:00")},                 # misma hora: todavía no
             {"home": "roma", "away": "como", "kickoff": UTC("2026-11-01 18:30"),
              "espn_kickoff": UTC("2026-11-01 18:05")}]                 # ESPN a 4 min: ya no se toca
    assert PP.frozen_by_espn(kicks, now) == {"panathinaikos vs aek athens", "roma vs como"}
    assert PP.KICK_LOCK_MIN == 5


def test_the_snapshot_does_not_touch_a_pick_that_espn_says_already_started(monkeypatch):
    from tests.test_program_picks import KICK, _reads
    eng = FakeEngine()
    reads = _reads({"FROM espn_kickoffs": pd.DataFrame([{
        "home": "atlas", "away": "club america", "kickoff": KICK, "espn_kickoff": UTC("2026-01-01 00:00")}])})
    monkeypatch.setattr(PP.pd, "read_sql", lambda sql, con=None, params=None, **kw: next(
        df for k, df in reads.items() if k in str(sql)))
    assert PP.snapshot_picks(engine=eng) == 0
    assert eng.statements("INSERT INTO program_picks") == []


def test_without_espn_kickoffs_the_system_time_rules_as_before(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('relation "espn_kickoffs" does not exist')
    monkeypatch.setattr(PP.pd, "read_sql", boom)
    assert PP.started_early(FakeEngine(), now=UTC("2026-11-01 18:00")) == set()


def _parlay_patch(monkeypatch, stored, kicks):
    from tests import test_program_parlays as T
    reads = T._reads(stored)
    reads["FROM espn_kickoffs"] = pd.DataFrame(kicks, columns=["home", "away", "kickoff", "espn_kickoff"])
    T._patch(monkeypatch, reads)


def test_a_parlay_slot_freezes_when_espn_says_one_of_its_matches_started(monkeypatch):
    from tests.test_program_parlays import NOW
    eng = FakeEngine()
    slot = UTC("2030-01-05 19:45")                                   # el sistema: 19:45; ESPN: napoli a las 14:50
    stored = pd.DataFrame([{"slot": slot, "match": "napoli vs lecce"}, {"slot": slot, "match": "roma vs como"}])
    _parlay_patch(monkeypatch, stored, [{"home": "napoli", "away": "lecce", "kickoff": slot,
                                         "espn_kickoff": NOW - pd.Timedelta(minutes=10)}])
    assert PL.snapshot_parlays(engine=eng) == 0
    (sql, params), = eng.statements("DELETE FROM program_parlays")
    assert "slot <> :keep0" in sql and params["keep0"] == slot      # el borrador de ese horario queda como estaba
    assert eng.statements("INSERT INTO program_parlays") == []       # y nada pisa ese horario


def test_a_match_that_espn_says_started_does_not_enter_a_new_parlay(monkeypatch):
    from tests.test_program_parlays import NOW
    eng = FakeEngine()
    _parlay_patch(monkeypatch, None, [{"home": "napoli", "away": "lecce", "kickoff": UTC("2030-01-05 17:45"),
                                       "espn_kickoff": NOW - pd.Timedelta(minutes=10)}])
    # v2.9.0: sin napoli quedan 3 favoritos en horarios distintos, no alcanzan para el parlay del día
    assert PL.snapshot_parlays(engine=eng) == 0
    assert eng.statements("INSERT INTO program_parlays") == []


def test_the_daily_check_catches_a_pick_saved_after_the_real_kickoff():
    from src.utils.output_checks import repicked_after_start
    kicks = [{"home": "panathinaikos", "away": "aek athens", "kickoff": UTC("2026-11-01 19:00"),
              "espn_kickoff": UTC("2026-11-01 17:30")}]
    picks = [{"match": "panathinaikos vs aek athens", "match_date": UTC("2026-11-01 19:00"),
              "picked_at": UTC("2026-11-01 18:34")},                    # con momios en juego
             {"match": "lecce vs genoa", "match_date": UTC("2026-11-01 17:00"),
              "picked_at": UTC("2026-11-01 16:31")},                    # antes de empezar: bien
             {"match": "roma vs como", "match_date": UTC("2026-11-01 17:00"),
              "picked_at": UTC("2026-11-01 17:01")}]                    # el closing tarda un minuto: bien
    out = repicked_after_start(picks, kicks)
    assert len(out) == 1 and out[0].startswith("panathinaikos vs aek athens") and "17:30" in out[0]


def test_the_daily_check_skips_rebuilt_picks_and_reads_parlays_too():
    from src.utils import output_checks as OC
    src = inspect.getsource(OC.run_output_checks)
    assert "FROM espn_kickoffs" in src and "program_parlays" in src
    assert "source IS DISTINCT FROM 'reconstruido'" in src            # se guardan después del partido a propósito


# ─── 2. el pick de antes de la alineación ───────────────────────────────────

def test_the_live_snapshot_keeps_the_pick_from_before_the_lineup_once():
    eng = FakeEngine()
    row = {c: None for c in PP._COLS + ("source",)}
    with eng.begin() as conn:
        PP._upsert(conn, [{**row, "match": "a vs b", "match_date": UTC("2030-01-05 19:00")}], "TRUE",
                   track_changes=True)
        PP._upsert(conn, [{**row, "match": "a vs b", "match_date": UTC("2030-01-05 19:00")}], "TRUE")
    (live, _), (rebuilt, _) = eng.statements("INSERT INTO program_picks")
    first = ("(program_picks.pre_lineup_at IS NULL AND program_picks.lineup IS NULL "
             "AND EXCLUDED.lineup LIKE 'confirmada%')")
    for col, old in (("label", "program_picks.label"), ("market", "program_picks.market"),
                     ("odds", "program_picks.playdoit_odds"), ("at", "NOW()")):
        assert f"pre_lineup_{col} = CASE WHEN {first} THEN {old} ELSE program_picks.pre_lineup_{col} END" in live
    assert "pre_lineup_star = CASE WHEN" in live
    assert "pre_lineup" not in rebuilt                               # la reconstrucción de picks pasados no


def test_the_table_gets_the_pre_lineup_columns():
    cols = " ".join(PP.PICKS_ALTERS)
    for c in ("pre_lineup_label TEXT", "pre_lineup_market TEXT", "pre_lineup_star BOOLEAN",
              "pre_lineup_odds NUMERIC", "pre_lineup_at TIMESTAMPTZ", "pre_lineup_result TEXT",
              "pre_lineup_profit NUMERIC"):
        assert c in cols


def test_the_pick_from_before_the_lineup_settles_like_the_pick(monkeypatch):
    import src.models.save_bets as sb
    kick = UTC("2030-01-05 19:00")
    eng = FakeEngine()
    pre = pd.DataFrame([{"id": 7, "match": "atlas vs club america", "match_date": kick, "market": "home_win",
                         "playdoit_odds": 2.4},
                        {"id": 8, "match": "atlas vs club america", "match_date": kick, "market": "over_1.5",
                         "playdoit_odds": None}])
    empty = pd.DataFrame(columns=["id", "match", "match_date", "market", "playdoit_odds"])
    matches = pd.DataFrame([{"home_team_l": "atlas", "away_team_l": "club america", "date": kick.tz_convert(None),
                             "home_goals": 2, "away_goals": 1, "home_corners": None, "away_corners": None,
                             "home_yellow": None, "away_yellow": None, "home_goals_ht": None, "away_goals_ht": None,
                             "home_goals_h2": None, "away_goals_h2": None, "home_shots_target": None,
                             "away_shots_target": None}])
    monkeypatch.setattr(PP.pd, "read_sql", lambda sql, *a, **k: pre if "pre_lineup_market AS market" in str(sql)
                        else empty)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: matches)
    monkeypatch.setattr(sb, "normalize_team", lambda t: t)
    PP.resolve_picks(engine=eng)
    (sql, plan), = eng.statements("UPDATE program_picks SET pre_lineup_result = :result")
    by_id = {p["id"]: p for p in plan}
    assert by_id[7]["result"] == "win" and by_id[7]["profit"] == pytest.approx(1.4)
    assert by_id[8]["result"] == "win" and by_id[8]["profit"] is None       # sin momio guardado
    assert eng.statements("UPDATE program_picks SET pre_lineup_result = 'stale'")


def _chg(before, after, b_star, a_star, b_res, a_res, b_odds=1.9, a_odds=1.9, when="2030-01-05 19:00"):
    """Una fila de program_picks que vio la alineación (stake 1)."""
    profit = lambda res, o: (o - 1 if res == "win" else -1.0) if o is not None or res == "loss" else None
    return {"match": f"{before}|{after}", "display": "A vs B", "match_date": UTC(when),
            "label": after, "market": after and after.lower(), "reason": None if a_star else "Sin ventaja",
            "playdoit_odds": a_odds, "result": a_res, "profit": profit(a_res, a_odds),
            "pre_lineup_label": before, "pre_lineup_market": before and before.lower(), "pre_lineup_star": b_star,
            "pre_lineup_odds": b_odds, "pre_lineup_at": UTC(when) - pd.Timedelta(minutes=60),
            "pre_lineup_result": b_res, "pre_lineup_profit": profit(b_res, b_odds)}


def test_the_measure_counts_what_the_star_bets_would_have_left():
    rows = [_chg("Over", "Over", True, True, "win", "win"),           # no cambió: solo cuenta que la vio
            _chg("Home", "Home", True, False, "loss", "loss"),        # quitó la 🎯 y perdía: +1u
            _chg("Btts", "Under", True, True, "loss", "win"),         # 🎯 → otro 🎯: −1 → +0.9
            _chg("Away", "Btts no", False, False, "win", "loss"),     # sin 🎯: solo aciertos
            _chg("Over", "Home", True, False, "win", "win", b_odds=None),   # ganaba sin momio guardado
            _chg("Over", "Home", True, True, "win", None)]            # sin liquidar: no cuenta
    s = LC.summarize(rows)
    assert s["seen"] == 5 and s["changed"] == 4 and s["unknown"] == 1
    assert (s["hits_before"], s["hits_after"]) == (2, 2) and (s["helped"], s["hurt"]) == (1, 1)
    assert (s["bets_before"], s["bets_after"]) == (2, 1)
    assert s["units_before"] == pytest.approx(-2.0) and s["units_after"] == pytest.approx(0.9)
    assert s["verdict"].startswith("todavía pocos datos (2 de 20")


def test_the_verdict_needs_enough_changes_and_a_clear_difference():
    helped = [_chg("Home", "Home", True, False, "loss", "loss")] * 12 + \
             [_chg("Btts", "Under", True, True, "loss", "win")] * 10
    assert LC.summarize(helped)["verdict"].startswith("AYUDAN")
    hurt = [_chg("Home", "Home", True, False, "win", "win")] * 12 + \
           [_chg("Btts", "Under", True, True, "win", "loss")] * 10
    assert LC.summarize(hurt)["verdict"].startswith("ESTORBAN")
    mixed = [_chg("Home", "Home", True, False, "win", "win"), _chg("Home", "Home", True, False, "loss", "loss")] * 11
    assert LC.summarize(mixed)["verdict"].startswith("sin diferencia clara")


def test_the_weekly_block_reads_the_week_and_the_total(monkeypatch):
    now = UTC("2030-01-10 12:00")
    rows = [_chg("Home", "Home", True, False, "loss", "loss", when="2030-01-08 19:00"),
            _chg("Btts", "Under", True, True, "loss", "win", when="2029-12-20 19:00")]
    monkeypatch.setattr(LC.pd, "read_sql", lambda *a, **k: pd.DataFrame(rows))
    lines = LC.weekly_lines(engine=object(), now=now)
    text = "\n".join(lines)
    assert lines[0] == "— <b>CAMBIOS CON LA ALINEACIÓN</b> —"
    # en palabras normales (6-nov-26): la ganancia apostando 100 a cada uno, sin "u" ni desviaciones
    assert "Esta semana se revisaron 1 picks con la alineación y 1 cambiaron." in text
    assert "con el pick de antes, 1 apuestas y -100; con el nuevo, 0 apuestas y 0." in text
    assert "Desde el 20-12: 2 cambios · en los 🎯, con el pick de antes -200 y con el nuevo +90" in text
    assert "¿Conviene cambiar el pick con la alineación? todavía no se sabe: van 2 de 20 cambios en 🎯" in text
    assert not __import__("re").search(r"\du\b", text) and "desviaciones" not in text


def test_the_weekly_block_is_quiet_before_the_columns_exist(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('column "pre_lineup_at" does not exist')
    monkeypatch.setattr(LC.pd, "read_sql", boom)
    assert LC.weekly_lines(engine=object()) == []


def test_the_weekly_telegram_report_carries_the_block():
    import scripts.notify_telegram as NT
    src = inspect.getsource(NT.send_weekly_report)
    assert "lineup_changes.weekly_lines(engine)" in src
