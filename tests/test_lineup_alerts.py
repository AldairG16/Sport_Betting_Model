"""
Alertas de alineación (20-oct-26): con la alineación de ESPN, si a un equipo
le faltan titulares habituales o su goleador, se avisa el momio mínimo que
debe pagar PlayDoit por el rival, con el precio de Pinnacle de ese momento.
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from src.features import espn_lineups as EL
from src.models import lineup_alerts as LA


# ============================================================
# ESPN
# ============================================================

def _roster(prefix, n_starters=11, goals=None):
    goals = goals or {}
    return [{"athlete": {"id": f"{prefix}{i}", "displayName": f"Jugador {prefix}{i}"},
             "starter": i < n_starters,
             "stats": [{"name": "totalGoals", "value": goals.get(f"{prefix}{i}", 0)}]}
            for i in range(16)]


def test_summary_needs_eleven_starters_per_side():
    full = {"rosters": [{"homeAway": "home", "formation": "4-3-3", "roster": _roster("h", goals={"h9": 2})},
                        {"homeAway": "away", "formation": "4-4-2", "roster": _roster("a")}]}
    s = EL.parse_summary(full)
    assert len(s["home"]["starters"]) == 11 and s["home"]["goals"] == {"h9": 2}
    assert s["home"]["names"]["h3"] == "Jugador h3" and s["away"]["formation"] == "4-4-2"
    partial = {"rosters": [full["rosters"][0], {"homeAway": "away", "roster": _roster("a", n_starters=0)}]}
    assert EL.parse_summary(partial) is None                         # sin publicar todavía
    assert EL.parse_summary(None) is None


def test_scoreboard_events():
    data = {"events": [{"id": 1, "date": "2026-10-18T13:00Z", "status": {"type": {"completed": True}},
                        "competitions": [{"competitors": [
                            {"homeAway": "home", "team": {"displayName": "AFC Bournemouth"}},
                            {"homeAway": "away", "team": {"displayName": "Sunderland"}}]}]}]}
    (ev,) = EL.parse_scoreboard(data)
    assert ev["event"] == "1" and ev["home"] == "AFC Bournemouth" and ev["completed"]
    assert ev["kick"].hour == 13


def test_history_uses_the_system_team_names():
    ev = {"home": "AFC Bournemouth", "away": "Sunderland", "kick": pd.Timestamp("2026-10-18T13:00Z")}
    cands = [("2026-10-18", "bournemouth", "sunderland"), ("2026-10-18", "brighton", "crystal palace")]
    assert EL.system_names(ev, cands) == ("bournemouth", "sunderland")
    assert EL.system_names(ev, [("2026-10-25", "bournemouth", "sunderland")]) is None   # otra fecha


# ============================================================
# Titulares habituales, bajas y alertas
# ============================================================

XI = [f"p{i}" for i in range(11)]


def _history(n=10, top_goals=4):
    names = {f"p{i}": f"Jugador {i}" for i in range(15)}
    return [{"starters": XI, "goals": {"p9": 1} if k < top_goals else {}, "names": names} for k in range(n)]


def test_regulars_and_top_scorer():
    prof = LA.team_profile(_history())
    assert prof["regulars"] == set(XI) and prof["top"] == "p9" and prof["top_goals"] == 4
    assert LA.team_profile(_history(n=5))["regulars"] == set()       # menos de 6 partidos: nadie
    assert LA.team_profile(_history(top_goals=2))["top"] is None      # 2 goles no hacen goleador


def test_only_a_big_rotation_triggers_an_alert():
    prof = LA.team_profile(_history())
    full = LA.absences(XI, prof)
    assert full["missing"] == [] and full["top_out"] is None and not LA.triggered(full)
    two_out = LA.absences(XI[:9] + ["x1", "x2"], prof)
    assert two_out["missing"] == ["Jugador 10", "Jugador 9"] and two_out["top_out"] == "Jugador 9"
    assert not LA.triggered(two_out)            # 2 fuera: rotación normal; el goleador no avisa
    four_out = LA.absences(XI[:7] + ["x1", "x2", "x3", "x4"], prof)
    assert len(four_out["missing"]) == 4 and LA.triggered(four_out)


NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)


def _fixture(age_h=1.0):
    return {"sport_key": "soccer_epl", "home_team_norm": "alpha", "away_team_norm": "beta",
            "match_date": NOW + timedelta(hours=1), "odds_fetched_at": NOW - timedelta(hours=age_h),
            "pin_home_odds": 2.10, "pin_draw_odds": 3.40, "pin_away_odds": 3.60}


def test_alert_prices_the_rival_with_pinnacle():
    profiles = {"alpha": LA.team_profile(_history()), "beta": LA.team_profile(_history())}
    lineups = {"alpha": XI[:7] + ["x1", "x2", "x3", "x4"], "beta": XI}
    alerts = LA.alerts_for_match(_fixture(), lineups, profiles, now=NOW)
    assert [a["market"] for a in alerts] == ["away_win", "dc_x2"]     # alpha (local) debilitado
    from src.features.pinnacle import pinnacle_prob
    from src.utils.min_odds import min_american
    p = pinnacle_prob("away_win", _fixture())
    assert alerts[0]["pin_prob"] == pytest.approx(p) and alerts[0]["min_american"] == min_american(p)
    assert all(a["weakened"] == "alpha" for a in alerts)
    assert LA.alerts_for_match(_fixture(age_h=13), lineups, profiles, now=NOW) == []   # precio viejo
    assert LA.alerts_for_match(_fixture(), {"alpha": XI, "beta": XI}, profiles, now=NOW) == []
    longshot = {**_fixture(), "pin_home_odds": 1.30, "pin_draw_odds": 5.5, "pin_away_odds": 11.0}
    assert [a["market"] for a in LA.alerts_for_match(longshot, lineups, profiles, now=NOW)] == ["dc_x2"]


def test_message_and_value_at_close():
    profiles = {"alpha": LA.team_profile(_history()), "beta": LA.team_profile(_history())}
    alerts = LA.alerts_for_match(_fixture(), {"alpha": XI[:7] + ["x1", "x2", "x3", "x4"]}, profiles, now=NOW)
    msg = LA.format_alerts(alerts)
    assert "ALINEACIÓN — Alpha vs Beta" in msg and "no salen 4 titulares habituales" in msg
    assert "tampoco sale su goleador (Jugador 9)" in msg
    assert "apuesta solo si PlayDoit paga" in msg and "Empate o Beta" in msg
    assert "(13:00)" in msg                                          # hora de México
    assert LA.value_at_close(+150, 0.42) == pytest.approx(0.42 * 2.5 - 1)
    assert LA.value_at_close(-120, 0.60) == pytest.approx(0.60 * (1 + 100 / 120) - 1)
    assert LA.value_at_close(None, 0.5) is None and LA.format_alerts([]) == ""
