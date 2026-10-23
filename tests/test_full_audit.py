"""
Auditoría completa (23-oct-26). El dueño: "quiero el sistema 100% funcional…
que al correr auditorías no estemos encontrando errores nuevos que
auditorías pasadas no encontraron". Cada clase de error, con su prueba y su
chequeo diario:

  - fechas con el día y el mes cruzados: dateutil con dayfirst lee
    "2025-02-06" como 2-jun; el cargador de FBref dejó 2,271 partidos
    argentinos repetidos con la fecha espejo;
  - "Enrich data" escribía columnas que nadie lee y chocaba (deadlock) con
    el closing: 20 partidos por mañana sin enriquecer, el paso en "OK";
  - errores que solo se imprimían (bankroll, presupuesto de Anthropic,
    descargas) y nunca llegaban a Telegram;
  - una feature que deja de aportar en todos los partidos no fallaba.
Sin DB.
"""

import pandas as pd
import pytest


# ─── fechas ──────────────────────────────────────────────────────────────────

def test_dates_are_read_with_an_explicit_format():
    from src.utils.dates import parse_match_date, parse_match_dates
    d = parse_match_dates(["2025-02-06", "06/02/2025", "06/02/25", "2025-02-06 19:30:00", "basura", None])
    assert list(d[:4]) == [pd.Timestamp("2025-02-06")] * 4          # dayfirst a secas: 2-jun en el ISO
    assert d[4] is pd.NaT and pd.isna(d[5])
    assert parse_match_date("20/10/2026") == pd.Timestamp("2026-10-20")
    assert parse_match_date("x") is None


def test_the_extra_leagues_loader_keeps_iso_dates():
    from scripts.load_extra_leagues import _normalize_date
    assert _normalize_date("06/02/2025") == "2025-02-06"
    assert _normalize_date("2025-02-06") == "2025-02-06"             # antes: 2025-06-02


def test_the_argentina_cleanup_deletes_only_the_mirror_copy():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fx", "scripts/fix_argentina_swapped_dates.py")
    fx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fx)
    source = pd.DataFrame([
        {"Date": "06/02/2025", "Home": "Aldosivi", "Away": "Barracas Central", "HG": 1, "AG": 3},
        {"Date": "08/05/2022", "Home": "Talleres Cordoba", "Away": "Sarmiento Junin", "HG": 1, "AG": 0},
        {"Date": "05/08/2022", "Home": "Talleres Cordoba", "Away": "Sarmiento Junin", "HG": 1, "AG": 0},
        {"Date": "03/09/2025", "Home": "Aldosivi", "Away": "Tigre", "HG": 0, "AG": 2}])
    rows = pd.DataFrame([
        # la copia mala (2-jun) del 6-feb, con su gemelo bueno ya cargado
        {"id": 1, "date": "2025-06-02", "h": "aldosivi", "a": "barracas central", "home_goals": 1, "away_goals": 3,
         "twin": 9, "occupied": True},
        # los dos partidos son reales: se deja
        {"id": 2, "date": "2022-08-05", "h": "talleres cordoba", "a": "sarmiento junin", "home_goals": 1,
         "away_goals": 0, "twin": 8, "occupied": True},
        # mala y sin gemelo: se corrige la fecha (9-mar → 3-sep)
        {"id": 3, "date": "2025-03-09", "h": "aldosivi", "a": "tigre", "home_goals": 0, "away_goals": 2,
         "twin": None, "occupied": False},
        # no está en el archivo: se deja
        {"id": 4, "date": "2024-01-02", "h": "x", "a": "y", "home_goals": 0, "away_goals": 0,
         "twin": None, "occupied": False}])
    delete, fix, keep = fx.decide(rows, fx.source_games(source))
    assert delete == [1]
    assert fix == [(3, pd.Timestamp("2025-03-09").date(), pd.Timestamp("2025-09-03").date())]
    assert [k[0] for k in keep] == [2, 4]


# ─── el paso que chocaba con el closing ──────────────────────────────────────

@pytest.mark.parametrize("fn", ["run_morning", "run_evening"])
def test_no_run_enriches_upcoming_matches_anymore(monkeypatch, fn):
    from tests.test_program_picks import _steps
    names, _, O = _steps(monkeypatch, fn)
    assert "Enrich data" not in names and not hasattr(O, "step_enrich")


# ─── errores que solo se imprimían ───────────────────────────────────────────

def test_a_bankroll_update_that_fails_is_an_error_not_a_fake_balance(monkeypatch):
    import src.models.bankroll_manager as BM
    from src.utils.log import RUN_ISSUES
    RUN_ISSUES.reset()

    def boom():
        raise RuntimeError("neon caído")
    monkeypatch.setattr(BM, "ensure_bankroll_schema", boom)
    with pytest.raises(RuntimeError):
        BM.update_bankroll(profit=1.0, notes="prueba")                  # antes: devolvía 100
    assert any("Bankroll no actualizado" in e for e in RUN_ISSUES.errors())
    RUN_ISSUES.reset()


# ─── lo que entra al modelo ──────────────────────────────────────────────────

def test_a_feature_that_stops_arriving_is_an_error():
    from src.utils.output_checks import feature_coverage
    ok = {"evaluated": 136, "h2h_used": 134, "xg_used": 96, "corners_used": 90, "shots_used": 90,
          "big5": 43, "xg_real_big5": 43}
    assert feature_coverage(ok) == []
    broken = {**ok, "corners_used": 0, "xg_real_big5": 30}
    msgs = feature_coverage(broken)
    assert any("córners en 0 de 136" in m for m in msgs) and any("30 de 43" in m for m in msgs)
    assert feature_coverage({"evaluated": 5}) == []                    # corrida chica: sin juicio


def test_data_health_flags_each_known_class_of_bad_data():
    from src.utils.output_checks import data_health
    fresh = {"team_xg_real": 3, "club_elo": 1, "espn_matches": 0, "upcoming_matches": 0, "odds_history": 0,
             "kalshi_history": 1, "model_vs_sharp": 0, "program_picks": 0}
    assert data_health({"mirror": 0, "future": 0, "duplicate": 0}, fresh) == []
    assert data_health({"mirror": 3, "future": 0, "duplicate": 0}, fresh) == []   # coincidencias reales
    msgs = data_health({"mirror": 40, "future": 2, "duplicate": 5}, {**fresh, "club_elo": 15, "espn_matches": None})
    assert len(msgs) == 5
    assert any("día y el mes cruzados" in m for m in msgs) and any("club_elo sin actualizar" in m for m in msgs)


# ─── nombres de otras fuentes (parte 2) ──────────────────────────────────────

@pytest.mark.parametrize("raw,canon", [
    ("Houston Dynamo FC", "houston dynamo"), ("Chicago Fire FC", "chicago fire"),
    ("Red Bull New York", "ny red bulls"), ("New York Red Bulls", "ny red bulls"), ("NY Red Bulls", "ny red bulls"),
    ("Talleres (Córdoba)", "talleres cordoba"), ("Estudiantes de La Plata", "estudiantes"),
    ("Central Córdoba (Santiago del Estero)", "central cordoba"), ("Atlético de San Luis", "atl san luis"),
    ("AFC Bournemouth", "bournemouth"), ("Brighton & Hove Albion", "brighton"), ("Olympiacos", "olympiakos piraeus"),
    ("1. FC Heidenheim 1846", "heidenheim"), ("Borussia M.Gladbach", "gladbach"), ("RasenBallsport Leipzig", "leipzig"),
    ("Racing Santander", "real racing club de santander")])
def test_names_from_espn_and_understat_reach_our_teams(raw, canon):
    from src.utils.team_normalizer import normalize_team
    assert normalize_team(raw) == canon


def test_off_canon_lists_only_names_that_are_no_team_of_ours():
    from scripts.learn_team_aliases import off_canon
    canon = {"stuttgart", "bayern munchen", "ny red bulls"}
    assert off_canon(["VfB Stuttgart", "Bayern Munich", "Red Bull New York", "Colo Colo"], canon) == [("Colo Colo", "colo colo")]


def test_espn_slugs_point_to_the_right_league():
    from scripts.backfill_espn_results import ESPN_LEAGUES
    assert ESPN_LEAGUES["soccer_china_superleague"] == "chn.1"             # chi.1 es Chile
    assert "soccer_korea_kleague1" not in ESPN_LEAGUES                       # ESPN no la tiene


def test_the_all_star_game_is_not_a_lineup_of_ours():
    from src.features.espn_lineups import parse_scoreboard
    ev = lambda h, a: {"id": "1", "date": "2026-07-30T00:00Z", "status": {"type": {"completed": True}},
                       "competitions": [{"competitors": [{"homeAway": "home", "team": {"displayName": h}},
                                                         {"homeAway": "away", "team": {"displayName": a}}]}]}
    assert parse_scoreboard({"events": [ev("MLS All-Stars", "Liga MX All Stars"), ev("LA Galaxy", "Austin FC")]}) == [
        {"event": "1", "kick": pd.Timestamp("2026-07-30T00:00Z").to_pydatetime(), "home": "LA Galaxy",
         "away": "Austin FC", "completed": True}]


def test_a_scorer_who_changed_club_counts_for_the_last_one(monkeypatch):
    import src.models.scorer_model as SM
    rows = pd.DataFrame([{"player": "Cole Palmer", "team": "Chelsea,Manchester City", "goals": 6, "matches": 8}])
    monkeypatch.setattr(SM.pd, "read_sql", lambda *a, **k: rows.copy())
    out = {}
    SM._merge_club_scorers(out, {"manchester city": 8})
    assert out["cole palmer"]["team"] == "manchester city"


def test_exchange_prices_count_net_of_their_commission():
    # The Odds API da el momio de respaldo de Betfair sin la comisión (23-oct-26)
    from scripts.update_upcoming_matches import net_of_commission
    bf = {"key": "betfair_ex_eu", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 2.10}]}]}
    out = net_of_commission(bf)
    assert out["markets"][0]["outcomes"][0]["price"] == pytest.approx(1 + 1.10 * 0.95)
    assert bf["markets"][0]["outcomes"][0]["price"] == 2.10                 # el JSON original no se toca
    book = {"key": "unibet_eu", "markets": [{"key": "h2h", "outcomes": [{"name": "A", "price": 2.10}]}]}
    assert net_of_commission(book) is book


# ─── la auditoría del sistema, siempre la misma ──────────────────────────────

def test_the_audit_flags_a_missing_old_or_thin_pipeline_coverage():
    from scripts.system_audit import coverage_problems
    now = pd.Timestamp("2026-10-23 12:00", tz="UTC")
    ok = {"evaluated": 136, "h2h_used": 134, "xg_used": 96, "corners_used": 90, "shots_used": 90, "big5": 43,
          "xg_real_big5": 43, "at": "2026-10-23T06:00:00+00:00"}
    assert coverage_problems(ok, now) == []
    assert coverage_problems(None, now) == ["Cobertura: ninguna corrida del pipeline la guardó todavía"]
    assert any("hace 54 h" in p for p in coverage_problems({**ok, "at": "2026-10-21T06:00:00+00:00"}, now))
    assert any("córners en 0" in p for p in coverage_problems({**ok, "corners_used": 0}, now))


def test_the_audit_checks_each_espn_slug_is_the_right_league():
    from scripts.system_audit import espn_problems
    leagues = {"soccer_china_superleague": "chi.1", "soccer_epl": "eng.1", "soccer_japan_j_league": "jpn.1"}
    names = {"soccer_china_superleague": "Chilean Primera División", "soccer_epl": "English Premier League",
             "soccer_japan_j_league": None}
    out = espn_problems(names, leagues)
    assert out == ["ESPN: soccer_china_superleague → chi.1 es 'Chilean Primera División', no Chinese",
                   "ESPN: soccer_japan_j_league → jpn.1 no responde"]


def test_the_audit_runs_every_check_and_reports_each_problem(monkeypatch):
    import scripts.system_audit as SA
    import scripts.learn_team_aliases as LTA
    import src.utils.model_state as MS
    import src.features.elo_rating as ER
    import src.utils.output_checks as OC
    from src.utils.log import RUN_ISSUES
    RUN_ISSUES.reset()
    monkeypatch.setattr(OC, "run_output_checks", lambda: ["Datos: 40 partidos repetidos"])
    monkeypatch.setattr(LTA, "check_names_without_history", lambda: [])
    monkeypatch.setattr(LTA, "check_source_names", lambda: [("lineups (ESPN)", [("x", "x")])])
    monkeypatch.setattr(MS, "load_state", lambda key: None)
    monkeypatch.setattr(ER, "compute_elo", lambda: {"a": 1500.0, "b": float("nan")})
    monkeypatch.setattr(SA, "_espn_names", lambda leagues, get=None: {k: SA.ESPN_EXPECTED.get(k, "x") for k in leagues})
    import src.models.side_bias as SB
    monkeypatch.setattr(SB, "read_pairs", lambda: pd.DataFrame())
    monkeypatch.setattr(SB, "load_side_bias", lambda: {"updated_at": pd.Timestamp.now(tz="UTC").isoformat()})
    out = SA.run_system_audit()
    assert out["Sesgo del modelo"] == []
    assert out["Equipos próximos con historia"] == [] and out["Ligas de ESPN"] == []
    assert out["Salidas y datos de entrada"] == ["Datos: 40 partidos repetidos"]
    assert out["Nombres de Understat y ESPN"] == ["lineups (ESPN): 1"]
    assert out["Elo"] and out["Lo que entra al modelo"]
    assert sum(1 for e in RUN_ISSUES.errors() if "Auditoría del sistema" in e) == 4
    RUN_ISSUES.reset()


def test_the_weekly_runs_the_system_audit_after_the_dc_fit(monkeypatch):
    from scripts import orchestrator as O
    ran = []
    monkeypatch.setattr(O, "run_step", lambda logger, name, fn, *a, **k: ran.append((name, fn)) or True)
    O.run_weekly(type("L", (), {"log": lambda self, msg: None})())
    names = [n for n, _ in ran]
    assert names.index("Auditoría del sistema") == names.index("Fit DC-MLE parameters") + 1
    assert dict(ran)["Auditoría del sistema"] is O.step_system_audit


# ─── apuestas reales liquidadas al precio de PlayDoit ────────────────────────

def test_a_real_bet_settles_at_the_playdoit_minimum_not_the_best_european_price():
    from tests.test_market_resolution import MATCHES, _bet, _matches_like_production
    from src.models.save_bets import plan_bet_settlements, playdoit_settle_odds
    from src.utils.min_odds import min_odds
    # el mensaje: "PlayDoit: apuesta si paga X o mejor" (X = min_odds con Pinnacle)
    bet = {"odds": 2.10, "probability": 0.55, "pin_prob": 0.52}
    floor = min_odds(0.55, pin_prob=0.52)
    assert playdoit_settle_odds(bet) == pytest.approx(min(floor, 2.10))
    assert playdoit_settle_odds({**bet, "odds": 1.50}) == pytest.approx(1.50)    # PlayDoit no paga más que Europa
    assert playdoit_settle_odds({"odds": 2.10, "probability": None}) is None
    bets = pd.DataFrame([{**_bet(1, "alpha vs beta", "home_win", "pending", 20, odds=2.10), "odds_playdoit_min": 1.95},
                         {**_bet(2, "alpha vs beta", "draw", "pending", 20, odds=3.40), "odds_playdoit_min": None}])
    plan = {p["id"]: p for p in plan_bet_settlements(bets, _matches_like_production(MATCHES))}
    assert plan[1]["result"] == "win" and plan[1]["profit"] == pytest.approx(0.95)      # no 1.10
    assert plan[2]["result"] == "loss"                                                   # sin dato: como antes


def test_the_settle_odds_reads_pinnacle_from_the_decision_log_like_telegram():
    import json
    from src.models.save_bets import playdoit_settle_odds
    from src.utils.min_odds import min_odds
    bet = {"odds": 2.10, "probability": 0.55, "decision_log": json.dumps({"market_ctx": {"pin_prob": 0.50}})}
    assert playdoit_settle_odds(bet) == pytest.approx(min(min_odds(0.55, pin_prob=0.50), 2.10))
