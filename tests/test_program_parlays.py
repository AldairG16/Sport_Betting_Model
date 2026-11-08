"""
El parlay (v2.6.0, 3-nov-26) con bono (v2.9.0, 6-nov-26).

La extensión de Claude revisó PlayDoit el 5-nov sin apostar: el boleto paga un
bono sobre la ganancia (3 = 5%, 4 = 7.5%, 5 = 10%, 6 = 15%), cada selección a
−500 o más largo, y el Pago Anticipado (2 goles de ventaja) vale en el parlay.
El dueño eligió (1b, 2c, 4a): favoritos de 65%+ (Pinnacle sin margen) en las
ligas con pago anticipado; el parlay del día de 4 a 6 y el de un horario (3 o
más que empiezan juntos, con alineación); reemplaza al de "la apuesta más
segura". Sin DB.
"""

from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from dashboard import parlay as X
from src.models import early_payout as EP
from src.models import parlay_alerts as PA
from src.models import program_parlays as PL
from src.models import program_picks as PP
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine

NOW = pd.Timestamp("2030-01-05 15:00", tz="UTC")


def _mvs(match="napoli vs lecce", kick="2030-01-05 19:45", league="soccer_italy_serie_a", **kw):
    return {"match": match, "match_date": pd.Timestamp(kick, tz="UTC"), "league": league,
            "p_home": 0.70, "p_draw": 0.18, "p_away": 0.12, "pin_home": 0.69, "pin_draw": 0.19, "pin_away": 0.12,
            "lam_home": 2.2, "lam_away": 0.7, "p_over25": 0.62, "p_btts": None,
            "created_at": pd.Timestamp("2030-01-05 12:00", tz="UTC"), **kw}


def _up(home="napoli", away="lecce", **kw):
    # Pinnacle 1.40 / 5.0 / 8.0 → el local 68.7% sin margen; promedio de las casas 1.36 (-278)
    return {"home_team_norm": home, "away_team_norm": away, "home_team": home.title(), "away_team": away.title(),
            "pin_home_odds": 1.40, "pin_draw_odds": 5.0, "pin_away_odds": 8.0,
            "home_odds": 1.42, "draw_odds": 5.2, "away_odds": 8.5,
            "consensus_home_odds": 1.36, "consensus_draw_odds": 4.8, "consensus_away_odds": 7.6,
            "over25_odds": 1.70, "under25_odds": 2.20, "consensus_over25_odds": 1.66, "consensus_under25_odds": 2.15,
            "btts_yes_odds": None, "btts_no_odds": None, **kw}


# ─── la pierna: el favorito fuerte con pago anticipado ──────────────────────

def test_a_leg_is_the_65pct_favourite_with_early_payout_at_minus_500_or_longer():
    leg = PL.strong_leg(_mvs(), SimpleNamespace(**_up()))
    assert leg["market"] == "home_win" and leg["market_prob"] == pytest.approx(0.687, abs=1e-3)
    assert leg["playdoit_odds"] == pytest.approx(1.36)
    # Pinnacle 1.65 / 4.0 / 5.5: el local 58%, no es favorito fuerte
    assert PL.strong_leg(_mvs(), SimpleNamespace(**_up(pin_home_odds=1.65, pin_draw_odds=4.0, pin_away_odds=5.5))) is None
    # MLS: sin pago anticipado en la lista; Escocia tampoco (sin la etiqueta "PA")
    assert PL.strong_leg(_mvs(league="soccer_usa_mls"), SimpleNamespace(**_up())) is None
    assert PL.strong_leg(_mvs(league="soccer_spl"), SimpleNamespace(**_up())) is None
    # más corto que -500 no entra al bono del boleto
    assert PL.strong_leg(_mvs(), SimpleNamespace(**_up(consensus_home_odds=1.15))) is None
    # el favorito con 4+ titulares fuera no es pierna
    assert PL.strong_leg(_mvs(), SimpleNamespace(**_up()), weak={"home_win": "Alineación: …"}) is None
    # la Champions sí (la etiqueta "PA" salió en Bodø/Glimt–LASK)
    assert PL.strong_leg(_mvs(league="soccer_uefa_champs_league", league_factor=1.0), SimpleNamespace(**_up()))


def test_the_bonus_is_paid_on_the_profit_by_number_of_legs():
    assert [PL.bonus_for(n) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 12)] == \
        [0, 0, 0.05, 0.075, 0.10, 0.15, 0.20, 0.25, 0.35, 0.35]
    pay = PL.payout([1.5, 1.5, 1.5, 1.5])
    assert pay["odds"] == pytest.approx(5.0625) and pay["bonus"] == 0.075
    assert pay["with_bonus"] == pytest.approx(1 + 4.0625 * 1.075)      # sobre la ganancia, no sobre el pago
    # lo que vio la extensión: 5 selecciones a +779 con 10 → bono 7.79
    assert (PL.payout([8.79 ** 0.2] * 5)["with_bonus"] - 8.79) * 10 == pytest.approx(7.79, abs=0.01)


def _leg(match, kick, p=0.70, odds=1.40, league="soccer_italy_serie_a"):
    return {"match": match, "match_date": pd.Timestamp(kick, tz="UTC").to_pydatetime(), "league": league,
            "market": "home_win", "label": "Gana X", "family": "Ganador", "market_prob": p, "model_prob": p,
            "safety": p, "playdoit_odds": odds, "lineup": None, "pa": True}


def test_the_day_parlay_takes_the_strongest_four_to_six_and_another_with_the_rest():
    legs = [_leg(f"m{i} vs r{i}", f"2030-01-05 {10 + i}:00", p=0.66 + i / 100) for i in range(10)]
    days = [p for p in PL.group_parlays(legs) if p["kind"] == "dia"]
    assert [len(p["legs"]) for p in days] == [6, 4] and [p["seq"] for p in days] == [1, 2]
    assert [x["match"] for x in days[0]["legs"]][:2] == ["m9 vs r9", "m8 vs r8"]      # del más fuerte
    assert days[0]["slot"] == pd.Timestamp("2030-01-05 14:00", tz="UTC")             # su primer partido
    assert {x["rank"] for x in days[0]["legs"]} == set(range(1, 7))
    # 3 en el día no son parlay del día; 8 son uno de 6 (los otros 2 no alcanzan)
    assert [p for p in PL.group_parlays(legs[:3]) if p["kind"] == "dia"] == []
    assert [len(p["legs"]) for p in PL.group_parlays(legs[:8]) if p["kind"] == "dia"] == [6]


def test_three_or_more_starting_together_are_also_a_slot_parlay_with_their_lineups():
    legs = [_leg("a vs b", "2030-01-05 14:00"), _leg("c vs d", "2030-01-05 14:30"), _leg("e vs f", "2030-01-05 14:15"),
            _leg("g vs h", "2030-01-05 19:00"), _leg("i vs j", "2030-01-05 20:00")]
    out = PL.group_parlays(legs)
    slot = [p for p in out if p["kind"] == "horario"]
    assert len(slot) == 1 and {x["match"] for x in slot[0]["legs"]} == {"a vs b", "c vs d", "e vs f"}
    assert [len(p["legs"]) for p in out if p["kind"] == "dia"] == [5]                 # repite partidos: es otro
    # si el horario es el mismo parlay que el del día, es uno solo
    same = PL.group_parlays(legs[:3] + [_leg("k vs l", "2030-01-05 14:20")])
    assert [p["kind"] for p in same] == ["dia"]


def test_a_late_match_in_mexico_counts_on_its_mexican_day():
    legs = [_leg(f"m{i} vs r{i}", "2030-01-06 03:00") for i in range(2)] + \
           [_leg(f"n{i} vs s{i}", "2030-01-05 18:00") for i in range(2)]
    (p,) = [p for p in PL.group_parlays(legs) if p["kind"] == "dia"]                  # 21:00 del sábado en México
    assert p["legs"][0]["day"] == pd.Timestamp("2030-01-05").date()


def test_a_match_in_a_fixed_parlay_does_not_enter_another_of_the_same_kind():
    legs = [_leg(f"m{i} vs r{i}", f"2030-01-05 {10 + i}:00") for i in range(9)]
    out = PL.group_parlays(legs, exclude={"dia": {f"m{i} vs r{i}" for i in range(5)}},
                           fixed_seq={pd.Timestamp("2030-01-05").date(): 1})
    (p,) = [p for p in out if p["kind"] == "dia"]
    assert p["seq"] == 2 and {x["match"] for x in p["legs"]} == {f"m{i} vs r{i}" for i in range(5, 9)}


def test_the_table_moves_once_to_the_bonus_parlays_and_keeps_the_old_rule():
    from tests.fake_db import FakeResult
    old = FakeEngine(lambda sql, p: FakeResult())                   # sin columnas: las dos migraciones
    with old.begin() as conn:
        PL._ensure(conn)
    sqls = [s for s, _ in old.executed]
    assert any("ADD COLUMN IF NOT EXISTS slot" in s for s in sqls)
    assert any("SET rule = 'seguro'" in s for s in sqls)            # lo guardado: la regla anterior
    assert any("DROP CONSTRAINT IF EXISTS program_parlays_slot_rank_key" in s for s in sqls)
    assert any("UNIQUE (slot, rule, kind, seq, rank)" in s and "ADD CONSTRAINT" in s for s in sqls)
    new = FakeEngine(lambda sql, p: FakeResult(rows=[("slot",), ("rule",)]) if "information_schema" in sql
                     else FakeResult())
    with new.begin() as conn:
        PL._ensure(conn)
    assert not any("ALTER TABLE" in s for s, _ in new.executed)     # ya migrada: nada


def _four():
    names = [("napoli", "lecce"), ("roma", "como"), ("inter", "genoa"), ("milan", "torino")]
    mvs = pd.DataFrame([_mvs(f"{h} vs {a}", kick=f"2030-01-05 {17 + i}:45") for i, (h, a) in enumerate(names)])
    up = pd.DataFrame([_up(h, a) for h, a in names])
    return mvs, up


def _reads(stored=None):
    mvs, up = _four()
    return {"FROM model_vs_sharp": mvs, "FROM upcoming_matches": up,
            "FROM program_parlays": stored if stored is not None else
            pd.DataFrame(columns=["slot", "day", "match", "rule", "kind", "seq"]),
            "playdoit_prices": pd.DataFrame(), "FROM matches": pd.DataFrame(), "FROM lineups": pd.DataFrame()}


def _patch(monkeypatch, reads):
    monkeypatch.setattr(PL.pd, "read_sql", lambda sql, con=None, params=None, **kw: next(
        df for k, df in reads.items() if k in str(sql)))
    monkeypatch.setattr(PL.pd.Timestamp, "now", classmethod(lambda cls, tz=None: NOW))


def test_the_snapshot_saves_the_day_parlay_until_its_first_leg_starts(monkeypatch):
    eng = FakeEngine()
    _patch(monkeypatch, _reads())
    assert PL.snapshot_parlays(engine=eng) == 4
    (sql, params), = eng.statements("DELETE FROM program_parlays")
    assert "result IS NULL" in sql and "slot > :lock" in sql         # un borrador, nunca algo fijo ni liquidado
    (sql, params), = eng.statements("INSERT INTO program_parlays")
    assert "ON CONFLICT (slot, rule, kind, seq, rank)" in sql
    assert params["rule0"] == "bono" and params["kind0"] == "dia" and params["seq0"] == 1 and params["pa0"] is True
    assert params["slot0"] == pd.Timestamp("2030-01-05 17:45", tz="UTC")
    assert params["market0"] == "home_win" and params["label0"] == "Gana Napoli"


def test_a_fixed_day_parlay_keeps_its_matches(monkeypatch):
    eng = FakeEngine()
    stored = pd.DataFrame([{"slot": pd.Timestamp("2030-01-05 15:04", tz="UTC"), "day": pd.Timestamp("2030-01-05"),
                            "match": "napoli vs lecce", "rule": "bono", "kind": "dia", "seq": 1}])
    _patch(monkeypatch, _reads(stored))
    assert PL.snapshot_parlays(engine=eng) == 0                      # quedan 3: no alcanzan para otro del día
    assert eng.statements("INSERT INTO program_parlays") == []
    (sql, _), = eng.statements("DELETE FROM program_parlays")
    assert "slot > :lock" in sql                                     # el fijo no se borra


def test_a_failed_parlay_snapshot_is_an_error_that_reaches_telegram(monkeypatch):
    RUN_ISSUES.reset()

    def boom(*a, **k):
        raise RuntimeError("neon caído")
    monkeypatch.setattr(PL.pd, "read_sql", boom)
    assert PL.snapshot_parlays(engine=FakeEngine()) == 0
    assert any("Parlays no guardados" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


def _settle(monkeypatch, score, idx, kick="2030-01-05 19:45", market="home_win"):
    import src.models.save_bets as sb
    eng = FakeEngine()
    kick = pd.Timestamp(kick, tz="UTC")
    pending = pd.DataFrame([{"id": 7, "match": "napoli vs lecce", "match_date": kick, "market": market,
                             "league": "soccer_italy_serie_a", "playdoit_odds": 1.5}])
    matches = pd.DataFrame([{"home_team_l": "napoli", "away_team_l": "lecce", "date": kick.tz_convert(None),
                             "home_goals": score[0], "away_goals": score[1], "home_corners": None, "away_corners": None,
                             "home_yellow": None, "away_yellow": None, "home_goals_ht": None, "away_goals_ht": None,
                             "home_goals_h2": None, "away_goals_h2": None, "home_shots_target": None,
                             "away_shots_target": None}])
    monkeypatch.setattr(PL.pd, "read_sql", lambda *a, **k: pending)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: matches)
    monkeypatch.setattr(sb, "normalize_team", lambda t: t)
    monkeypatch.setattr(EP, "load_leads", lambda *a, **k: idx)
    out = PL.resolve_parlays(engine=eng)
    stmts = eng.statements("UPDATE program_parlays SET result = :result")
    return out, (stmts[0][1] if stmts else [])


def test_a_leg_that_went_two_goals_up_is_paid_early(monkeypatch):
    kick = pd.Timestamp("2030-01-05 19:45", tz="UTC")
    idx = {"napoli vs lecce": [(kick, 2, 0)]}                        # iba 2-0 y acabó 2-2
    out, plan = _settle(monkeypatch, (2, 2), idx)
    assert out["resolved"] == 1 and plan[0]["result"] == "win" and plan[0]["pa_paid"] is True
    assert plan[0]["profit"] == pytest.approx(0.5)
    _, plan = _settle(monkeypatch, (2, 2), {"napoli vs lecce": [(kick, 1, 0)]})     # solo 1 arriba: perdida
    assert plan[0]["result"] == "loss" and plan[0]["pa_paid"] is False
    _, plan = _settle(monkeypatch, (2, 0), {})                                     # ganada: nada que esperar
    assert plan[0]["result"] == "win" and plan[0]["pa_paid"] is False


def test_a_lost_leg_waits_for_espn_before_settling_without_early_payout(monkeypatch):
    soon = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
    out, plan = _settle(monkeypatch, (0, 1), {}, kick=soon)
    assert out["resolved"] == 0 and plan == []                      # sigue pendiente: falta la ventaja de ESPN
    old = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
    _, plan = _settle(monkeypatch, (0, 1), {}, kick=old)
    assert plan[0]["result"] == "loss" and plan[0]["pa_paid"] is False      # pasadas 30 h: perdida


def test_the_closing_saves_the_parlay_right_after_the_picks(monkeypatch):
    from scripts import orchestrator as O
    called = []
    monkeypatch.setattr(O, "_void_rescheduled", lambda: called.append("void"))     # v2.9.1: antes, el horario viejo
    monkeypatch.setattr(PP, "snapshot_picks", lambda: called.append("picks"))
    monkeypatch.setattr(PL, "snapshot_parlays", lambda: called.append("parlay"))
    O.step_program_picks()
    assert called == ["void", "picks", "parlay"]


def test_the_closing_warns_after_redoing_the_parlay_with_the_lineups(monkeypatch):
    from tests.test_program_picks import _steps
    names, steps, O = _steps(monkeypatch, "run_closing")
    # v2.11.0: el parlay va en los picks finales (src/models/final_alerts.py)
    assert names.index("Picks finales (Telegram)") > names.index("Picks del programa")
    assert names.index("Picks del programa") > names.index("Alineaciones publicadas")
    assert steps["Picks finales (Telegram)"] is O.step_final_alerts and "Parlay listo (Telegram)" not in names


def test_a_rescheduled_match_voids_its_parlay_leg_too():
    from src.models import rescheduled as R
    assert "program_parlays" in R.WRITTEN and "SET result = 'stale'" in R.VOID_SQL["program_parlays"]


# ─── el pago anticipado: la ventaja más grande de ESPN ─────────────────────

def _goal(team, shown):
    return {"scoringPlay": True, "team": {"id": team}, "clock": {"displayValue": shown, "value": 0}}


def test_the_biggest_lead_comes_from_espn_goals_and_must_add_up_to_the_score():
    det = [_goal("1", "10'"), _goal("1", "30'"), _goal("2", "60'"), _goal("2", "88'")]
    assert EP.max_leads(det, "1", 2, 2) == (2, 0)
    # el descuento del primer tiempo va antes del 46'
    det = [_goal("2", "46'"), _goal("1", "45'+2'"), _goal("1", "20'")]
    assert EP.max_leads(det, "1", 2, 1) == (2, 0)
    assert EP.max_leads(det, "1", 3, 1) == (None, None)             # no da el marcador: sin dato
    assert EP.max_leads([], "1", 0, 0) == (0, 0)


def test_live_scores_keep_the_biggest_lead_of_each_team():
    from src.features.live_scores import parse_live
    data = {"events": [{"id": "9", "date": "2030-01-05T19:45Z", "competitions": [{
        "status": {"type": {"state": "post", "completed": True}},
        "competitors": [{"homeAway": "home", "team": {"id": "1", "displayName": "Napoli"}, "score": "2"},
                        {"homeAway": "away", "team": {"id": "2", "displayName": "Lecce"}, "score": "2"}],
        "details": [_goal("1", "10'"), _goal("1", "30'"), _goal("2", "60'"), _goal("2", "88'")]}]}]}
    (row,) = parse_live(data)
    assert (row["home_max_lead"], row["away_max_lead"]) == (2, 0)


def test_only_the_winner_in_a_league_with_the_pa_tag_gets_it():
    assert EP.applies("soccer_epl", "home_win") and EP.applies("soccer_uefa_champs_league", "away_win")
    assert not EP.applies("soccer_epl", "draw") and not EP.applies("soccer_spl", "home_win")
    assert not EP.applies("soccer_mexico_ligamx", "home_win")       # el súper PA de México es otro mercado


# ─── la pestaña ────────────────────────────────────────────────────────────

def _row(rank, odds=1.5, result=None, kick="2030-01-05 19:45", match=None, kind="dia", seq=1, slot="2030-01-05 19:45",
         lineup=None, league="soccer_italy_serie_a", rule="bono", pa_paid=None, market="home_win"):
    return {"day": pd.Timestamp("2030-01-05").date(), "rank": rank, "match": match or f"team{rank} vs rival{rank}",
            "match_date": pd.Timestamp(kick, tz="UTC"), "league": league, "market": market,
            "label": f"Gana Team{rank}", "family": "Ganador", "model_prob": 0.72, "market_prob": 0.70,
            "safety": 0.70, "playdoit_odds": odds, "lineup": lineup, "result": result, "profit": None,
            "slot": pd.Timestamp(slot, tz="UTC"), "rule": rule, "kind": kind, "seq": seq, "pa": True,
            "pa_paid": pa_paid}


def _board(rows, live=None, now=NOW, official=None, covered=None):
    return X.parlay_board(pd.DataFrame(rows), pd.DataFrame(live or []), now=now,
                          official=pd.DataFrame(official) if official else None, covered=covered)


def test_each_parlay_shows_its_bonus_and_what_it_pays():
    b = _board([_row(i) for i in range(1, 5)] + [_row(9, rule="seguro")])
    (d,) = b["parlays"]                                              # el de la regla anterior no sale
    p = d["parlay"]
    assert d["kind"] == "dia" and p["k"] == 4 and p["bonus"] == 0.075
    assert p["odds_us"] == "+406" and p["odds_bonus_us"] == "+437"   # 5.0625 → con 7.5% sobre la ganancia
    assert p["p_market"] == pytest.approx(0.7 ** 4, abs=1e-3) and p["status"] == "pending"


def test_a_won_parlay_gets_the_bonus_and_a_void_leg_lowers_it():
    won = _board([_row(i, result="win") for i in range(1, 5)])["parlays"][0]["parlay"]
    assert won["status"] == "win" and won["profit"] == pytest.approx(4.0625 * 1.075, abs=1e-3)
    one_void = _board([_row(i, result="win") for i in range(1, 4)] + [_row(4, result="stale")])["parlays"][0]["parlay"]
    assert one_void["profit"] == pytest.approx((1.5 ** 3 - 1) * 1.05, abs=1e-3)     # 3 válidas: 5%
    lost = _board([_row(1, result="loss")] + [_row(i, result="win") for i in range(2, 5)])["parlays"][0]["parlay"]
    assert lost["status"] == "loss" and lost["profit"] == -1.0


def test_a_leg_two_goals_up_is_a_provisional_win_by_early_payout():
    live = [{"home": "team1", "away": "rival1", "kickoff": pd.Timestamp("2030-01-05 19:45", tz="UTC"),
             "home_goals": 2, "away_goals": 2, "status": "final", "clock": "FT", "home_max_lead": 2, "away_max_lead": 0}]
    d = _board([_row(i) for i in range(1, 5)], live=live, now=pd.Timestamp("2030-01-06 03:00", tz="UTC"))["parlays"][0]
    leg = next(x for x in d["legs"] if x["rank"] == 1)
    assert leg["result"] == "win" and leg["provisional"] and leg["pa_paid"]


def test_the_record_counts_only_settled_parlays_by_kind():
    rows = [_row(i, result="win") for i in range(1, 5)] + \
           [_row(i, result="loss" if i == 1 else "win", kind="horario", slot="2030-01-04 19:45",
                 kick="2030-01-04 19:45", match=f"x{i} vs y{i}") for i in range(1, 4)] + \
           [_row(i, kind="dia", seq=2, slot="2030-01-06 19:45", kick="2030-01-06 19:45", match=f"z{i} vs w{i}")
            for i in range(1, 5)]
    r = _board(rows)["record"]
    assert (r["all"]["n"], r["all"]["won"], r["all"]["lost"]) == (2, 1, 1)
    assert r["dia"]["units"] == pytest.approx(4.37, abs=0.01) and r["horario"]["units"] == -1.0


def test_parlay_endpoint_and_tab(monkeypatch):
    import dashboard.app as A
    legs = pd.DataFrame([_row(i) for i in range(1, 5)])

    def fake_q(sql, params=None, optional=False):
        s = " ".join(sql.split())
        if "FROM program_parlays" in s:
            return legs
        if "FROM live_scores" in s or "FROM matches" in s:
            return pd.DataFrame()
        raise AssertionError(s[:90])
    monkeypatch.setattr(A, "_q", fake_q)
    c = A.app.test_client()
    d = c.get("/api/board/parlay").get_json()
    assert d["ok"] and d["parlays"][0]["parlay"]["odds_bonus_us"] == "+437"
    assert d["rules"]["min_odds_us"] == "-500" and d["rules"]["strong"] == 0.65 and d["rules"]["bonus"]["6"] == 0.15
    assert "Champions League" in " ".join(d["rules"]["leagues"]) or d["rules"]["leagues"]
    html = c.get("/").get_data(as_text=True)
    for el_id in ('data-tab="parlay"', 'id="tab-parlay"', 'id="xrule"', 'id="xnext"', 'id="xkpis"', 'id="xhist"'):
        assert el_id in html
    assert 'id="xsize"' not in html                                  # ya no hay botón de 2, 3 o 4
    from pathlib import Path
    js = (Path(A.__file__).parent / "ui_es5.js").read_text(encoding="utf-8")
    assert "/api/board/parlay" in js and "_px.parlays" in js and "odds_bonus_us" in js and "_pxK" not in js


# ─── el aviso de Telegram ──────────────────────────────────────────────────

MX = ZoneInfo("America/Mexico_City")
SLOT = pd.Timestamp("2030-01-05 18:00", tz="UTC")


def _alert(rank, match, lineup="confirmada", odds=1.5, kick=None, kind="horario", seq=1, slot=SLOT,
           league="soccer_italy_serie_a"):
    return {"slot": slot, "rank": rank, "kind": kind, "seq": seq, "match": match, "display": match.title(),
            "match_date": kick or slot, "league": league, "label": f"Gana {match.split(' vs ')[0].title()}",
            "market": "home_win", "model_prob": 0.72, "market_prob": 0.70, "playdoit_odds": odds, "lineup": lineup,
            "pa": True}


def test_the_announcement_shows_the_bonus_the_early_payout_and_american_odds():
    rows = [_alert(1, "napoli vs lecce", kind="dia"), _alert(2, "roma vs como", kind="dia", odds=1.4,
                                                            kick=SLOT + pd.Timedelta(hours=2), lineup=None),
            _alert(3, "inter vs genoa", kind="dia", odds=1.3), _alert(4, "milan vs torino", kind="dia", odds=1.6)]
    t = PA.alert_text(rows, MX)
    assert "Parlay del día" in t and "4 partidos" in t and "antes de las 11:55" in t
    assert "Gana Napoli · -200 · ⚡" in t and "📋" in t and "⏳" in t
    assert "Paga <b>+337</b>" in t and "con el bono de 7.5%: <b>+362</b>" in t
    assert "Se gana más o menos 1 de cada 4 veces (según Pinnacle)" in t and "4.3" not in t
    assert "Salen todas" not in t and "PA =" not in t and "⚡ = pago anticipado" in t
    assert PA.times_text(0.14) == "más o menos 1 de cada 7 veces" and PA.times_text(0.7) == "70 de cada 100 veces"
    assert "Cambió el parlay del día" in PA.alert_text(rows, MX, changed=True)
    assert "Faltan alineaciones" in PA.alert_text(rows, MX, late=True)


# ─── el chequeo diario ─────────────────────────────────────────────────────

def test_the_daily_check_catches_a_parlay_outside_the_rule_and_unbacked_early_payouts():
    from src.utils import output_checks as OC
    good = [{"slot": SLOT, "kind": "dia", "seq": 1, "match": f"m{i} vs r{i}", "market": "home_win",
             "league": "soccer_epl", "market_prob": 0.70, "playdoit_odds": 1.40} for i in range(4)]
    assert OC.bono_parlay_problems(good) == []
    bad = good[:3] + [{**good[3], "market_prob": 0.60}, {**good[0], "slot": SLOT + pd.Timedelta(hours=1),
                                                         "kind": "horario", "playdoit_odds": 1.10}]
    found = OC.bono_parlay_problems(bad)
    assert any("0.6" in x for x in found) and any("1 piernas" in x for x in found) and any("1.1" in x for x in found)
    kick = pd.Timestamp("2030-01-05 19:45", tz="UTC")
    live = [{"home": "napoli", "away": "lecce", "kickoff": kick, "status": "final", "home_max_lead": 1,
             "away_max_lead": 0}]
    paid = [{"table": "program_parlays", "id": 1, "match": "napoli vs lecce", "match_date": kick,
             "market": "home_win", "league": "soccer_italy_serie_a", "result": "win", "pa_paid": True}]
    assert len(OC.early_payout_problems(paid, live)) == 1            # solo fue 1 arriba
    live[0]["home_max_lead"] = 2
    assert OC.early_payout_problems(paid, live) == []
    # la liquidación independiente no lo cuenta como error
    m = [{"h": "napoli", "a": "lecce", "d": kick.date(), "home_goals": 2, "away_goals": 2}]
    assert OC.settlement_mismatches(paid, m) == []
    assert len(OC.settlement_mismatches([{**paid[0], "pa_paid": None}], m)) == 1
