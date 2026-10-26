"""
Picks del programa guardados y medidos + dashboard v1.9.0 (21-oct-26).

El dueño: "cómo nos fue con las apuestas de nuestros modelos, no los veo en
el dashboard y ahorita el dashboard es un desastre". El pick de cada partido
no se guardaba en ningún lado; ahora se guarda (morning, evening y cada
closing), se liquida en la noche y la página tiene pestañas en lenguaje
simple. Sin DB.
"""

from types import SimpleNamespace

import pandas as pd
import pytest

from dashboard import visibility as V
from src.models import program_picks as PP
from src.utils.log import RUN_ISSUES
from tests.fake_db import FakeEngine

KICK = pd.Timestamp("2030-01-05 19:00", tz="UTC")


def _mvs(match="atlas vs club america", model=(0.30, 0.22, 0.48), pin=(0.27, 0.24, 0.49),
         over=0.67, btts=0.55, created="2030-01-05 06:00"):
    return {"match": match, "match_date": KICK, "league": "soccer_mexico_ligamx",
            "p_home": model[0], "p_draw": model[1], "p_away": model[2],
            "pin_home": pin[0], "pin_draw": pin[1], "pin_away": pin[2],
            "lam_home": 1.6, "lam_away": 2.0, "p_over25": over, "p_btts": btts,
            "created_at": pd.Timestamp(created, tz="UTC")}


UP = {"home_team_norm": "atlas", "away_team_norm": "club america", "home_team": "Atlas", "away_team": "América",
      "home_odds": 4.05, "draw_odds": 3.90, "away_odds": 2.22, "pin_home_odds": None, "pin_draw_odds": None,
      "pin_away_odds": None, "over25_odds": 1.80, "under25_odds": 2.05, "consensus_over25_odds": 1.72,
      "consensus_under25_odds": 2.00, "btts_yes_odds": 1.70, "btts_no_odds": 2.10,
      "consensus_home_odds": 3.80, "consensus_draw_odds": 3.70, "consensus_away_odds": 2.10}


# ─── la regla (22-oct-26): una apuesta real entre todos los mercados ─────────

PIN_EVEN = {"pin_home_odds": 2.25, "pin_draw_odds": 3.70, "pin_away_odds": 3.40}   # ≈ 43 / 26 / 28
MODEL_EVEN = [0.44, 0.27, 0.29]                                                  # sin ventaja en el 1X2


def _u(**kw):
    return SimpleNamespace(**{**UP, **PIN_EVEN, **kw})


def test_the_pick_can_be_a_goals_market_not_only_the_winner():
    cs = PP.candidates(MODEL_EVEN, (1.6, 1.4), 0.60, None, _u())
    pk, why = PP.choose(cs)
    # más de 2.5: el programa 60%, el promedio de las casas ~54% (6 pts)
    assert (pk["market"], pk["family"]) == ("over25", "Goles") and why is None
    assert pk["playdoit_odds"] == pytest.approx(1.72)


def test_a_huge_gap_with_the_market_is_still_the_pick_but_outside_the_rule():
    # v2.2 (22-oct-26): "quiero que todos tengan picks"; la regla marca, no esconde
    pk, why = PP.choose(PP.candidates(MODEL_EVEN, (1.6, 1.4), 0.75, None, _u()))
    assert pk["market"] == "over25" and why == PP.TOO_FAR        # 21 pts: probablemente se equivoca


def test_only_what_the_program_believes_will_happen():
    # el visitante: 5 pts más que Pinnacle, pero el favorito del programa es el local
    cs = PP.candidates([0.40, 0.27, 0.33], (1.5, 1.4), None, None, _u())
    pk, why = PP.choose(cs)
    assert pk["market"] == "home_win" and why == PP.NO_EDGE       # su favorito, sin ventaja
    # más de 2.5 con 46%: 5 pts sobre el mercado (~41%), pero no lo cree
    u = _u(consensus_over25_odds=2.30, consensus_under25_odds=1.62)
    pk, why = PP.choose(PP.candidates(MODEL_EVEN, (1.6, 1.4), 0.46, None, u))
    assert pk["market"] == "home_win" and why == PP.NO_EDGE


def test_a_market_that_moved_against_after_the_analysis_cancels_the_pick():
    cs = PP.candidates(MODEL_EVEN, (1.6, 1.4), 0.60, None, _u())
    pk, why = PP.choose(cs, then={"over25": 0.59})               # estaba en 59% y bajó a ~54%
    assert pk["market"] == "over25" and why == PP.MOVED


def test_matches_between_leagues_wait_for_the_league_strength():
    pk, why = PP.choose(PP.candidates(MODEL_EVEN, (1.6, 1.4), 0.60, None, _u()), league="soccer_uefa_champs_league")
    assert pk["market"] == "over25" and why == PP.CROSS


def test_the_pick_that_passes_the_rule_wins_over_a_bigger_gap_that_does_not():
    u = _u(btts_yes_odds=2.60, btts_no_odds=1.50)                # ambos anotan: mercado ~37%
    cs = PP.candidates(MODEL_EVEN, (1.6, 1.4), 0.60, 0.55, u)     # 18 pts en ambos anotan, 6 en más de 2.5
    pk, why = PP.choose(cs)
    assert pk["market"] == "over25" and why is None


def test_a_match_without_saved_odds_still_has_its_winner_pick():
    # antes del 12-oct odds_history no tiene Pinnacle ni promedio: el 1X2
    # contra el Pinnacle de model_vs_sharp, sin precio (cuenta acierto, no unidades)
    pk, why = PP.decide(_mvs(), None)
    assert (pk["market"], pk["playdoit_odds"], pk["market_prob"]) == ("away_win", None, 0.49)
    assert why == PP.NO_EDGE
    row = PP._row(_mvs(), KICK, None, pk, why)
    assert row["market"] == "away_win" and row["reason"] == PP.NO_EDGE


def test_corners_and_cards_enter_when_they_have_a_price():
    u = _u(corners_line=9.5, corners_over_odds=1.85, corners_under_odds=1.95)
    cs = PP.candidates(MODEL_EVEN, (1.6, 1.4), None, None, u, props={"corners": lambda line: 0.57})
    pk, _ = PP.choose(cs)
    assert (pk["market"], pk["family"]) == ("corners_over_9.5", "Córners")
    assert pk["playdoit_odds"] == pytest.approx(1.85 * PP.TWO_WAY_AVG_RATIO)


def test_other_goal_lines_follow_the_programs_own_numbers():
    u = _u(pin_total_line=3.5, pin_total_over_odds=2.40, pin_total_under_odds=1.62)
    cs = {c["market"]: c for c in PP.candidates(MODEL_EVEN, (1.7, 1.5), 0.62, None, u)}
    assert cs["over_3.5"]["model"] < cs["over25"]["model"]          # coherente con su más de 2.5
    assert cs["over_3.5"]["model"] + cs["under_3.5"]["model"] == pytest.approx(1.0)


def test_the_dashboard_shows_the_saved_pick_or_why_there_is_none():
    shown = V.saved_pick({"market": "btts", "label": "Ambos anotan", "family": "Ambos anotan",
                          "model_prob": 0.62, "market_prob": 0.56, "playdoit_odds": 1.70})
    assert (shown["label"], shown["family"], shown["playdoit_us"]) == ("Ambos anotan", "Ambos anotan", "-143")
    assert shown["rule"] is True and shown["reason"] == ""
    out = V.saved_pick({"market": "over25", "label": "Más de 2.5 goles", "reason": PP.TOO_FAR, "playdoit_odds": None})
    assert out["label"] == "Más de 2.5 goles" and out["rule"] is False and out["reason"] == PP.TOO_FAR
    assert V.saved_pick({"market": None, "reason": PP.NO_PRICE}) == {"label": "", "reason": PP.NO_PRICE}


# ─── guardar ─────────────────────────────────────────────────────────────────

def test_build_picks_uses_the_latest_analysis_and_canonical_names():
    mvs = pd.DataFrame([_mvs(model=(0.60, 0.20, 0.20), created="2030-01-04 06:00"),   # corrida vieja
                        _mvs(model=tuple(MODEL_EVEN), over=0.60, btts=None, created="2030-01-05 06:00")])
    up = pd.DataFrame([{**UP, **PIN_EVEN, "away_team_norm": "america"}])               # alias de hoy
    rows = PP.build_picks(mvs, up, normalize=lambda t: {"america": "club america"}.get(t, t))
    (r,) = rows
    assert r["match"] == "atlas vs club america" and r["display"] == "Atlas vs América"
    assert (r["market"], r["family"], r["model_prob"]) == ("over25", "Goles", 0.6)
    # fuera de la regla también es pick, con el motivo: todos los partidos tienen uno
    (r2,) = PP.build_picks(pd.DataFrame([_mvs(model=tuple(MODEL_EVEN), over=0.80, btts=None)]),
                           pd.DataFrame([{**UP, **PIN_EVEN}]))
    assert r2["market"] == "over25" and r2["reason"] == PP.TOO_FAR


def _reads(extra=None):
    props_cols = ["date", "league", "home", "away", "home_corners", "away_corners", "home_cards", "away_cards"]
    return {"model_vs_sharp": pd.DataFrame([_mvs(model=tuple(MODEL_EVEN), over=0.60, btts=None)]),
            "upcoming_matches": pd.DataFrame([{**UP, **PIN_EVEN}]), "playdoit_prices": pd.DataFrame(),
            "odds_history": pd.DataFrame(columns=["match", "kickoff", "captured_at"]),
            "FROM matches": pd.DataFrame(columns=props_cols), **(extra or {})}


def test_snapshot_upserts_only_matches_that_have_not_started(monkeypatch):
    eng = FakeEngine()
    reads = _reads()
    monkeypatch.setattr(PP.pd, "read_sql", lambda sql, con=None, params=None, **kw: next(
        df for k, df in reads.items() if k in str(sql)))
    assert PP.snapshot_picks(engine=eng) == 1
    (sql, params), = eng.statements("INSERT INTO program_picks")
    assert "ON CONFLICT (match, match_date) DO UPDATE" in sql
    assert "WHERE program_picks.result IS NULL AND program_picks.match_date > NOW()" in sql
    assert params["market0"] == "over25" and params["family0"] == "Goles" and params["display0"] == "Atlas vs América"


def test_a_failed_snapshot_is_an_error_that_reaches_telegram(monkeypatch):
    RUN_ISSUES.reset()

    def boom(*a, **k):
        raise RuntimeError("neon caído")
    monkeypatch.setattr(PP.pd, "read_sql", boom)
    assert PP.snapshot_picks(engine=FakeEngine()) == 0
    assert any("Picks del programa no guardados" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


# ─── liquidar ────────────────────────────────────────────────────────────────

def test_picks_settle_at_the_playdoit_price_and_without_it_only_count(monkeypatch):
    import src.models.save_bets as sb
    eng = FakeEngine()
    pending = pd.DataFrame([
        {"id": 1, "match": "atlas vs club america", "match_date": KICK, "market": "over25", "playdoit_odds": 1.72},
        {"id": 2, "match": "atlas vs club america", "match_date": KICK, "market": "btts", "playdoit_odds": None},
    ])
    matches = pd.DataFrame([{"home_team_l": "atlas", "away_team_l": "club america", "date": KICK.tz_convert(None),
                             "home_goals": 1, "away_goals": 2, "home_corners": None, "away_corners": None,
                             "home_yellow": None, "away_yellow": None, "home_goals_ht": None, "away_goals_ht": None,
                             "home_goals_h2": None, "away_goals_h2": None, "home_shots_target": None,
                             "away_shots_target": None}])
    monkeypatch.setattr(PP.pd, "read_sql", lambda *a, **k: pending)
    monkeypatch.setattr(sb, "_preload_matches", lambda dates: matches)
    monkeypatch.setattr(sb, "normalize_team", lambda t: t)
    out = PP.resolve_picks(engine=eng)
    assert out["resolved"] == 2
    (sql, plan), = eng.statements("UPDATE program_picks SET result = :result")
    by_id = {p["id"]: p for p in plan}
    assert by_id[1]["result"] == "win" and by_id[1]["profit"] == pytest.approx(0.72)
    assert by_id[2]["result"] == "win" and by_id[2]["profit"] is None       # sin momio de PlayDoit


# ─── el orquestador ─────────────────────────────────────────────────────────

def _steps(monkeypatch, fn):
    import scripts.odds_history as oh
    from scripts import orchestrator as O
    monkeypatch.setattr(oh, "capture_snapshot", lambda verbose=False: None)   # el morning la toma fuera de los pasos
    ran = []
    monkeypatch.setattr(O, "run_step", lambda logger, name, f, *a, **k: ran.append((name, f)) or True)
    getattr(O, fn)(type("L", (), {"log": lambda self, msg: None})())
    return [n for n, _ in ran], dict(ran), O


@pytest.mark.parametrize("fn,after", [("run_morning", "Predictions"), ("run_evening", "Predictions (mañana)"),
                                      # el closing lee antes los marcadores del día (v2.4.4)
                                      ("run_closing", "Marcadores del día (ESPN)")])
def test_picks_are_saved_after_every_analysis_and_every_closing(monkeypatch, fn, after):
    names, steps, O = _steps(monkeypatch, fn)
    assert names.index("Picks del programa") == names.index(after) + 1
    assert steps["Picks del programa"] is O.step_program_picks


def test_picks_are_settled_with_the_shadow_bets(monkeypatch):
    import src.models.proposals as P
    import src.models.save_bets as sb
    from scripts import orchestrator as O
    called = []
    monkeypatch.setattr(sb, "resolve_shadow_outcomes", lambda: called.append("shadow"))
    monkeypatch.setattr(P, "resolve_proposals", lambda: called.append("proposals"))
    monkeypatch.setattr(PP, "resolve_picks", lambda: called.append("picks"))
    O.step_shadow_results()
    assert called == ["shadow", "proposals", "picks"]


# ─── el tablero ──────────────────────────────────────────────────────────────

def _pick_row(result, profit, lean=True, value=False, odds=1.72, display="Atlas vs América"):
    return {"match": "atlas vs club america", "match_date": KICK, "league": "soccer_mexico_ligamx",
            "display": display, "label": "Más de 2.5 goles", "model_prob": 0.67, "market_prob": 0.53,
            "lean": lean, "min_us": -132, "playdoit_odds": odds, "value": value, "result": result, "profit": profit}


def test_picks_board_counts_wins_and_units_by_group():
    df = pd.DataFrame([_pick_row("win", 0.72), _pick_row("loss", -1.0, lean=False),
                       _pick_row("win", None, odds=None), _pick_row(None, None, value=True)])
    b = V.picks_board(df)
    assert b["all"] == {"n": 3, "won": 2, "lost": 1, "profit": -0.28, "priced": 2, "roi": -0.14}
    assert b["lean"]["n"] == 2 and b["lean"]["profit"] == 0.72
    assert b["value"] is None                                    # el que pagaba aún no se juega
    assert b["rows"][0]["playdoit_us"] == "-139" and b["rows"][3]["result"] == "Pendiente"


def test_real_money_by_stage():
    df = pd.DataFrame([
        {"match_date": "2026-09-01T18:00:00Z", "result": "win", "stake": 1.0, "profit": 0.8},
        {"match_date": "2026-09-01T18:00:00Z", "result": "stale", "stake": 1.0, "profit": 0.0},
        {"match_date": "2026-09-20T18:00:00Z", "result": "loss", "stake": 0.5, "profit": -0.5},
        {"match_date": "2026-10-02T18:00:00Z", "result": "stale", "stake": 1.0, "profit": 0.0},
    ])
    first, second, collection = V.real_periods(df)
    assert (first["n"], first["profit"], first["cancelled"]) == (1, 0.8, 1)
    assert (second["n"], second["lost"], second["staked"]) == (1, 1, 0.5)
    assert (collection["n"], collection["cancelled"]) == (0, 1)


def test_proposal_weeks_split_the_broken_model_from_the_fixed_one():
    df = pd.DataFrame([
        {"match_date": "2026-10-13T18:00:00Z", "market": "home_win", "odds": 2.0, "result": "win"},
        {"match_date": "2026-10-14T18:00:00Z", "market": "under_3.5", "odds": 1.65, "result": "win"},   # momio inventado
        {"match_date": "2026-10-20T18:00:00Z", "market": "draw", "odds": 3.4, "result": "loss"},
        {"match_date": "2026-10-23T18:00:00Z", "market": "over25", "odds": 1.9, "result": "win"},
    ])
    weeks = V.proposal_weeks(df)
    assert [(w["week"], w["old_model"], w["n"], w["profit_1u"]) for w in weeks] == [
        ("2026-10-12", True, 1, 1.0), ("2026-10-19", False, 1, 0.9), ("2026-10-19", True, 1, -1.0)]


def test_dashboard_endpoints_and_tabs(monkeypatch):
    import dashboard.app as dash

    def fake_q(sql, params=None, optional=False):
        s = " ".join(str(sql).split())
        if "FROM program_picks" in s:
            return pd.DataFrame([_pick_row("win", 0.72)])
        if "FROM bets_history" in s:
            return pd.DataFrame([{"match_date": "2026-09-01T18:00:00Z", "result": "win", "stake": 1.0, "profit": 0.8}])
        if "FROM model_proposals" in s:
            return pd.DataFrame([{"match_date": "2026-10-13T18:00:00Z", "market": "home_win", "odds": 2.0,
                                  "result": "win"}])
        raise AssertionError(s[:80])
    monkeypatch.setattr(dash, "_q", fake_q)
    c = dash.app.test_client()
    p = c.get("/api/picks").get_json()
    assert p["ok"] and p["all"]["won"] == 1 and p["rows"][0]["match"] == "Atlas vs América"
    sb = c.get("/api/scoreboard").get_json()
    assert sb["ok"] and sb["real"][0]["profit"] == 0.8 and sb["weeks"][0]["old_model"] is True
    assert sb["last_real_bet"].startswith("2026-09-01")
    html = c.get("/").get_data(as_text=True)
    for tab in ("proximos", "anteriores", "equipos", "graficas"):                     # v2.0
        assert f'data-tab="{tab}"' in html and f'id="tab-{tab}"' in html
    for el in ("uplist", "daybar", "pkpis", "pastbody", "teamlist", "cbank", "cacc", "cpicks", "cprops"):
        assert f'id="{el}"' in html


def test_without_saved_picks_the_page_explains_when_they_arrive(monkeypatch):
    import dashboard.app as dash
    monkeypatch.setattr(dash, "_q", lambda sql, params=None: pd.DataFrame())
    p = dash.app.test_client().get("/api/picks").get_json()
    assert p["ok"] is False and "después de que termina su partido" in p["msg"]


def test_daily_check_skips_the_picks_table_until_it_exists(monkeypatch):
    import src.utils.output_checks as oc
    now = pd.Timestamp.now(tz="UTC")

    def fake_read_sql(sql, con=None, params=None, **kw):
        s = " ".join(str(sql).split())
        if "FROM program_picks" in s:
            raise RuntimeError('relation "program_picks" does not exist')
        if s.startswith("SELECT COUNT(*) FROM matches"):          # datos de entrada (23-oct-26): sanos
            return pd.DataFrame([[0]])
        if s.startswith("SELECT MAX(") and " AS t FROM " in s:
            return pd.DataFrame([{"t": now}])
        if "AS bets" in s:
            return pd.DataFrame([{"bets": 0, "shadow": 0, "props": 0}])
        if "model_state" in s:
            return pd.DataFrame([{"key": k, "updated_at": now} for k in oc.WEEKLY_KEYS])
        if "team_xg_real" in s:
            return pd.DataFrame([{"m": 2.0}])
        return pd.DataFrame(columns=["id", "match", "match_date", "market", "result", "h", "a", "d"])
    monkeypatch.setattr(pd, "read_sql", fake_read_sql)
    assert oc.run_output_checks(engine=object(), normalize=lambda t: t) == []


# ─── picks reconstruidos (22-oct-26) ─────────────────────────────────────────

def test_past_picks_use_only_the_odds_before_kickoff_and_at_the_analysis():
    mvs = pd.DataFrame([_mvs(model=tuple(MODEL_EVEN), over=0.60, btts=None, created="2030-01-05 06:00")])
    snap = {**UP, **PIN_EVEN, "match": "Atlas vs América", "kickoff": KICK}
    snaps = pd.DataFrame([
        {**snap, "captured_at": KICK - pd.Timedelta(hours=14)},                                 # al analizar
        {**snap, "captured_at": KICK - pd.Timedelta(hours=1), "consensus_over25_odds": 1.75},  # la última antes
        {**snap, "captured_at": KICK + pd.Timedelta(minutes=30), "consensus_over25_odds": 9.0},  # en vivo: no
    ])
    norm = lambda t: {"américa": "club america"}.get(t.lower(), t.lower())
    (r,) = PP.build_past_picks(mvs, snaps, normalize=norm)
    assert r["source"] == "reconstruido" and r["display"] == "Atlas vs América"
    assert r["market"] == "over25" and r["playdoit_odds"] == pytest.approx(1.75)
    # el más de 2.5 bajó de ~59% (al analizar) a ~53% antes del partido: se movió en contra
    moved = snaps.assign(consensus_over25_odds=[1.62, 1.80, 9.0], consensus_under25_odds=[2.30, 2.00, 9.0])
    (r2,) = PP.build_past_picks(mvs, moved, normalize=norm)
    assert r2["market"] == "over25" and r2["reason"] == PP.MOVED


def test_backfill_redoes_only_matches_already_played(monkeypatch):
    eng = FakeEngine()
    reads = {"model_vs_sharp": pd.DataFrame([_mvs()]),
             "odds_history": pd.DataFrame(columns=["match", "kickoff", "captured_at"])}
    monkeypatch.setattr(PP.pd, "read_sql", lambda sql, con=None, params=None, **k: next(
        df for key, df in reads.items() if key in str(sql)))
    monkeypatch.setattr(PP, "resolve_picks", lambda engine=None: {})
    assert PP.backfill_picks(engine=eng) == 1
    (sql, params), = eng.statements("INSERT INTO program_picks")
    assert "WHERE program_picks.match_date < NOW() - INTERVAL '3 hours'" in sql and params["source0"] == "reconstruido"
    # solo llena los que no tienen pick: los que ya lo tienen no cambian con la regla de hoy
    assert sql.endswith("AND program_picks.market IS NULL")
    eng = FakeEngine()
    PP.backfill_picks(engine=eng, overwrite=True)
    (sql, _), = eng.statements("INSERT INTO program_picks")
    assert "market IS NULL" not in sql
