"""
tests/test_sanity_audit.py
==========================
Auditor de sanidad (scripts/weekly_sanity_audit.py), 22-sep-26:

- La fusión de duplicados reventaba con NaN en columnas enteras ("integer
  out of range") y se revertía entera: los duplicados nunca se fusionaban.
- El chequeo de xG usaba solo tiros del local contra un rango fijo.
- Se prueban las funciones puras, sin DB.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import scripts.weekly_sanity_audit as sa


# ============================================================
# Fusión de duplicados
# ============================================================

@pytest.mark.parametrize("value,expected", [
    (float("nan"), None),
    (None, None),
    (np.nan, None),
    (np.int64(7), 7),
    (5.0, 5),
    (np.float64(3.0), 3),
])
def test_sql_value_never_sends_nan(value, expected):
    assert sa._sql_value(value) == expected


def _match(id_, home, away, league="soccer_epl", date="2026-09-20", hg=1, ag=1, **stats):
    row = {"id": id_, "date": date, "league": league, "home_team": home,
           "away_team": away, "home_goals": hg, "away_goals": ag}
    for c in sa._STAT_COLS:
        row[c] = stats.get(c, float("nan"))
    return row


def test_merge_params_have_no_nan():
    """Caso real del 21/22-sep: donante con stats vacías (NaN de pandas)."""
    keep = _match(1425318, "nottm forest", "arsenal")
    donor = _match(99, "nott'm forest", "arsenal", home_corners=np.float64(6.0))
    params = sa.merge_params(keep, donor)
    assert params["kid"] == 1425318
    assert params["home_corners"] == 6
    assert all(v is None or isinstance(v, int) for v in params.values())
    assert not any(isinstance(v, float) and math.isnan(v) for v in params.values())


def test_name_variant_is_a_duplicate_and_clean_name_is_kept():
    df = pd.DataFrame([_match(1, "nott'm forest", "arsenal"),
                       _match(2, "nottm forest", "arsenal")])
    pairs = sa.find_duplicate_pairs(df)
    assert len(pairs) == 1
    keep, donor = sa.choose_keep(*pairs[0])
    assert keep["id"] == 2 and donor["id"] == 1


def test_same_day_and_score_in_different_leagues_is_not_a_duplicate():
    df = pd.DataFrame([_match(1, "atletico mg", "flamengo", league="soccer_brazil_campeonato"),
                       _match(2, "atletico go", "fluminense", league="soccer_brazil_serie_b")])
    assert sa.find_duplicate_pairs(df) == []


def test_missing_league_still_allows_merge():
    """Si una fila no trae liga (fuente distinta), manda el parecido de nombres."""
    df = pd.DataFrame([_match(1, "nott'm forest", "arsenal", league=None),
                       _match(2, "nottm forest", "arsenal")])
    assert len(sa.find_duplicate_pairs(df)) == 1


def test_keep_prefers_row_with_more_stats_when_both_names_are_clean():
    a = _match(1, "man united", "chelsea")
    b = _match(2, "man utd", "chelsea", home_corners=5, away_corners=3)
    keep, donor = sa.choose_keep(a, b)
    assert keep["id"] == 2


def test_keep_prefers_the_established_name_over_a_one_off_variant():
    """5-oct-26: "atl madrid" (1 partido) y "ath madrid" (428) con las mismas
    stats. Quedaba la que saliera primero; si quedaba la rara, la carga
    siguiente volvía a meter el partido con el nombre bueno cada semana."""
    freq = {"ath madrid": 428, "atl madrid": 1, "malaga": 60}
    rara = _match(1275476, "atl madrid", "malaga", home_corners=6)
    buena = _match(1945271, "ath madrid", "malaga", home_corners=6)
    for a, b in ((rara, buena), (buena, rara)):
        keep, donor = sa.choose_keep(a, b, freq)
        assert keep["id"] == 1945271 and donor["id"] == 1275476


def test_merge_keeps_the_established_name_with_counts_from_the_db(monkeypatch):
    from tests.fake_db import FakeEngine, FakeResult
    counts = [("ath madrid", 428), ("atl madrid", 1), ("malaga", 60)]
    rows = {1275476: _match(1275476, "atl madrid", "malaga", date="2026-08-19", hg=2, ag=0, home_corners=6),
            1945271: _match(1945271, "ath madrid", "malaga", date="2026-08-19", hg=2, ag=0, home_corners=6)}
    for order in ((1275476, 1945271), (1945271, 1275476)):
        eng = FakeEngine(lambda sql, p: FakeResult(rows=counts)
                         if sql.startswith("SELECT name, COUNT(*)") else FakeResult())
        monkeypatch.setattr(sa, "engine", eng)
        monkeypatch.setattr(sa.pd, "read_sql",
                            lambda *a, o=order, **k: pd.DataFrame([rows[i] for i in o]))
        status, _ = sa.audit_duplicate_matches()
        assert status == "ok"
        assert eng.statements("DELETE FROM matches")[0][1]["ids"] == [1275476]
        sql, params = eng.statements("UPDATE matches")[0]
        assert params["kid"] == 1945271
        assert "home_goals_ht = COALESCE(k.home_goals_ht, :home_goals_ht)" in sql
        # cada parámetro del UPDATE tiene valor (el error del --apply de fix_match_stats)
        from sqlalchemy import text
        assert set(text(sql).compile().params) == set(params)


def test_merge_also_passes_half_time_and_red_cards():
    """El medio tiempo liquida los mercados de primer tiempo: si solo lo
    traía la fila que se borra, se perdía (5-oct-26)."""
    keep = _match(1, "ath madrid", "malaga")
    donor = _match(2, "atl madrid", "malaga", home_goals_ht=1, away_goals_ht=0, away_red=1)
    params = sa.merge_params(keep, donor)
    assert (params["home_goals_ht"], params["away_goals_ht"], params["away_red"]) == (1, 0, 1)
    assert params["home_red"] is None


def test_merged_duplicates_are_backed_up_before_the_delete(monkeypatch):
    """En matches no se borra nada sin copia (1-oct-26)."""
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(sa, "engine", eng)
    monkeypatch.setattr(sa.pd, "read_sql", lambda *a, **k: pd.DataFrame(
        [_match(1, "nott'm forest", "arsenal"), _match(2, "nottm forest", "arsenal", home_corners=6)]))
    status, msg = sa.audit_duplicate_matches()
    assert status == "ok" and msg.startswith("1 duplicados")
    sqls = [s for s, _ in eng.executed]
    i_backup = next(i for i, s in enumerate(sqls) if s.startswith("INSERT INTO matches_identity_backup"))
    i_delete = next(i for i, s in enumerate(sqls) if s.startswith("DELETE FROM matches"))
    assert i_backup < i_delete
    assert eng.executed[i_backup][1]["ids"] == [1] == eng.executed[i_delete][1]["ids"]


# ============================================================
# Repetidos con un día de diferencia (9-nov-26)
# ============================================================

PT = "soccer_portugal_primeira_liga"


def _nacional_rows():
    """El caso real: The Odds API con la fecha de antes de que lo pasaran al
    domingo (sin stats), football-data con la buena (con stats)."""
    return [_match(2067044, "nacional", "academico de viseu", league=PT, date="2026-11-07", hg=1, ag=0),
            _match(2067553, "nacional", "academico de viseu", league=PT, date="2026-11-08", hg=1, ag=0,
                   home_corners=6, away_corners=5, home_shots=19)]


def test_one_day_apart_is_a_duplicate_and_the_row_with_stats_stays():
    pairs = sa.find_duplicate_pairs(pd.DataFrame(_nacional_rows()))
    assert len(pairs) == 1
    keep, donor = sa.choose_keep(*pairs[0], {"nacional": 300, "academico de viseu": 40})
    assert keep["id"] == 2067553 and donor["id"] == 2067044


def test_one_day_apart_with_nothing_else_keeps_the_later_date():
    """Un partido se pospone, no se adelanta."""
    a = _match(1, "nacional", "academico de viseu", league=PT, date="2026-11-07", hg=1, ag=0)
    b = _match(2, "nacional", "academico de viseu", league=PT, date="2026-11-08", hg=1, ag=0)
    for x, y in ((a, b), (b, a)):
        keep, donor = sa.choose_keep(x, y)
        assert keep["id"] == 2 and donor["id"] == 1


def test_one_day_apart_with_one_team_written_differently():
    df = pd.DataFrame([_match(1, "preston", "sheffield utd", league="soccer_efl_champ", date="2026-11-03"),
                       _match(2, "preston", "sheffield united", league="soccer_efl_champ", date="2026-11-04")])
    assert len(sa.find_duplicate_pairs(df)) == 1


@pytest.mark.parametrize("rows", [
    # otra liga, o sin liga: no se puede saber que es el mismo partido
    [_match(1, "nacional", "academico de viseu", league=PT, date="2026-11-07"),
     _match(2, "nacional", "academico de viseu", league="soccer_portugal_segunda_liga", date="2026-11-08")],
    [_match(1, "nacional", "academico de viseu", league=None, date="2026-11-07"),
     _match(2, "nacional", "academico de viseu", league=PT, date="2026-11-08")],
    # dos días
    [_match(1, "nacional", "academico de viseu", league=PT, date="2026-11-06"),
     _match(2, "nacional", "academico de viseu", league=PT, date="2026-11-08")],
    # otro marcador
    [_match(1, "nacional", "academico de viseu", league=PT, date="2026-11-07", hg=1, ag=0),
     _match(2, "nacional", "academico de viseu", league=PT, date="2026-11-08", hg=2, ag=0)],
    # un equipo igual y el otro distinto
    [_match(1, "nacional", "benfica", league=PT, date="2026-11-07"),
     _match(2, "nacional", "arouca", league=PT, date="2026-11-08")],
    # béisbol: una serie juega días seguidos y un doble juego el mismo día
    [_match(1, "New York Yankees", "Boston Red Sox", league="baseball_mlb", date="2025-07-04", hg=3, ag=2),
     _match(2, "New York Yankees", "Boston Red Sox", league="baseball_mlb", date="2025-07-05", hg=3, ag=2)],
    [_match(1, "New York Yankees", "Boston Red Sox", league="baseball_mlb", date="2025-07-04", hg=3, ag=2),
     _match(2, "New York Yankees", "Boston Red Sox", league="baseball_mlb", date="2025-07-04", hg=3, ag=2)],
])
def test_not_a_duplicate(rows):
    assert sa.find_duplicate_pairs(pd.DataFrame(rows)) == []


def test_a_pair_found_both_ways_is_merged_once(monkeypatch):
    """Los dos equipos iguales: el par sale por el local y por el visitante."""
    from tests.fake_db import FakeEngine
    eng = FakeEngine()
    monkeypatch.setattr(sa, "engine", eng)
    monkeypatch.setattr(sa.pd, "read_sql", lambda *a, **k: pd.DataFrame(_nacional_rows()))
    status, msg = sa.audit_duplicate_matches()
    assert status == "ok" and msg.startswith("1 duplicados")
    assert eng.statements("DELETE FROM matches")[0][1]["ids"] == [2067044]
    assert eng.statements("UPDATE matches")[0][1]["kid"] == 2067553


def test_the_merge_and_the_check_look_at_all_history_without_baseball(monkeypatch):
    """~20-oct-26 una carga de historia metió 19 partidos de China de 2023-25
    como "China" junto a "China PR": la fusión (60 días) y el chequeo (120
    días) miraban por la fecha del partido y no los vieron."""
    from src.utils.output_checks import DATA_HEALTH_SQL
    for k in ("duplicate", "renamed"):
        assert "CURRENT_DATE" not in DATA_HEALTH_SQL[k] and "baseball" in DATA_HEALTH_SQL[k]
    seen = []
    monkeypatch.setattr(sa, "engine", None)
    monkeypatch.setattr(sa.pd, "read_sql", lambda sql, *a, **k: seen.append(str(sql)) or pd.DataFrame(
        [_match(231516, "Thailand", "China PR", league="soccer_afc", date="2023-11-16", hg=1, ag=2),
         _match(2050818, "Thailand", "China", league="soccer_afc", date="2023-11-16", hg=1, ag=2)]).iloc[:0])
    assert sa.audit_duplicate_matches() == ("ok", "sin duplicados ✓")
    # los últimos 60 días completos; de antes, solo los candidatos que encuentra la base
    # (la primera versión leía los ~120 mil partidos en cada corrida: Neon cortó por cuota)
    assert "date >= CURRENT_DATE - 60" in seen[0] and "baseball" in seen[0]
    assert "WITH hist AS" in seen[1] and "x.h = y.h" in seen[1] and "x.a = y.a" in seen[1]
    assert "ABS(x.date - y.date) <= 1" in seen[1] and "baseball" in seen[1]
    sa.audit_duplicate_matches(days=400)
    assert "CURRENT_DATE - 400" in seen[2] and len(seen) == 3
    df = pd.DataFrame([_match(231516, "Thailand", "China PR", league="soccer_afc", date="2023-11-16", hg=1, ag=2),
                       _match(2050818, "Thailand", "China", league="soccer_afc", date="2023-11-16", hg=1, ag=2)])
    (pair,) = sa.find_duplicate_pairs(df)
    keep, donor = sa.choose_keep(*pair, {"Thailand": 90, "China PR": 19, "China": 19})
    assert keep["id"] == 231516 and donor["id"] == 2050818


def test_the_results_download_does_not_bring_back_the_old_date(monkeypatch):
    """Fusionado el repetido, The Odds API lo volvía a mandar con la fecha de
    antes: ON CONFLICT solo ve la misma fecha. En fútbol el INSERT busca el
    partido ±1 día con el mismo marcador; en béisbol no."""
    import scripts.fetch_results as FR
    from tests.fake_db import FakeEngine, FakeResult
    eng = FakeEngine(lambda sql, p: FakeResult(rowcount=0 if p["league"] == PT else 1))
    monkeypatch.setattr(FR, "engine", eng)
    rows = [{"date": "2026-11-07", "home_team": "nacional", "away_team": "academico de viseu",
             "home_goals": 1, "away_goals": 0, "league": PT},
            {"date": "2025-07-05", "home_team": "New York Yankees", "away_team": "Boston Red Sox",
             "home_goals": 3, "away_goals": 2, "league": "baseball_mlb"}]
    assert FR.insert_results(rows) == (1, 1)
    soccer, baseball = (s for s, _ in eng.statements("INSERT INTO matches"))
    assert "NOT EXISTS" in soccer and "CAST(:date AS date) - 1" in soccer
    assert "NOT EXISTS" not in baseball


# ============================================================
# Sanidad del xG proxy
# ============================================================

def test_xg_check_uses_both_sides_against_real_goals():
    """Con tiros de local y visitante típicos y goles reales de ~1.4 por
    equipo, el proxy cuadra (ratio ~1). Mirando solo al local, el número
    salía inflado por la ventaja de local — la falsa alarma de "1.63"."""
    xg, goals, ratio = sa.xg_proxy_vs_goals(hsot=4.9, asot=3.9, hsh=13.8, ash=11.2,
                                            hg=1.55, ag=1.25)
    assert 0.90 <= ratio <= 1.10
    home_only, _, _ = sa.xg_proxy_vs_goals(4.9, 4.9, 13.8, 13.8, 1.55, 1.55)
    assert home_only > xg


def test_xg_check_flags_a_real_inflation():
    _, _, ratio = sa.xg_proxy_vs_goals(hsot=6.5, asot=5.5, hsh=18, ash=16, hg=1.3, ag=1.1)
    assert ratio > sa.XG_RATIO_BAND[1]


# ============================================================
# Calibración rodante (2-nov-26)
# ============================================================

def _picks(n, prob, wins, market=None):
    return pd.DataFrame({"probability": [prob] * n, "market_prob": [market] * n,
                         "result": ["win"] * wins + ["loss"] * (n - wins)})


def test_calibration_reads_the_program_picks_not_the_frozen_bets(monkeypatch):
    # bets_history no recibe apuestas desde el 25-sep: el 🚨 de cada lunes era
    # de las del 19 al 22 de septiembre
    seen = []

    def read_sql(sql, con=None, **kw):
        seen.append(str(sql))
        return _picks(100, 0.608, 53, 0.55)                     # los últimos 100 del 2-nov
    monkeypatch.setattr(sa.pd, "read_sql", read_sql)
    status, msg = sa.audit_calibration()
    assert "FROM program_picks" in seen[0] and "bets_history" not in seen[0]
    assert status == "ok" and "53.0%" in msg and "el mercado decía 55.0%" in msg


@pytest.mark.parametrize("prob,wins,status,says", [
    (0.60, 47, "alerta", "sobreconfianza"),
    (0.45, 60, "alerta", "subconfianza"),          # antes decía "sobreconfianza" también aquí
    (0.55, 52, "ok", "✓")])
def test_calibration_verdict(prob, wins, status, says):
    got, msg = sa.calibration_verdict(_picks(100, prob, wins))
    assert got == status and says in msg and "mercado" not in msg
    assert sa.calibration_verdict(_picks(20, prob, 5))[0] == "ok"          # muestra chica: sin opinión
