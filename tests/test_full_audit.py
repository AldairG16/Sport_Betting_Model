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
    fresh = {"team_xg_real": 3, "club_elo": 1, "espn_matches": 0}
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
