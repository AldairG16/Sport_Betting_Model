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
    assert msg.startswith("📋 <b>Alpha vs Beta</b> · 13:00")                    # hora de México
    assert "❌ Alpha: Jugador 10, Jugador 7, Jugador 8, Jugador 9 ⚽" in msg      # solo los que faltan
    assert "apuesta si PlayDoit paga" in msg and "Empate o Beta" in msg
    assert LA.value_at_close(+150, 0.42) == pytest.approx(0.42 * 2.5 - 1)
    assert LA.value_at_close(-120, 0.60) == pytest.approx(0.60 * (1 + 100 / 120) - 1)
    assert LA.value_at_close(None, 0.5) is None and LA.format_alerts([]) == ""


# ============================================================
# 23-oct-26: emparejamiento estricto, alineaciones ajenas y un mensaje por partido
# ============================================================

def test_a_shared_generic_word_is_not_the_same_team():
    assert EL.name_score("sarmiento", "sarmiento junin") == 1.0                    # palabra propia
    assert EL.name_score("gimnasia mendoza", "gimnasia lp") < EL.MIN_SIDE_SCORE    # "gimnasia" no basta
    assert EL.name_score("rosario central", "central cordoba") < EL.MIN_SIDE_SCORE
    assert EL.name_score("estudiantes", "estudiantes rio cuarto") < EL.MIN_SIDE_SCORE
    # los dos nombres tienen que parecerse, no solo la suma
    assert EL.pair_score("gimnasia mendoza", "platense", "gimnasia lp", "sarmiento junin") is None
    assert EL.pair_score("gimnasia lp", "sarmiento", "gimnasia lp", "sarmiento junin") == 2.0


KICK = pd.Timestamp("2026-10-23 21:30", tz="UTC")


def test_the_espn_match_goes_to_one_system_match_only():
    """El 23-oct la alineación de Gimnasia Mendoza vs Platense se guardó también como
    la de Gimnasia LP vs Sarmiento (30 min después): avisos de 8 y 9 titulares fuera."""
    gm = {"event": "401841637", "home": "Gimnasia Mendoza", "away": "Platense", "kick": KICK.to_pydatetime()}
    glp = {"event": "401841638", "home": "Gimnasia La Plata", "away": "Sarmiento", "kick": (KICK + pd.Timedelta("30min")).to_pydatetime()}
    lg = "soccer_argentina_primera_division"
    only_glp = [(lg, "gimnasia lp", "sarmiento junin", KICK + pd.Timedelta("30min"))]
    assert EL.assign_events(lg, [gm], only_glp) == {}                          # el de las 21:32 del 23-oct
    both = only_glp + [(lg, "gimnasia mendoza", "platense", KICK)]
    got = EL.assign_events(lg, [gm, glp], both)
    assert got == {"401841637": ("gimnasia mendoza", "platense", KICK),
                   "401841638": ("gimnasia lp", "sarmiento junin", KICK + pd.Timedelta("30min"))}
    twice = EL.assign_events(lg, [gm, {**gm, "event": "x"}], [(lg, "gimnasia mendoza", "platense", KICK)])
    assert len(twice) == 1                                                     # uno a uno
    assert EL.assign_events("soccer_epl", [gm], both) == {}                    # otra liga


def test_history_rejects_a_match_where_only_one_name_fits():
    """Agosto-26: sin el Estudiantes–Defensa en matches (fechas cruzadas), el viejo
    emparejamiento guardó la alineación de Estudiantes como la de Newell's."""
    ev = {"home": "Estudiantes de La Plata", "away": "Defensa y Justicia", "kick": pd.Timestamp("2026-08-01T21:00Z")}
    assert EL.system_names(ev, [("2026-08-01", "newells old boys", "defensa y justicia")]) is None
    assert EL.system_names(ev, [("2026-08-01", "estudiantes", "defensa y justicia")]) == ("estudiantes", "defensa y justicia")


def test_a_lineup_that_is_not_the_teams_does_not_alert():
    prof = LA.team_profile(_history())
    other = LA.absences([f"z{i}" for i in range(11)], prof)                     # el XI de otro equipo
    assert other.get("foreign") and other["missing"] == [] and not LA.triggered(other)
    rotated = LA.absences(XI[:5] + [f"z{i}" for i in range(6)], prof)          # 5 conocidos: rotación real
    assert not rotated.get("foreign") and len(rotated["missing"]) == 6 and LA.triggered(rotated)


def test_identity_problems_find_lineups_of_other_matches():
    def row(i, team, ev, starters, completed=True, opp="x"):
        return {"id": i, "event_id": ev, "sport_key": "lg", "kickoff": "2026-10-01", "team": team,
                "opponent": opp, "completed": completed, "starters": starters}
    rows = [row(i, "alpha", f"e{i}", XI) for i in range(1, 6)]
    rows += [row(10, "alpha", "e10", [f"z{i}" for i in range(11)])]                   # ajena
    rows += [row(20, "beta", "e1", ["b"]), row(21, "gamma", "e1", ["c"])]            # e1 con 3 equipos
    rows += [row(30, "alpha", "e5", XI, completed=False)]                           # mismo partido, antes
    found = EL.identity_problems(rows)
    assert found == {"shared_events": ["e1"], "foreign": [10]}
    from scripts.system_audit import lineup_problems
    out = lineup_problems(found)
    assert len(out) == 2 and "más de un partido" in out[0] and "ids 10" in out[1]
    assert lineup_problems({"shared_events": [], "foreign": []}) == []


def _alerts_alpha_out():
    profiles = {"alpha": LA.team_profile(_history()), "beta": LA.team_profile(_history())}
    return LA.alerts_for_match(_fixture(), {"alpha": XI[:7] + ["x1", "x2", "x3", "x4"], "beta": XI}, profiles, now=NOW)


def test_one_short_message_per_match_with_both_teams_and_the_pick():
    alerts = _alerts_alpha_out()
    absent = {"alpha": {"missing": ["Jugador 7", "Jugador 8", "Jugador 9", "Jugador 10"], "top_out": "Jugador 9"},
              "beta": {"missing": [], "top_out": None}}
    pick = {"market": "under25", "label": "Menos de 2.5 goles", "family": "Goles", "reason": None, "min_us": -136}
    review = {"verdict": "CONFIRMA", "reason": "lesiones confirmadas"}
    msg = LA.format_match(alerts, absent, pick, review, now=NOW)
    lines = msg.split("\n")
    assert lines[0] == "📋 <b>Alpha vs Beta</b> · 13:00"
    assert lines[1] == "❌ Alpha: Jugador 7, Jugador 8, Jugador 9 ⚽, Jugador 10" and lines[2] == "✅ Beta: completo"
    assert sum("PlayDoit paga" in ln for ln in lines) == 3                         # 2 del rival + el pick
    assert "🔎 Claude: <b>confirma las bajas</b> — lesiones confirmadas" in msg
    assert lines[-1] == "🎯 Pick del programa: <b>Menos de 2.5 goles</b> — apuesta solo si PlayDoit paga <b>-136</b> o más."
    assert "Pinnacle lo ve" not in msg and "titulares habituales" not in msg        # corto
    assert len(lines) == 8 and "(precio de Pinnacle de hace 60 min" in lines[5]          # el precio tiene 1 h


def test_the_pick_line_says_when_to_bet_it():
    alerts = _alerts_alpha_out()                                                       # alpha (local) sin titulares
    win = lambda m, **k: {"market": m, "label": "x", "reason": None, "min_us": 120, **k}
    assert "las bajas van contra este pick: no lo apuestes" in LA.pick_line(win("home_win"), alerts)
    # si Claude dice que las bajas no pesan, el pick del local vale con su mínimo
    assert "<b>+120</b> o más" in LA.pick_line(win("home_win"), alerts, {"verdict": "DESCARTA"})
    assert LA.pick_line(win("away_win"), alerts).endswith("Las bajas del rival lo favorecen.")
    no = LA.pick_line(win("btts", reason="El programa se separa demasiado del mercado: probablemente se equivoca"), alerts)
    assert no.endswith("no pasa la regla del programa (el programa se separa demasiado del mercado): no lo apuestes.")
    assert LA.pick_line(None, alerts) == "" and "sin precio" in LA.pick_line(win("draw", min_us=None), alerts)
