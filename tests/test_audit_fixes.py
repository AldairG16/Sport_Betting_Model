"""
Auditoría de funciones (20-oct-26): lo que estaba roto o apagado sin aviso.

- Créditos de The Odds API: el closing gastaba ~925/día y el saldo se acababa
  el 26-oct; ahora tiene presupuesto diario hasta la renovación del día 1.
- Goleadores: todos los picks salían en 70% y ninguno se liquidaba.
- Filtro de alineaciones: dependía de API-Football (suspendida); ahora ESPN.
- Kalshi: reintento ante el límite de llamadas.
- Resumen nocturno: hablaba del día anterior en modo recolección.
- Nombres: historia partida en dos nombres del mismo club.
- Ligas extra: el mismo partido con la fecha local y la de UTC.
"""

import io
import json
import sys
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# ============================================================
# Presupuesto de créditos
# ============================================================

def test_next_reset_is_the_first_of_next_month():
    from src.utils.credit_budget import next_reset
    assert next_reset(utc(2026, 10, 20, 18)) == utc(2026, 11, 1)
    assert next_reset(utc(2026, 12, 15)) == utc(2027, 1, 1)
    assert next_reset(utc(2026, 11, 1, 0, 0)) == utc(2026, 12, 1)     # el mismo instante ya pasó
    assert next_reset(utc(2026, 10, 31, 23), reset_day=5) == utc(2026, 11, 5)


def test_daily_allowance_spreads_the_balance_minus_the_reserve():
    from src.utils.credit_budget import daily_allowance
    # 20-oct 00:00 → 1-nov: 12 días; (6,600 − 600) / 12 = 500
    assert daily_allowance(6600, utc(2026, 10, 20), reserve=600) == pytest.approx(500)
    assert daily_allowance(500, utc(2026, 10, 20), reserve=600) == 0        # bajo la reserva: nada
    # la víspera no se gasta todo: mínimo medio día de divisor
    assert daily_allowance(1600, utc(2026, 10, 31, 23), reserve=600) == pytest.approx(2000)


def test_the_day_keeps_its_start_and_allowance_until_utc_midnight():
    from src.utils.credit_budget import day_state, run_budget
    morning = day_state(None, 6600, utc(2026, 10, 20, 11))
    assert morning["start"] == 6600 and morning["day"] == "2026-10-20"
    later = day_state(morning, 6300, utc(2026, 10, 20, 20))
    assert later == morning                                    # el saldo bajó, el presupuesto no se recalcula
    assert run_budget(later, 6300, mandatory=0) == pytest.approx(morning["allowance"] - 300)
    assert run_budget(later, 5000, mandatory=0) == 0           # se pasó: nada más hoy
    tomorrow = day_state(later, 6000, utc(2026, 10, 21, 0, 7))
    assert tomorrow["day"] == "2026-10-21" and tomorrow["start"] == 6000


def test_unknown_balance_does_not_stop_anything():
    from src.utils.credit_budget import load_and_update
    assert load_and_update(None) is None


# ============================================================
# Goleadores
# ============================================================

RATES = {
    "estrella": {"team": "club a", "goals": 40, "rate_raw": 0.60, "rate_weighted": 0.55},
    "suplente": {"team": "club a", "goals": 8, "rate_raw": 0.25, "rate_weighted": 0.24},
}


def test_scorer_probability_depends_on_the_player_and_the_match():
    from src.models.scorer_model import anytime_scorer_prob
    star = anytime_scorer_prob("Estrella", 1.6, rates=RATES, team_avg_goals=1.6)
    sub = anytime_scorer_prob("Suplente", 1.6, rates=RATES, team_avg_goals=1.6)
    assert star["lambda"] == pytest.approx(0.55) and sub["lambda"] == pytest.approx(0.24)
    assert star["prob"] > sub["prob"]                          # antes: los dos 70%
    strong = anytime_scorer_prob("Estrella", 2.4, rates=RATES, team_avg_goals=1.6)
    assert strong["lambda"] == pytest.approx(0.55 * 1.5)       # el equipo espera 50% más goles hoy
    assert anytime_scorer_prob("Estrella", 9.0, rates=RATES, team_avg_goals=1.6)["lambda"] == 1.1  # escala ≤ 2
    assert anytime_scorer_prob("Estrella", 1.6, rates=RATES, team_avg_goals=0)["lambda"] == pytest.approx(0.55)


def test_same_player_across_spellings():
    from scripts.goalscorer_picks import same_player
    assert same_player("Kylian Mbappe-Lottin", "Kylian Mbappé")
    assert same_player("Haaland", "Erling Haaland")
    assert not same_player("Raphinha", "Rafael Leão")
    assert not same_player("Lucas Paquetá", "Lucas Vázquez")   # dos palabras: hacen falta dos en común


def test_espn_result_win_loss_and_void():
    from scripts.goalscorer_picks import espn_result
    names = {"11": "Fer Niño", "9": "Iago Aspas", "7": "Borja Iglesias"}
    goals = {"9": 1}
    assert espn_result(goals, names, "Iago Aspas") == "win"
    assert espn_result(goals, names, "Borja Iglesias") == "loss"
    assert espn_result(goals, names, "Fernando Niño") == "loss"   # mismo apellido, único en el plantel
    assert espn_result(goals, names, "Jonathan Bamba") == "void"  # no estuvo: como en las casas
    twins = {"1": "Ansu Fati", "2": "Braima Fati"}
    assert espn_result({"1": 1}, twins, "Ousmane Fati") == "void"  # apellido repetido: no se adivina


# ============================================================
# Filtro de alineaciones (ESPN)
# ============================================================

class FakeConn:
    def __init__(self, row, table=True):
        self.row, self.table, self.params = row, table, None

    def execute(self, stmt, params=None):
        conn = self

        class R:
            def scalar(self_):
                return "lineups" if conn.table else None

            def first(self_):
                conn.params = params
                return conn.row
        return R()


def test_lineup_guard_reads_the_starters_from_espn():
    from scripts.revalidate_pending_bets import espn_starter_surnames
    from src.features.lineups import star_missing
    row = (json.dumps(["10", "9", "5"]), json.dumps({"10": "Lamine Yamal", "9": "Robert Lewandowski",
                                                    "5": "Pau Cubarsí", "21": "Frenkie de Jong"}))
    conn = FakeConn(row)
    xi = espn_starter_surnames(conn, "barcelona", "2026-10-25 15:15:00+00:00")
    assert xi == {"yamal", "lewandowski", "cubarsi"}           # el suplente no cuenta
    assert conn.params == {"t": "barcelona", "k": "2026-10-25 15:15:00+00:00"}
    assert star_missing(xi, ["robert lewandowski", "ferran torres"]) == "ferran torres"
    assert star_missing(xi, ["robert lewandowski"]) is None
    assert espn_starter_surnames(FakeConn(None), "barcelona", "2026-10-25") is None      # sin once publicado
    assert espn_starter_surnames(FakeConn(row, table=False), "barcelona", "2026-10-25") is None


def test_no_api_football_left_in_the_closing():
    import scripts.orchestrator as orch
    assert not hasattr(orch, "step_api_football_check")
    assert not hasattr(orch, "step_optimize_thresholds")       # su archivo no lo leía nadie


# ============================================================
# Kalshi
# ============================================================

def test_kalshi_retries_the_rate_limit_and_gives_up_on_other_errors(monkeypatch):
    import src.features.kalshi as K
    calls, waits = [], []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp(b'{"markets": []}')

    monkeypatch.setattr(K.urllib.request, "urlopen", fake_urlopen)
    assert K._get("/markets?x=1", sleep=waits.append) == {"markets": []}
    assert len(calls) == 3 and [w for w in waits if w >= 1] == [1, 2]

    def not_found(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(K.urllib.request, "urlopen", not_found)
    with pytest.raises(urllib.error.HTTPError):
        K._get("/markets?x=2", sleep=lambda s: None)


# ============================================================
# Resumen nocturno
# ============================================================

def test_evening_summary_is_about_today_unless_the_run_slipped_past_midnight():
    from scripts.orchestrator import evening_target_day
    from zoneinfo import ZoneInfo
    mx = ZoneInfo("America/Mexico_City")
    assert evening_target_day(datetime(2026, 10, 20, 21, 7, tzinfo=mx)) == date(2026, 10, 20)
    assert evening_target_day(datetime(2026, 10, 21, 2, 13, tzinfo=mx)) == date(2026, 10, 20)


# ============================================================
# Nombres de equipo
# ============================================================

def test_history_split_in_two_names_is_flagged():
    from scripts.learn_team_aliases import split_identities
    d = lambda *a: date(*a)
    dates = {
        "nuevo nombre fc": [d(2026, 4, k) for k in range(1, 17)],                  # The Odds API desde abril
        "nuevo nombre": [d(2025, m, 1) for m in range(1, 13)] * 20,                # la historia, hasta 2025
        "nuevo norte": [d(2026, 4, k) for k in range(1, 17)] * 2,                  # otro club: juega a la vez
        "ascendido": [d(2026, 8, k) for k in range(1, 9)],
    }
    counts = {k: len(v) for k, v in dates.items()}
    league_names = {"soccer_x": set(dates)}
    live = [("soccer_x", "Nuevo Nombre FC"), ("soccer_x", "Ascendido")]
    assert split_identities(live, counts, league_names, dates) == [
        ("soccer_x", "nuevo nombre fc", "nuevo nombre")]


def test_similar_names_of_different_clubs_are_not_aliases():
    from scripts.learn_team_aliases import _looks_alike
    assert _looks_alike("atleticomg", "atletico mineiro mg")
    assert _looks_alike("pumas unam", "unam pumas")
    assert _looks_alike("beveren", "waasland beveren")
    assert not _looks_alike("kalamata", "lamia")
    assert not _looks_alike("amedspor", "kayserispor")
    assert not _looks_alike("academico de viseu", "academica")


def test_thin_recent_history_is_listed():
    from scripts.learn_team_aliases import thin_recent
    live = [("soccer_x", "Club Nuevo"), ("soccer_x", "Club Viejo"), ("soccer_x", "Sin Partidos")]
    assert thin_recent(live, {"club nuevo": 3, "club viejo": 30}) == [("soccer_x", "club nuevo", 3)]


@pytest.mark.parametrize("api_name, history", [
    ("Atletico Mineiro", "atleticomg"), ("Athletico Paranaense", "athleticopr"),
    ("Pumas UNAM", "unam pumas"), ("Atlante FC", "atlante"), ("Aldosivi Mar del Plata", "aldosivi"),
    ("Atletico Huracan", "huracan"), ("Beveren", "waasland beveren"),
    # nombres de football-data de las ligas que se agregan
    ("Club America", "america"), ("Club Leon", "leon"), ("Club Tijuana", "tijuana"),
    ("Union de Santa Fe", "union santa fe"), ("Atletico-MG", "atleticomg"),
    ("Atlanta Utd", "atlanta united"), ("St. Louis City", "st louis city sc"),
    ("Dorados de Sinaloa", "dorados sinaloa"),
])
def test_split_names_reach_their_history(api_name, history):
    from src.utils.team_normalizer import normalize_team
    assert normalize_team(api_name) == history == normalize_team(history)


# ============================================================
# Ligas extra y segundas divisiones
# ============================================================

def test_extra_league_rows_already_stored_a_day_apart_are_skipped():
    from scripts.load_extra_leagues import already_stored
    rows = [{"date": "2026-10-18", "home_team": "seattle sounders", "away_team": "la galaxy"},
            {"date": "2026-10-18", "home_team": "dallas", "away_team": "austin"}]
    existing = [(date(2026, 10, 19), "seattle sounders", "la galaxy")]    # The Odds API: fecha UTC
    assert already_stored(rows, existing) == [rows[0]]


def test_new_leagues_are_loaded_with_their_country_check():
    from scripts.load_extra_leagues import EXPECTED_COUNTRY, EXTRA_LEAGUES
    from scripts.load_historical_data import leagues
    for code in ("USA", "MEX", "BRA", "ARG"):
        assert code in EXTRA_LEAGUES and code in EXPECTED_COUNTRY
    for code in ("E2", "D2", "I2", "SP2", "F2", "SC1"):
        assert code in leagues
