"""
El factor de motivación y el "partido sin nada en juego" (23-oct-26).

Las jornadas que faltan se contaban como equipos − 1 − jugados, la cuenta de
una sola vuelta: en la Premier el "final de temporada" empezaba en la
jornada 11 y desde la 19 el líder quedaba "campeón" (−8%). Brasil, de enero a
diciembre, se contaba desde julio: la tabla salía con la mitad de los
partidos y 16 de sus 20 equipos tenían ajuste. Sin DB.
"""

from datetime import datetime

import pandas as pd

import src.features.motivation_factor as MF


def _table(n, played, leader_gap=1):
    rows = [{"team": f"t{i}", "pts": 60 - 3 * i - (leader_gap if i else 0), "played": played, "position": i + 1}
            for i in range(n)]
    rows[0]["pts"] += leader_gap
    return pd.DataFrame(rows)


def _factor(monkeypatch, league, n, played, team="t0"):
    monkeypatch.setattr(MF, "_get_current_standings", lambda lg, season_start=None: _table(n, played))
    monkeypatch.setattr(MF, "_match_standings_row",
                        lambda st, t: st[st["team"] == t].iloc[0] if (st["team"] == t).any() else None)
    return MF.get_motivation_factor(team, league)


def test_a_double_round_league_is_not_at_its_end_in_november(monkeypatch):
    # Premier, jornada 11: faltan 27, no 8 → sin "pelea por el título"
    assert MF.games_left("soccer_epl", 11) == 27
    assert _factor(monkeypatch, "soccer_epl", 20, 11) == 0.0
    # jornada 19: el líder por 1 punto no es "campeón" (antes −8%)
    assert _factor(monkeypatch, "soccer_epl", 20, 19) == 0.0
    # en las últimas 8 sí pelea el título
    assert _factor(monkeypatch, "soccer_epl", 20, 31) == MF.TITLE_FIGHT


def test_liga_mx_is_one_round_of_17():
    assert MF.games_left("soccer_mexico_ligamx", 13) == 4
    assert MF.games_left("soccer_germany_bundesliga", 6) == 28


def test_the_season_starts_when_each_league_starts():
    oct_ = datetime(2026, 10, 23)
    mar = datetime(2027, 3, 10)
    assert MF.current_season_start("soccer_epl", oct_) == "2026-07-01"
    assert MF.current_season_start("soccer_epl", mar) == "2026-07-01"
    assert MF.current_season_start("soccer_brazil_campeonato", oct_) == "2026-01-01"   # antes: julio
    assert MF.current_season_start("soccer_mexico_ligamx", oct_) == "2026-07-01"       # Apertura
    assert MF.current_season_start("soccer_mexico_ligamx", mar) == "2027-01-01"        # Clausura (antes: julio)


def test_a_mid_table_match_in_november_is_not_a_dead_rubber(monkeypatch):
    monkeypatch.setattr(MF, "_get_current_standings", lambda lg, season_start=None: _table(20, 14))
    monkeypatch.setattr(MF, "_match_standings_row",
                        lambda st, t: st[st["team"] == t].iloc[0] if (st["team"] == t).any() else None)
    unreliable, why = MF.is_unreliable_match("t9", "t10", "soccer_epl")       # antes: "dead rubber, 5j"
    assert unreliable is False


def test_argentina_has_no_motivation_its_table_has_two_zones():
    assert MF.get_motivation_factor("river plate", "soccer_argentina_primera_division") == 0.0
    assert MF.is_unreliable_match("river plate", "racing", "soccer_argentina_primera_division") == (False, "no aplica liga")
